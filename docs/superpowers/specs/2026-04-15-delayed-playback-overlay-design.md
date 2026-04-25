# Delayed Playback with Pre-Computed Overlays — Design Spec

**Date:** 2026-04-15
**Status:** ⚠️ **PARTIALLY SUPERSEDED 2026-04-17** — the architecture
(HLS @ ~10s delay + SSE event buffer + per-frame canvas overlay) shipped
and is still in production. The CLOCK SOURCE was pivoted: `hls.playingDate`
(PDT-based) was replaced by a Python-stamped sidecar manifest because PDT
is anchored to ffmpeg start time, not frame arrival, and could not be
drift-proof. Read the "What's still true / what changed" banner below
before using this spec as a reference.
**Author:** Claude (Opus 4.6) + David
**Builds on:** v3 pipeline (working), Frigate research, live detection v3 design

**Revision history:**
- Draft 1: Assumed go2rtc native HLS + hls.js + rVFC mediaTime. **Three unverified assumptions, two were wrong.**
- Draft 2 (this): Pipeline-generated HLS (ffmpeg `-c copy`) + hls.js + `hls.playingDate`. All critical paths verified from official docs.
- **Draft 3 (2026-04-17, separate doc):** Pivoted clock source from
  `hls.playingDate` to Python-stamped sidecar manifest. See
  `2026-04-17-smooth-label-overlay-design.md` and the verification doc
  `2026-04-16-overlay-sync-ground-truth-verification.md` (which is
  itself superseded — read its top banner).

---

## ⚠️ What's still true / what changed

**Still in production from this spec:**
- ffmpeg HLS recorder with `-c copy -f hls -hls_time 2 -hls_list_size 15
  -hls_flags delete_segments+program_date_time` — **shipped, current**
  (see `pipeline/hls_recorder.py`)
- hls.js with `liveSyncDuration: 8` for ~10s delay — **shipped, current**
  (see `dashboard/live.html` line 198, with tightened drift recovery added
  2026-04-23)
- Browser-side `eventBuffer` of SSE events with `wall_time_ms`, ~120s
  sliding window — **shipped, current**
- Per-frame `requestVideoFrameCallback` overlay rendering — **shipped, current**

**Replaced by the 2026-04-17 pivot:**
- `hls.playingDate` as the clock source → REPLACED by sidecar manifest
  (`/api/hls-live/feeder/segments.json`, written by
  `pipeline/hls_recorder.py::_manifest_loop`, read by
  `dashboard/live.html::displayedFrameWallMs()`)
- Section 5c "Timestamp Sync via hls.playingDate" → no longer how it works
- The PDT verification cascade in §3 → never executed; obviated by pivot

**Why pivoted:**
PDT is anchored to ffmpeg start time, not the wall-clock at which each
frame physically arrived on the iMac. Verifying PDT was drift-proof
against an external NTP reference required Gates 0-4 of
`2026-04-16-overlay-sync-ground-truth-verification.md` — that work
hit "iMac is +180ms ahead of NTP" at Gate 1a and stopped. The sidecar
approach sidesteps the entire NTP question because both the segment
`completed_ms` and the SSE `wall_time_ms` are stamped by Python
`time.time()` on the iMac at corresponding stages — same clock source =
internally consistent regardless of absolute truth.

**For the current architecture, read:**
- `2026-04-17-smooth-label-overlay-design.md` (the pivot)
- `~/docs/bird-observatory/31-label-motion-adaptive-lock.md` (the
  Adaptive Lock smoothing that ships on `/live` today; the Catmull-Rom
  approach in 04-17 was itself replaced by Gaussian-kernel-based
  smoothing on 04-18)
- `~/bird-classifier/docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` §2-3
  (the two clocks reconciliation + Adaptive Lock math, code-level citations)

Original Draft 2 spec preserved below for the record.

---

## 0. The Problem

The v3 pipeline detects and classifies birds correctly. The video player works. But syncing labels to live video is unreliable — the pipeline needs 1-2 seconds to detect, classify, and vote-lock a species, so labels always lag behind the video. Previous attempts at wall-clock sync, manual offset sliders, and fMP4 timestamp parsing produced inconsistent results across devices.

## 1. The Insight (from David)

Stop trying to sync labels to live video. Instead, **delay the video to match the labels.**

The user sees video that's several seconds behind real-time. By the time any frame reaches the screen, the pipeline has already fully processed it — every bird detected, every species locked, every bbox recorded. Labels appear the instant a bird enters frame.

