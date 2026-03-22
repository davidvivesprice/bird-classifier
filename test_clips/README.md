# Test Clips for Mock RTSP Feed

This directory holds short video clips captured from live cameras, used to
serve a reproducible mock RTSP stream for pipeline testing and benchmarking
at any time of day without depending on live camera access.

**Note:** `.mp4`, `.mkv`, and `.avi` files are gitignored. Store clips
locally; do not commit them.

---

## Camera streams (from rtsp_urls.json)

| Name         | Description        | RTSP URL                                          |
|--------------|--------------------|---------------------------------------------------|
| `birds`      | Feeder cam         | `rtsp://192.168.4.9:7447/KWdr6I7LmAwwbC0Y`       |
| `ground`     | Ground cam         | `rtsp://192.168.4.9:7447/AXBMY59UoTi1Adi2`       |
| `newbackyard`| New backyard cam   | `rtsp://192.168.4.9:7447/8ieiUhQBl1rNq4z2`       |
| `magnolia`   | Magnolia cam       | `rtsp://192.168.4.9:7447/T95Uof6qSmukq9x1`       |

---

## Capturing test clips with ffmpeg

General form:

```bash
ffmpeg -i <RTSP_URL> -t <DURATION_SECONDS> -c copy <OUTPUT_FILE>
```

### 5-minute feeder clip (birds cam)

```bash
ffmpeg -i rtsp://192.168.4.9:7447/KWdr6I7LmAwwbC0Y \
    -t 300 -c copy test_clips/feeder_5min.mp4
```

### 5-minute ground cam clip

```bash
ffmpeg -i rtsp://192.168.4.9:7447/AXBMY59UoTi1Adi2 \
    -t 300 -c copy test_clips/ground_5min.mp4
```

### 1-minute multi-bird clip (feeder cam, pick a busy moment)

```bash
ffmpeg -i rtsp://192.168.4.9:7447/KWdr6I7LmAwwbC0Y \
    -t 60 -c copy test_clips/multi_bird_1min.mp4
```

### 1-minute difficult-species clip (low light, partial occlusion, etc.)

```bash
ffmpeg -i rtsp://192.168.4.9:7447/AXBMY59UoTi1Adi2 \
    -t 60 -c copy test_clips/difficult_species_1min.mp4
```

Tips:
- Use `-ss <TIMESTAMP>` before `-i` for hardware-accelerated seeking to a
  specific time within a longer recording.
- Add `-vf scale=1280:720` if you need to down-scale before saving.
- If the live stream requires authentication, embed credentials:
  `rtsp://user:pass@192.168.4.9:7447/...`

---

## Serving a clip as a mock RTSP stream

Use the included `serve_test_feed.sh` script (see below), or run manually:

### Option A – mediamtx (recommended)

1. Install: `brew install mediamtx`
2. Start the server + pipe the clip in a loop:

```bash
# Terminal 1 – start mediamtx
mediamtx

# Terminal 2 – loop the clip into mediamtx
ffmpeg -re -stream_loop -1 -i test_clips/feeder_5min.mp4 \
    -c copy -f rtsp rtsp://localhost:8554/test-feeder
```

3. Point any service at `rtsp://localhost:8554/test-feeder`.

### Option B – ffmpeg only (no mediamtx)

ffmpeg can act as a minimal RTSP server:

```bash
ffmpeg -re -stream_loop -1 -i test_clips/feeder_5min.mp4 \
    -c copy -f rtsp -rtsp_transport tcp rtsp://localhost:8554/test-feeder
```

Note: the built-in ffmpeg RTSP server is limited and may not handle multiple
simultaneous readers. mediamtx is preferred for robust testing.

### Scripted helper

```bash
# Default port 8554, stream name test-feeder
./test_clips/serve_test_feed.sh test_clips/feeder_5min.mp4

# Custom port and stream name
./test_clips/serve_test_feed.sh test_clips/ground_5min.mp4 8555 ground-test
```

Then update your service config (or environment) to use:
`rtsp://localhost:<PORT>/<STREAM_NAME>`
instead of the live camera URL.
