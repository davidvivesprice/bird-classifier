#!/usr/bin/env python3
"""Capture bird snapshots on motion via UniFi Protect snapshot API.

Runs directly on the iMac — captures frames, detects motion, and saves
directly to the classifier's incoming directory. No NAS sync needed.

Supports multiple cameras via threaded polling. Each camera has independent
motion state, cooldown, and daily count tracking. Filenames are prefixed
with camera name: {camera}_{YYYY-MM-DD_HH-MM-SS}.jpg
"""

from __future__ import annotations

import io
import os
import pathlib
import signal
import ssl
import sys
import threading
import time
import urllib.request

from PIL import Image

# Config (all overridable via env)
PROTECT_HOST = os.environ.get('PROTECT_HOST', '192.168.4.9')
API_KEY = os.environ.get('UNIFI_PROTECT_API_KEY', '9X1Ua2_GyZHsvW2jRTkO1-zcM-S2F_g-')

# Camera config: comma-separated name:id pairs
CAMERAS_STR = os.environ.get(
    'CAMERAS',
    'feeder:690e999401027503e400043b,ground:690e999400887503e4000439'
)

POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '5'))
OUTPUT_DIR = pathlib.Path(os.environ.get('OUTPUT_DIR', '/Users/vives/bird-snapshots/incoming'))
MOTION_THRESHOLD = float(os.environ.get('MOTION_THRESHOLD', '1.0'))
PIXEL_THRESHOLD = int(os.environ.get('PIXEL_THRESHOLD', '20'))
COMPARE_SCALE = int(os.environ.get('COMPARE_SCALE', '8'))
COOLDOWN = int(os.environ.get('COOLDOWN', '10'))
MAX_PER_DAY = int(os.environ.get('MAX_PER_DAY', '2000'))

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

running = True


def handle_signal(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def parse_cameras(cameras_str: str) -> list[tuple[str, str]]:
    """Parse 'name:id,name:id' into list of (name, camera_id) tuples."""
    cameras = []
    for entry in cameras_str.split(','):
        entry = entry.strip()
        if ':' in entry:
            name, cam_id = entry.split(':', 1)
            cameras.append((name.strip(), cam_id.strip()))
    return cameras


def fetch_snapshot(camera_id: str) -> bytes | None:
    url = f'https://{PROTECT_HOST}/proxy/protect/integration/v1/cameras/{camera_id}/snapshot'
    req = urllib.request.Request(url, headers={'X-API-KEY': API_KEY})
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


def day_count(directory: pathlib.Path, camera_name: str) -> int:
    """Count today's captures for a specific camera."""
    today = time.strftime('%Y-%m-%d')
    prefix = f'{camera_name}_{today}'
    return sum(1 for f in directory.iterdir() if f.name.startswith(prefix) and f.suffix == '.jpg')


def camera_loop(camera_name: str, camera_id: str) -> None:
    """Main capture loop for a single camera. Runs in its own thread."""
    prev_thumb = None
    last_save = 0.0
    errors = 0

    sys.stdout.write(
        f'[{camera_name}] started: camera_id={camera_id}, poll={POLL_INTERVAL}s, '
        f'motion={MOTION_THRESHOLD}%, pixel_thresh={PIXEL_THRESHOLD}, '
        f'scale=1/{COMPARE_SCALE}, cooldown={COOLDOWN}s\n'
    )
    sys.stdout.flush()

    while running:
        data = fetch_snapshot(camera_id)
        if data is None:
            errors += 1
            if errors > 10:
                sys.stderr.write(f'[{camera_name}] too many errors, sleeping 60s\n')
                time.sleep(60)
                errors = 0
            else:
                time.sleep(POLL_INTERVAL)
            continue
        errors = 0

        try:
            curr_thumb = to_grayscale_thumbnail(data)
        except Exception as e:
            sys.stderr.write(f'[{camera_name}] image decode error: {e}\n')
            time.sleep(POLL_INTERVAL)
            continue

        now = time.time()
        if prev_thumb is not None:
            score = motion_score(prev_thumb, curr_thumb)
            if score >= MOTION_THRESHOLD and (now - last_save) >= COOLDOWN:
                if day_count(OUTPUT_DIR, camera_name) < MAX_PER_DAY:
                    ts = time.strftime('%Y-%m-%d_%H-%M-%S')
                    fname = OUTPUT_DIR / f'{camera_name}_{ts}.jpg'
                    fname.write_bytes(data)
                    last_save = now
                    sys.stdout.write(
                        f'[{camera_name}] saved: {fname.name} '
                        f'({len(data)} bytes, motion={score:.1f}%)\n'
                    )
                    sys.stdout.flush()

        prev_thumb = curr_thumb
        time.sleep(POLL_INTERVAL)

    sys.stdout.write(f'[{camera_name}] shutting down\n')
    sys.stdout.flush()


def main() -> None:
    if not API_KEY:
        sys.stderr.write('UNIFI_PROTECT_API_KEY not set\n')
        sys.exit(1)

    cameras = parse_cameras(CAMERAS_STR)
    if not cameras:
        sys.stderr.write('No cameras configured. Set CAMERAS=name:id,name:id\n')
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    sys.stdout.write(f'capture_snapshots: {len(cameras)} camera(s), saving to {OUTPUT_DIR}\n')
    sys.stdout.flush()

    threads = []
    for name, cam_id in cameras:
        t = threading.Thread(target=camera_loop, args=(name, cam_id), daemon=True, name=f'cam-{name}')
        t.start()
        threads.append(t)

    # Wait for shutdown signal
    while running:
        time.sleep(1)

    sys.stdout.write('capture_snapshots: shutting down all cameras\n')
    sys.stdout.flush()

    for t in threads:
        t.join(timeout=5)


if __name__ == '__main__':
    main()
