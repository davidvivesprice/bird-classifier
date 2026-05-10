# Pi browser-side overlay sync — bedrock design

**Date**: 2026-05-10
**Author**: Claude (with David)
**Companion to**: server-side single-stream + PTS clock (commit `92dd6a2`, same session)

## Why this exists

This session's earlier server-side rewrite (single-stream PyAV decode, canonical
camera PTS as the only clock) made the *server side* of label sync drift-proof.
The browser side never received the same treatment and has gone through two
patch iterations this session that failed verification:

1. iPad Safari: video plays then goes black; no overlay.
2. Mac Firefox: video plays; no bounding boxes.

Both failures share a root cause: the design tried to make WebRTC, MSE, and
multiple sync paths all work on every device, glued together with after-the-fact
JS patches. That approach has been "fix until it breaks again" since at least
the iMac live-overlay work in April 2026. We're not doing that here.

This spec is bedrock: one transport, one canonical clock, one rendering path,
one test harness that asserts correctness against frame-by-frame human ground
truth. It is meant to never be replaced — only extended.

## What "bedrock" means concretely

- **One canonical clock**: the camera's H.264 PTS, in seconds. Every component
  in the chain (camera, PyAV decoder, segmenter, sidecar, browser, overlay
  renderer) reads or computes the *exact same number* for any given frame.
  Wall-clock (`Date.now()`, `time.time()`, NTP) is allowed for log timestamps
  and snapshot filenames; never for sync decisions.
- **One transport**: HLS only. No WebRTC, no MSE-WebSocket, no transport
  fallback chain. iOS Safari plays HLS natively; everywhere else uses hls.js.
- **One origin**: everything served under `pi5.vivessato.com`. One Cloudflare
  Access cookie covers manifest, segments, sidecar, SSE, dashboard.
- **One test fixture**: `may10_demo_video.mp4` looped over LAN as a fake camera,
  with frame-by-frame ground truth (`may10_demo_video.annotations.json`). Replay
  this through the full pipeline; assert every annotated bird shows up at the
  right PTS with the right bbox.

## Scope

In:
- Pi dashboard (`dashboard/pi_dash.html`) live view rendering
- New server-side HLS segmenter that taps the existing PyAV decoder
- Sidecar PTS index file (`segments.json`)
- Browser canvas overlay using hls.js + `requestVideoFrameCallback`
- Adaptive Lock smoothing ported from iMac's `dashboard/index.html`
- Test harness (live diagnostic, offline replay, production sentinel)
- iOS Safari PWA install path

Out:
- iMac dashboard (separate effort, same patterns, separate session)
- WebRTC live view (deliberately gutted — see "Removed scope" below)
- Long-term segment retention (parked in `forget_me_nots.md`)

## Removed scope (WebRTC gutting)

The Pi dashboard previously used go2rtc's `<video-stream>` custom element with
a `mode="webrtc,mse"` fallback chain. This is deleted entirely:

- No `<video-rtc>` / `<video-stream>` custom elements on the page.
- No `video-rtc.js` / `video-stream.js` vendored JS dependencies.
- No `mode` attribute, no `ON_TUNNEL` transport branching.
- No `/api/ws` WebSocket proxy use for video (proxy stays in `dashboard/api.py`
  for any other consumer; not needed for the live view).

Replaced with: a vanilla `<video playsinline muted autoplay>` element with
`src` set to the HLS manifest URL. iOS Safari's native HLS handles it. Other
browsers use hls.js as a transparent shim.

go2rtc itself is unchanged: still our RTSP relay (UniFi auth, reconnect,
exposes localhost:8554 to PyAV).

## Architecture

```
                          ┌─ already shipped (this session, commit 92dd6a2) ─┐
                          │                                                  │
   feeder-main (UniFi)    │  PyAV decoder reads RTSP from go2rtc             │
                  │       │  for av_frame in container.decode():             │
                  ▼       │     pts = av_frame.time      ← canonical clock   │
            go2rtc :1984  │     bgr_full, bgr_detect = downscale            │
                  │       │     ┌─────────────────────────────────┐         │
                  ▼       │     ▼                                  ▼         │
      rtsp://localhost:8554│  detection path (MotionGate, YOLO,    HLS muxer │ NEW
       /feeder-main        │   Tracker, Classifier, SnapshotWriter)          │
                          │     │                                  │         │
                          │     ▼                                  ▼         │
                          │  SSE event with `pts` field            seg_*.ts  │
                          │                                        + segments.json
                          └──────────────────────────────────────────────────┘
                                                │
                                                ▼
                          ┌─ Pi dashboard FastAPI (existing) ──────────────┐
                          │                                                │
                          │  GET /api/hls-live/feeder/                     │
                          │      live.m3u8                                 │
                          │      segments.json                             │
                          │      seg_NNNN.ts                               │
                          │  GET /api/pipeline/events/sse?camera=feeder    │
                          │       (proxy to local SSE server, same as today) │
                          └────────────────────────────────────────────────┘
                                                │
                                                │ HTTPS via Cloudflare Access
                                                ▼
                          ┌─ Browser (PWA / desktop) ─────────────────────┐
                          │                                                │
                          │  <video> + hls.js (or iOS native HLS)          │
                          │  EventSource for /events/sse                   │
                          │  XHR poll segments.json every 2s               │
                          │  Canvas overlay redrawn per video frame        │
                          │     (requestVideoFrameCallback)                │
                          └────────────────────────────────────────────────┘
```

## Section 1 — Server-side HLS segmenter

### File layout (new)

- `pipeline/hls_segmenter.py` (new): consumes PyAV packets, writes segments
- `dashboard/api.py` (existing): adds three routes for HLS endpoints
- `bird_pipeline_v3.py` (existing): instantiates the segmenter alongside
  FrameCapture

