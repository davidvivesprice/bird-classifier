# iMac live-classify subsystem — as it actually is, 2026-04-25

**Audience:** future-me, future-Claude, anyone touching the live overlay or snapshot path.
**Source of truth:** the code. Everything in this doc has a file:line citation. Where the code disagrees with `~/docs/bird-observatory/*` or earlier specs, **the code wins** — those docs may be stale.

## 0 · The complete data flow, end-to-end

```
UniFi G3 Dome (camera) ──RTSP──┐
                                ▼
                         go2rtc :1984 (native binary, LaunchAgent com.vives.go2rtc)
                                │
                ┌───────────────┴────────────────┐
                ▼                                ▼
         feeder-sub                        feeder-main
       (640×360, native)                 (1080p, native fps)
                │                                │
   ┌────────────┴──────────────┐                 │
   │ FrameCapture              │                 │     ┌─ MSE/WebSocket → /api/ws → browser <video>
   │ (one ffmpeg per camera)   │                 │ ────┤  (real-time path, not used by /live)
   │ pipe-drain thread reads   │                 │     └─ HlsRecorder
   │ raw BGR → bounded queue   │                 │        (one ffmpeg per camera)
   │ stamps wall_time_ms       │                 │        -c copy → ~/bird-snapshots/hls/{cam}/seg_*.ts
   │   = time.time()*1000      │                 │        ↓
   │   AT pipe-read            │                 │        _manifest_loop watches dir, writes
   │ NO -vf fps=N (avoid lag)  │                 │        segments.json with completed_ms
   └────────────┬──────────────┘                 │            = int(st.st_mtime * 1000)
                │                                │            (settles after MANIFEST_SETTLE_S=0.5s
                ▼                                │             of no-size-change)
         CameraProcessThread                     │
   ┌─────────────────────────────────────┐       │
   │ MotionGate (MOG2 + AOI polygon)     │       │
   │ → BirdDetector (YOLO, full-frame    │       │
   │   on motion, 10s forced full)       │       │
   │ → BirdTracker (Norfair + Frigate    │       │
   │   distance, threshold 2.0)          │       │
   │ → SmartClassifier (per-camera tree, │       │
   │   yard→AIY for feeder, AIY-only     │       │
   │   for ground; Coral lock)           │       │
   │ → vote-lock (≥3 votes, ≥0.35 conf,  │       │
   │   ≥60% agreement on top species)    │       │
   └──────────┬──────────────────────────┘       │
              │                                  │
   ┌──────────┴───────┬──────────────────┐       │
   ▼                  ▼                  ▼       │
EventStore       SSEEventServer     SnapshotWriter (separate background thread)
(pipeline.db,    :8104 or :8105     submit() called when track LOCKS
WAL, batched     emits per-frame    queue (maxsize=32) → _loop dequeues → _write_one
flush 0.5s)      JSON to /events/   path branches:
                 sse?camera=...     (1) ring authoritative (PIPELINE_HIRES_RING=authoritative)
                 wall_time_ms       (2) cheap restore (default since 2026-04-23)
                 from frame         (3) old broken /api/frame.mp4 (PIPELINE_HIRES_RECROP=1)
                                    + _authoritative_species: AIY rerun on hi-res crop
                                    → JPG to ~/bird-snapshots/classified/{species}/
                                    → annotated/ with corner brackets
                                    → classifications.db row via insert_classification
                                                                                          │
Browser /live.html                                                                        │
   ┌─────────────────────────────────────────────────────────────────────────────────────┴───┐
   │ hls.js plays /api/hls-live/feeder/live.m3u8 with liveSyncDuration=8 → ~10-12s delay     │
   │ EventSource('/api/pipeline/events/sse?camera=feeder') → 120s sliding eventBuffer        │
   │ refreshManifest() polls /api/hls-live/feeder/segments.json every 2s → segmentWallclock  │
   │ requestVideoFrameCallback per displayed frame:                                          │
   │   displayedFrameWallMs() = segmentWallclock[currentFragment().filename]                 │
   │                            - fragDuration*1000 + (currentTime-fragStart)*1000           │
   │   indexTracksFromBuffer() groups events by track_id                                     │
   │   adaptiveAnchorAt(track, T) blends two Gaussian kernels (σ=450ms wide, 220ms narrow)   │
   │     by velocity (low: wide kernel; high: narrow kernel) — symmetric (uses past+future)  │
   │   drawLabel(cx, top-36) for each placed track                                           │
   └─────────────────────────────────────────────────────────────────────────────────────────┘
```

