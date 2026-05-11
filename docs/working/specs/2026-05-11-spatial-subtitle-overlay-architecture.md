# Spatial subtitle overlay architecture for bird labels

**Date**: 2026-05-11
**Author**: Codex audit pass
**Status**: Delivery-ready design memo for review
**Companion docs**:
- `docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md`
- `docs/working/plans/2026-05-11-pipeline-cpu-audit-plan.md`
- `docs/working/progress/2026-05-10-overlay-sync-handoff.md`

## Executive decision

The bird observatory should treat visible labels as **spatial subtitles**:
timecoded cues with species text, confidence, and moving coordinates. The
browser should render those cues against the actual displayed video frame's
media time, not against event arrival time, wall-clock time, or a best-effort
SSE stream.

Production subtitle, live-caption, sports-graphics, ad-marker, and broadcast
graphics systems all converge on the same pattern:

```
shared clock -> media timeline -> timed cue/event store -> frame-synchronous renderer
```

The current Pi overlay failure is not just a CSS or canvas bug. Runtime evidence
showed HLS segment PTS around `181s` while SSE event PTS was around `6329s` at
the same moment. Those streams are not in one timeline. Until the system has an
explicit **ClockBridge** between detection time and displayed video time, labels
will remain unreliable.

## Non-negotiable product requirements

- Labels must appear on the bird, move with the bird, and remain toggleable.
- Bounding boxes are diagnostic scaffolding. The target experience is labels
  only, with optional boxes for debugging.
- Classifier confidence can take time. The live view must allow intentional
  delay so labels can appear on the correct historical frame once the classifier
  locks.
- The high-resolution ring buffer stays. It feeds snapshots, human confirmation,
  model improvement, and training data.
- Detection should run on the light detection stream. High-resolution video is
  for display, snapshots, and review, not full-frame per-frame inference.
- Sync decisions must use media time. Wall-clock time is acceptable for logs,
  filenames, health checks, and offset diagnostics, but not as the authority for
  whether a label belongs on a displayed frame.

## What we steal from production systems

### 1. Subtitles: cues over media time

HLS WebVTT subtitles do not rely on when text arrives in the player. A WebVTT
segment carries cues with start and end times, and HLS defines
`X-TIMESTAMP-MAP` to map cue timestamps to MPEG transport timestamps in the
audio/video rendition. That is the exact class of mechanism missing from the
Pi overlay stack.

Borrowed concept: every bird label is a cue with a start time, end time, and
payload. The player asks which cues are active for the frame currently being
presented.

Reference:
<https://datatracker.ietf.org/doc/html/rfc8216#section-3.5>

### 2. Media frameworks: one running-time

GStreamer normalizes streams onto a pipeline clock and running-time. Production
pipelines do not treat decode callbacks, socket delivery, and UI timers as
equivalent clocks.

Borrowed concept: all bird observations must be normalized to a canonical media
timeline before the overlay layer sees them.

Reference:
<https://gstreamer.freedesktop.org/documentation/application-development/advanced/clocks.html>

### 3. Player APIs: render on the real video frame

`HTMLVideoElement.requestVideoFrameCallback()` gives the browser a callback when
a video frame is sent to the compositor, including `metadata.mediaTime`, which
is the presentation timestamp on the media element timeline. For WebRTC frames,
browsers may also expose capture and RTP timestamp metadata, but the portable
baseline for our dashboard is media time.

Borrowed concept: draw labels only from a frame callback, using the displayed
frame's `mediaTime`.

Reference:
<https://developer.mozilla.org/en-US/docs/Web/API/HTMLVideoElement/requestVideoFrameCallback>

### 4. Subtitle renderers: `getCues(time)`

Media3/ExoPlayer's subtitle model has the right shape: `getCues(timeUs)`
returns the cues that should be visible at a given media timestamp.

Borrowed concept: implement a small in-browser `BirdCueStore.getCues(mediaTime)`
instead of pushing every SSE event directly into visible DOM state.

Reference:
<https://developer.android.com/reference/androidx/media3/extractor/text/Subtitle>

### 5. Custom text displayers

