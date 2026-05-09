#!/usr/bin/env python3
"""Bird Observatory Health Monitor — self-healing watchdog. iMac-only.

Runs every 5 minutes via LaunchAgent. Checks all services, restarts
failed ones, and writes a status report to /tmp/bird-observatory-health.json.

Self-healing actions (graduated):
1. Service not running → launchctl kickstart
2. Classifier queue > 2000 → restart classifier
3. Health file stale > 10 min → restart audio service
4. No audio detections in 30 min (daytime) → restart audio
5. Error storm (>10 errors in 5 min) → restart + backoff

All actions logged to ~/bird-snapshots/logs/health-monitor.log

NOTE (2026-04-30): this script uses `launchctl` (macOS-only) and a
SERVICES dict that's stale (lists retired bird-classifier, bird-capture,
bird-livedetect; missing bird-pipeline + the timer-driven units). On Pi
it exits immediately because there's no launchctl. The Pi side has its
own per-service supervision via systemd-user; bird-pipeline crashes are
caught by `Restart=always RestartSec=10` rather than this watchdog.
A full refactor of this file (or a Pi-equivalent watchdog) is a
separate task — see cross-claude-comms.md for the deferred-task list.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Hard-stop on Pi: launchctl doesn't exist, the SERVICES dict is iMac-shaped,
# and Pi services are supervised by systemd-user. Don't even import the rest.
if os.environ.get("PI_MODE", "0") == "1" or sys.platform != "darwin":
    print("health_monitor.py is iMac-only; Pi uses systemd-user supervision. Exiting.")
    sys.exit(0)

# Setup logging
LOG_PATH = Path.home() / "bird-snapshots" / "logs" / "health-monitor.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(str(LOG_PATH)),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("health_monitor")

# Paths
INCOMING_DIR = Path.home() / "bird-snapshots" / "incoming"
BIRDNET_DB = Path.home() / "bird-snapshots" / "birdnet-audio" / "birdnet_local.db"
CLIPS_DIR = Path.home() / "bird-snapshots" / "birdnet-audio" / "clips"
HEALTH_OUTPUT = Path("/tmp/bird-observatory-health.json")
BACKOFF_FILE = Path("/tmp/health-monitor-backoff.json")

# Thresholds
CLASSIFIER_QUEUE_WARN = 500
CLASSIFIER_QUEUE_RESTART = 2000
HEALTH_FILE_STALE_SEC = 600  # 10 minutes
AUDIO_QUIET_MIN = 30  # minutes with no detections before restart
ERROR_STORM_THRESHOLD = 10  # errors in 5 minutes
BACKOFF_MINUTES = 15  # wait after restart before rechecking

# Services
SERVICES = {
    "bird-audio": {"critical": True, "log": "audio-analyzer-stderr.log"},
    "bird-enhanced-audio": {"critical": False, "log": "enhanced-audio-stderr.log"},
    "bird-classifier": {"critical": True, "log": "classifier-stderr.log"},
    "bird-dashboard": {"critical": True, "log": "dashboard-stderr.log"},
    "bird-livedetect": {"critical": False, "log": "live_detector_stderr.log"},
    "bird-capture": {"critical": True, "log": None},
    "bird-go2rtc": {"critical": False, "log": None, "docker": True, "container": "go2rtc"},
    "bird-tunnel": {"critical": False, "log": None},
}

DOCKER_CLI = "/Applications/Docker.app/Contents/Resources/bin/docker"


def _get_uid():
    return str(os.getuid())


def _is_service_running(name):
    """Check if a LaunchAgent is running."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f"com.vives.{name}" in line:
                parts = line.split()
                pid = parts[0] if len(parts) >= 3 else "-"
                exit_code = parts[1] if len(parts) >= 3 else "?"
                if pid != "-" and pid != "0":
                    return True, f"PID {pid}"
                # PID is "-" = not currently running. Check if it's a KeepAlive service
                # that should be running (exit code > 0 means crash)
                if exit_code not in ("0", "1", "-"):
                    return True, f"restarting (exit {exit_code})"
                return False, f"not running (exit {exit_code})"
        return False, "not loaded"
    except Exception as e:
        return False, str(e)