It doesn't matter that the video isn't truly live. It just needs to be accurate, fun, exciting, and full of information. A real feeder with real birds being identified seemingly in real-time. Whether it's attached to real-time in reality doesn't matter.

## 2. Architecture

```
Camera (1080p 30fps RTSP)
    │
    ├──► go2rtc ──► RTSP restream at localhost:8554/feeder-main
    │                    │
    │                    ├──► ffmpeg -c copy -f hls ──► HLS segments on disk
    │                    │    (with #EXT-X-PROGRAM-DATE-TIME tags)
    │                    │
    │                    └──► pipeline ffmpeg (substream, 5fps, 640x360)
    │                              │
    │                              ▼
    │                    MotionGate → YOLO → Track → Classify → Vote Lock
    │                              │
    │                              ▼
    │                    SSE events with wall_time_ms
    │                    (arrive in real-time, buffered client-side)
    │
    ▼
Browser (/live page)
    │
    ├── hls.js loads HLS segments (liveSyncDuration = 8s)
    │   Video plays 8 seconds behind the live edge.
    │   hls.js manages delay, buffering, drift automatically.
    │
    ├── hls.playingDate → wall-clock time of displayed frame
    │   (from #EXT-X-PROGRAM-DATE-TIME embedded by ffmpeg)
    │
    ├── Lookup SSE events near playingDate → pre-computed detections
    │   (pipeline processed this moment 8 seconds ago — fully locked)
    │
    └── Draw bboxes + species labels on <canvas> overlay
        Frame-accurate. No guessing. No drift.
```

## 3. Verification Status

Every claim below is marked:
- **[VERIFIED-DOCS]** — confirmed from official documentation
- **[VERIFIED-CODE]** — confirmed from reading our codebase
- **[VERIFIED-TEST]** — confirmed by running on the actual system
- **[UNVERIFIED]** — needs testing before implementation starts

### Video Transport

| Claim | Status | Source |
|---|---|---|
| ffmpeg `-c copy -f hls` remuxes RTSP to HLS with negligible CPU (<1%) | **[VERIFIED-DOCS]** | FFmpeg formats doc: `-c copy` does no decode/encode, pure I/O demux/remux |
| ffmpeg `-hls_flags program_date_time` embeds `#EXT-X-PROGRAM-DATE-TIME` tags in ISO 8601 format | **[VERIFIED-DOCS]** | FFmpeg formats doc + FFmpeg patchwork ticket #7986 |
| PROGRAM-DATE-TIME source is system wall clock at mux time (av_gettime(), microsecond resolution) | **[VERIFIED-DOCS]** | FFmpeg patch from 2019 switched from time() to av_gettime() |
| `hls_list_size N` limits playlist to N segments | **[VERIFIED-DOCS]** | FFmpeg formats doc |
| `-hls_flags delete_segments` auto-deletes old .ts files | **[VERIFIED-DOCS]** | FFmpeg formats doc |
| `hls_time 2` produces ~2-second segments | **[VERIFIED-DOCS]** | FFmpeg formats doc (segment at next keyframe after duration) |
| go2rtc native HLS is unsuitable (500ms hardcoded segments, 2-segment window, no PROGRAM-DATE-TIME, no config) | **[VERIFIED-DOCS]** | go2rtc source code analysis + GitHub issues #1602, #1699 |

### hls.js Player

| Claim | Status | Source |
|---|---|---|
| `liveSyncDuration` maintains a fixed delay behind the live edge automatically | **[VERIFIED-DOCS]** | hls.js API.md |
| `liveMaxLatencyDuration` seeks forward if drift exceeds threshold | **[VERIFIED-DOCS]** | hls.js API.md |
| `hls.playingDate` exposes wall-clock time from PROGRAM-DATE-TIME tags | **[VERIFIED-DOCS]** | hls.js API.md — "Wall-clock time from PROGRAM-DATE-TIME" |
| `initialLiveManifestSize` defaults to 1 (can start with small playlists) | **[VERIFIED-DOCS]** | hls.js API.md |
| hls.js works with ffmpeg-generated HLS served over HTTP | **[VERIFIED-DOCS]** | hls.js docs — source-agnostic, needs correct MIME types |
| hls.js works with 2-second segments at a fixed 8s delay | **[UNVERIFIED]** | Docs say `liveSyncDuration` below ~3 segment durations risks stalls. 8s / 2s = 4 segments — should be fine, but needs testing |
| hls.js handles segment fetching, buffer eviction, and drift correction | **[VERIFIED-DOCS]** | hls.js API.md — automatic buffer management |