Shaka Player separates text parsing from text display and supports custom text
displayers. We do not need Shaka itself for the Pi dashboard, but the boundary
is useful.

Borrowed concept: separate cue ingestion, cue lookup, and cue rendering. This
keeps the overlay renderer replaceable without changing detection/classifier
logic.

Reference:
<https://shaka-player-demo.appspot.com/docs/api/tutorial-text-displayer.html>

### 6. Offline subtitles and burn-in

ASS/libass and FFmpeg subtitle filters are useful for offline verification. A
diagnostic render with burned-in labels can prove whether track/cue math is
right before the live browser overlay is debugged.

Borrowed concept: live overlays stay soft and toggleable; offline burn-in is a
test artifact.

References:
- <https://github.com/libass/libass>
- <https://ffmpeg.org/ffmpeg-filters.html#subtitles-1>

### 7. Camera NVR practice: different streams for different jobs

Frigate's camera setup guidance matches the intended observatory architecture:
use a tuned detection stream for processing, a high-resolution stream for
recording/reference, and avoid decoding frames only to discard them.

Borrowed concept: use the substream for inference, preserve the high-res stream
for viewing/snapshots, and keep aspect ratios matched so coordinate projection
is simple.

Reference:
<https://docs.frigate.video/frigate/camera_setup/>

## Open-source and standards inventory

| Example | Open? | What we can reuse | What not to copy |
| --- | --- | --- | --- |
| HLS + WebVTT | Open standard | Timestamp mapping, cues in media time, rendition relationship | Plain WebVTT is too limited for moving bird labels |
| GStreamer clocks | Open source / LGPL | Clock discipline, running-time vocabulary | Rewriting the app in GStreamer is too large for the next pass |
| hls.js | Open source / Apache 2.0 | HLS playback events, subtitle/timed-metadata concepts | Do not force all browsers through hls.js when Safari native HLS is better |
| Shaka Player | Open source / Apache 2.0 | Custom text displayer boundary | Full player migration is unnecessary right now |
| Media3 / ExoPlayer | Open source / Apache 2.0 | `getCues(time)` API shape | Android-specific code |
| FFmpeg + libass | Open source | Offline burn-in diagnostics | Live burn-in would lose toggles and require encoding |
| OBS Studio | Open source / GPL | Layered source/compositor mental model | GPL code should not be copied into this repo |
| Vizrt / broadcast graphics | Commercial | Genlock/timecode discipline, explicit latency budgets | Code is not available; use concepts only |

## Concept dictionary

### MediaClock

The canonical timeline used by the displayed video. For browser rendering this
is the `<video>` element's media timeline, observed through
`requestVideoFrameCallback().metadata.mediaTime`.

### DetectionClock

The timeline attached to frames used for YOLO/Hailo detection and classifier
work. In the current code this is derived from PyAV frame time/PTS on the stream
being decoded.

### ClockBridge

A continuously updated mapping from `DetectionClock` to `MediaClock`.

Required API shape:

```python
display_time = clock_bridge.detect_to_display(detect_pts)
detect_time = clock_bridge.display_to_detect(media_time)
```

The bridge may initially be an offset plus drift estimate. It must be measured,
reported, and tested. It cannot assume independent RTSP readers expose one
shared epoch.

### BirdTrack

The tracker's history for one physical bird. It owns coordinates, velocity,
visibility, and classifier observations over time. It should not own display
state.

### ClassifierLock

The moment a track has enough evidence to show a species label. The lock can be
computed after the bird entered the scene. In delayed playback, that is fine:
once locked, the label cue can cover earlier visible frames in the same track.

### BirdCue

A display-ready label interval on the media timeline. It is not raw detector
output. It is the product of tracking, smoothing, classifier lock, and
ClockBridge mapping.

### SpatialCue

A `BirdCue` with time-varying coordinates. This is the bird-observatory-specific
extension of a subtitle cue.

### CueStore

A bounded in-memory and/or on-disk index of cues. It supports queries by media
time and by track id.

### SoftOverlay

The browser-rendered overlay layer. It is toggleable, inspectable, and does not
modify the video stream.

### BurnIn

A diagnostic artifact where labels are rendered into video frames using FFmpeg,
libass, canvas, or another offline renderer. Burn-in is for verification and
sharing, not the live dashboard.

