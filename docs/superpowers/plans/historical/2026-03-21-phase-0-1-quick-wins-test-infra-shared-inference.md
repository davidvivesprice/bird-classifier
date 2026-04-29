> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Phase 0 + 0.5 + 1: Quick Wins, Test Infrastructure, Shared Inference

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish test infrastructure, fix operational gaps, and extract shared inference code — setting the foundation for all subsequent work.

**Architecture:** Extract duplicated ML inference code into `bird_inference.py` and solar calculations into `solar_utils.py`. Set up pytest with a test image fixture. Create mock RTSP feeds from recorded video for reproducible benchmarking. Fix operational gaps (log rotation, health checks, indexes, stale configs).

**Tech Stack:** Python 3.9 (venv-coral), pytest, ONNX Runtime, ffmpeg (mock RTSP), SQLite, macOS LaunchAgents, newsyslog

**Spec:** `docs/superpowers/specs/2026-03-21-foundations-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `bird_inference.py` | Shared YOLO detection + AIY species classification + species aliases |
| `solar_utils.py` | Shared sunrise/sunset calculation + nighttime check |
| `tests/conftest.py` | Pytest fixtures (test images, model paths, temp dirs) |
| `tests/test_bird_inference.py` | Unit tests for YOLO detector, species classifier, parse_label, normalize_species |
| `tests/test_solar_utils.py` | Unit tests for solar calculations |
| `tests/test_integration.py` | Integration test: same image through classify.py and live_detector.py paths |
| `tests/fixtures/` | Test images directory |
| `pytest.ini` | Pytest configuration |
| `test_clips/` | Directory for mock RTSP video clips |
| `test_clips/README.md` | Instructions for capturing test clips |

### Modified Files
| File | What Changes |
|------|-------------|
| `classify.py` | Remove duplicated functions, import from bird_inference.py and solar_utils.py |
| `live_detector.py` | Remove duplicated functions, import from bird_inference.py and solar_utils.py |
| `audio_analyzer.py` | Remove _solar_times/is_nighttime, import from solar_utils.py |
| `classifications_db.py` | Remove SPECIES_ALIASES (L20-24), import from bird_inference.py. Add 2 composite indexes (L91-100) |
| `dashboard/api.py` | Remove SPECIES_ALIASES (L48-52) and normalize_species (L54-55), import from bird_inference.py |
| `models/chilmark_feeder_species.txt` | Remove "Slate-colored Junco" (line 53) |
| `.gitignore` | Add test_clips/*.mp4 (large video files) |

### Deleted Files
| File | Why |
|------|-----|
| `config/go2rtc.yaml` | Stale — RTSP tokens and WebRTC candidate IP don't match active root copy |

---

## Task 1: Set Up Test Infrastructure

**Files:**
- Create: `pytest.ini`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/fixtures/` (directory)

- [ ] **Step 1: Create pytest.ini**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
```

- [ ] **Step 2: Create tests directory structure**

```bash
mkdir -p tests/fixtures
touch tests/__init__.py
```

- [ ] **Step 3: Create conftest.py with fixtures**

```python
"""Shared pytest fixtures for bird classifier tests."""
import os
import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

@pytest.fixture
def project_root():
    return PROJECT_ROOT

@pytest.fixture
def models_dir():
    return PROJECT_ROOT / "models"

@pytest.fixture
def yolo_model_path(models_dir):
    path = models_dir / "yolov8n_bird.onnx"
    if not path.exists():
        pytest.skip(f"YOLO model not found at {path}")
    return str(path)

@pytest.fixture
def species_model_path(models_dir):
    path = models_dir / "aiy_birds_v1.onnx"
    if not path.exists():
        pytest.skip(f"Species model not found at {path}")
    return str(path)

@pytest.fixture
def labels_path(models_dir):
    path = models_dir / "inat_bird_labels.txt"
    if not path.exists():
        pytest.skip(f"Labels not found at {path}")
    return str(path)

@pytest.fixture
def regional_species_path(models_dir):
    path = models_dir / "chilmark_feeder_species.txt"
    if not path.exists():
        pytest.skip(f"Regional species not found at {path}")
    return str(path)