### Timestamp Correlation

| Claim | Status | Source |
|---|---|---|
| `hls.playingDate` returns a Date object with the PROGRAM-DATE-TIME of the current playback position | **[VERIFIED-DOCS]** | hls.js API.md |
| Pipeline SSE events carry `wall_time_ms` (server epoch ms from system clock) | **[VERIFIED-CODE]** | `pipeline/process_thread.py` line 148: `wall_time_ms=int(frame.wall_time_ms)` |
| ffmpeg's PROGRAM-DATE-TIME and pipeline's wall_time_ms use the same clock (iMac system clock) | **[VERIFIED-DOCS]** | Both use the iMac's system wall clock — ffmpeg via av_gettime(), pipeline via Python time.time() |
| Therefore `hls.playingDate.getTime()` ≈ matching SSE event's `wall_time_ms` | **[UNVERIFIED]** | Logically follows from above, but clock skew between ffmpeg process and Python process needs testing. Expected <100ms on same machine. |

### Overlay Rendering

| Claim | Status | Source |
|---|---|---|
| `requestVideoFrameCallback` fires per displayed video frame with metadata | **[VERIFIED-DOCS]** | MDN + WICG spec |
| `mediaTime` for live HLS streams MAY be zero or meaningless | **[VERIFIED-DOCS]** | WICG spec warning: "MAY have a zero value for live-streams" |
| We do NOT rely on `mediaTime` — we use `hls.playingDate` instead | Design decision | Avoids the mediaTime unreliability entirely |
| `requestVideoFrameCallback` is supported in Brave (Chromium-based) | **[VERIFIED-DOCS]** | Chrome 83+, Brave is Chromium |
| rVFC is NOT supported in Firefox | **[VERIFIED-DOCS]** | MDN — Firefox 132+ (late 2024) — actually now supported |
| Canvas overlay can draw bboxes and labels per frame at 30fps | **[VERIFIED-TEST]** | Already working in current v3 overlay — rAF callbacks take 0.5ms avg |

### Pipeline

| Claim | Status | Source |
|---|---|---|
| Pipeline emits SSE events with tracks including species=null before classification | **[VERIFIED-CODE]** | `pipeline/process_thread.py` lines 132-152 |
| Vote lock fires after 3 votes, 60% agreement, 0.6 confidence | **[VERIFIED-CODE]** | `pipeline/process_thread.py` + v3 design spec |
| Pipeline processes at 5fps — full detection chain takes ~1-2s per bird | **[VERIFIED-TEST]** | Measured via health endpoint and debug harness |
| 8-second delay gives >4x margin for confident vote-locked labels | Follows from above | 1-2s processing vs 8s delay |

### Previous HLS Recorder

| Claim | Status | Source |
|---|---|---|
| `pipeline/hls_recorder.py` already exists and wraps ffmpeg HLS muxing | **[VERIFIED-CODE]** | File exists, used by bird_pipeline_v3.py line 184 (currently commented out) |
| Previous recorder consumed 15% CPU + 2.3GB RAM | **[VERIFIED-TEST]** | Measured via `ps aux` this session |
| Previous recorder used `hls_list_size 0` (unlimited) — the cause of high resource usage | **[VERIFIED-CODE]** | Confirmed in ffmpeg command line args |
| With `hls_list_size 15` + `delete_segments`, CPU should be <1% and RAM minimal | **[VERIFIED-DOCS]** | ffmpeg docs: `-c copy` is I/O-bound only. Bounded list = bounded memory. |
| Previous recorder did NOT use `-hls_flags program_date_time` | **[UNVERIFIED]** | Need to check `pipeline/hls_recorder.py` source |

## 4. Chosen Architecture: Path A (ffmpeg HLS + hls.js)

### Why not Path B (Modified MSE via go2rtc WebSocket)?

Path B was also verified as feasible (delayed `appendBuffer()` works per MDN docs), but Path A wins on:
- **Timestamp correlation**: Path A gives us wall-clock time directly via `hls.playingDate`. Path B requires parsing fMP4 tfdt boxes (64-bit, 90kHz, counter starts at 0 on connect) and correlating with wall_time_ms.
- **Delay management**: hls.js handles delay, drift, buffer eviction automatically. Path B requires manual `currentTime` management and `QuotaExceededError` handling.
- **Proven at scale**: hls.js is used by Twitter/Dailymotion. No shipping product uses delayed MSE appends.