## Proposed architecture

```
                      high-res main stream
 Camera / demo loop  --------------------->  HLS / delayed display
        |                                      |
        | detection substream                  | MediaClock
        v                                      v
 FrameCapture  -> Detector -> Tracker -> ClassifierLock
        |                         |            |
        | DetectionClock          |            |
        +---------------> ClockBridge <--------+
                                  |
                                  v
                             BirdCueStore
                                  |
                                  v
 Browser frame callback -> getCues(mediaTime) -> soft overlay
```

### Key architectural move

The overlay renderer should never consume raw SSE events as immediate visual
instructions. SSE or polling may still transport data, but the transported data
must become timed cues indexed by media time before rendering.

## BirdCue schema

Minimum viable cue:

```json
{
  "schema": "bird-cue.v1",
  "camera": "feeder",
  "track_id": 42,
  "label": "Northern Cardinal",
  "label_state": "locked",
  "confidence": 0.94,
  "media_start": 123.400,
  "media_end": 131.733,
  "source_start_detect_pts": 6329.100,
  "source_end_detect_pts": 6337.433,
  "clock_bridge_id": "feeder-2026-05-11T22:14:00Z",
  "space": "normalized_display",
  "keyframes": [
    {
      "t": 123.400,
      "cx": 0.521,
      "cy": 0.314,
      "w": 0.083,
      "h": 0.121,
      "quality": 0.88
    }
  ]
}
```

Coordinates are normalized to displayed video content, not CSS pixels. The
renderer converts normalized coordinates to canvas or DOM coordinates after
accounting for letterboxing/object-fit.

### Label states

- `unknown`: tracked object exists but no displayable classifier result.
- `candidate`: classifier has a leading label but confidence is below lock.
- `locked`: label is displayable.
- `human_confirmed`: label has been confirmed by human review.
- `retracted`: previous label should no longer be shown.

The live default should show only `locked` and `human_confirmed` cues. Debug mode
may show `candidate` cues.

## ClockBridge design

### Problem it solves

The Pi audit found that the current HLS sidecar timeline and SSE event timeline
can disagree by thousands of seconds. That means a label may be perfectly
tracked and classified but still invisible because the browser is looking at one
time range while events occupy another.

### Required behavior

The bridge records paired observations:

```json
{
  "camera": "feeder",
  "sample_id": 1187,
  "detect_pts": 6329.3606,
  "display_pts": 181.7680,
  "monotonic_ns": 2749810039912,
  "source": "paired-ingest-sample"
}
```

The first implementation can use an affine model:

```
display_pts = detect_pts * scale + offset
```

For a stable camera/demo stream, `scale` should be close to `1.0`. The important
number is offset, but drift must still be measured because reconnects and demo
loops can reset epochs.

### Bridge sources, ranked

1. **Best**: one ingest service observes both streams and records paired
   monotonic arrival/capture samples.
2. **Good**: independent ingest paths publish local monotonic timestamps and a
   calibration worker estimates offset and drift.
3. **Weak**: assume camera PTS epochs match across independent RTSP readers.
   Runtime evidence says this is not safe enough.

## Delayed display model

The system should intentionally delay the display path by a configurable target,
initially 8-12 seconds. The delay has a product purpose: it gives YOLO, tracker,
classifier, smoothing, and cue construction time to settle before the viewer
sees the corresponding frame.

The delay does not need to be exactly 10 seconds forever. It should be a latency
budget:

```
display_delay >= classifier_lock_p99 + cue_transport_p99 + safety_margin
```

Initial target:

- `display_delay_target_s`: `10.0`
- `minimum_usable_delay_s`: `5.0`
- `sync_error_budget_ms`: `100` for label anchor position
- `classifier_lock_budget_s`: measured from live/demo data, not guessed

If the classifier locks after the bird has already entered the scene, cue
generation can backfill the track interval. Delayed playback then makes the
label visible on the correct earlier frames.

## Data flow by responsibility

### Capture and detection

- Decode the detection stream, ideally the camera substream.
- Attach DetectionClock PTS and local monotonic arrival time to each frame.
- Preserve enough frame metadata for ClockBridge calibration.