## 1 · Process inventory on iMac

LaunchAgents (~/Library/LaunchAgents/, all KeepAlive unless noted):

| Plist | Cmd | Notes |
|---|---|---|
| `com.vives.bird-pipeline` | `venv-coral/bin/python3 -u bird_pipeline_v3.py` | venv-coral = Python 3.9 (pycoral). Env: `PIPELINE_HEALTH_PORT=8100`, `PIPELINE_SSE_PORT=8105`, `PIPELINE_DB_PATH=...pipeline_v3_dev.db` |
| `com.vives.bird-dashboard` | `venv/bin/uvicorn dashboard.api:app --host 0.0.0.0 --port 8099` | venv = newer Python. Env: `PIPELINE_BACKEND_URL=http://127.0.0.1:8105` |
| `com.vives.go2rtc` | go2rtc native binary | Reads `go2rtc.yaml`. RTSP relay :1984 |
| `com.vives.bird-tunnel` | `cloudflared tunnel run bird-observatory` | Exposes :8099 at birds.vivessato.com |
| `com.vives.bird-rtsp-sync` | `venv/bin/python3 refresh_rtsp.py` | StartCalendarInterval 3:10 AM daily. Refreshes UniFi RTSP tokens |
| `com.vives.bird-audio` | `/usr/bin/python3 audio_analyzer.py` (system Python via PYTHONPATH trick — Apple-signed only) | BirdNET |
| `com.vives.bird-enhanced-audio` | `venv-coral/bin/python3 enhanced_audio_stream.py` | Filtered audio MP3 |
| `com.vives.bird-integrity-audit` | (LaunchAgent) | The 1a integrity audit, runs on schedule |
| Deactivated: `com.vives.bird-classifier`, `bird-livedetect`, `bird-capture` | `classify.py --watch`, `live_detector.py`, `capture_snapshots.py` | Replaced by v3 pipeline. classify.py disabled because Coral USB is single-session and v3 holds it. |

Restart pattern: `launchctl kickstart -k gui/$(id -u)/com.vives.bird-dashboard`

Logs land at `~/bird-snapshots/logs/` per service.

## 2 · The two clocks that matter (and the one that doesn't)

There are exactly two timestamps the live-classify subsystem uses, **both Python `time.time()` on the iMac, both stamped at frame ARRIVAL on the iMac side**:

### Clock A: SSE `wall_time_ms` (the detection events)

**Stamped at**: `pipeline/frame_capture.py:147` — `wall_time_ms=time.time() * 1000` inside `_pipe_drain`, the moment one full raw BGR frame's worth of bytes has been read from the sub-stream ffmpeg's pipe.

**Critical design choice** (frame_capture.py:86-99): NO `-vf fps=N` filter on this ffmpeg. Comment quoted verbatim:
> "The fps filter paces output to N frames/sec — ffmpeg holds a decoded frame until its scheduled emission slot arrives (up to 1/N seconds). That wait shows up as wall-clock latency between camera capture and pipe-read, which in turn makes SSE event wall_time_ms lag behind the main-stream HLS frames for the same physical moment → overlay appears behind the bird."

So sub-stream ffmpeg outputs at native ~30fps; Python reads as fast as YOLO/classification allows; bounded `out_queue` (maxsize=2) drops oldest if full. Every wall_time_ms is "right after this frame landed on the iMac".