### Why not go2rtc's native HLS?

**[VERIFIED-DOCS]**: go2rtc's HLS has 500ms hardcoded segments, 2-segment window, no `PROGRAM-DATE-TIME`, no configuration knobs, session-based with 5s keepalive timeout. go2rtc project itself calls HLS "the worst technology for real-time streaming." Not suitable.

## 5. Component Details

### 5a. HLS Source (ffmpeg via pipeline HLS recorder)

```bash
ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:8554/feeder-main \
  -c copy \
  -f hls \
  -hls_time 2 \
  -hls_list_size 15 \
  -hls_flags delete_segments+program_date_time \
  -strftime 1 \
  -hls_segment_filename '/path/to/hls/feeder/seg_%s.ts' \
  /path/to/hls/feeder/live.m3u8
```

- **[VERIFIED-DOCS]** `-c copy`: no decode/encode, <1% CPU
- **[VERIFIED-DOCS]** `-hls_flags program_date_time`: embeds ISO 8601 wall-clock tags
- **[VERIFIED-DOCS]** `-hls_flags delete_segments`: auto-cleans old .ts files
- **[VERIFIED-DOCS]** `-hls_list_size 15`: 15 segments x 2s = 30 seconds of history
- Segments served via dashboard API as static files over HTTP

### 5b. hls.js Configuration

```javascript
var hls = new Hls({
  liveSyncDuration: 8,           // play 8 seconds behind live edge
  liveMaxLatencyDuration: 15,    // seek forward if drift exceeds 15s
  maxBufferLength: 20,           // keep 20s of forward buffer
  backBufferLength: 5,           // keep 5s of played-back buffer
  maxLiveSyncPlaybackRate: 1.1,  // gentle 10% speed-up for drift correction
  enableWorker: true,            // offload demuxing to web worker
});
```

- **[VERIFIED-DOCS]** All config options from hls.js API.md
- **[UNVERIFIED]** Stability with 2s segments at 8s delay — needs testing

### 5c. Timestamp Sync via hls.playingDate

```javascript
// hls.playingDate gives us the wall-clock time of the displayed frame
// Pipeline SSE events carry wall_time_ms — same system clock
var displayedWallMs = hls.playingDate ? hls.playingDate.getTime() : null;
if (displayedWallMs) {
  var event = findEventNear(eventBuffer, displayedWallMs, 300);
  // event contains fully vote-locked tracks with bboxes and species
}
```

- **[VERIFIED-DOCS]** `hls.playingDate` from hls.js API
- **[VERIFIED-DOCS]** Both timestamp sources use iMac system clock
- **[UNVERIFIED]** Actual correlation accuracy between ffmpeg and Python clocks — expected <100ms

### 5d. Overlay Rendering (per displayed frame)

```javascript
video.requestVideoFrameCallback(function onFrame(now, metadata) {
  var displayedWallMs = hls.playingDate ? hls.playingDate.getTime() : null;
  if (!displayedWallMs) {
    video.requestVideoFrameCallback(onFrame);
    return;
  }

  var event = findEventNear(eventBuffer, displayedWallMs, 300);
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (event && event.tracks) {
    for (var i = 0; i < event.tracks.length; i++) {
      var track = event.tracks[i];
      drawBbox(ctx, track);
      if (track.species && track.is_locked) {
        drawLabel(ctx, track);
      }
    }
  }

  video.requestVideoFrameCallback(onFrame);
});
```

- **[VERIFIED-DOCS]** rVFC fires per displayed frame in Chromium
- **[VERIFIED-CODE]** drawBbox/drawLabel already implemented in current overlay
- We use `hls.playingDate` (verified) NOT `metadata.mediaTime` (unreliable for live)

### 5e. Event Ring Buffer

```javascript
var eventBuffer = [];
var MAX_BUFFER_AGE_MS = 60000;  // keep 60s of history

sseSource.onmessage = function(ev) {
  var data = JSON.parse(ev.data);
  if (!data.tracks || data.wall_time_ms == null) return;
  eventBuffer.push(data);
  // Prune events older than buffer window
  var cutoff = Date.now() - MAX_BUFFER_AGE_MS;
  while (eventBuffer.length > 0 && eventBuffer[0].wall_time_ms < cutoff) {
    eventBuffer.shift();
  }
};

function findEventNear(buffer, targetMs, toleranceMs) {
  // Binary search for closest event to targetMs
  var best = null, bestDist = Infinity;
  for (var i = buffer.length - 1; i >= 0; i--) {
    var dist = Math.abs(buffer[i].wall_time_ms - targetMs);
    if (dist < bestDist) { best = buffer[i]; bestDist = dist; }
    if (buffer[i].wall_time_ms < targetMs - toleranceMs) break;
  }
  return bestDist <= toleranceMs ? best : null;
}
```

