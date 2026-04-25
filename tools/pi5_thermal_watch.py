#!/usr/bin/env python3
"""Single-shot thermal/load sampler for Pi 5.

Appends one CSV row to /home/vives/logs/pi5-thermal-watch.csv.
Driven by pi5-thermal-watch.timer (systemd --user, every minute).

Captures:
- Wall-clock timestamp
- CPU temperature (thermal_zone0)
- Active CPU clock (arm)
- Hailo NPU temperature (via hailortcli sensors, if available)
- Fan RPM
- Pipeline counters (frames_processed, detections, snapshots written, hi-res ring picks)

Goal: 24h baseline of whether the 83-85°C operating range is steady-state
or trends upward, and whether ring buffer / detector throughput correlates
with thermal pressure. Per pi5-handoff §4 task #1.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

CSV_PATH = Path.home() / "logs" / "pi5-thermal-watch.csv"
HEALTH_URL = "http://localhost:8100/api/pipeline/health"

COLUMNS = [
    "ts",
    "cpu_temp_c",
    "arm_clock_hz",
    "hailo_temp_c",
    "fan_rpm",
    "frames_processed",
    "frames_dropped_oldest",
    "ffmpeg_restarts_lasthr",
    "yolo_ms_avg",
    "detections_total",
    "active_tracks",
    "snap_submitted",
    "snap_written",
    "ring_pick_ok",
    "ring_pick_empty",
    "uptime_s",
]


def read_cpu_temp_c() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return None


def read_arm_clock_hz() -> int | None:
    try:
        out = subprocess.check_output(["vcgencmd", "measure_clock", "arm"],
                                       text=True, timeout=2).strip()
        # format: "frequency(48)=1500000000"
        return int(out.split("=", 1)[1])
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def read_fan_rpm() -> int | None:
    """Walk /sys/class/hwmon/*/fan1_input. Returns first non-zero reading."""
    base = Path("/sys/class/hwmon")
    if not base.exists():
        return None
    for hwmon in base.iterdir():
        f = hwmon / "fan1_input"
        if f.exists():
            try:
                v = int(f.read_text().strip())
                if v > 0:
                    return v
            except (ValueError, OSError):
                continue
    return None


def read_hailo_temp_c() -> float | None:
    """Best-effort. hailortcli sensors output format is not stable across
    versions; this tries a few patterns and returns None on miss rather
    than failing the whole sample."""
    try:
        out = subprocess.check_output(["hailortcli", "sensors"],
                                       text=True, timeout=3, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return None
    for line in out.splitlines():
        # Look for any line with a temperature-shaped value
        low = line.lower()
        if "temp" in low or "°c" in low or "celsius" in low:
            # Pull the first float we can find
            for tok in line.replace(":", " ").replace(",", " ").split():
                try:
                    v = float(tok)
                    if 10.0 < v < 120.0:
                        return v
                except ValueError:
                    continue
    return None


def read_pipeline_health() -> dict:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return {}


def main() -> int:
    health = read_pipeline_health()
    feeder = (health.get("pipeline") or {}).get("feeder") or {}
    cap = feeder.get("capture") or {}
    det = feeder.get("detector") or {}
    trk = feeder.get("tracker") or {}
    sw = ((health.get("shared") or {}).get("snapshot_writer")) or {}

    row = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "cpu_temp_c": read_cpu_temp_c(),
        "arm_clock_hz": read_arm_clock_hz(),
        "hailo_temp_c": read_hailo_temp_c(),
        "fan_rpm": read_fan_rpm(),
        "frames_processed": cap.get("frames_processed"),
        "frames_dropped_oldest": cap.get("dropped_oldest"),
        "ffmpeg_restarts_lasthr": cap.get("ffmpeg_restarts_last_hour"),
        "yolo_ms_avg": det.get("yolo_ms_avg"),
        "detections_total": det.get("detections_total"),
        "active_tracks": trk.get("active_tracks"),
        "snap_submitted": sw.get("submitted"),
        "snap_written": sw.get("written"),
        "ring_pick_ok": sw.get("ring_pick_ok"),
        "ring_pick_empty": sw.get("ring_pick_empty"),
        "uptime_s": health.get("uptime_s"),
    }

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0
    with CSV_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
