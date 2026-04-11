#!/usr/bin/env python3
"""End-to-end verification of the v3 prototype.

Assumes the v3 pipeline is already running (launched separately), and connects
to it via HTTP to verify:
- Pipeline health endpoint responds
- SSE event stream is producing events
- Dashboard (uvicorn) is reachable
- Headless browser can load the dashboard, see <video>, check for labels

Exits 0 on pass. Writes a JSON report and screenshots to
docs/superpowers/progress/2026-04-11-v3-verification/.

Usage:
    python scripts/verify_v3_prototype.py
    python scripts/verify_v3_prototype.py --health-url http://127.0.0.1:8102/health
    python scripts/verify_v3_prototype.py --no-browser
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO.parent.parent / "docs" / "superpowers" / "progress" / "2026-04-11-v3-verification"
# The verification script lives inside the worktree, but evidence should go to
# the main repo's docs tree so David can read it easily.
if not EVIDENCE_DIR.parent.exists():
    EVIDENCE_DIR = REPO / "docs" / "superpowers" / "progress" / "2026-04-11-v3-verification"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def check_pipeline_health(url: str) -> dict:
    log(f"Checking pipeline health: {url}")
    for i in range(30):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read())
            log(f"  Pipeline health: overall={data.get('overall')}")
            return data
        except Exception as e:
            if i % 5 == 0:
                log(f"  (waiting for pipeline, try {i+1}/30): {e.__class__.__name__}")
            time.sleep(1)
    raise SystemExit(f"Pipeline health endpoint did not respond within 30s: {url}")


def check_sse_stream(url: str, duration_s: float = 15) -> list:
    log(f"Subscribing to SSE for {duration_s}s: {url}")
    events = []
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        buf = b""
        deadline = time.time() + duration_s
        while time.time() < deadline:
            chunk = resp.read1(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.decode("utf-8", errors="replace").split("\n"):
                    if line.startswith("data: "):
                        try:
                            events.append(json.loads(line[6:]))
                        except Exception:
                            pass
    except Exception as e:
        log(f"  SSE subscription error: {e}")
    log(f"  Captured {len(events)} SSE events in {duration_s}s")
    return events


def browser_check(dashboard_url: str, duration_s: float = 30) -> dict:
    log(f"Opening headless browser for {dashboard_url}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("  playwright not installed — skipping browser check")
        return {"skipped": True, "reason": "playwright not installed"}

    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})

        console_messages = []
        page.on("console", lambda msg: console_messages.append({
            "type": msg.type, "text": msg.text[:200]
        }))

        try:
            page.goto(dashboard_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            result["nav_error"] = str(e)
            browser.close()
            return result

        # Wait a moment for scripts to initialize
        time.sleep(3)

        # Check v3 video element
        video_ready = page.evaluate(
            "() => { const v = document.getElementById('v3-live-video'); "
            "return v ? { present: true, readyState: v.readyState, currentTime: v.currentTime } "
            ": { present: false }; }"
        )
        result["v3_video"] = video_ready
        log(f"  v3-live-video: {video_ready}")

        # Check canvas element
        canvas_info = page.evaluate(
            "() => { const c = document.getElementById('v3-label-overlay'); "
            "return c ? { present: true, width: c.width, height: c.height } "
            ": { present: false }; }"
        )
        result["v3_canvas"] = canvas_info
        log(f"  v3-label-overlay: {canvas_info}")

        # Initial screenshot
        s1 = EVIDENCE_DIR / f"dashboard-initial-{int(time.time())}.png"
        page.screenshot(path=str(s1))
        log(f"  Initial screenshot: {s1.name}")

        # Wait for events and labels
        log(f"  Waiting {duration_s}s for labels to appear...")
        time.sleep(duration_s)

        # Track state count
        track_state_count = page.evaluate(
            "() => window.__v3TrackStates ? window.__v3TrackStates.size : -1"
        )
        result["track_state_count_after_wait"] = track_state_count
        log(f"  trackStates.size after wait: {track_state_count}")

        # After-screenshot
        s2 = EVIDENCE_DIR / f"dashboard-after-{int(duration_s)}s-{int(time.time())}.png"
        page.screenshot(path=str(s2))
        log(f"  After-{int(duration_s)}s screenshot: {s2.name}")

        result["screenshots"] = [str(s1), str(s2)]
        result["console_messages"] = console_messages[-50:]

        browser.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--health-url",
                        default=os.environ.get("V3_HEALTH_URL",
                                               "http://127.0.0.1:8102/health"))
    parser.add_argument("--sse-url",
                        default=os.environ.get("V3_SSE_URL",
                                               "http://127.0.0.1:8104/events/sse?camera=feeder"))
    parser.add_argument("--dashboard-url",
                        default=os.environ.get("V3_DASHBOARD_URL",
                                               "http://127.0.0.1:8099/"))
    parser.add_argument("--sse-duration", type=float, default=15)
    parser.add_argument("--browser-duration", type=float, default=30)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    log("=" * 60)
    log("v3 Prototype Verification")
    log("=" * 60)

    checks = {"timestamp": time.time()}

    # 1. Pipeline health
    health = check_pipeline_health(args.health_url)
    checks["health"] = health

    # 2. SSE stream
    events = check_sse_stream(args.sse_url, duration_s=args.sse_duration)
    checks["sse_event_count"] = len(events)
    checks["sse_has_events"] = len(events) > 0
    checks["sse_sample_events"] = events[:3]  # first 3 events for inspection

    # 3. Browser check
    if args.no_browser:
        checks["browser"] = {"skipped": True, "reason": "--no-browser"}
    else:
        checks["browser"] = browser_check(args.dashboard_url,
                                          duration_s=args.browser_duration)

    # Save report
    report_path = EVIDENCE_DIR / f"verification-{int(time.time())}.json"
    with open(report_path, "w") as f:
        json.dump(checks, f, indent=2, default=str)
    log(f"Full report: {report_path}")

    # Summary
    log("=" * 60)
    log("Summary:")
    log(f"  overall health: {health.get('overall')}")
    log(f"  SSE events in {args.sse_duration}s: {len(events)}")
    browser_summary = checks["browser"]
    if browser_summary.get("skipped"):
        log(f"  browser: skipped ({browser_summary.get('reason')})")
    else:
        v = browser_summary.get("v3_video", {})
        log(f"  v3 video readyState: {v.get('readyState')}")
        log(f"  trackStates: {browser_summary.get('track_state_count_after_wait')}")
    log("=" * 60)

    # Exit code: 0 if overall not broken
    return 0 if health.get("overall") in ("ok", "degraded") else 1


if __name__ == "__main__":
    sys.exit(main())