### Clock B: HLS `completed_ms` (the segment manifest)

**Stamped at**: `pipeline/hls_recorder.py:170` — `entry["completed_ms"] = int(st.st_mtime * 1000)`. The mtime of the .ts file as recorded by the macOS filesystem when ffmpeg finished writing it. The manifest loop polls every 250ms and only stamps when the file size has been stable for `MANIFEST_SETTLE_S = 0.5s` (so it doesn't stamp while ffmpeg is still appending).

The HLS recorder runs `ffmpeg -c copy -f hls -hls_time 2 -hls_list_size 15 -hls_flags delete_segments+program_date_time`. **`-c copy` = no decode.** Just remux H.264 packets from RTSP into 2-second .ts segments on disk. Near-zero CPU. PROGRAM-DATE-TIME tags ARE in the .ts files but **the browser does not use them** — it uses the sidecar `segments.json` instead because PDT is anchored to ffmpeg start time, not frame arrival.

### Browser-side derivation: `displayedFrameWallMs()`

`dashboard/live.html:280-291`:
```javascript
function displayedFrameWallMs() {
  var frag = currentFragment();
  if (!frag) return null;
  var completedMs = segmentWallclock[frag.filename];
  if (typeof completedMs !== 'number') return null;
  var firstFrameMs = completedMs - (frag.fragDuration * 1000);
  var offsetMs = (video.currentTime - frag.fragStart) * 1000;
  return firstFrameMs + offsetMs;
}
```

= "the iMac wall-clock time at which the currently-displayed video frame landed on disk." Compared to the matching SSE `wall_time_ms`, both came from the same OS clock at corresponding pipeline stages. **No NTP truth required. No band-aid compensation.** That's the whole 5-day buffer arc the design unlocked.

### What used to be a problem and isn't anymore

`hls.playingDate` (PDT-derived) was tried first (April 15 spec). PDT is anchored to ffmpeg start, drifts vs. frame arrival, can't be drift-proof. Pivot to sidecar manifest happened April 16-17. The 1-second `OVERLAY_LEAD_COMPENSATION_MS` band-aid mentioned in the April 16 verification spec is **gone from current live.html** — verified by grep.

## 3 · Adaptive Lock — the smoothing math

`dashboard/live.html:414-514`. Two symmetric Gaussian kernels weighted-blended by velocity:

```
σ_wide = 450 ms  (Precision Lock — when bird is still)
σ_narrow = 220 ms (Weighted Average — when bird is moving)
v = |narrow(T) - narrow(T-150ms)| / 0.150 s   [pixels/sec]
α_raw = clamp((v - 20) / (80 - 20), 0, 1)
α = EMA(α_raw, gain=0.1)
label = (1-α) * wide + α * narrow
```

Symmetric kernels (uses events from BOTH sides of T) work because /live plays HLS at ~10s delay → at any displayed-frame T, the eventBuffer has events both before and after T. This is the "look-ahead future" the buffer makes available.

α-EMA persists per `track_id` in module-scope `_adaptiveAlphaEMA` map; pruned when the track fades out.

Currently shipping on `/live`. The main dashboard `/` has a degraded past-only variant in `setupV3LabelRenderer` (uses WebRTC ~400ms latency, no future buffer). Plan is to consolidate `/live` into `/`.

## 4 · The pipeline per frame (CameraProcessThread._process_frame)

`pipeline/process_thread.py:91-219`. In order:

1. **Motion gate** (`pipeline/motion_gate.py`) — MOG2 background subtraction, AOI polygon mask first (prevents sky/branches/grass from triggering YOLO). Returns list of bbox motion regions.
2. **Forced-full timer** — every 10s, run YOLO regardless of motion (catches stationary new arrivals).
3. **Detect** — if any motion regions OR forced full: `BirdDetector.detect()` runs YOLO on the FULL frame (region detection is dropped — ONNX resizes to 640×640 either way, so multiple regions = multiple full-cost calls). Stationary-suppression: if all motion regions are explained by stationary tracks (IoU > 0.8), skip YOLO entirely. Skip-frame near-zero timings are filtered out of `yolo_ms_avg` per the honesty contract.
4. **Track** — `BirdTracker.update()` wraps Norfair with `_frigate_distance`. Distance threshold = 2.0 (was 1.0; raised April 17 because birds flying fast lost track_id mid-flight). Returns `TrackerOutput(active, new, expired)`.
5. **Classify** — `_classify_tracks()`. For each track that `needs_classification`: crop bbox from frame.bgr, convert BGR→PIL RGB, call `classifier.classify(crop_pil, wall_time_ms, camera)`. Vote semantics:
   - Append `(species, confidence)` to `track.vote_history`
   - Set `track.species` to current top-voted species (so label shows immediately, even pre-lock)
   - Lock when: ≥3 votes AND top species ≥ 0.35 confidence (was 0.6; lowered April 18 for new yard softmax) AND top species holds ≥ 60% of votes
   - After `MAX_CLASSIFICATION_ATTEMPTS = 5` without lock: take plurality winner (or leave unlabeled)
   - Coral lock timeout returns `should_retry=True` → track stays needs_classification for next frame
6. **Snapshot** (`process_thread.py:130-140`) — for each `track.is_locked AND not track.snapshot_saved`: call `snapshot_writer.submit(camera, frame.bgr, frame.wall_time_ms, track)`. The frame passed is the SUB-STREAM 640×360 BGR. Mark `snapshot_saved=True`.
7. **EventStore write** — per active track: write to `pipeline_events` (camera, frame_time, track_id, species, confidence, model_source, bbox, is_new). Batched 50-row flush every 0.5s.
8. **SSE emit** — if any active tracks: emit JSON `{camera, wall_time_ms, tracks: [{track_id, bbox, species, ...}]}` to all subscribed clients on `:8105/events/sse?camera=feeder`.
9. **Track expiry** — for tracks norfair dropped: write summary row to `pipeline_tracks` (start/end times, peak conf, num_frames, motion_pct).
10. **Debug frame** — every 500ms (throttled) + only when active tracks: draw YOLO boxes on a 640×360 copy, JPEG encode at quality 70, store as `health.latest_debug_jpeg[camera]` for `/debug/latest.jpg` polling.
11. **Health update** — capture stats every frame (cheap), numpy stats (mean/p99) every 2s.

## 5 · The classifier decision tree (`pipeline/classifier.py`)

SmartClassifier per-camera config:

| Camera | use_yard | confident_threshold | uncertain_low |
|---|---|---|---|
| feeder | True | 0.25 (was 0.6) | 0.10 (was 0.3) |
| ground | False | 0.25 | 0.10 |

Thresholds were re-tuned April 18 after `yard_classifier.py` switched to honest full-distribution softmax with T=100. New peaked-yard prediction tops out at ~0.45-0.54 (was always reporting 1.0 pre-fix), so the old 0.6 gate would reject every yard answer.

**Feeder path (yard-first decision tree):**
```
acquire Coral lock (5s timeout; returns should_retry on timeout)
yard.classify(crop)
   ↓
   ├─ confident (≥0.25): accept yard, model_source=YARD
   ├─ low (<0.10): release lock, run AIY alone
   │     ├─ AIY confident (≥0.25): accept, model_source=AIY
   │     └─ else: unlabeled, increment unlabeled_call
   └─ uncertain band (0.10-0.25): release lock, cross-check with AIY
         ├─ AIY same species: accept, model_source=BOTH_AGREE
         └─ AIY disagrees: unlabeled (Path 4 audio cross-check was removed in v3)
```

**Ground path (AIY-only):** same structure, no Coral lock, no yard.

**`authoritative_classify()`** — separate method called by SnapshotWriter at write time. AIY-only on the (ideally hi-res) crop. Takes the Coral lock. The result OVERRIDES the track's live yard label for the classifications.db row. **This is why the DB labels are AIY's 965-species call, not yard's 12-species best guess.** Yard drives live UX speed; AIY drives the durable record.

`yard_classifier.py` runs on Coral USB Edge TPU via pycoral. AIY runs on Coral too via the same `_coral_lock`. CPU fallback is ONNX+CoreML (slower).

## 6 · The snapshot path — current state

`pipeline/snapshot_writer.py`. Background thread, queue-fed (maxsize=32, drop-oldest).

`_write_one(payload)` per locked track:

**Step 1: hi-res frame acquisition.** Three branches by env:
- **(1) ring authoritative** — `PIPELINE_HIRES_RING=authoritative`. Calls `_pick_from_ring()`. Replaces `p["frame"]` with the ring's nearest-timestamp 1080p frame, scales bbox to 1920×1080. Writes a `.ring.json` sidecar with the picker's metadata.
- **(2) cheap restore (default)** — keeps the sub-stream 640×360 frame and bbox as-is. Increments `hires_skipped`. This is the path live on iMac right now.
- **(3) hi-res recrop (env-gated, broken)** — `PIPELINE_HIRES_RECROP=1`. Fetches `/api/frame.mp4?src=feeder-main` from go2rtc, decodes one frame via subprocess ffmpeg pipe. Has 2-5s keyframe wait → bird often gone → empty-feeder crop. Retained for A/B comparison only.

**Step 2: authoritative classify.** Crops the (now-final) frame at the (now-final) bbox and calls `classifier.authoritative_classify()`. AIY rerun. If it returns a species, override the live (yard) label.

**Step 3: write.** JPG via `cv2.imencode` at quality 85 → `~/bird-snapshots/classified/{species}/feeder_YYYY-MM-DD_HH-MM-SS_{track_id}.jpg`. Annotated copy with corner brackets → `~/bird-snapshots/annotated/`. Row in `classifications.db` via `cdb.insert_classification(entry)`. If DB write fails, the JPG is unlinked to avoid orphans.

**Counter stats** (surfaced via `health.shared.snapshot_writer`): submitted, written, dropped_full, errors, hires_ok, hires_fail, hires_skipped, aiy_relabel, aiy_none, ring_pick_ok, ring_pick_empty, shadow_sidecar_written.

## 7 · The hi-res ring buffer — what it actually is

`pipeline/hires_ring.py`. Two classes:

### `HiResRingBuffer`
Thread-safe rolling buffer of `RingFrame(frame, wall_ms)` indexed by wall-clock ms. Eviction: anything older than `max_seconds=2.0` behind the newest is dropped. Hard cap: `max_seconds * expected_fps * 2 = 20`. `find_nearest(wall_ms, tolerance_ms)` returns the closest frame within tolerance (default `2 * (1000/fps)` = 400ms at 5fps). `find_candidates(wall_ms, k=3)` returns up to K closest unordered. `score_frame(frame, bbox, conf)` is the quality picker — Laplacian variance × center-position boost × size boost × confidence multiplier; rejects bboxes <80×80px ("can't see an eye").

### `HiResCapture`
Dedicated ffmpeg subprocess feeding the ring. Differences from `FrameCapture`:
- Uses `-vf scale=1920:1080,fps=5` → throttles decode rate to 5fps (sub-stream's CameraProcessThread rate). Saves CPU vs. native 30fps decode of 1080p, but **adds up to 200ms of pacing latency**, per the same `fps=N` trade-off `FrameCapture` deliberately avoids.
- Pushes directly to `HiResRingBuffer` instead of a Queue. Writer thread reads pipe, stamps `wall_ms = time.time() * 1000` at pipe-read.

**Scope**: env-gated off by default on iMac (`PIPELINE_HIRES_RING="0"` → not even instantiated). Currently authoritative on Pi 5 (where the redundant 1080p decode is acceptable and the cheap restore would lose the SAME-SOURCE alignment we get for free on iMac via the existing HLS recorder).

**Cost on iMac (i5-7400, 4 cores)**: a SECOND ffmpeg decoding 1080p at 5fps + ~62 MB rolling RAM + a third concurrent RTSP consumer of `feeder-main` (HLS recorder is the second; go2rtc client is the first). Estimated ~30-50% extra CPU on a busy system.

## 8 · The actual relationship between Clock A and Clock B

For a frame captured at camera-time `T_cam`:

- **Sub-stream path**: camera→NVR→go2rtc→sub-stream ffmpeg→pipe→Python `_pipe_drain`. SSE `wall_time_ms = T_cam + L_sub` where `L_sub` is transport+decode latency (~50-300ms typical on LAN).
- **Main-stream HLS path**: camera→NVR→go2rtc→main-stream ffmpeg `-c copy`→.ts file. The .ts file completes when ffmpeg writes its last frame and closes. `completed_ms = T_cam_last_in_segment + L_main + buffering_delay`. The first frame in the segment was captured `2s` earlier, so `first_frame_ms = completed_ms - 2000 = T_cam_first_in_segment + L_main + buffering_delay`.

If `L_sub ≈ L_main` (same camera, same network, same go2rtc), and `decode_sub ≈ buffering_delay`, the two clocks line up for "what physical moment am I looking at." The Adaptive Lock visibly working confirms this is true within a small fixed offset.

This is the foundation under everything in §9 below.

## 9 · The hi-res snapshot question — what the system already gives us for free

The `segments.json` sidecar IS exactly the buffer needed to look up "the iMac wall-clock time at which a given .ts segment's frames were ingested." It's currently used only for the overlay. **It can serve snapshot frames too.**

Procedure to extract a 1080p frame at SSE wall_time_ms `T`:

1. Read `segments.json` (already kept fresh at 4Hz by `_manifest_loop`, browser already polls it at 0.5Hz).
2. For each segment `S` with known `completed_ms`: compute `S.first_frame_ms = S.completed_ms - 2000`.
3. Find the segment where `S.first_frame_ms ≤ T ≤ S.completed_ms`.
4. Compute `offset_within_segment = T - S.first_frame_ms`.
5. Open `~/bird-snapshots/hls/feeder/<S.file>` via PyAV.
6. Decode K frames around `offset_within_segment` (cheap: H.264 within a single segment, no seek across keyframes).
7. Run `score_frame(frame, bbox_scaled, conf)` on each (existing quality picker from hires_ring.py — reusable).
8. Return the highest-scored frame as a numpy BGR array.

Cost: zero continuous CPU. ~50-200ms one-shot decode per snapshot — fine for the SnapshotWriter background thread. **No second ffmpeg decode running continuously. No 62MB rolling RAM. Same clock as the overlay. Same source as the displayed video.**

vs. the ring buffer — the ring's only architectural advantage is sub-millisecond lookup latency. We don't need that. Snapshots aren't on a hot path; they're written by a background thread when a track LOCKS, not per frame.

## 10 · Where every constant lives (so future me can find them)

| What | File | Line |
|---|---|---|
| Sub-stream RTSP URL | bird_pipeline_v3.py | 31 |
| Main-stream RTSP URL | bird_pipeline_v3.py | 37 |
| AOI polygon for feeder | bird_pipeline_v3.py | 59 |
| Forced-full YOLO interval | process_thread.py | 23 |
| YOLO confidence threshold | bird_pipeline_v3.py | 262 |
| Vote-lock thresholds | process_thread.py | 306-309 |
| MAX_CLASSIFICATION_ATTEMPTS | classifier.py | 16 |
| CORAL_ACQUIRE_TIMEOUT | classifier.py | 15 |
| Per-camera classifier config defaults | camera_config.py | 37-38 |
| Norfair distance threshold | tracker.py | 86 |
| Frame-capture watchdog stall | frame_capture.py | 23 |
| HLS segment duration / list size | hls_recorder.py | 59-61 |
| Manifest poll / settle | hls_recorder.py | 24, 29 |
| Ring buffer max_seconds / cap | hires_ring.py | 33-35 |
| Quality scorer min bbox side | hires_ring.py | 268 |
| SnapshotWriter queue maxsize | snapshot_writer.py | 124 |
| Adaptive Lock σ values | live.html | 427-432 |
| Adaptive Lock velocity thresholds | live.html | 429-430 |
| HLS playback delay target | live.html | 141 |
| Stale / fade-in / fade-out | live.html | 147-149 |
| Event match tolerance | live.html | 155 |
| Manifest refresh interval | live.html | 157 |

## 11 · What works well + what's open

**Works well:**
- Two-clock alignment via sidecar manifest (no NTP dependency)
- Adaptive Lock smoothing (labels glued to bird, no jitter, no lag)
- Cheap restore + authoritative AIY override (DB labels are 69.3% top-1 quality)
- Two-stream architecture (browser plays full 1080p MSE direct from go2rtc; pipeline does cheap detection on sub-stream)
- The HLS recorder's existing buffer (currently powering the overlay; ready to also power snapshots)

**Open:**
- Snapshots are 640×360 (cheap restore), not 1080p. Ring buffer gives 1080p but at the cost of a redundant decode. **§9 proposes the cleanest fix.**
- Main dashboard `/` runs a past-only variant of Adaptive Lock with WebRTC (~400ms latency). `/live` is the real one. Plan: consolidate.
- `OVERLAY_LEAD_COMPENSATION_MS` historical. Removed in current live.html — confirmed.
- The 1a integrity audit runs as a LaunchAgent but I haven't checked its recent runs in this session. Worth doing.
- Authoritative AIY happens in the SnapshotWriter background thread with the Coral lock. The pipeline's live yard inference also takes the Coral lock. They serialize cleanly (the lock is the mechanism), but if Coral contention spikes the snapshot can wait up to `CORAL_ACQUIRE_TIMEOUT=5s` for the lock.
- `hires_ring.py` exists, works, has a quality scorer. Even if we move to HLS-extract for the live system, the scorer + RingFrame dataclass + `score_frame()` function are reusable.

## 12 · How to verify any of the above

```bash
# What's actually running on iMac
launchctl list | grep -E 'vives.bird|vives.go2rtc'

# Pipeline health (snapshot_writer counters, capture stats, classifier stats)
curl -s http://localhost:8100/api/pipeline/health | jq

# What the manifest looks like right now
curl -s http://localhost:8099/api/hls-live/feeder/segments.json | jq '.segments[-3:]'

# Latest classifications (DB labels = AIY authoritative override)
sqlite3 ~/bird-snapshots/logs/classifications.db \
  "SELECT source_timestamp, common_name, confidence, json_extract(extra_json,'$.model_source')
   FROM classifications WHERE action='classified' ORDER BY id DESC LIMIT 10"

# Latest snapshot resolution (will be 640x360 with cheap-restore)
ls -t ~/bird-snapshots/classified/*/*.jpg | head -1 | xargs -I {} sh -c \
  './venv-coral/bin/python3 -c "from PIL import Image; im=Image.open(\"{}\"); print(im.size)"'

# Verify NO compensation in current live.html
grep -c OVERLAY_LEAD_COMPENSATION /Users/vives/bird-classifier/dashboard/live.html

# Restart dashboard to pick up code edits
launchctl kickstart -k gui/$(id -u)/com.vives.bird-dashboard
```

---

**This doc is a snapshot of the system at 2026-04-25. If the code changes, update this doc.** It exists so the next session doesn't have to re-derive from scratch.