@pytest.fixture
def regional_species(regional_species_path):
    species = set()
    with open(regional_species_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                species.add(line)
    return species

@pytest.fixture
def test_bird_image():
    """Return path to a known bird test image. Uses first classified image available."""
    classified_dir = PROJECT_ROOT.parent / "bird-snapshots" / "classified"
    if not classified_dir.exists():
        pytest.skip("No classified images directory")
    # Find first available species directory with images
    for species_dir in sorted(classified_dir.iterdir()):
        if species_dir.is_dir():
            jpgs = list(species_dir.glob("*.jpg"))
            if jpgs:
                return str(jpgs[0])
    pytest.skip("No classified images found")

@pytest.fixture
def test_bird_image_pil(test_bird_image):
    from PIL import Image
    return Image.open(test_bird_image).convert("RGB")
```

- [ ] **Step 4: Verify pytest discovers config**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest --collect-only 2>&1 | head -20`
Expected: "no tests ran" or "collected 0 items" (no errors)

- [ ] **Step 5: Commit**

```bash
git add pytest.ini tests/
git commit -m "test: set up pytest infrastructure with model and image fixtures"
```

---

## Task 2: Quick Wins — Indexes and Stale Config

**Files:**
- Modify: `classifications_db.py:91-100` (INDEXES list)
- Delete: `config/go2rtc.yaml`

- [ ] **Step 1: Add composite indexes to classifications_db.py**

Add two lines to the INDEXES list at line 98, before the closing `]`:

```python
    "CREATE INDEX IF NOT EXISTS idx_cls_action_common ON classifications(action, common_name)",
    "CREATE INDEX IF NOT EXISTS idx_cls_date_action_name ON classifications(source_date, action, common_name)",
```

The full INDEXES list should now have 10 entries.

- [ ] **Step 2: Delete stale config/go2rtc.yaml**

```bash
rm ~/bird-classifier/config/go2rtc.yaml
```

Verify the active go2rtc.yaml is still at the project root:
```bash
ls -la ~/bird-classifier/go2rtc.yaml
```

- [ ] **Step 3: Verify indexes get created**

Run: `cd ~/bird-classifier && venv-coral/bin/python -c "import classifications_db; print('Indexes:', len(classifications_db.INDEXES))"`
Expected: `Indexes: 10`

- [ ] **Step 4: Force index creation on live database**

```bash
cd ~/bird-classifier && venv-coral/bin/python -c "
from classifications_db import get_conn
conn = get_conn()
conn.execute('CREATE INDEX IF NOT EXISTS idx_cls_action_common ON classifications(action, common_name)')
conn.execute('CREATE INDEX IF NOT EXISTS idx_cls_date_action_name ON classifications(source_date, action, common_name)')
print('Indexes created successfully')
"
```

- [ ] **Step 5: Commit**

```bash
git add classifications_db.py
git commit -m "perf: add composite indexes (action,common_name) and (date,action,name)

Speeds up species-filtered queries and date+species queries.
Also removes stale config/go2rtc.yaml (RTSP tokens out of sync with active root copy)."
```

---

## Task 3: Quick Wins — Log Rotation and Health Check Scheduling

**Files:**
- Create: `com.vives.bird-healthcheck.plist` (in ~/Library/LaunchAgents/)
- Note: Log rotation requires sudo — document the command, run manually

- [ ] **Step 1: Create health check LaunchAgent plist**

Write to `~/Library/LaunchAgents/com.vives.bird-healthcheck.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vives.bird-healthcheck</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/vives/bird-classifier/check_health.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>StandardOutPath</key>
    <string>/Users/vives/bird-snapshots/logs/healthcheck-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/vives/bird-snapshots/logs/healthcheck-stderr.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/vives/bird-classifier</string>
</dict>
</plist>
```

- [ ] **Step 2: Load the health check agent**

```bash
launchctl load ~/Library/LaunchAgents/com.vives.bird-healthcheck.plist
```

Verify: `launchctl list | grep healthcheck`

- [ ] **Step 3: Document log rotation setup**

Log rotation requires sudo. Create the newsyslog config file content and instruct the user to run:

```bash
sudo tee /etc/newsyslog.d/bird-observatory.conf << 'EOF'
# logfilename                                          [owner:group] mode count size(KB) when  flags [/pid_file] [sig_num]
/Users/vives/bird-snapshots/logs/classifier-stdout.log              644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/classifier-stderr.log              644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/live_detector_stdout.log           644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/live_detector_stderr.log           644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/dashboard-stdout.log               644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/dashboard-stderr.log               644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/audio-analyzer-stdout.log          644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/audio-analyzer-stderr.log          644  5     10240    *     GN
/Users/vives/bird-snapshots/logs/healthcheck-stdout.log             644  3     1024     *     GN
EOF
```

This rotates each log at 10MB, keeps 5 compressed copies, and doesn't require a signal (GN = no signal, create new file).

- [ ] **Step 4: Fix duplicate log files**

Check if `classifier.log` is a separate Python logging output:
```bash
ls -la ~/bird-snapshots/logs/classifier.log ~/bird-snapshots/logs/classifier-stdout.log
diff <(tail -5 ~/bird-snapshots/logs/classifier.log) <(tail -5 ~/bird-snapshots/logs/classifier-stdout.log)
```

If they're duplicates, the Python logger in classify.py should be checked. The LaunchAgent stdout capture + Python's own file handler = double logging. Fix by removing the Python file handler (the LaunchAgent captures stdout already).

- [ ] **Step 5: Commit health check plist**

```bash
git add ~/Library/LaunchAgents/com.vives.bird-healthcheck.plist 2>/dev/null || true
git add check_health.sh
git commit -m "ops: schedule health checks every 15 minutes via LaunchAgent

Runs check_health.sh automatically. Logs to healthcheck-stdout.log.
Also documents log rotation setup (requires sudo for newsyslog.d)."
```

---

## Task 4: Quick Win — Remove Slate-colored Junco from Species List

**Files:**
- Modify: `models/chilmark_feeder_species.txt` (line 53)

- [ ] **Step 1: Verify current state**

```bash
grep -n "Slate" ~/bird-classifier/models/chilmark_feeder_species.txt
```

Expected: Line 53 (or nearby) shows "Slate-colored Junco"

- [ ] **Step 2: Remove the line**

Delete the "Slate-colored Junco" line from `models/chilmark_feeder_species.txt`. "Dark-eyed Junco" should already be in the file separately. Verify:

```bash
grep -n "Junco\|Dark-eyed" ~/bird-classifier/models/chilmark_feeder_species.txt
```

Expected: Only "Dark-eyed Junco" remains.

- [ ] **Step 3: Commit**

```bash
git add models/chilmark_feeder_species.txt
git commit -m "fix: remove Slate-colored Junco from species list (alias of Dark-eyed Junco)

SPECIES_ALIASES already normalizes this. Having both in the target list
wastes a classification slot."
```

---

## Task 5: Fix HANDOFF.md Wrong Path

**Files:**
- Modify: `~/docs/bird-observatory/migration/HANDOFF.md`

- [ ] **Step 1: Fix reviews.jsonl path**

In HANDOFF.md, find the reference to `~/bird-snapshots/logs/reviews.jsonl` and correct it to `~/bird-classifier/dashboard/reviews.jsonl`.

```bash
grep -n "reviews.jsonl" ~/docs/bird-observatory/migration/HANDOFF.md
```

Update the incorrect path.

- [ ] **Step 2: No commit needed** (docs are in a separate Syncthing-managed directory, not the bird-classifier git repo)

---

## Task 6: Mock RTSP Test Feeds Setup

**Files:**
- Create: `test_clips/README.md`
- Create: `test_clips/serve_test_feed.sh`
- Modify: `.gitignore`

- [ ] **Step 1: Create test_clips directory and README**

```bash
mkdir -p ~/bird-classifier/test_clips
```

Write `test_clips/README.md`:

```markdown
# Test Clips for Mock RTSP Feeds

Video clips used for reproducible pipeline testing and benchmarking.

## Capturing Test Clips

Record clips from live cameras using ffmpeg:

    # 5-minute feeder clip (daytime with birds)
    ffmpeg -i "rtsp://192.168.4.9:7447/$(jq -r '.streams.birds' ../rtsp_urls.json)" \
      -t 300 -c copy feeder_5min.mp4

    # 5-minute ground cam clip
    ffmpeg -i "rtsp://192.168.4.9:7447/$(jq -r '.streams.ground' ../rtsp_urls.json)" \
      -t 300 -c copy ground_5min.mp4

    # Short clips for specific test scenarios
    ffmpeg -i <source> -t 60 -c copy multi_bird_60s.mp4
    ffmpeg -i <source> -t 60 -c copy sparrow_confusion_60s.mp4

## Serving as Mock RTSP

    # Simple: ffmpeg looping server
    bash serve_test_feed.sh feeder_5min.mp4

    # Then point any service at it:
    python live_detector.py --cameras test-feeder:test --go2rtc-url http://localhost:8554

## Files

*.mp4 files are gitignored (too large). Capture your own using the commands above.
```

- [ ] **Step 2: Create serve_test_feed.sh**

Write `test_clips/serve_test_feed.sh`:

```bash
#!/bin/bash
# Serve a video file as a looping RTSP stream for testing.
# Usage: bash serve_test_feed.sh <video_file> [port] [stream_name]
#
# Example:
#   bash serve_test_feed.sh feeder_5min.mp4 8554 test-feeder
#   # Then access at: rtsp://localhost:8554/test-feeder

VIDEO="${1:?Usage: $0 <video_file> [port] [stream_name]}"
PORT="${2:-8554}"
STREAM="${3:-test-feeder}"

if [ ! -f "$VIDEO" ]; then
    echo "Error: Video file not found: $VIDEO"
    exit 1
fi

echo "Serving $VIDEO as rtsp://localhost:$PORT/$STREAM (Ctrl+C to stop)"
echo "Point your service at this URL for testing."

# mediamtx is preferred (proper RTSP server), fall back to ffmpeg
if command -v mediamtx &>/dev/null; then
    # Use mediamtx for proper RTSP serving
    cat > /tmp/mediamtx_test.yml << EOF
paths:
  $STREAM:
    source: "publisher"
    sourceOnDemand: no
EOF
    mediamtx /tmp/mediamtx_test.yml &
    MTXPID=$!
    sleep 1
    ffmpeg -re -stream_loop -1 -i "$VIDEO" -c copy -f rtsp "rtsp://localhost:$PORT/$STREAM"
    kill $MTXPID 2>/dev/null
else
    echo "Note: Install mediamtx for a proper RTSP server."
    echo "Falling back to ffmpeg output (may not work with all clients)."
    echo ""
    echo "Install mediamtx: brew install mediamtx"
    echo ""
    # ffmpeg can output RTSP but needs a server to receive it
    # For testing, we can output frames as MJPEG over HTTP instead
    echo "Starting MJPEG HTTP stream on http://localhost:$PORT/$STREAM"
    ffmpeg -re -stream_loop -1 -i "$VIDEO" \
        -c:v mjpeg -q:v 3 -an \
        -f mpjpeg -boundary_tag ffmpeg \
        "http://localhost:$PORT/$STREAM" 2>/dev/null || \
    echo "ffmpeg RTSP output failed. Install mediamtx for proper RTSP serving."
fi
```

- [ ] **Step 3: Add test clip gitignore**

Add to `.gitignore`:

```
test_clips/*.mp4
test_clips/*.mkv
test_clips/*.avi
```

- [ ] **Step 4: Make serve script executable and commit**

```bash
chmod +x test_clips/serve_test_feed.sh
git add test_clips/README.md test_clips/serve_test_feed.sh .gitignore
git commit -m "test: add mock RTSP test feed infrastructure

Scripts and docs for capturing test clips and serving them as RTSP
streams for reproducible pipeline testing and benchmarking at any
time of day."
```

- [ ] **Step 5: Capture initial test clips (when daytime)**

This step must wait for daylight. When birds are active:

```bash
cd ~/bird-classifier/test_clips
# Get current RTSP URL
FEEDER_URL="rtsp://192.168.4.9:7447/$(jq -r '.streams.birds' ../rtsp_urls.json)"
GROUND_URL="rtsp://192.168.4.9:7447/$(jq -r '.streams.ground' ../rtsp_urls.json)"

# Record 5-minute clips
ffmpeg -i "$FEEDER_URL" -t 300 -c copy feeder_5min.mp4
ffmpeg -i "$GROUND_URL" -t 300 -c copy ground_5min.mp4
```

---

## Task 7: Extract `solar_utils.py`

**Files:**
- Create: `solar_utils.py`
- Create: `tests/test_solar_utils.py`
- Modify: `classify.py:95-165` (remove _solar_times, is_nighttime, is_twilight_window)
- Modify: `live_detector.py:111-153` (remove _solar_times, is_nighttime)
- Modify: `audio_analyzer.py:103-145` (remove _solar_times, is_nighttime)

- [ ] **Step 1: Write tests for solar_utils**

Create `tests/test_solar_utils.py`:

```python
"""Tests for solar utility functions."""
import pytest
from datetime import datetime, timezone


def test_solar_times_returns_sunrise_sunset():
    from solar_utils import solar_times
    sunrise, sunset = solar_times(41.35, -70.75)
    # Chilmark, MA: sunrise roughly 5-7 UTC, sunset roughly 22-01 UTC depending on season
    assert 3 < sunrise < 12, f"Unexpected sunrise hour: {sunrise}"
    assert 20 < sunset or sunset < 4, f"Unexpected sunset hour: {sunset}"


def test_solar_times_with_explicit_date():
    from solar_utils import solar_times
    # Summer solstice — long day
    summer = datetime(2026, 6, 21, tzinfo=timezone.utc)
    sr_summer, ss_summer = solar_times(41.35, -70.75, summer)
    # Winter solstice — short day
    winter = datetime(2026, 12, 21, tzinfo=timezone.utc)
    sr_winter, ss_winter = solar_times(41.35, -70.75, winter)
    # Summer days are longer
    summer_length = (ss_summer - sr_summer) % 24
    winter_length = (ss_winter - sr_winter) % 24
    assert summer_length > winter_length


def test_is_nighttime_at_midnight():
    from solar_utils import is_nighttime
    # Midnight should generally be nighttime at any latitude
    result = is_nighttime(41.35, -70.75, offset_minutes=0)
    # This depends on current time, so we just verify it returns a bool
    assert isinstance(result, bool)


def test_is_nighttime_returns_bool():
    from solar_utils import is_nighttime
    assert isinstance(is_nighttime(41.35, -70.75), bool)


def test_is_twilight_window():
    from solar_utils import is_twilight_window
    assert isinstance(is_twilight_window(41.35, -70.75, window_minutes=30), bool)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_solar_utils.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'solar_utils'`

- [ ] **Step 3: Implement solar_utils.py**

Create `solar_utils.py` by extracting from `classify.py` lines 95-165. Use the classify.py version as the base (it has the DST fix from Phase 8):

```python
"""Sunrise/sunset calculations for Chilmark, Martha's Vineyard.

Shared by classify.py, live_detector.py, and audio_analyzer.py.
Uses NOAA simplified solar algorithm.
"""
import math
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def solar_times(lat: float, lon: float, dt: datetime = None) -> tuple[float, float]:
    """Return (sunrise_hour_utc, sunset_hour_utc) for a given location and date.

    Uses NOAA simplified algorithm. Accurate to ~2 minutes.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    N = dt.timetuple().tm_yday
    # Solar noon approximation
    lng_hour = lon / 15.0
    t_rise = N + (6 - lng_hour) / 24.0
    t_set = N + (18 - lng_hour) / 24.0

    def _sun_calc(t):
        M = 0.9856 * t - 3.289
        M_rad = math.radians(M)
        L = M + 1.916 * math.sin(M_rad) + 0.020 * math.sin(2 * M_rad) + 282.634
        L = L % 360
        L_rad = math.radians(L)
        tan_ra = 0.91764 * math.tan(L_rad)
        RA = math.degrees(math.atan(tan_ra)) % 360
        L_quad = (L // 90) * 90
        RA_quad = (RA // 90) * 90
        RA = RA + (L_quad - RA_quad)
        RA = RA / 15.0
        sin_dec = 0.39782 * math.sin(L_rad)
        cos_dec = math.cos(math.asin(sin_dec))
        zenith = 90.833
        cos_h = (math.cos(math.radians(zenith)) - sin_dec * math.sin(math.radians(lat))) / (
            cos_dec * math.cos(math.radians(lat))
        )
        return RA, cos_h

    RA_r, cos_h_r = _sun_calc(t_rise)
    RA_s, cos_h_s = _sun_calc(t_set)

    if cos_h_r > 1 or cos_h_r < -1 or cos_h_s > 1 or cos_h_s < -1:
        # Midnight sun or polar night
        log.warning("Solar calculation out of range for lat=%.2f", lat)
        return (6.0, 18.0)  # fallback

    H_rise = (360 - math.degrees(math.acos(cos_h_r))) / 15.0
    H_set = math.degrees(math.acos(cos_h_s)) / 15.0

    T_rise = H_rise + RA_r - 0.06571 * t_rise - 6.622
    T_set = H_set + RA_s - 0.06571 * t_set - 6.622

    sunrise_utc = (T_rise - lng_hour) % 24
    sunset_utc = (T_set - lng_hour) % 24

    return sunrise_utc, sunset_utc


def _utc_offset_hours() -> int:
    """Get current UTC offset using timezone-aware datetime (handles DST correctly)."""
    local_dt = datetime.now(timezone.utc).astimezone()
    return int(local_dt.utcoffset().total_seconds() / 3600)


def is_nighttime(lat: float, lon: float, offset_minutes: int = 30) -> bool:
    """True if it's dark — past sunset+offset or before sunrise.

    Uses timezone-aware datetime to handle DST correctly.
    """
    sunrise_utc, sunset_utc = solar_times(lat, lon)
    utc_offset = _utc_offset_hours()
    now_h = (datetime.now(timezone.utc).hour + datetime.now(timezone.utc).minute / 60.0)
    sunrise_local = (sunrise_utc + utc_offset) % 24
    sunset_local = (sunset_utc + utc_offset) % 24
    now_local = (now_h + utc_offset) % 24
    offset_h = offset_minutes / 60.0
    return now_local < (sunrise_local - offset_h) or now_local > (sunset_local + offset_h)


def is_twilight_window(lat: float, lon: float, window_minutes: int = 30) -> bool:
    """True if within window_minutes of sunrise or sunset."""
    sunrise_utc, sunset_utc = solar_times(lat, lon)
    utc_offset = _utc_offset_hours()
    now_h = (datetime.now(timezone.utc).hour + datetime.now(timezone.utc).minute / 60.0)
    sunrise_local = (sunrise_utc + utc_offset) % 24
    sunset_local = (sunset_utc + utc_offset) % 24
    now_local = (now_h + utc_offset) % 24
    win_h = window_minutes / 60.0
    near_sunrise = abs(now_local - sunrise_local) < win_h
    near_sunset = abs(now_local - sunset_local) < win_h
    return near_sunrise or near_sunset
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_solar_utils.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Update classify.py — remove solar functions, import from solar_utils**

Replace the imports. In classify.py, at the top (near line 30), add:

```python
from solar_utils import solar_times, is_nighttime, is_twilight_window
```

Then delete the following functions from classify.py:
- `_solar_times()` (lines 95-124)
- `is_nighttime()` (lines 141-151)
- `is_twilight_window()` (lines 154-165)

Verify all call sites still work — they call `is_nighttime(LAT, LON)` and `is_twilight_window(LAT, LON, IR_WINDOW_MINUTES)` which matches the new API.

- [ ] **Step 6: Update live_detector.py — remove solar functions**

At the top of live_detector.py, add:

```python
from solar_utils import solar_times, is_nighttime
```

Delete from live_detector.py:
- `_solar_times()` (lines 111-140)
- `is_nighttime()` (lines 142-153)

- [ ] **Step 7: Update audio_analyzer.py — remove solar functions**

At the top of audio_analyzer.py, add:

```python
from solar_utils import solar_times, is_nighttime
```

Delete from audio_analyzer.py:
- `_solar_times()` (lines 103-132)
- `is_nighttime()` (lines 134-145)

- [ ] **Step 8: Verify all three modules still import cleanly**

```bash
cd ~/bird-classifier
venv-coral/bin/python -c "from classify import is_nighttime; print('classify OK')" 2>&1 | head -3
venv-coral/bin/python -c "from live_detector import is_nighttime; print('live_detector OK')" 2>&1 | head -3
venv-coral/bin/python -c "from audio_analyzer import is_nighttime; print('audio_analyzer OK')" 2>&1 | head -3
```

Expected: All three print "OK" (or fail on unrelated imports like missing hardware — that's fine as long as solar_utils is found)

- [ ] **Step 9: Run solar tests again**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_solar_utils.py -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add solar_utils.py tests/test_solar_utils.py classify.py live_detector.py audio_analyzer.py
git commit -m "refactor: extract solar calculations to shared solar_utils.py

Eliminates ~130 lines of duplicated _solar_times/is_nighttime code
across classify.py, live_detector.py, and audio_analyzer.py.
Includes DST-aware timezone handling (Phase 8 fix)."
```

---

## Task 8: Extract `bird_inference.py` — Part 1: Species Aliases and Utilities

**Files:**
- Create: `bird_inference.py` (initial — aliases, parse_label, crop_bird, providers)
- Create: `tests/test_bird_inference.py` (initial tests)
- Modify: `classifications_db.py:20-24` (remove SPECIES_ALIASES)
- Modify: `dashboard/api.py:48-55` (remove SPECIES_ALIASES and normalize_species)

- [ ] **Step 1: Write tests for aliases and utilities**

Create `tests/test_bird_inference.py`:

```python
"""Tests for shared bird inference utilities."""
import pytest
import numpy as np


class TestNormalizeSpecies:
    def test_alias_slate_colored_junco(self):
        from bird_inference import normalize_species
        assert normalize_species("Slate-colored Junco") == "Dark-eyed Junco"

    def test_alias_myrtle_warbler(self):
        from bird_inference import normalize_species
        assert normalize_species("Myrtle Warbler") == "Yellow-rumped Warbler"

    def test_alias_feral_pigeon(self):
        from bird_inference import normalize_species
        assert normalize_species("Feral Pigeon") == "Rock Pigeon"

    def test_alias_yellow_shafted_flicker(self):
        from bird_inference import normalize_species
        assert normalize_species("Yellow-shafted Flicker") == "Northern Flicker"

    def test_no_alias_passthrough(self):
        from bird_inference import normalize_species
        assert normalize_species("Northern Cardinal") == "Northern Cardinal"

    def test_empty_string(self):
        from bird_inference import normalize_species
        assert normalize_species("") == ""


class TestParseLabel:
    def test_standard_format(self):
        from bird_inference import parse_label
        sci, common = parse_label("Cardinalis cardinalis (Northern Cardinal)")
        assert sci == "Cardinalis cardinalis"
        assert common == "Northern Cardinal"

    def test_nested_parens(self):
        """This was a bug in classify.py — split('(')[0] breaks on nested parens."""
        from bird_inference import parse_label
        sci, common = parse_label("Accipiter cooperii (Cooper's Hawk (Sharp-shinned))")
        assert "cooperii" in sci
        # Should not crash or return garbage

    def test_no_parens(self):
        from bird_inference import parse_label
        sci, common = parse_label("Unknown Bird")
        assert sci == "Unknown Bird"
        assert common == "Unknown Bird"


class TestCropBird:
    def test_crop_basic(self):
        from bird_inference import crop_bird
        # Create a 100x100 test image
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        img[40:60, 40:60] = 255  # White square in center
        box = [40, 40, 60, 60]  # x1, y1, x2, y2
        crop = crop_bird(img, box, pad_ratio=0.0)
        assert crop.shape[0] == 20  # height
        assert crop.shape[1] == 20  # width

    def test_crop_with_padding(self):
        from bird_inference import crop_bird
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        box = [40, 40, 60, 60]
        crop = crop_bird(img, box, pad_ratio=0.15)
        # With 15% padding on a 20px box, pad = 3px each side
        assert crop.shape[0] >= 20
        assert crop.shape[1] >= 20

    def test_crop_clamps_to_image_bounds(self):
        from bird_inference import crop_bird
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        box = [0, 0, 10, 10]  # Corner — padding would go negative
        crop = crop_bird(img, box, pad_ratio=0.5)
        assert crop.shape[0] > 0
        assert crop.shape[1] > 0


class TestGetProviders:
    def test_returns_list(self):
        from bird_inference import get_providers
        providers = get_providers()
        assert isinstance(providers, list)
        assert len(providers) > 0
        # Should always have CPUExecutionProvider as fallback
        assert "CPUExecutionProvider" in providers
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_bird_inference.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bird_inference'`

- [ ] **Step 3: Implement bird_inference.py — Part 1 (aliases and utilities)**

Create `bird_inference.py`:

```python
"""Shared bird detection and classification inference.

Provides YOLO detection, AIY species classification, species aliases,
and utility functions. Used by classify.py and live_detector.py.
"""
import logging
import math
import numpy as np

log = logging.getLogger(__name__)

# ── Species Aliases ──────────────────────────────────────────────────
# Subspecies/regional forms that should map to canonical names.
# Single source of truth — imported by api.py, classifications_db.py, etc.

SPECIES_ALIASES = {
    "Slate-colored Junco": "Dark-eyed Junco",
    "Myrtle Warbler": "Yellow-rumped Warbler",
    "Feral Pigeon": "Rock Pigeon",
    "Yellow-shafted Flicker": "Northern Flicker",
}


def normalize_species(name: str) -> str:
    """Map subspecies/regional form names to canonical species names."""
    return SPECIES_ALIASES.get(name, name)


# ── Label Parsing ────────────────────────────────────────────────────

def parse_label(raw_label: str) -> tuple:
    """Parse 'Scientific name (Common Name)' format from AIY labels.

    Returns (scientific_name, common_name). Handles nested parentheses
    correctly using rindex (not split).
    """
    try:
        idx = raw_label.rindex("(")
        scientific = raw_label[:idx].strip()
        common = raw_label[idx + 1:].rstrip(")")
        return scientific, common
    except ValueError:
        return raw_label, raw_label


# ── Image Utilities ──────────────────────────────────────────────────

def crop_bird(image: np.ndarray, box: list, pad_ratio: float = 0.15) -> np.ndarray:
    """Crop bird region from image with padding.

    Args:
        image: HWC numpy array (uint8)
        box: [x1, y1, x2, y2] bounding box
        pad_ratio: fraction of box size to pad (default 15%)

    Returns:
        Cropped HWC numpy array
    """
    h, w = image.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    pad_x = int(bw * pad_ratio)
    pad_y = int(bh * pad_ratio)
    cx1 = max(0, x1 - pad_x)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(w, x2 + pad_x)
    cy2 = min(h, y2 + pad_y)
    return image[cy1:cy2, cx1:cx2]


# ── ONNX Runtime Providers ──────────────────────────────────────────

def get_providers() -> list:
    """Return ONNX Runtime execution providers, preferring CoreML on macOS."""
    import platform
    providers = []
    if platform.system() == "Darwin":
        try:
            import onnxruntime
            available = onnxruntime.get_available_providers()
            if "CoreMLExecutionProvider" in available:
                providers.append("CoreMLExecutionProvider")
        except Exception:
            pass
    providers.append("CPUExecutionProvider")
    return providers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_bird_inference.py -v`
Expected: All tests PASS

- [ ] **Step 5: Update classifications_db.py — import from bird_inference**

In `classifications_db.py`, replace lines 20-24 (the local SPECIES_ALIASES definition) with:

```python
from bird_inference import SPECIES_ALIASES, normalize_species
```

Find all usages of the old local `SPECIES_ALIASES` dict in the file and verify they still work with the import.

- [ ] **Step 6: Update dashboard/api.py — import from bird_inference**

In `api.py`, replace lines 48-55 (SPECIES_ALIASES and normalize_species) with:

```python
from bird_inference import SPECIES_ALIASES, normalize_species
```

Search for all uses of `normalize_species` and `SPECIES_ALIASES` in api.py to verify nothing breaks.

- [ ] **Step 7: Verify imports work**

```bash
cd ~/bird-classifier
venv-coral/bin/python -c "from classifications_db import SPECIES_ALIASES; print('DB OK:', len(SPECIES_ALIASES), 'aliases')"
venv-coral/bin/python -c "from bird_inference import normalize_species; print(normalize_species('Slate-colored Junco'))"
```

Expected: `DB OK: 4 aliases` and `Dark-eyed Junco`

- [ ] **Step 8: Run all tests**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add bird_inference.py tests/test_bird_inference.py classifications_db.py dashboard/api.py
git commit -m "refactor: extract SPECIES_ALIASES and utilities to bird_inference.py

Single source of truth for species aliases (was in 4 files).
Shared parse_label (fixes nested-parens bug from classify.py),
crop_bird, and get_providers. Tests included."
```

---

## Task 9: Extract `bird_inference.py` — Part 2: YOLO Detector

**Files:**
- Modify: `bird_inference.py` (add YOLODetector class)
- Modify: `tests/test_bird_inference.py` (add YOLO tests)

- [ ] **Step 1: Write YOLO detector tests**

Add to `tests/test_bird_inference.py`:

```python
class TestYOLODetector:
    def test_init(self, yolo_model_path):
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        assert detector is not None

    def test_detect_returns_list(self, yolo_model_path, test_bird_image_pil):
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        detections = detector.detect(test_bird_image_pil)
        assert isinstance(detections, list)

    def test_detection_has_required_fields(self, yolo_model_path, test_bird_image_pil):
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        detections = detector.detect(test_bird_image_pil)
        if len(detections) > 0:
            det = detections[0]
            assert "box" in det, "Detection must have 'box'"
            assert "confidence" in det, "Detection must have 'confidence'"
            assert len(det["box"]) == 4, "Box must be [x1, y1, x2, y2]"
            assert 0 < det["confidence"] <= 1.0

    def test_detect_empty_image(self, yolo_model_path):
        """An empty (black) image should return no detections."""
        from PIL import Image
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        black = Image.new("RGB", (640, 640), (0, 0, 0))
        detections = detector.detect(black)
        assert isinstance(detections, list)
        # Should be empty or very few false positives
        assert len(detections) <= 2

    def test_confidence_threshold(self, yolo_model_path, test_bird_image_pil):
        from bird_inference import YOLODetector
        # High threshold should return fewer detections
        det_low = YOLODetector(yolo_model_path, confidence=0.1).detect(test_bird_image_pil)
        det_high = YOLODetector(yolo_model_path, confidence=0.9).detect(test_bird_image_pil)
        assert len(det_low) >= len(det_high)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_bird_inference.py::TestYOLODetector -v`
Expected: FAIL — `cannot import name 'YOLODetector' from 'bird_inference'`

- [ ] **Step 3: Implement YOLODetector class**

Add to `bird_inference.py`, using classify.py lines 226-352 as the source (the more complete version). Key changes from original:
- Wrap in a class
- Accept PIL Image input (convert to numpy internally)
- Return clean list of dicts

```python
# ── YOLO Detection ───────────────────────────────────────────────────

class YOLODetector:
    """YOLOv8n bird detector using ONNX Runtime."""

    def __init__(self, model_path: str, confidence: float = 0.3,
                 nms_iou: float = 0.45, providers: list = None):
        import onnxruntime as ort
        if providers is None:
            providers = get_providers()
        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.confidence = confidence
        self.nms_iou = nms_iou
        self.input_size = 640

    def detect(self, image) -> list:
        """Detect birds in image. Accepts PIL Image or numpy HWC array.

        Returns list of dicts: [{"box": [x1,y1,x2,y2], "confidence": float}, ...]
        Coordinates are in original image space.
        """
        from PIL import Image as PILImage
        if isinstance(image, PILImage.Image):
            img_np = np.array(image)
        else:
            img_np = image

        preprocessed, scale, pad_x, pad_y = self._preprocess(img_np)
        outputs = self.session.run(None, {self.input_name: preprocessed})
        predictions = outputs[0]

        if predictions.ndim == 3:
            predictions = predictions[0]
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.T

        # Filter by confidence
        scores = predictions[:, 4:]
        max_scores = scores.max(axis=1)
        mask = max_scores >= self.confidence
        filtered = predictions[mask]
        filtered_scores = max_scores[mask]

        if len(filtered) == 0:
            return []

        # Convert xywh to xyxy
        boxes = filtered[:, :4].copy()
        boxes[:, 0] -= boxes[:, 2] / 2  # x1
        boxes[:, 1] -= boxes[:, 3] / 2  # y1
        boxes[:, 2] = boxes[:, 0] + boxes[:, 2]  # x2
        boxes[:, 3] = boxes[:, 1] + boxes[:, 3]  # y2

        # NMS
        keep = self._nms(boxes, filtered_scores, self.nms_iou)
        boxes = boxes[keep]
        filtered_scores = filtered_scores[keep]

        # Map back to original image coordinates
        h_orig, w_orig = img_np.shape[:2]
        detections = []
        for i in range(len(boxes)):
            x1 = int(max(0, (boxes[i][0] - pad_x) / scale))
            y1 = int(max(0, (boxes[i][1] - pad_y) / scale))
            x2 = int(min(w_orig, (boxes[i][2] - pad_x) / scale))
            y2 = int(min(h_orig, (boxes[i][3] - pad_y) / scale))
            detections.append({
                "box": [x1, y1, x2, y2],
                "confidence": float(filtered_scores[i]),
            })

        # Sort by confidence descending
        detections.sort(key=lambda d: d["confidence"], reverse=True)
        return detections

    def _preprocess(self, img_np: np.ndarray) -> tuple:
        """Letterbox resize to input_size, normalize, NCHW format."""
        h, w = img_np.shape[:2]
        scale = min(self.input_size / w, self.input_size / h)
        new_w, new_h = int(w * scale), int(h * scale)

        from PIL import Image as PILImage
        resized = PILImage.fromarray(img_np).resize((new_w, new_h), PILImage.BILINEAR)
        resized_np = np.array(resized)

        # Pad to input_size x input_size with gray (114)
        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        pad_x = (self.input_size - new_w) // 2
        pad_y = (self.input_size - new_h) // 2
        padded[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized_np

        # Normalize and transpose to NCHW
        blob = padded.astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)[np.newaxis]
        return blob, scale, pad_x, pad_y

    @staticmethod
    def _nms(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list:
        """Non-maximum suppression in pure numpy."""
        x1 = boxes[:, 0]
        y1 = boxes[:, 1]
        x2 = boxes[:, 2]
        y2 = boxes[:, 3]
        areas = (x2 - x1) * (y2 - y1)

        order = scores.argsort()[::-1]
        keep = []
        while len(order) > 0:
            i = order[0]
            keep.append(i)
            if len(order) == 1:
                break
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
            iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
            remaining = np.where(iou <= iou_threshold)[0]
            order = order[remaining + 1]
        return keep
```

- [ ] **Step 4: Run YOLO tests**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_bird_inference.py::TestYOLODetector -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add bird_inference.py tests/test_bird_inference.py
git commit -m "feat: add YOLODetector class to bird_inference.py

Shared YOLO detection with configurable confidence/NMS thresholds.
Letterbox preprocessing, NMS, coordinate remapping. Tests included."
```

---

## Task 10: Extract `bird_inference.py` — Part 3: Species Classifier

**Files:**
- Modify: `bird_inference.py` (add SpeciesClassifier class)
- Modify: `tests/test_bird_inference.py` (add classifier tests)

- [ ] **Step 1: Write species classifier tests**

Add to `tests/test_bird_inference.py`:

```python
class TestSpeciesClassifier:
    def test_init(self, species_model_path, labels_path):
        from bird_inference import SpeciesClassifier
        classifier = SpeciesClassifier(species_model_path, labels_path)
        assert classifier is not None
        assert len(classifier.labels) > 900  # 965 species

    def test_classify_returns_tuple(self, species_model_path, labels_path, regional_species,
                                     yolo_model_path, test_bird_image_pil):
        from bird_inference import SpeciesClassifier, YOLODetector, crop_bird
        detector = YOLODetector(yolo_model_path)
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
        detections = detector.detect(test_bird_image_pil)
        if not detections:
            pytest.skip("No birds detected in test image")
        img_np = np.array(test_bird_image_pil)
        crop = crop_bird(img_np, detections[0]["box"])
        filtered, raw = classifier.classify(crop)
        assert isinstance(filtered, list)
        assert isinstance(raw, list)
        assert len(raw) > 0

    def test_prediction_has_fields(self, species_model_path, labels_path, regional_species,
                                    yolo_model_path, test_bird_image_pil):
        from bird_inference import SpeciesClassifier, YOLODetector, crop_bird
        detector = YOLODetector(yolo_model_path)
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
        detections = detector.detect(test_bird_image_pil)
        if not detections:
            pytest.skip("No birds detected in test image")
        img_np = np.array(test_bird_image_pil)
        crop = crop_bird(img_np, detections[0]["box"])
        filtered, raw = classifier.classify(crop)
        if filtered:
            pred = filtered[0]
            assert "common_name" in pred
            assert "scientific_name" in pred
            assert "raw_score" in pred

    def test_classify_uses_uint8_input(self, species_model_path, labels_path):
        """AIY Birds V1 expects uint8 input (0-255), NOT normalized floats."""
        from bird_inference import SpeciesClassifier
        classifier = SpeciesClassifier(species_model_path, labels_path)
        # Create a dummy 224x224 uint8 image
        dummy = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        filtered, raw = classifier.classify(dummy)
        # Should not crash and should return predictions
        assert isinstance(raw, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_bird_inference.py::TestSpeciesClassifier -v`
Expected: FAIL — `cannot import name 'SpeciesClassifier'`

- [ ] **Step 3: Implement SpeciesClassifier class**

Add to `bird_inference.py`, based on classify.py lines 427-490 (more complete version):

```python
# ── Species Classification ───────────────────────────────────────────

class SpeciesClassifier:
    """AIY Birds V1 species classifier using ONNX Runtime.

    IMPORTANT: AIY Birds V1 expects uint8 input (0-255), NOT normalized floats.
    """

    def __init__(self, model_path: str, labels_path: str,
                 regional_species: set = None, providers: list = None):
        import onnxruntime as ort
        if providers is None:
            providers = get_providers()
        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.labels = self._load_labels(labels_path)
        self.regional_species = regional_species or set()
        self.input_size = 224

    @staticmethod
    def _load_labels(path: str) -> list:
        with open(path) as f:
            return [line.strip() for line in f if line.strip()]

    def classify(self, crop) -> tuple:
        """Classify a bird crop image.

        Args:
            crop: PIL Image, or numpy HWC array (uint8)

        Returns:
            (filtered_predictions, raw_predictions) — both are lists of dicts
            filtered = regional species only (top 3)
            raw = all species (top 3)
            Each dict: {"common_name", "scientific_name", "raw_score"}
        """
        from PIL import Image as PILImage
        if isinstance(crop, PILImage.Image):
            crop_img = crop.resize((self.input_size, self.input_size), PILImage.BILINEAR)
            arr = np.array(crop_img, dtype=np.uint8)
        elif isinstance(crop, np.ndarray):
            crop_img = PILImage.fromarray(crop).resize(
                (self.input_size, self.input_size), PILImage.BILINEAR
            )
            arr = np.array(crop_img, dtype=np.uint8)
        else:
            raise TypeError(f"Expected PIL Image or numpy array, got {type(crop)}")

        # CRITICAL: uint8 input, NOT normalized float
        input_data = arr[np.newaxis].astype(np.uint8)
        outputs = self.session.run(None, {self.input_name: input_data})
        scores = outputs[0][0]

        # Get top predictions
        top_indices = scores.argsort()[::-1]

        raw_preds = []
        filtered_preds = []
        for idx in top_indices:
            if len(raw_preds) >= 3 and len(filtered_preds) >= 3:
                break
            raw_label = self.labels[idx]
            scientific, common = parse_label(raw_label)
            common = normalize_species(common)
            score = int(scores[idx]) if scores.dtype == np.uint8 else float(scores[idx])
            pred = {
                "common_name": common,
                "scientific_name": scientific,
                "raw_score": score,
            }
            if len(raw_preds) < 3:
                raw_preds.append(pred)
            if common in self.regional_species and len(filtered_preds) < 3:
                filtered_preds.append(pred)

        return filtered_preds, raw_preds
```

- [ ] **Step 4: Run species classifier tests**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_bird_inference.py::TestSpeciesClassifier -v`
Expected: All tests PASS

- [ ] **Step 5: Run ALL tests**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add bird_inference.py tests/test_bird_inference.py
git commit -m "feat: add SpeciesClassifier class to bird_inference.py

AIY Birds V1 classifier with uint8 input (critical gotcha documented).
Returns filtered (regional) and raw (global) top-3 predictions.
Applies species alias normalization. Tests included."
```

---

## Task 11: Wire Up classify.py to Use bird_inference.py

**Files:**
- Modify: `classify.py` (remove duplicated functions, import from bird_inference)

- [ ] **Step 1: Add imports to classify.py**

Near the existing imports at the top of classify.py, add:

```python
from bird_inference import (
    YOLODetector, SpeciesClassifier, SPECIES_ALIASES, normalize_species,
    parse_label, crop_bird, get_providers,
)
```

- [ ] **Step 2: Remove duplicated functions from classify.py**

Delete these functions (they now come from bird_inference.py):
- `_get_providers()` (lines 205-212)
- `preprocess_yolo()` (lines 226-253)
- `nms_numpy()` (lines 256-292)
- `detect_birds()` (lines 295-352)
- `parse_label()` (lines 402-408)
- `classify_species()` (lines 427-490)
- `crop_bird()` (lines 411-424)
- Any local `SPECIES_ALIASES` if present

- [ ] **Step 3: Update classify.py to use YOLODetector and SpeciesClassifier**

Replace the model loading and inference calls. The current code loads ONNX sessions directly — change to use the class constructors:

```python
# In the model loading section, replace session creation with:
_yolo_detector = YOLODetector(
    str(YOLO_MODEL_PATH),
    confidence=DETECTION_CONFIDENCE,
    nms_iou=NMS_IOU_THRESHOLD,
)
_species_classifier = SpeciesClassifier(
    str(SPECIES_MODEL_PATH),
    str(LABELS_PATH),
    regional_species=_regional_species,
)
```

Then update all `detect_birds(...)` calls to `_yolo_detector.detect(...)` and all `classify_species(...)` calls to `_species_classifier.classify(...)`.

**Important:** The return types may differ slightly. Verify each call site gets the fields it expects. The classify.py pipeline expects detections as `[{"box": [...], "confidence": float}]` which matches.

- [ ] **Step 4: Verify classify.py loads without errors**

```bash
cd ~/bird-classifier && venv-coral/bin/python -c "
import classify
print('classify.py imports OK')
print('YOLO model:', hasattr(classify, '_yolo_detector') or 'loaded on demand')
"
```

- [ ] **Step 5: Test on a real image**

```bash
cd ~/bird-classifier && venv-coral/bin/python -c "
from pathlib import Path
import classify
# Find a classified image to reprocess
classified = Path('../bird-snapshots/classified')
for sp_dir in sorted(classified.iterdir()):
    jpgs = list(sp_dir.glob('*.jpg'))[:1]
    if jpgs:
        print(f'Testing with: {jpgs[0].name}')
        result = classify.process_file(jpgs[0])
        print(f'Result: {result.get(\"action\", \"unknown\")} -> {result.get(\"common_name\", \"N/A\")}')
        break
"
```

- [ ] **Step 6: Commit**

```bash
git add classify.py
git commit -m "refactor: classify.py uses shared bird_inference.py

Removes ~200 lines of duplicated YOLO/AIY code. Uses YOLODetector
and SpeciesClassifier classes. parse_label bug fixed (nested parens).
SPECIES_ALIASES now from single source."
```

---

## Task 12: Wire Up live_detector.py to Use bird_inference.py

**Files:**
- Modify: `live_detector.py` (remove duplicated functions, import from bird_inference)

- [ ] **Step 1: Add imports to live_detector.py**

```python
from bird_inference import (
    YOLODetector, SpeciesClassifier, SPECIES_ALIASES, normalize_species,
    parse_label, crop_bird, get_providers,
)
```

- [ ] **Step 2: Remove duplicated functions from live_detector.py**

Delete these functions:
- `SPECIES_ALIASES` dict (lines 261-265)
- `_get_providers()` (lines 283-289)
- `preprocess_yolo()` (lines 338-352)
- `nms_numpy()` (lines 355-375)
- `detect_birds()` (lines 378-415)
- `parse_label()` (lines 418-425)
- `classify_species()` (lines 428-463)
- `CROP_PAD_RATIO = 0.15` constant (line 85) — now a parameter on crop_bird

- [ ] **Step 3: Refactor load_models() to use shared classes**

The current `load_models()` (lines 295-335) creates ONNX sessions directly. Replace with:

```python
def load_models():
    """Load YOLO detector and species classifier."""
    global _yolo_detector, _species_classifier
    _yolo_detector = YOLODetector(
        str(YOLO_MODEL),
        confidence=DETECTION_CONFIDENCE,
        nms_iou=NMS_IOU_THRESHOLD,
    )
    _species_classifier = SpeciesClassifier(
        str(SPECIES_MODEL),
        str(LABELS_FILE),
        regional_species=_load_regional_species(),
    )
    log.info("Models loaded via bird_inference.py")
```

- [ ] **Step 4: Update camera_loop() to use shared classes**

Replace direct function calls:
- `detect_birds(yolo_sess, ...)` → `_yolo_detector.detect(pil_image)`
- `classify_species(species_sess, ...)` → `_species_classifier.classify(crop)`
- `crop = img.crop(...)` → `crop = crop_bird(np.array(img), det["box"])`

- [ ] **Step 5: Add motion gate to live_detector.py**

Now that inference is shared, adding the motion gate is simple:

```python
from motion_gate import MotionGate

_motion_gates = {}  # per-camera motion gates

# In camera_loop(), before YOLO detection:
if cam_name not in _motion_gates:
    _motion_gates[cam_name] = MotionGate(threshold_pct=1.5, resize_width=320)
if not _motion_gates[cam_name].has_motion(frame_np):
    continue  # skip static frame
```

- [ ] **Step 6: Verify live_detector.py loads without errors**

```bash
cd ~/bird-classifier && venv-coral/bin/python -c "
import live_detector
print('live_detector.py imports OK')
"
```

- [ ] **Step 7: Commit**

```bash
git add live_detector.py
git commit -m "refactor: live_detector.py uses shared bird_inference.py + motion gate

Removes ~180 lines of duplicated YOLO/AIY code. Uses same YOLODetector
and SpeciesClassifier as classify.py. Adds motion gate (was only in
batch classifier) — expected 40-60% fewer inference calls on static frames."
```

---

## Task 13: Integration Test — Same Image Through Both Paths

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
"""Integration test: verify classify.py and live_detector.py produce
consistent results on the same input image."""
import pytest
import numpy as np


def test_both_paths_detect_same_birds(yolo_model_path, species_model_path,
                                       labels_path, regional_species,
                                       test_bird_image_pil):
    """Both paths should detect birds in the same locations."""
    from bird_inference import YOLODetector

    detector = YOLODetector(yolo_model_path, confidence=0.3)
    detections = detector.detect(test_bird_image_pil)

    # Since both paths now use the same YOLODetector, this is really
    # testing that the class works consistently
    detections2 = detector.detect(test_bird_image_pil)

    assert len(detections) == len(detections2)
    for d1, d2 in zip(detections, detections2):
        assert d1["box"] == d2["box"]
        assert abs(d1["confidence"] - d2["confidence"]) < 0.001


def test_full_pipeline_classify(yolo_model_path, species_model_path,
                                 labels_path, regional_species,
                                 test_bird_image_pil):
    """Full detection + classification pipeline produces valid output."""
    from bird_inference import YOLODetector, SpeciesClassifier, crop_bird

    detector = YOLODetector(yolo_model_path)
    classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)

    detections = detector.detect(test_bird_image_pil)
    if not detections:
        pytest.skip("No birds in test image")

    img_np = np.array(test_bird_image_pil)
    for det in detections:
        crop = crop_bird(img_np, det["box"])
        filtered, raw = classifier.classify(crop)
        assert len(raw) > 0, "Should have at least one raw prediction"
        assert raw[0]["common_name"], "Prediction should have a common name"
        assert raw[0]["raw_score"] >= 0, "Score should be non-negative"
```

- [ ] **Step 2: Run integration tests**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/test_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite**

Run: `cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add integration tests for shared inference pipeline

Verifies YOLODetector + SpeciesClassifier produce consistent results.
Full detection→classification pipeline test with real models."
```

---

## Task 14: Final Health Check and Git Tag

- [ ] **Step 1: Run complete test suite**

```bash
cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/ -v --tb=short
```

Expected: All tests PASS

- [ ] **Step 2: Verify running services still work**

```bash
# Check if classifier is running and healthy
curl -s http://localhost:8099/api/health | python3 -m json.tool

# Check recent classifications (should still be working)
curl -s "http://localhost:8099/api/stats?date=$(date +%Y-%m-%d)" | python3 -m json.tool
```

- [ ] **Step 3: Create summary commit if any loose changes**

```bash
cd ~/bird-classifier && git status
# If clean, skip. If not, commit remaining changes.
```

- [ ] **Step 4: Tag the milestone**

```bash
git tag -a v0.5-foundations-phase1 -m "Phase 0+1 complete: test infra, quick wins, shared inference

- pytest infrastructure with model/image fixtures
- Mock RTSP test feed scripts
- Composite indexes for common queries
- Stale config cleanup
- Health check LaunchAgent
- solar_utils.py (shared across 3 files)
- bird_inference.py (YOLODetector, SpeciesClassifier, SPECIES_ALIASES)
- Motion gate added to live_detector.py
- Integration tests"
```

- [ ] **Step 5: Verify final state**

```bash
# Line count comparison
echo "=== Before (approximate) ==="
echo "classify.py duplicated code: ~243 lines"
echo "live_detector.py duplicated code: ~167 lines"
echo "SPECIES_ALIASES copies: 4"
echo "solar_times copies: 3"
echo ""
echo "=== After ==="
wc -l bird_inference.py solar_utils.py
echo "SPECIES_ALIASES copies: 1 (bird_inference.py)"
echo "solar_times copies: 1 (solar_utils.py)"
echo ""
echo "=== Tests ==="
cd ~/bird-classifier && venv-coral/bin/python -m pytest tests/ --tb=no -q
```

---

## Summary

| Task | What | Risk | Files Changed |
|------|------|------|--------------|
| 1 | Test infrastructure | None | pytest.ini, tests/ |
| 2 | Composite indexes + stale config | Low | classifications_db.py, config/ |
| 3 | Health check + log rotation | Low | LaunchAgent, newsyslog |
| 4 | Remove Slate-colored Junco | Low | chilmark_feeder_species.txt |
| 5 | Fix HANDOFF.md path | None | docs (not git) |
| 6 | Mock RTSP feeds | None | test_clips/ |
| 7 | Extract solar_utils.py | Medium | 4 files |
| 8 | bird_inference.py Part 1 (aliases) | Medium | 3 files |
| 9 | bird_inference.py Part 2 (YOLO) | Medium | 1 file |
| 10 | bird_inference.py Part 3 (classifier) | Medium | 1 file |
| 11 | Wire up classify.py | High | classify.py |
| 12 | Wire up live_detector.py + motion gate | High | live_detector.py |
| 13 | Integration tests | None | tests/ |
| 14 | Health check + tag | None | git |

---

## Plan Errata (Post-Review Corrections)

The following corrections were identified by the plan reviewer and **override** the corresponding sections above. Implementers must apply these corrections.

### E1. solar_utils.py: Use Existing Algorithm Verbatim (overrides Task 7 Step 3)

The solar_utils.py code in Task 7 Step 3 contains a **different** NOAA algorithm than what the codebase uses. The plan must use the exact existing algorithm from classify.py:95-138 (the gamma/equation-of-time approach), including the `_utc_offset_for_date()` DST fix.

Copy `_solar_times()` (lines 95-124), `_utc_offset_for_date()` (lines 127-138), `is_nighttime()` (lines 141-151), and `is_twilight_window()` (lines 154-165) from classify.py **verbatim**, then rename `_solar_times` to `solar_times` (public API).

### E2. is_nighttime() Signature: Zero-Arg Compatibility (overrides Task 7 Steps 3, 5-7)

The current code calls `is_nighttime()` with **zero arguments** in all three files (classify.py:1032, live_detector.py:623, audio_analyzer.py:551). The functions read module-level constants (`LATITUDE`, `LONGITUDE`, `NIGHT_OFFSET_MINUTES`).

The shared `solar_utils.py` must accept optional arguments with defaults matching Chilmark, MA:

```python
# Chilmark, MA defaults
DEFAULT_LAT = 41.35
DEFAULT_LON = -70.75
DEFAULT_NIGHT_OFFSET = 30  # minutes

def is_nighttime(lat=DEFAULT_LAT, lon=DEFAULT_LON, offset_minutes=DEFAULT_NIGHT_OFFSET):
    ...

def is_twilight_window(lat=DEFAULT_LAT, lon=DEFAULT_LON, window_minutes=30):
    ...
```

This way existing call sites (`is_nighttime()` with no args) continue to work unchanged. Call sites that need custom coordinates can pass them.

### E3. crop_bird(): Support Both PIL and numpy (overrides Task 8 Step 3)

The existing `crop_bird()` in classify.py takes a **PIL Image** and uses `image.crop()` and `image.width`/`image.height`. The plan's version takes numpy arrays only.

The shared version must accept both:

```python
def crop_bird(image, box, pad_ratio=0.15):
    """Crop bird region from image with padding. Accepts PIL Image or numpy HWC array."""
    from PIL import Image as PILImage
    if isinstance(image, PILImage.Image):
        w, h = image.width, image.height
    else:
        h, w = image.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    pad_x = int(bw * pad_ratio)
    pad_y = int(bh * pad_ratio)
    cx1 = max(0, x1 - pad_x)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(w, x2 + pad_x)
    cy2 = min(h, y2 + pad_y)
    if isinstance(image, PILImage.Image):
        return image.crop((cx1, cy1, cx2, cy2))
    return image[cy1:cy2, cx1:cx2]
```

This preserves the return type of the input — PIL in, PIL out; numpy in, numpy out. No downstream changes needed.

### E4. SpeciesClassifier: Coral TPU Support (overrides Task 10 Step 3)

The plan's `SpeciesClassifier` only supports ONNX Runtime. The existing `classify_species()` in classify.py (lines 427-490) has a Coral TPU code path (`_species_backend == "coral"`) that is the primary backend on the iMac. Dropping this would be a performance regression.

Add Coral support to `SpeciesClassifier.__init__()`:

```python
class SpeciesClassifier:
    def __init__(self, model_path, labels_path, regional_species=None,
                 providers=None, tpu_model_path=None):
        self.labels = self._load_labels(labels_path)
        self.regional_species = regional_species or set()
        self.input_size = 224
        self.backend = "onnx"

        # Try Coral TPU first
        if tpu_model_path:
            try:
                from pycoral.utils.edgetpu import make_interpreter
                from pycoral.adapters import common as coral_common
                interp = make_interpreter(str(tpu_model_path))
                interp.allocate_tensors()
                self.session = interp
                self._coral_common = coral_common
                self.backend = "coral"
                log.info("Species classifier loaded on Coral TPU")
                return
            except Exception as e:
                log.warning("Coral TPU failed (%s), falling back to ONNX", e)

        # ONNX fallback
        import onnxruntime as ort
        if providers is None:
            providers = get_providers()
        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.backend = "onnx"
```

And in `classify()`:

```python
if self.backend == "coral":
    self._coral_common.set_input(self.session, arr[0])
    self.session.invoke()
    scores = np.array(self._coral_common.output_tensor(self.session, 0), dtype=np.float32)
    if scores.ndim == 2:
        scores = scores[0]
else:
    scores = self.session.run(None, {self.input_name: input_data})[0][0]
```

Callers pass `tpu_model_path=str(SPECIES_TPU_PATH)` if available.

### E5. classify_species() Return Format (overrides Task 10 Step 3)

The existing `classify_species()` returns dicts with `index` and `label` fields that downstream code uses (annotation, logging). The plan's `SpeciesClassifier.classify()` omits these. Include them:

```python
pred = {
    "index": int(idx),
    "label": self.labels[idx],
    "common_name": common,
    "scientific_name": scientific,
    "raw_score": score,
}
```

### E6. classify.py Function Signature Refactoring (overrides Task 11)

The plan says to create module-level `_yolo_detector` and `_species_classifier` but doesn't address the function signatures. Every pipeline function takes raw sessions:

```python
process_file(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, image_path, ...)
process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, ...)
watch_mode(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, ...)
reprocess(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, ...)
```

**Approach:** Use module-level globals for the detector and classifier. Simplify all function signatures:

```python
# Module-level (set in main())
_detector = None       # YOLODetector
_classifier = None     # SpeciesClassifier

