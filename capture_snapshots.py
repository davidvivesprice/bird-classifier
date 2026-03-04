#!/usr/bin/env python3
"""Capture bird feeder snapshots on motion via UniFi Protect snapshot API.

Runs directly on the iMac — captures frames, detects motion, and saves
directly to the classifier's incoming directory. No NAS sync needed.

Based on the NAS version (bird_snapshots.py) but outputs locally.
"""

from __future__ import annotations

import io
import os
import pathlib
import signal
import ssl
import sys
import time
import urllib.request

from PIL import Image

# Config (all overridable via env)
PROTECT_HOST = os.environ.get('PROTECT_HOST', '192.168.4.9')
API_KEY = os.environ.get('UNIFI_PROTECT_API_KEY', '9X1Ua2_GyZHsvW2jRTkO1-zcM-S2F_g-')
CAMERA_ID = os.environ.get('CAMERA_ID', '690e999401027503e400043b')
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '5'))
OUTPUT_DIR = pathlib.Path(os.environ.get('OUTPUT_DIR', '/Users/vives/bird-snapshots/incoming'))
# Percentage of pixels that must change to trigger a save
MOTION_THRESHOLD = float(os.environ.get('MOTION_THRESHOLD', '1.0'))
# Per-pixel intensity change required to count as "changed"
PIXEL_THRESHOLD = int(os.environ.get('PIXEL_THRESHOLD', '20'))
# Downscale factor for comparison (faster, less noise)
COMPARE_SCALE = int(os.environ.get('COMPARE_SCALE', '8'))
COOLDOWN = int(os.environ.get('COOLDOWN', '10'))
MAX_PER_DAY = int(os.environ.get('MAX_PER_DAY', '2000'))

SNAPSHOT_URL = f'https://{PROTECT_HOST}/proxy/protect/integration/v1/cameras/{CAMERA_ID}/snapshot'

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

running = True


def handle_signal(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def fetch_snapshot() -> bytes | None:
    req = urllib.request.Request(SNAPSHOT_URL, headers={'X-API-KEY': API_KEY})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            return resp.read()
    except Exception as e:
        sys.stderr.write(f'snapshot error: {e}\n')
        sys.stderr.flush()
        return None


def to_grayscale_thumbnail(data: bytes) -> Image.Image:
    """Load JPEG bytes, downscale, convert to grayscale for fast comparison."""
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    small = img.resize((w // COMPARE_SCALE, h // COMPARE_SCALE), Image.NEAREST)
    return small.convert('L')


def motion_score(prev: Image.Image, curr: Image.Image) -> float:
    """Return percentage of pixels that changed significantly."""
    prev_data = prev.getdata()
    curr_data = curr.getdata()
    if len(prev_data) != len(curr_data):
        return 100.0
    changed = sum(1 for a, b in zip(prev_data, curr_data) if abs(a - b) > PIXEL_THRESHOLD)
    return changed / len(prev_data) * 100


def day_count(directory: pathlib.Path) -> int:
    today = time.strftime('%Y-%m-%d')
    return sum(1 for f in directory.iterdir() if f.name.startswith(today) and f.suffix == '.jpg')


def main() -> None:
    if not API_KEY:
        sys.stderr.write('UNIFI_PROTECT_API_KEY not set\n')
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prev_thumb = None
    last_save = 0.0
    errors = 0

    sys.stdout.write(
        f'capture_snapshots: poll={POLL_INTERVAL}s, motion={MOTION_THRESHOLD}%, '
        f'pixel_thresh={PIXEL_THRESHOLD}, scale=1/{COMPARE_SCALE}, cooldown={COOLDOWN}s\n'
    )
    sys.stdout.write(f'capture_snapshots: saving to {OUTPUT_DIR}\n')
    sys.stdout.flush()

    while running:
        data = fetch_snapshot()
        if data is None:
            errors += 1
            if errors > 10:
                sys.stderr.write('too many errors, sleeping 60s\n')
                time.sleep(60)
                errors = 0
            else:
                time.sleep(POLL_INTERVAL)
            continue
        errors = 0

        try:
            curr_thumb = to_grayscale_thumbnail(data)
        except Exception as e:
            sys.stderr.write(f'image decode error: {e}\n')
            time.sleep(POLL_INTERVAL)
            continue

        now = time.time()
        if prev_thumb is not None:
            score = motion_score(prev_thumb, curr_thumb)
            if score >= MOTION_THRESHOLD and (now - last_save) >= COOLDOWN:
                if day_count(OUTPUT_DIR) < MAX_PER_DAY:
                    ts = time.strftime('%Y-%m-%d_%H-%M-%S')
                    fname = OUTPUT_DIR / f'{ts}.jpg'
                    fname.write_bytes(data)
                    last_save = now
                    sys.stdout.write(f'saved: {fname.name} ({len(data)} bytes, motion={score:.1f}%)\n')
                    sys.stdout.flush()

        prev_thumb = curr_thumb
        time.sleep(POLL_INTERVAL)

    sys.stdout.write('capture_snapshots: shutting down\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