### Tracking and classification

- Tracker owns object continuity and positions.
- Classifier owns species evidence.
- A track becomes displayable when `ClassifierLock` is reached.
- Smoothing produces stable cue keyframes but does not hide sync errors.

### Cue construction

- Convert track coordinates and time ranges into `BirdCue` objects.
- Apply ClockBridge to convert detection PTS into media/display PTS.
- Emit cues in a bounded rolling window.

### Transport

The first version can keep SSE as the cue transport if Cloudflare/LAN behavior
is understood, but SSE must carry cues, not raw per-frame visual state.

Acceptable transports:

- LAN EventSource for development.
- Polling `bird_cues.json` sidecar for HLS-style robustness.
- HLS timed metadata later, if the HLS stack stabilizes.
- WebSocket later, if Cloudflare Access/SSE remains a problem.

Transport is not the source of truth. Cue timestamps are.

### Browser rendering

On every video frame:

1. Read `mediaTime` from `requestVideoFrameCallback`.
2. Query `BirdCueStore.getCues(mediaTime)`.
3. Interpolate each cue's keyframes to the current media time.
4. Convert normalized video coordinates to overlay pixels.
5. Draw labels if the user toggle is on.
6. Draw boxes/diagnostics only in debug mode.

No render path should draw a label merely because an event just arrived.

## UI behavior

### Normal mode

- Show label text only.
- Anchor label near the bird center/top, with collision avoidance when multiple
  labels overlap.
- Use smoothing from cue keyframes, not CSS transitions that drift independent
  of the video frame.
- Hide labels when no locked cue exists for the displayed media time.

### Debug mode

- Show bounding boxes, track ids, confidence, media time, cue time, bridge
  offset, and cue freshness.
- Include a visible warning when the current media time is outside the CueStore
  window.
- Include a visible warning when ClockBridge drift exceeds threshold.

### Toggle behavior

The label toggle controls rendering only. It must not stop cue ingestion,
tracking, classification, snapshot writing, or health telemetry.

## High-resolution snapshot mapping

The high-res ring buffer remains authoritative for saved images. To crop or
annotate snapshots:

1. Choose the target media/display time from the cue or track event.
2. Use ClockBridge to find the nearest high-res frame/ring entry.
3. Scale normalized cue coordinates into the high-res frame.
4. Save raw high-res frame plus metadata:

```json
{
  "snapshot_schema": "bird-snapshot.v1",
  "camera": "feeder",
  "media_time": 123.400,
  "detect_pts": 6329.100,
  "track_id": 42,
  "label": "Northern Cardinal",
  "bbox_norm": [0.480, 0.253, 0.083, 0.121],
  "clock_bridge_id": "feeder-2026-05-11T22:14:00Z",
  "human_label_state": "pending"
}
```

The snapshot path should not depend on what the browser is currently rendering.

## Implementation slices

### Slice 0: Audit guardrails

Deliverables:

- Add a small timeline probe that samples latest HLS sidecar PTS and latest cue
  or SSE PTS.
- Report `timeline_delta_s`, `clock_bridge_state`, and cue window coverage.
- Fail the overlay health sentinel when the delta is outside budget.

Exit criteria:

- The dashboard can explain "no labels" as a timeline mismatch instead of
  silently drawing nothing.

### Slice 1: Cue model and store

Deliverables:

- Define `bird-cue.v1` JSON.
- Add a server-side cue builder from tracker/classifier events.
- Add browser `BirdCueStore.getCues(mediaTime)`.

Exit criteria:

- A static demo cue file can render labels at known media times in the browser.

### Slice 2: ClockBridge

Deliverables:

- Record paired detection/display samples.
- Estimate offset and drift.
- Convert detection PTS into display media time before cue emission.

Exit criteria:

- In demo mode, cue media times overlap the HLS/media timeline continuously for
  at least 15 minutes.

### Slice 3: Delayed overlay renderer

Deliverables:

- Use `requestVideoFrameCallback` as the only label render loop.
- Render labels from cues, not from event arrival.
- Keep label toggle and debug-box toggle separate.

Exit criteria:

- With the May 10 annotated demo loop, labels are visible on active birds and
  remain attached through movement.

### Slice 4: Snapshot alignment

Deliverables:

- Use cue/track media time to select the high-res ring frame.
- Save snapshot metadata with clock bridge id and normalized bbox.

Exit criteria:

- Snapshot crops match the bird shown in the delayed view.

### Slice 5: Transport hardening

Deliverables:

- Decide between SSE, sidecar polling, WebSocket, or HLS timed metadata for cue
  delivery.
- Keep renderer unchanged by adapting transport into CueStore.

Exit criteria:

- LAN and Cloudflare paths both deliver cues within the delay budget.

## Verification plan

### Timeline sanity

Run for 15 minutes in demo mode:

- latest display/media PTS
- latest raw detection PTS
- latest cue media PTS
- ClockBridge offset and drift
- CueStore coverage around current media time

Pass:

- Cue media window covers displayed media time for at least 99% of sampled
  frames.
- Estimated drift stays below `50ms/min` after warmup.
- Reconnect or demo loop reset creates a new bridge epoch instead of corrupting
  the current one.

### Label visibility

Use the annotated May 10 demo loop:

- For every annotated bird interval longer than `1s`, at least one locked cue
  appears while that interval is displayed.
- Once classifier lock exists, the cue covers the track's visible interval
  according to the delayed display model.

Pass:

- No active locked bird remains unlabeled for more than `500ms` of displayed
  time after the delay budget.

### Spatial accuracy

At sampled frames:

- Interpolated label anchor should be within `100ms` of the displayed frame's
  track position.
- Label anchor should remain inside or adjacent to the projected bbox.

Pass:

- Median anchor error under `5%` of frame width.
- P95 anchor error under `10%` of frame width, excluding detector misses.

### Toggle behavior

Pass:

- Label toggle hides labels without stopping cue ingestion.
- Debug toggle shows/hides boxes independently.
- Video playback continues without reload.

### CPU and thermal budget

Pass for N=1 demo:

- Detection returns to substream or otherwise stays within the agreed per-camera
  CPU budget.
- No high-res per-frame software decode is added solely for labels.
- Pi temperature avoids sustained thermal throttling during the 15-minute test.

## Risks and decisions

### HLS vs WebRTC

WebRTC is not rejected as a transport. It is rejected as the clock authority for
this label problem unless we deliberately delay it and can expose or reconstruct
the presented frame timeline. A low-latency WebRTC view can coexist later as a
"live raw" view, but accurate labels require a delayed cue-aligned view.

### Burned-in labels

Burn-in would make sync visually simple, but it conflicts with toggleable labels
and forces video encoding. On a Pi 5 with current H.264 camera output, that is
the wrong live-path tradeoff. Keep burn-in for offline diagnostics.

### Hardware decode

Raspberry Pi 5 has HEVC decode capability, but current runtime tests showed
H.264 V4L2 M2M decode was not available for the observed stream. The label
architecture must not depend on a hypothetical H.264 hardware decode win.

References:
- <https://www.raspberrypi.com/news/introducing-raspberry-pi-5/>
- <https://forums.raspberrypi.com/viewtopic.php?t=364180>
- <https://forums.raspberrypi.com/viewtopic.php?t=387861>

### SSE through Cloudflare

Prior runtime checks indicated SSE behaved differently over LAN and Cloudflare.
That should influence transport choice, but it should not change the cue model.
Transport failures should degrade cue freshness, not corrupt cue timing.

## Delivery checklist

- [x] Production pattern identified: subtitles/timed graphics, not ad hoc UI.
- [x] Open-source examples mapped to reusable concepts.
- [x] Core vocabulary defined for future docs and code review.
- [x] Cue schema proposed.
- [x] ClockBridge requirement made explicit.
- [x] Delayed display model tied to classifier confidence requirement.
- [x] High-res snapshot path preserved.
- [x] Verification gates defined.

## Recommended next document

Write an implementation plan named:

`docs/working/plans/2026-05-11-spatial-subtitle-overlay-implementation-plan.md`

That plan should implement the slices above in order and should start with Slice
0. Do not begin with UI polish. First make the system prove which timeline each
label belongs to.