def process_file(image_path, regional_species=None, range_filter=None):
    """Full pipeline using module-level _detector and _classifier."""
    ...
    detections = _detector.detect(img)
    ...
    filtered, raw = _classifier.classify(crop)
    ...

def process_all(regional_species=None, range_filter=None):
    ...

def watch_mode(regional_species=None, range_filter=None):
    ...

def reprocess(regional_species=None, range_filter=None):
    ...

def main():
    global _detector, _classifier
    _detector = YOLODetector(str(YOLO_MODEL_PATH), confidence=DETECTION_CONFIDENCE)
    _classifier = SpeciesClassifier(
        str(SPECIES_MODEL_PATH), str(LABELS_PATH),
        regional_species=regional_species,
        tpu_model_path=str(SPECIES_TPU_PATH) if SPECIES_TPU_PATH.exists() else None,
    )
    ...
```

This is a larger refactor than the plan originally scoped but it's the right approach — passing 5 model parameters through every function was tech debt.

### E7. live_detector.py Variable Name Fixes (overrides Task 12 Step 3)

The plan's `load_models()` snippet uses wrong variable names. Correct names from the actual code:

- `YOLO_MODEL` → should be referenced from wherever the actual model path constant is (check the file)
- `SPECIES_MODEL` → same
- `LABELS_FILE` → same
- `_load_regional_species()` → regional species are loaded inline in the current `load_models()` at lines 329-333

The implementer must read live_detector.py to get the actual constant names before writing this code.

### E8. YOLO Single-Class Model (overrides Task 9 Step 3)

The plan's `YOLODetector` uses `scores.max(axis=1)` across all classes. The custom model (`yolov8n_bird.onnx`) is single-class, so this works. But add a comment and optional `class_id` parameter for safety:

```python
class YOLODetector:
    def __init__(self, model_path, confidence=0.3, nms_iou=0.45,
                 providers=None, class_id=0):
        ...
        self.class_id = class_id
```

In `detect()`: if `self.class_id is not None`, use `scores[:, self.class_id]` instead of `scores.max(axis=1)`. This handles both single-class and multi-class models correctly.

### E9. SPECIES_ALIASES Count Correction

The spec says 4 copies of SPECIES_ALIASES. Actual count is 3 (classifications_db.py, live_detector.py, api.py). classify.py does NOT have its own copy — it uses normalization via classifications_db. There is also a copy in `migrate_jsonl_to_sqlite.py` (one-time script, low priority).

### E10. Health Check Plist Location (overrides Task 3 Step 5)

The LaunchAgent plist at `~/Library/LaunchAgents/` is outside the git repo. Don't try to `git add` it. Instead, keep a template in `config/launchagents/` within the repo and document the symlink/copy step.