### Approach: passthrough mux

The segmenter does not decode and does not re-encode. It opens its **own
`av.open()`** against the same `rtsp://localhost:8554/feeder-main` URL that
FrameCapture reads. Two independent PyAV consumers, two TCP connections into
go2rtc — go2rtc fans out the camera RTP packets to multiple consumers
natively. **The PTS values both consumers see are identical**, because PTS
is stamped by the camera's encoder into the H.264 bitstream itself; PyAV
exposes those same numbers untouched. There is no cross-stream sync to
debug because there is no transformation between the two readers — they
both quote the camera verbatim.

Why two consumers instead of sharing one container: PyAV's `demux()`
iterator is single-pass. Tapping in two places requires either a
producer/consumer queue (more code, lock contention) or two reads of the
same network stream (simpler, ~minimal extra cost since go2rtc is local
and decode is the expensive part — which segmenter doesn't do).

Each H.264 packet has:
- `packet.pts` — presentation time in stream units
- `packet.dts` — decode time
- `packet.is_keyframe` — keyframe boundary marker

We open a fresh segment file at every keyframe:

```python
# Pseudocode
container = av.open(rtsp_url, options={...})
out = None
seg_pts_start = None
seq = 0

for packet in container.demux(video_stream):
    if packet.is_keyframe:
        if out is not None:
            close_segment(out, seg_pts_start, packet.pts)
        seq += 1
        out = av.open(f"seg_{seq:010d}.ts", "w", format="mpegts")
        out.add_stream(template=video_stream)
        seg_pts_start = packet.pts
    out.mux(packet)
```

UniFi G3 emits a keyframe every ~2s, so segments end up ~2s long. Acceptable
for our 8s `liveSyncDuration`. No CPU cost beyond the demux work that's already
happening.

### Sidecar: `segments.json`

Generated alongside the manifest. Format:

```json
{
  "stream": "feeder",
  "time_base_seconds": 1.0,
  "segments": [
    {"name": "seg_0000123456.ts", "pts_start": 1230.0,  "pts_end": 1232.0,  "duration": 2.0},
    {"name": "seg_0000123457.ts", "pts_start": 1232.0,  "pts_end": 1234.05, "duration": 2.05},
    {"name": "seg_0000123458.ts", "pts_start": 1234.05, "pts_end": 1236.1,  "duration": 2.05}
  ],
  "discontinuities": [
    {"after": "seg_0000123456.ts", "old_pts_end": 1232.0, "new_pts_start": 0.0}
  ]
}
```

`pts_start` is the PTS of the *first frame* in the segment, in seconds. This is
what the browser uses to compute frame PTS:

```js
frame_pts = seg.pts_start + (video.currentTime - fragment.start)
```

`fragment.start` here is hls.js's media-timeline value for that fragment.

`discontinuities` records where the camera reset PTS (a normal occurrence
on UniFi reboot or RTSP reconnect). The browser reads this list to invalidate
its event buffer relative to the discontinuity.

### Manifest: `live.m3u8`

Standard HLS with sliding window of 30 segments (~60s):

```
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:3
#EXT-X-MEDIA-SEQUENCE:123456
#EXT-X-INDEPENDENT-SEGMENTS
#EXTINF:2.0,
seg_0000123456.ts
#EXTINF:2.05,
seg_0000123457.ts
#EXT-X-DISCONTINUITY
#EXTINF:2.05,
seg_0000123458.ts
...
```

`EXT-X-DISCONTINUITY` is emitted when our packet stream observes a PTS reset
(i.e., the next keyframe's PTS is less than the previous segment's pts_end).
hls.js handles this natively.

### File pruning

A separate worker thread runs every 5 seconds:
- Drop segments from `segments.json` whose `pts_end` is more than 60s behind
  the live edge (so the browser doesn't try to fetch deleted files).
- Delete the actual `.ts` files from disk.
- Rewrite `live.m3u8` and `segments.json`.

60s sliding window = ~30MB on disk. (Long-term retention is parked in
`forget_me_nots.md`.)

### Endpoints — use existing wildcard route

`dashboard/api.py:282` already declares
`@app.get("/api/hls-live/{camera}/{path:path}")` which serves files from
`~/bird-snapshots/hls/{camera}/`. It already handles `.m3u8`, `.ts`, `.json`
content-types and applies path-traversal protection. **No new routes
required** — the wildcard serves whatever files our segmenter writes
into the directory.

The single change needed: ensure the existing route's allowed-camera
allowlist includes the segment we use (currently `("feeder", "ground",
"feeder-sub", "ground-sub")` which already covers our case).

### Crash / restart recovery

- **Segmenter restarts**: keep an on-disk `state.json` with the last `seq`
  number. On startup, resume from `seq+1`. Old segments stay on disk; pruner
  cleans them. PTS values from the camera don't reset on segmenter restart
  (they're driven by the camera's encoder), so SSE events still align.
- **Camera reconnect with PTS reset**: emit `EXT-X-DISCONTINUITY` in
  manifest; record in `segments.json#discontinuities`. Browser invalidates
  its event buffer and resumes.

## Section 2 — Browser-side rendering

### HTML structure

```html
<div class="live-stage" id="live-stage">
  <video id="live-video"
         class="live-video"
         playsinline
         webkit-playsinline
         muted
         autoplay
         preload="auto"></video>
  <canvas class="live-overlay" id="live-overlay"></canvas>
  <div class="sync-diag" id="sync-diag" hidden></div>
  <button class="overlay-toggle" id="overlay-toggle">Labels</button>
</div>
```

`playsinline` + `webkit-playsinline` set as HTML attributes (not via JS) so
iOS Safari sees them at element creation, not after-the-fact.

### Player setup

```js
import Hls from '/hls.js';   // vendored library, served from same origin

const video = document.getElementById('live-video');
const HLS_URL = '/api/hls-live/feeder/live.m3u8';

if (Hls.isSupported()) {
  const hls = new Hls({
    liveSyncDuration: 8,        // target 8s behind live edge
    liveMaxLatencyDuration: 12,
    enableWorker: true,
    lowLatencyMode: false,
  });
  hls.loadSource(HLS_URL);
  hls.attachMedia(video);
} else if (video.canPlayType('application/vnd.apple.mpegurl')) {
  // iOS Safari native HLS
  video.src = HLS_URL;
}
```

`hls.js` is vendored locally at `dashboard/hls.js` — one library, one HTTP
fetch from the same origin, no CDN dependency. Served via a `FileResponse`
route in `api.py`.

### State

- `eventBuf` — sorted array of SSE events, `{pts, tracks: [...]}`. Pruned to
  last 90 events (~18s at 5/sec).
- `segmentsIndex` — sidecar map, `{name: {pts_start, pts_end, duration}}`.
  Polled every 2s via `fetch('/api/hls-live/feeder/segments.json')`.
- `trackHistory` — per-track ring of `(pts, cx, top)` for Adaptive Lock
  smoothing.
- `discontinuities` — list from sidecar, used to invalidate eventBuf when
  crossing.

### Per-frame rendering loop (`requestVideoFrameCallback`)

```
1. Read video.currentTime + which fragment is playing
   - hls.js: hls.media.fragments OR hls.streamController.fragCurrent
   - native HLS: estimate from currentTime + segmentsIndex.pts_starts
2. frame_pts = segmentsIndex[frag.name].pts_start + (currentTime - frag.start)
3. Walk eventBuf for events near frame_pts (±2s window for smoothing kernel)
4. For each track present:
   a. anchor = adaptiveLockAnchor(trackHistory[track_id], frame_pts)
   b. opacity = computeOpacity(track, frame_pts)  // pre-fade-in + fade-out
   c. drawBBox + drawLabel on canvas
5. setTimeout next rVFC
```

### Adaptive Lock — port from iMac, made symmetric

iMac's `dashboard/index.html:8351-8414` defines `gaussianAt()` and
`adaptiveAnchorAt()`. We port both verbatim except: **delete the
`if (d > 0) continue;` line**. iMac skips future events because it has
none (live WebRTC, past-only events). On Pi+HLS, every rendered frame has
~5–8s of future events available in eventBuf, so the kernel is symmetric
(past+future), giving zero phase lag on moving birds.

Constants ported as-is:

```js
const SIGMA_WIDE_MS   = 380;   // smoothness regime
const SIGMA_NARROW_MS = 190;   // motion regime
const VEL_LO_PX_S     = 20;    // below: full wide
const VEL_HI_PX_S     = 80;    // above: full narrow
const VEL_LOOKBACK_MS = 150;
const ALPHA_EMA_GAIN  = 0.1;
const ANCHOR_LERP     = 0.5;
```

### Pre-arrival fade-in (the "magic")

When a track's first SSE event arrives, the corresponding frame is still
~5–8s in the future (HLS buffer). We can read this:

```js
function computeOpacity(track, frame_pts) {
  const first = track.firstEventPts;
  const last  = track.lastEventPts;
  // 300ms ramp before first frame would render
  if (frame_pts < first - 0.3) return 0;
  if (frame_pts < first)       return (frame_pts - (first - 0.3)) / 0.3;
  // 300ms fade out after last event
  if (frame_pts > last + 0.3)  return 0;
  if (frame_pts > last)        return 1 - (frame_pts - last) / 0.3;
  return 1;
}
```

Position during pre-fade: use the bird's first known bbox. Label appears in
the right spot before the bird visually arrives, fades up to full opacity
exactly as the bird lands.

### Reconnect / recovery

- Network blip: hls.js auto-retries. Native HLS handles it.
- Segmenter restart: media-sequence jumps; hls.js handles it.
- PTS reset: sidecar reports discontinuity; we clear `eventBuf` of events
  whose pts is on the wrong side of the discontinuity.
- SSE drop: `EventSource` auto-reconnects; events during the gap are lost.
  Acceptable — the gap is short and the bird may re-detect after.

## Section 3 — UI affordances

- **Labels toggle**: `<button id="overlay-toggle">Labels</button>` top-right of
  live stage. Click flips `showLabels` flag. When false, `requestVideoFrameCallback`
  skips canvas drawing entirely (no work). Persisted to
  `localStorage['showLabels']`. Default ON.
- **Sync diagnostic**: `?syncdiag=1` URL param shows a small fixed-position
  readout in the corner: `rVFC fps`, `eventBuf size`, `frame_pts`, `latest_event_pts`,
  `lag = frame_pts - latest_event_pts`, `drawn tracks`. Updated every 500ms.
  Cheap; can be left on permanently if needed.
- **Lead time tunable**: `window.__leadTimeS = 0.5` in DevTools shifts label
  rendering to a frame `0.5s` ahead. Default 0. Capped to ~3s by the buffer
  size. Useful for trying out anticipation feel without re-deploying.

## Section 4 — Testing & verification harness

### Layer 1: live diagnostic chip (always available)

The `?syncdiag=1` readout described above. Anyone with the URL can verify
basic health: rVFC firing, events arriving, lag bounded, labels drawing.
Not a "test" — a smoke alarm.

### Layer 2: offline replay against ground truth

This is the bedrock test.

**Fixture** (one-time setup, then frozen):
1. `may10_demo_video.mp4` — saved at
   `/Users/vives/docs/bird-observatory/training videos/may10_demo_video.mp4`.
   ~30 minutes of representative feeder activity.
2. `may10_demo_video.annotations.json` — frame-by-frame ground truth from
   David. Schema:
   ```json
   {
     "video": "may10_demo_video.mp4",
     "fps": 30,
     "annotations": [
       {"frame": 142, "pts_seconds": 4.733, "species": "House Finch",
        "bbox_normalized": [0.31, 0.42, 0.46, 0.62], "notes": "male, perched"}
     ]
   }
   ```
   Each annotation = one bird visible in one frame. `bbox_normalized` is
   `[x1, y1, x2, y2]` in 0..1 range so it scales independently of resolution.
   David provides this once; it never needs to be regenerated unless we
   change the fixture video.

**Replay rig:**
1. On the iMac (LAN): start `mediamtx` and loop `may10_demo_video.mp4` via
   `ffmpeg -re -stream_loop -1 ...`. Existing script:
   `test_clips/serve_test_feed.sh`.
2. The mediamtx server exposes `rtsp://192.168.4.X:8554/test-feeder`.
3. On the Pi, set `PIPELINE_TEST_RTSP_URL` env to that URL. The pipeline
   reads from it instead of the live UniFi camera. (Add this env var hook
   to `bird_pipeline_v3.py:CAMERAS_DETECT`.)
4. Start the Pi pipeline + dashboard normally.

The full pipeline now sees the recorded video as if it were a live camera:
go2rtc relays, PyAV decodes, segmenter writes HLS, dashboard serves it,
SSE emits PTS-tagged events.

**Assertion harness** (`tools/sync_replay_assert.py`, new):
1. Run for one full loop of the demo video (~30 minutes).
2. Capture all SSE events to a jsonl file (`replay_events.jsonl`).
3. Drive a headless Playwright browser against `pi5.local:8099/?syncdiag=1`,
   capture canvas screenshots at PTS values matching the annotations
   (one screenshot per annotated frame).
4. For each annotation:
   - Find the SSE event(s) with PTS within ±0.5s of `pts_seconds`.
   - Assert: `species` matches (or "any bird" — species accuracy is a
     separate metric, not part of sync verification).
   - From the canvas screenshot at `pts_seconds`: find the drawn bbox.
     Compare to `bbox_normalized` (scaled). Assert IoU ≥ 0.5.
5. Output: pass/fail per annotation, summary statistics (mean IoU,
   median lag in ms, max lag).

**Pass criteria** (binary):
- Every annotation has a matching SSE event within ±500ms.
- Every annotation has a drawn bbox with IoU ≥ 0.5 against ground truth.
- Median lag (`frame_pts - matched_event_pts`) within ±50ms across all
  annotations.

This test runs:
- On every code change touching the sync path (manual, before deploy)
- Optionally in CI on every commit (Playwright supports headless Chromium,
  Firefox, WebKit — three browsers tested simultaneously)
- Periodically in production (weekly cron) against the same fixture, to
  catch slow drift

### Layer 3: production sentinel

A small script in the dashboard checks invariants in live operation. Bumps
counters on `/api/system-health`:

- `frame_pts < latest_event_pts - 30s`: events stalled relative to video
  → SSE broken or pipeline hung.
- `latest_event_pts - frame_pts > 60s`: video stalled relative to events
  → segmenter or HLS serving broken.
- `5 consecutive seconds of drawnTracks > 0 but Adaptive Lock returns null`:
  smoothing failed (eventBuf vs trackHistory mismatch).

These don't auto-fix anything; they show up in `/api/system-health` so we
notice silently rather than during a David-watching-for-birds session.

## Section 5 — Implementation order

Suggested order (each step is independently deployable + testable):

1. **HLS segmenter** (`pipeline/hls_segmenter.py`) writing to disk.
   Verify: segments + manifest + sidecar appear under `~/bird-snapshots/hls/feeder/`,
   PTS values look right, hls.js can play the manifest from a localhost test page.
2. **Dashboard HLS routes** in `api.py`. Verify: `curl` returns manifest and
   segment files; `Content-Type` correct.
3. **Browser HLS player** — strip out WebRTC pieces, add vanilla `<video>` +
   hls.js, point at the new endpoint. Verify: video plays in
   Chrome/Firefox/Safari/iPad.
4. **Canvas overlay rewrite** — sidecar polling, frame_pts computation,
   Adaptive Lock symmetric port, pre-fade-in, labels toggle. Verify: live
   diagnostic chip shows healthy numbers; labels visibly track birds.
5. **Test fixture annotation** — David annotates as many visits in
   `may10_demo_video.annotations.md` as needed to satisfy the gate
   count (default: any 5 visits with both timecode windows + species).
   Implementation does not block on this — the harness runs against
   whatever's annotated; "any 5 visits" is a v1 floor we can raise.
   Manual iPad smoke test step in §9 is what guards the cases the
   harness can't capture (rVFC nuance, PWA install).
6. **Replay rig** — `serve_test_feed.sh` already exists; add the
   `PIPELINE_TEST_RTSP_URL` env hook to `bird_pipeline_v3.py`.
7. **Assertion harness** — `tools/sync_replay_assert.py`. First run is the
   bedrock-test pass; subsequent runs catch regressions.
8. **Production sentinel** — health-counter checks. Lowest priority; add
   once the rest is stable.

## Section 6 — Adversarial review findings & resolutions

This spec went through one adversarial review pass (2026-05-10). Findings
were ranked critical/important/nice-to-have. Each is addressed below; the
spec is updated inline to incorporate the resolutions.

### C1 — VERIFIED. PyAV mpegts muxer preserves PTS byte-exact.

Reviewer concern: PyAV's `mpegts` muxer might rebase packet PTS to start
at 0, breaking the "two consumers see identical PTS" assumption.

Verification (commit `ac77abc`, run on Pi against `rtsp://127.0.0.1:8554/feeder-main`):
30 packets across 1 keyframe boundary, demuxed, written via
`av.open(..., 'w', format='mpegts')` + `add_stream_from_template()` +
`packet.stream = out_stream; out_container.mux(packet)`, then re-demuxed
from the output file. **Max |output_pts - input_pts| = 0.000 ms.**

Decision: spec stands as written for the muxer path. Tool kept at
`tools/prototype_hls_passthrough.py` as a regression guard — must pass
before any segmenter change is merged.

### C2 / S3 — RESOLVED via PDT-based universal computation.

Reviewer concerns (combined): hls.js's `fragment.start` is the cumulative
duration in the *current* manifest window and unstable across rotations
(C2). Native iOS HLS doesn't expose `frag.startPTS` at all, so any sync
math that uses hls.js-internal fields breaks on iPad — exactly the failure
mode that motivated this rewrite (S3).

**Resolution: encode our PTS into the EXT-X-PROGRAM-DATE-TIME tag**, then
use `video.getStartDate()` + `video.currentTime` — APIs that are
**identical on hls.js and native iOS Safari**, so one code path serves
both transports.

#### How PDT carries our PTS

We emit `EXT-X-PROGRAM-DATE-TIME = ISO 8601 representation of
(epoch=1970-01-01T00:00:00Z, seconds=pts_start)` for each segment.
Example: a segment whose first frame has PTS = 873.002 seconds
produces `#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:14:33.002Z`.

This is "fake NTP" — semantically PDT means wall-clock, but iOS Safari
and hls.js treat the value as an opaque `Date`. They don't validate the
date is sane; they just expose it via `getStartDate()`. The math works
identically regardless of whether the year is 1970 or 2026.

Reasons to embrace this fully (vs the original "sidecar-only" plan):

- **One code path** for hls.js and native iOS. No "for native, walk
  `video.buffered` and infer fragment from durations" hand-waving.
- **Player-internal**: hls.js's `frag.programDateTime` and Safari's
  `video.getStartDate()` both return Date objects derived from the
  manifest's PDT tag. Player handles fragment-rotation reanchoring
  for us — we don't have to track it.
- **Naturally monotonic** (addresses N-I1): our PTS is monotonic
  (it's the camera's encoder clock); when we encode `1970 + pts_seconds`,
  PDT is monotonic too, regardless of NTP wall-clock jumps on the host.
- **Discontinuity-aware**: at every `EXT-X-DISCONTINUITY` we emit a
  fresh PDT seeded from the new fragment's PTS. Players resync.

#### Per-frame computation (browser side)

```js
// Universal — works for hls.js MSE playback AND iOS native HLS.
const PDT_EPOCH_MS = 0;  // 1970-01-01T00:00:00Z

function computeFramePts(video) {
  // getStartDate(): returns the Date from the most recent
  // EXT-X-PROGRAM-DATE-TIME tag, anchored to the start of the
  // currently playing media-timeline. Stable for the fragment's
  // lifetime; auto-updates across DISCONTINUITY boundaries.
  // Returns Invalid Date or NaN if PDT is not yet known.
  const startDate = video.getStartDate();
  if (!startDate || isNaN(startDate.getTime())) return null;
  const pts_at_zero_s = (startDate.getTime() - PDT_EPOCH_MS) / 1000;
  return pts_at_zero_s + video.currentTime;
}
```

That's it. No sidecar required for sync. Sidecar `segments.json` stays
as **debug/redundancy only** — useful for the diagnostic chip and the
test harness's per-segment cross-check, not for the hot path.

For the absolute frame-accurate version, replace `video.currentTime`
(which has ~16ms granularity) with `metadata.mediaTime` from
`requestVideoFrameCallback` — gives sub-frame precision. Native iOS
Safari ≥15.4 supports rVFC; older devices fall back to `currentTime`
(still better than the iMac's wall-clock approach).

#### Player setup

```js
import Hls from '/hls.js';   // vendored locally; pin ≥1.5.7
const HLS_URL = '/api/hls-live/feeder/live.m3u8';
const video = document.getElementById('live-video');

if (video.canPlayType('application/vnd.apple.mpegurl')) {
  // iOS Safari (and macOS Safari): native HLS path. No hls.js needed.
  // canPlayType is checked FIRST — Safari pretends to support MSE but its
  // native HLS path is more reliable on iOS.
  video.src = HLS_URL;
} else if (Hls.isSupported()) {
  // Chrome/Firefox/Edge: hls.js polyfill via MSE.
  const hls = new Hls({
    liveSyncDuration: 8,
    liveMaxLatencyDuration: 12,
    enableWorker: true,
    lowLatencyMode: false,
  });
  hls.loadSource(HLS_URL);
  hls.attachMedia(video);
}
```

Pin **`hls.js` ≥ 1.5.7** (vendored at `dashboard/hls.js`).

### C3 — RESOLVED. Symmetric Adaptive Lock written as full code.

Reviewer concern: the iMac `gaussianAt` function (index.html:8355) walks
backward with `if (d < -halfWindow) break;` — that early break is
correct ONLY for a past-only kernel. A symmetric kernel needs both
breaks (past tail and future tail) and must handle out-of-order events
under SSE jitter.

Resolution: replace the one-line-delta description with a full port:

```js
// state.events: insertion-sorted by pts ASCENDING. Maintained by
// applyEvent() which uses bisect-insert; not a sort-on-every-insert.

function gaussianAt(events, T_pts, sigma_s) {
  // T_pts: server-PTS-in-seconds of the rendered frame
  // sigma_s: kernel width in seconds (e.g. 0.38 for wide, 0.19 narrow)
  // events[i].pts in seconds; events[i].cx, events[i].top in detector coords.
  if (events.length === 0) return null;
  const sigma2 = sigma_s * sigma_s;
  const halfWindow = sigma_s * 3.2;

  // Find the insertion index for T_pts (binary search on .pts).
  // Walk OUTWARD from there: backward into past, forward into future,
  // break each direction independently when |d| > halfWindow.
  let lo = 0, hi = events.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (events[mid].pts < T_pts) lo = mid + 1; else hi = mid;
  }
  const center = lo;  // first index with .pts >= T_pts

  let sx = 0, sy = 0, sw = 0;

  // Walk backward (past)
  for (let i = center - 1; i >= 0; i--) {
    const d = events[i].pts - T_pts;     // negative
    if (d < -halfWindow) break;
    const w = Math.exp(-(d * d) / (2 * sigma2));
    sx += events[i].cx * w; sy += events[i].top * w; sw += w;
  }
  // Walk forward (future)
  for (let i = center; i < events.length; i++) {
    const d = events[i].pts - T_pts;     // non-negative
    if (d > halfWindow) break;
    const w = Math.exp(-(d * d) / (2 * sigma2));
    sx += events[i].cx * w; sy += events[i].top * w; sw += w;
  }

  if (sw === 0) return null;
  return { cx: sx / sw, top: sy / sw };
}
```

Insertion (called when an SSE event arrives):

```js
function applyEvent(events, evt) {
  // bisect-insert by pts (events arrays are per-track, small ~30 entries,
  // so linear walk from the tail is fine and avoids a binary-search step).
  let i = events.length;
  while (i > 0 && events[i-1].pts > evt.pts) i--;
  events.splice(i, 0, evt);
}
```

Constants stay (380ms wide, 190ms narrow, 20–80 px/s velocity blend
band, 0.1 EMA gain, 0.5 anchor LERP).

### I1 — RESOLVED. Manifest includes DISCONTINUITY-SEQUENCE + PROGRAM-DATE-TIME.

Reviewer concern: iOS Safari does NOT honor `EXT-X-DISCONTINUITY` for
live without `EXT-X-DISCONTINUITY-SEQUENCE` (RFC 8216 §4.3.3.3) and
without an `EXT-X-PROGRAM-DATE-TIME` tag at the discontinuity boundary.

Resolution: manifest format updated to include both. Example:

```
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:3
#EXT-X-MEDIA-SEQUENCE:123456
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-PROGRAM-DATE-TIME:2026-05-10T15:00:00.000Z
#EXTINF:2.000,
seg_0000123456.ts
#EXTINF:2.050,
seg_0000123457.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-05-10T15:00:30.500Z
#EXTINF:2.050,
seg_0000123458.ts
```

`PROGRAM-DATE-TIME` here is wall-clock-at-segment-write (NOT our PTS —
PDT is for player resync purposes only; our internal sync still uses
the sidecar). `DISCONTINUITY-SEQUENCE` increments on every actual
discontinuity, persisted across segmenter restart in `state.json`.

### I2 — RESOLVED. Two distinct tolerances, defined explicitly.

Old spec had `±0.5s` (event matching window) and `±50ms` (median lag
pass criterion) without distinguishing them. Resolution:

- **MATCH_WINDOW_MS = 500**: maximum allowed gap between an annotation's
  identifiable midpoint and the matching SSE event's PTS. If no event
  within this window, the annotation is unmatched (FAIL).
- **MEDIAN_LAG_MS = 50**: pass criterion on the *distribution* of
  `frame_pts - matched_event_pts` across all matched annotations. The
  median must be within ±50ms of zero (a distribution centered on the
  pipeline's natural ~200-400ms processing lag is acceptable; a
  distribution that's growing/drifting is the failure signal).
- **MAX_LAG_MS = 1000**: any single matched event whose lag exceeds
  this is reported as a per-annotation warning even if median passes.

These are now properties on the harness config, not magic numbers.

### I3 — RESOLVED. 1:1 matching via greedy nearest.

Reviewer concern: if two annotations fall within ±500ms of one event,
both pass with the same event — false positive in the harness.

Resolution: matching is greedy-nearest, 1:1:

1. Sort annotations by `identifiable_midpoint_pts` ascending.
2. For each annotation in order:
   a. Find the closest *unclaimed* SSE event with matching species,
      within MATCH_WINDOW_MS.
   b. If found: claim it (mark as used). Record the lag.
   c. If not found: annotation is unmatched (FAIL).
3. Annotations with no `first_identifiable` (just in-frame): step 2 uses
   any species (detection-only assertion).
4. After all annotations processed: any SSE event with PTS *outside*
   all in-frame windows AND not claimed is a false positive.

### I4 — RESOLVED. Atomic publication via `.part` + rename.

Resolution: segmenter writes to `seg_NNNN.ts.part`. On keyframe
boundary close: `os.replace()` to `seg_NNNN.ts`. Manifest is *also*
written atomically: `live.m3u8.tmp` → `os.replace()` → `live.m3u8`.
Same for `segments.json`. The manifest is *only* updated to include
the new segment AFTER the segment file's atomic rename completes.

### I5 — RESOLVED. Layer 2b drives the harness through the tunnel.

Reviewer concern: mediamtx-on-iMac LAN test bypasses Cloudflare Access
and PWA service worker — exactly the surface where the prior browser
failures occurred.

Resolution: the test harness has two execution modes:

- **Layer 2a (fast loop, every code change)**: Pi reads from
  `rtsp://192.168.4.X:8554/test-feeder` (mediamtx on iMac LAN). Browser
  drives at `http://pi5.local:8099`. Tests pipeline + sync math.
- **Layer 2b (deploy gate, every deploy)**: same Pi-side replay, but
  Playwright drives at `https://pi5.vivessato.com` using a Cloudflare
  Access service-token (set via env `CF_ACCESS_CLIENT_ID` /
  `CF_ACCESS_CLIENT_SECRET`). Headers attached to every request:
  `CF-Access-Client-Id` and `CF-Access-Client-Secret`. Tests
  PWA-shaped path: HTTPS termination, Access cookie/token, hls.js
  through tunnel.

Both layers must pass before deploy. Layer 2a is the dev-loop check;
Layer 2b is the regression-against-tunnel-stack check.

### N-series — Nice-to-haves applied

- **N1**: hls.js pinned ≥1.5.7 (incorporated into C2 above).
- **N2**: Playwright headless WebKit ≠ iOS Safari noted; spec adds a
  manual-verification step to §5 (Implementation order):
  *"After Layer 2b passes, perform one manual smoke test on a real
  iPad Safari before declaring acceptance."*
- **N3**: Pruner and manifest-window separated. Pruner walks
  `~/bird-snapshots/hls/feeder/` for `.ts` files older than a
  configurable retention period (default = manifest window = 60s).
  Setting `HLS_RETENTION_S` to e.g. 86400 (1 day) keeps segments on
  disk past their manifest lifetime. Long-term retention is then a
  flag flip, not a code change.
- **N4**: iMac scope deferral noted in §Scope as a dated TODO with a
  reference to this spec — same patterns will be ported when an iMac
  session prioritizes it.
- **N5**: Snapshot-PTS / segmenter-PTS relationship: both are
  identical because both flow from the same camera RTSP stream's
  bitstream-stamped PTS values. The C1 prototype proves preservation
  through the muxer; the snapshot path uses the SSE event's PTS which
  the same FrameCapture stamps. Therefore "click snapshot → seek HLS
  to same PTS" is well-defined and works.

## Section 7 — Second-pass review findings & resolutions

A second adversarial review pass after the first revision. Findings:

### S1 (extension of C1) — RESOLVED via extended prototype.

First prototype tested 30 packets in a single segment. Reviewer flagged
this as too narrow: didn't cross multiple keyframe boundaries, didn't
prove SPS/PPS preserved across per-segment muxer init, didn't decode
back through any client.

`tools/prototype_hls_passthrough_v2.py` (commit `6c873cc`) addresses this:

- 4 segments written via per-keyframe mux open/close (the actual
  production hot path)
- 3 keyframe boundary crossings traversed
- 150 packets per segment, all PTS byte-exact (diff = +0 across every
  segment boundary)
- Each segment re-opened via fresh `av.open()`, decoded to a BGR frame
  using only that segment — proves SPS/PPS carried through
  `add_stream_from_template` so hls.js can decode any segment standalone

Both prototypes (v1 single-segment, v2 multi-segment + decode-back) kept
as regression guards.

### S2 — RESOLVED. Use existing route, no new endpoints required.

Reviewer found that `/api/hls-live/{camera}/{path:path}` already exists
at `dashboard/api.py:282` and serves m3u8 / ts / json with correct
content-types. The spec's "three new routes" claim was wrong.

Resolution: the spec's §1 endpoints block has been replaced with "use
the existing wildcard." Implementation cost drops to zero on this axis.

### S3 — RESOLVED via PDT-based universal computation.

Already integrated into the C2 resolution above.

### N-I1 — RESOLVED. PDT monotonicity guaranteed by epoch-1970 encoding.

Reviewer concern: NTP step on the Pi could make wall-clock-based PDT
non-monotonic.

Resolution: we don't use wall-clock for PDT. We use
`epoch=1970 + pts_seconds`, which is automatically monotonic because
camera PTS is monotonic. NTP drift on the Pi has no effect.

### N-I2 — Greedy 1:1 matching ACCEPTED with caveat.

Reviewer correctly notes that greedy-by-annotation-order can be
sub-optimal vs Hungarian for adjacent annotations + adjacent events.
Counter-example: A@1.0, B@1.2, C@1.6 with events E1@1.45, E2@1.55 →
greedy claims A→E1, B→E2, C unmatched; Hungarian gives B→E1, C→E2.

Decision: greedy stays as v1 of the matcher. For the bedrock harness:

- Document greedy as a known approximation in the harness comments
- Ensure `MATCH_WINDOW_MS = 500` is wide enough that this only matters
  for adjacent annotations (which is rare in practice — birds don't
  arrive at the feeder 200ms apart)
- Add a flag to the harness to upgrade to Hungarian (via `scipy.optimize.linear_sum_assignment`)
  if greedy starts misclassifying. Out of v1 scope.

### N-I3 — `applyEvent` cost noted, within budget.

Per-track event arrays are ~30 entries; bisect-insert is fine. Global
event buffer is ~240 entries at 5 events/sec; one splice every 200ms
is ~1200 splices/sec worst case. On Pi-class hardware this is sub-1%
CPU. Profiled in the v2 prototype's verification phase.

If the per-track count grows (e.g., 10 simultaneous tracks × 30 events =
300 entries on a global ringbuffer), revisit with a flat sorted array
+ binary insert. Out of v1 scope.

### N-I4 — RESOLVED. CF Access service token guidance.

The Layer 2b Playwright harness needs to authenticate to Cloudflare
Access. Resolution:

- Service token created in Cloudflare Zero Trust dashboard, scoped
  **only** to the `pi5.vivessato.com` Access application (not all
  apps in the account)
- Stored as repository secrets `CF_ACCESS_CLIENT_ID` and
  `CF_ACCESS_CLIENT_SECRET` (in CI) or in `~/.bird-observatory-env`
  (locally, not committed)
- Playwright config: `extraHTTPHeaders: { 'CF-Access-Client-Id': ID,
  'CF-Access-Client-Secret': SECRET }` — propagates to all browser
  fetches including hls.js worker fetches in modern Playwright
  (verified in Playwright ≥1.40)
- Token rotation: 90-day expiry on the service token; calendar reminder
  in `~/docs/bird-observatory-pi/01-secrets.md`

### N-I5 — RESOLVED. Implementation order step 5 reworded.

Old: "David annotates may10_demo_video.mp4 frame-by-frame."
New: "David annotates as many visits as needed to satisfy the gate
count (default: any 5 visits with both windows + species). The harness
runs against the partial annotation file with no code changes."

Update applied in §5 below.

### N-I6 — RESOLVED. Backpressure handling.

Segmenter writes via `out_container.mux(packet)` which can block on
disk I/O if the device fills or write latency spikes. Resolution:

- Bounded **demux→mux queue** (default `maxsize=300`, ~10s of packets).
  When full, oldest packet is dropped + counter bumped (`segmenter_pkts_dropped`).
- Disk-fill alarm in production sentinel: if free space on
  `~/bird-snapshots/hls/` falls below 1 GB, log + bump
  `/api/system-health` counter `segmenter_disk_low`.
- Segmenter does NOT block FrameCapture's decoder. They share the same
  RTSP source via two independent `av.open()` calls (per Section 1) —
  segmenter falling behind only affects HLS output, never detection.

### N-I7 — RESOLVED. `state.json` corruption recovery.

If `state.json` is missing or corrupt on startup:

1. Scan `~/bird-snapshots/hls/feeder/` for the highest existing
   `seg_NNNN.ts` filename. Resume `seq` from there + 1.
2. `discontinuity_sequence` resumes from 0 (worst case: hls.js sees
   a clean restart and resyncs, which is what we want).
3. Log `WARNING: state.json missing/corrupt; resumed seq=N from disk scan`.
4. The harness's Layer 1 sentinel will see the warning bump and
   surface it.

### C-1 — Window math noted in comments only.

The 60s manifest window leaves comfortable room for the 8s
`liveSyncDuration` + 1.2s kernel halfWindow + 3s capped lead time
(total ~13s into past edge). Documented inline in the segmenter source;
not a spec-level concern.

### C-2 — RESOLVED via Playwright config (see N-I4).

### C-3 — RESOLVED. Blank species annotation handled.

If an annotation has `species: ""` or omitted: the matcher treats it as
"any species" within the in-frame window. Detection-only assertion,
species correctness skipped for that visit.

## Annotation tolerance

This is a project-management constraint, not a sync-correctness one.
David has told us annotation will take time, and that some frames have
ambiguous identifiability. The harness handles this by:

1. **Annotations file may be partial.** Empty visit blocks (all four
   timecodes blank) are skipped. The harness reports
   `N annotations active, M skipped` at the start of each run.
2. **Identifiable window may be empty.** The harness asserts only
   detection coverage for those visits, not species correctness.
3. **First N visits is a valid scope.** If David has filled in 5 of
   20 visits, the harness runs against those 5 with full rigor.
4. The acceptance gate (Section 9) requires *the configured set of
   annotations* to pass, not "all 20 visits annotated." David picks
   the gate count when annotations are good enough.

This means: implementation can proceed, the harness can be wired up,
the segmenter and overlay can be developed, all without waiting for
the annotation file to be complete.

## Open questions / decisions deferred

- Should we segment the audio track too? Currently HLS manifest is
  video-only, audio dropped. Audio at the feeder is mostly birdsong (worth
  hearing) but adds complexity. Defer; trivial to add later.
- Should the overlay support multiple cameras (ground cam) when ground
  cam is re-enabled? Architecture supports it (segmenter is per-camera);
  UI currently shows feeder only. Defer.
- Should the sidecar be embedded into the HLS manifest itself via a custom
  header (`#EXT-X-VIVES-PTS:1234.567,1236.567`) instead of a separate JSON?
  Cleaner conceptually; harder to evolve. Sidecar JSON wins for now.

## Files this design touches

New:
- `pipeline/hls_segmenter.py`
- `dashboard/hls.js` (vendored)
- `tools/sync_replay_assert.py`
- `/Users/vives/docs/bird-observatory/training videos/may10_demo_video.annotations.json`

Modified:
- `bird_pipeline_v3.py` (instantiate segmenter, add test-RTSP-URL env hook)
- `dashboard/api.py` (HLS routes)
- `dashboard/pi_dash.html` (replace `<video-stream>` with `<video>` + hls.js,
  rewrite overlay code)
- `dashboard/video-rtc.js` (delete or leave as dead code)
- `dashboard/video-stream.js` (delete)

Deleted from code:
- The `BirdVideoRTC` subclass at top of `pi_dash.html`
- The `<video-rtc>` element + all related transport-mode JS
- The `script.src = videoStreamJsUrl` block

## Acceptance

Design is bedrock-correct when:
1. **Layer 2a (LAN)**: replay harness passes against ≥5 annotated
   visits in `may10_demo_video.annotations.md`, on three browsers
   (Playwright headless Chromium, Firefox, WebKit).
2. **Layer 2b (tunnel)**: same harness passes through
   `https://pi5.vivessato.com` with CF Access service token.
3. **Manual iPad smoke** (per N-2 / N-I5): real iPad Safari
   add-to-home-screen install, open the PWA, observe video plays +
   labels track + sync diagnostic numbers healthy. Captured as
   screenshots in `~/docs/bird-observatory-pi/05-dashboard.md`.
4. The Pi dashboard runs in production for 7 consecutive days without
   the production sentinel firing.

After (4), this design is considered locked. Future changes to the sync
path require Layers 2a + 2b to pass before merge.