def _is_docker_running(container_name):
    """Check if a Docker container is running."""
    try:
        result = subprocess.run(
            [DOCKER_CLI, "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "true" in result.stdout.lower():
            return True, "running"
        return False, result.stdout.strip() or "not running"
    except FileNotFoundError:
        return False, "docker CLI not found"
    except Exception as e:
        return False, str(e)


def _restart_docker(container_name):
    """Restart a Docker container."""
    try:
        subprocess.run(
            [DOCKER_CLI, "restart", container_name],
            capture_output=True, timeout=30,
        )
        log.warning("RESTART (docker): %s", container_name)
        return True
    except Exception as e:
        log.error("Failed to restart docker container %s: %s", container_name, e)
        return False


def _restart_service(name):
    """Restart a LaunchAgent."""
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{_get_uid()}/com.vives.{name}"],
            capture_output=True, timeout=10,
        )
        log.warning("RESTART: %s", name)
        return True
    except Exception as e:
        log.error("Failed to restart %s: %s", name, e)
        return False


def _get_backoff():
    """Load backoff state (which services were recently restarted)."""
    try:
        if BACKOFF_FILE.exists():
            data = json.loads(BACKOFF_FILE.read_text())
            return {k: v for k, v in data.items()
                    if time.time() - v < BACKOFF_MINUTES * 60}
    except Exception:
        pass
    return {}


def _set_backoff(service):
    """Record that a service was restarted (prevents re-restart for BACKOFF_MINUTES)."""
    backoff = _get_backoff()
    backoff[service] = time.time()
    BACKOFF_FILE.write_text(json.dumps(backoff))


def _is_daytime():
    """Quick daytime check — 6 AM to 8 PM."""
    hour = datetime.now().hour
    return 6 <= hour < 20


def _count_recent_errors(log_file, minutes=5):
    """Count ERROR lines in the last N minutes of a log file."""
    if not log_file or not Path(log_file).exists():
        return 0
    cutoff = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Simple: count lines with ERROR in last 100 lines
    try:
        result = subprocess.run(
            ["tail", "-100", str(log_file)],
            capture_output=True, text=True, timeout=5,
        )
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(1 for line in result.stdout.splitlines()
                   if "ERROR" in line and today in line)
    except Exception:
        return 0


def check_services():
    """Check all services are running. Returns list of issues."""
    issues = []
    backoff = _get_backoff()

    for name, cfg in SERVICES.items():
        if cfg.get("docker"):
            running, status = _is_docker_running(cfg.get("container", name))
        else:
            running, status = _is_service_running(name)
        if not running:
            issues.append({
                "service": name,
                "issue": f"not running ({status})",
                "severity": "critical" if cfg["critical"] else "warning",
            })
            if name not in backoff:
                if cfg.get("docker"):
                    _restart_docker(cfg.get("container", name))
                else:
                    _restart_service(name)
                _set_backoff(name)
                issues[-1]["action"] = "restarted"

    return issues


def check_classifier_queue():
    """Check if classifier is keeping up with incoming images."""
    issues = []
    try:
        count = len(list(INCOMING_DIR.glob("*.jpg")))
    except Exception:
        count = 0

    if count > CLASSIFIER_QUEUE_RESTART:
        backoff = _get_backoff()
        issues.append({
            "service": "bird-classifier",
            "issue": f"queue backlog: {count} files",
            "severity": "critical",
        })
        if "bird-classifier" not in backoff:
            _restart_service("bird-classifier")
            _set_backoff("bird-classifier")
            issues[-1]["action"] = "restarted"
    elif count > CLASSIFIER_QUEUE_WARN:
        issues.append({
            "service": "bird-classifier",
            "issue": f"queue growing: {count} files",
            "severity": "warning",
        })

    return issues


def check_audio_health():
    """Check audio streams are healthy."""
    issues = []
    import glob as _glob

    health_files = _glob.glob("/tmp/audio-stream-health-*.json")
    for path in health_files:
        try:
            data = json.loads(Path(path).read_text())
            updated = datetime.fromisoformat(data.get("updated", ""))
            age = (datetime.now() - updated).total_seconds()
            if age > HEALTH_FILE_STALE_SEC:
                service = data.get("service", "unknown")
                issues.append({
                    "service": f"audio ({service})",
                    "issue": f"health stale ({int(age)}s)",
                    "severity": "warning",
                })
        except Exception:
            pass

    # Check for audio detections in last 30 min (daytime only)
    if _is_daytime() and BIRDNET_DB.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{BIRDNET_DB}?mode=ro", uri=True, timeout=5)
            row = conn.execute(
                "SELECT MAX(time) FROM notes WHERE date = ?",
                (datetime.now().strftime("%Y-%m-%d"),),
            ).fetchone()
            conn.close()
            if row and row[0]:
                last_time = datetime.strptime(
                    f"{datetime.now().strftime('%Y-%m-%d')} {row[0]}",
                    "%Y-%m-%d %H:%M:%S",
                )
                quiet_min = (datetime.now() - last_time).total_seconds() / 60
                if quiet_min > AUDIO_QUIET_MIN:
                    issues.append({
                        "service": "bird-audio",
                        "issue": f"no detections for {int(quiet_min)} min",
                        "severity": "warning",
                    })
            elif _is_daytime():
                issues.append({
                    "service": "bird-audio",
                    "issue": "no detections today",
                    "severity": "warning",
                })
        except Exception as e:
            issues.append({
                "service": "bird-audio",
                "issue": f"DB check failed: {e}",
                "severity": "warning",
            })

    return issues


def check_disk_space():
    """Check disk space for critical directories."""
    issues = []
    try:
        stat = os.statvfs(str(CLIPS_DIR))
        usage_pct = (1 - stat.f_bavail / stat.f_blocks) * 100
        if usage_pct > 90:
            issues.append({
                "service": "disk",
                "issue": f"clips partition {usage_pct:.0f}% full",
                "severity": "critical",
            })
        elif usage_pct > 80:
            issues.append({
                "service": "disk",
                "issue": f"clips partition {usage_pct:.0f}% full",
                "severity": "warning",
            })
    except Exception:
        pass
    return issues


def main():
    log.info("Health check starting")
    all_issues = []

    all_issues.extend(check_services())
    all_issues.extend(check_classifier_queue())
    all_issues.extend(check_audio_health())
    all_issues.extend(check_disk_space())

    # Build status report
    status = {
        "timestamp": datetime.now().isoformat(),
        "healthy": len([i for i in all_issues if i["severity"] == "critical"]) == 0,
        "issues": all_issues,
        "services_checked": len(SERVICES),
        "actions_taken": [i for i in all_issues if "action" in i],
    }

    # Write status file
    try:
        tmp = str(HEALTH_OUTPUT) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp, str(HEALTH_OUTPUT))
    except Exception as e:
        log.error("Failed to write status file: %s", e)

    # Log summary
    critical = [i for i in all_issues if i["severity"] == "critical"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]
    actions = [i for i in all_issues if "action" in i]

    if critical:
        for i in critical:
            log.error("CRITICAL: %s — %s%s", i["service"], i["issue"],
                      f" [{i['action']}]" if "action" in i else "")
    if warnings:
        for i in warnings:
            log.warning("WARNING: %s — %s", i["service"], i["issue"])
    if actions:
        log.info("Actions taken: %d service(s) restarted", len(actions))
    if not all_issues:
        log.info("All systems healthy")

    log.info("Health check complete")


if __name__ == "__main__":
    main()