## 6. What the User Sees

1. Open `/live` page
2. Video loads — smooth 1080p 30fps of the bird feeder
3. A bird lands on the feeder
4. Immediately (from the viewer's perspective): green bounding box appears around the bird, "House Finch 92%" label floats above it
5. The bird hops to another perch — bbox and label track it smoothly
6. A second bird lands — a second bbox + label appears instantly
7. Both birds leave — bboxes fade out
8. The viewer has no idea the video is 8 seconds behind real-time

## 7. Risks and Mitigations

| Risk | Status | Mitigation |
|---|---|---|
| hls.js doesn't work with pipeline-generated HLS | **[UNVERIFIED]** | Test in step 1 before building anything else. Fallback: serve segments via nginx/static. |
| Clock skew between ffmpeg and Python on same machine | **[UNVERIFIED]** | Expected <100ms. If larger, add a one-time calibration offset. |
| 2s HLS segments cause stalls with hls.js at 8s delay | **[UNVERIFIED]** | 8s / 2s = 4 segments behind — hls.js docs warn below 3 segments. Should be fine but test. |
| `hls.playingDate` returns null | **[VERIFIED-DOCS]** | hls.js returns null if no PROGRAM-DATE-TIME in playlist. Our ffmpeg adds them. Guard with null check anyway. |
| HLS recorder adds CPU/RAM overhead | **[VERIFIED-DOCS]** | `-c copy` with bounded list is <1% CPU. Previous issue was `hls_list_size 0`. |
| No shipping product does exactly this | Research finding | Individual pieces are all proven. Novel combination but each step is testable independently. |

## 8. Implementation Steps

1. **[MUST DO FIRST] Test HLS pipeline end-to-end** — Run ffmpeg with the exact command from 5a, confirm .ts segments appear with PROGRAM-DATE-TIME tags, load in hls.js in a bare HTML test page, confirm `hls.playingDate` returns a valid Date.
2. **Update `pipeline/hls_recorder.py`** — Add `program_date_time` flag, set `hls_list_size 15`, add `delete_segments`. Re-enable for feeder only.
3. **Add HLS serving route** — dashboard api.py serves HLS segments from the pipeline output directory as static files.
4. **Build `/live` page** — hls.js player + SSE event buffer + rVFC overlay renderer.
5. **Test timestamp correlation** — Verify `hls.playingDate.getTime()` matches pipeline `wall_time_ms` within acceptable tolerance.
6. **Test with real birds** — Watch the feeder, verify labels appear instantly from the viewer's perspective.
7. **Embed in main dashboard** — iframe or replace the current live panel.

## 9. Research Sources

| Topic | Finding | Source |
|---|---|---|
| go2rtc native HLS | Unsuitable: 500ms hardcoded segments, no PROGRAM-DATE-TIME, no config | go2rtc wiki + GitHub issues #1602, #1699 |
| go2rtc MSE | Sends fMP4 with DTS/CTS timestamps, 5s sliding window | go2rtc wiki, video-rtc.js source |
| ffmpeg HLS muxer | `-c copy` <1% CPU, `program_date_time` embeds ISO 8601 wall-clock | ffmpeg-formats.html, patchwork ticket #7986 |
| hls.js | `liveSyncDuration` for fixed delay, `playingDate` for wall-clock sync | hls.js API.md |
| rVFC | `mediaTime` unreliable for live streams, but callback timing is reliable | WICG spec, MDN |
| MSE delayed append | Feasible but manual (no library, need currentTime management) | MDN SourceBuffer docs |
| Frigate NVR | Stale overlays on live, `annotation_offset` for recordings, no delayed live | Frigate docs + GitHub discussions |
| Blue Iris / Scrypted | No overlays on live view — only on recorded playback | Research agent findings |
| Commercial VMS | Sub-300ms edge inference, no software delay approach | Nx Witness docs |
| No shipping product | Implements delayed live view with pre-computed browser overlays | Research across 6 NVR/VMS platforms |
