# 2026-05-12 Codex Live Log

Purpose: running, backfilled log for the Pi 5 bird observatory takeover. This is the audit trail for what changed, why it changed, what was verified, and what is next.

## Backfill

### Takeover Baseline

- Accepted `/Users/vives/bird-classifier-pi` as the canonical repo and `vives@pi5.local:/home/vives/bird-classifier/` as the live runtime target.
- Re-read the local infrastructure docs before SSH/runtime work:
  - `/Users/vives/docs/_index.md`
  - `/Users/vives/docs/bird-observatory-pi/02-services.md`
  - `/Users/vives/docs/bird-observatory-pi/03-pipeline.md`
  - `/Users/vives/docs/bird-observatory-pi/10-overlay-sync.md`
- Captured the operating goal: labels must appear, be toggleable, stay on the bird, and remain synchronized with the displayed video. Bounding boxes are debugging scaffolding, not the intended final UI.
- Wrote `/Users/vives/bird-classifier-pi/docs/working/progress/2026-05-12-codex-takeover-control.md` and committed it as `4a3e1f7 docs: codex takeover control note`.

### Label Events Over Cloudflare

- Found that remote Cloudflare access was a poor fit for the existing SSE label-event path.
- Added a WebSocket mirror for parsed pipeline SSE events at `/api/pipeline/events/ws`.
- Kept LAN behavior on SSE and used WebSocket for Cloudflare-origin dashboard sessions.
- Added regression coverage in `tests/test_pipeline_events_ws.py`.
- Verified focused tests on the Pi.
- Committed as `0aeed01 fix(dashboard): mirror label events over WebSocket`.

### Same-Origin Video

- Found that the dashboard was loading WebRTC signaling and runtime JS in ways that were fragile behind Cloudflare/browser security boundaries.
- Vendored/exposed the VideoRTC wrapper through the dashboard as `/video-stream.js`.
- Proxied WebRTC signaling same-origin via `/api/ws`.
- Added `feeder-demo` to the allowed stream set and surfaced `video:` diagnostics in the sync diagnostics panel.
- Added regression coverage in `tests/test_dashboard_live_video_proxy.py`.
- Verified focused tests on the Pi.
- Committed as `4ca690e fix(dashboard): serve remote video same-origin`.

### Embedded Browser Visibility Fix

- Found that the Codex in-app browser could create the video element and then let the stream disappear after tab/visibility lifecycle behavior.
- Changed `dashboard/video-stream.js` so `background` mode and `visibilityCheck = false` are set before `super.oninit()`.
- Cache-busted the dashboard script URL with `v=20260512-visibility`.
- Extended the test to prove initialization order.
- Verified on the Pi:
  - `tests/test_dashboard_live_video_proxy.py tests/test_pipeline_events_ws.py` passed.
  - `https://pi5.vivessato.com/?syncdiag=1&cb=visibility-fix-20260512T0230` showed sustained `video: 4 640x360`, ~30 Hz events, visible labels, and no console warnings/errors across repeated samples.
- User confirmed video works at least for a while in normal browsers; Codex in-app browser remains a smoke-test surface, not the acceptance browser.
- Committed as `354c1eb fix(dashboard): keep embedded video connected`.

## Current State

- Label transport is no longer the immediate blocker.
- Remote video visibility is no longer the immediate blocker.
- The next risk is the snapshot/classification capture path:
  - Current docs/code say the pipeline uses one 640x360 decoded substream frame.
  - `bgr_full` is currently the same low-resolution buffer as `bgr`.
  - The historical `HiResRingBuffer` still exists but is not wired into `bird_pipeline_v3.py`.
  - Snapshot stats can make the path look healthier than it is because `hires_ok` can mean "used the provided frame", not "true high-res frame was used".

## Active Objective

Restore a credible high-resolution, time-aligned snapshot path without reintroducing unnecessary continuous 1080p decode load on the Pi.

Working preference:

1. Prove the current live behavior from code and runtime evidence.
2. Make health/stat reporting honest if it currently conflates low-res and high-res frames.
3. Prefer event-rate high-res extraction tied to a timestamped source over continuous high-res decode.
4. Keep label/video sync stable while changing snapshot internals.

## Investigation: Snapshot Resolution

### Runtime Evidence

- `http://localhost:8100/api/pipeline/health` on the Pi reported:
  - `snapshot_writer.submitted=14`
  - `snapshot_writer.written=14`
  - `snapshot_writer.hires_ok=14`
  - `snapshot_writer.hires_fail=0`
- The latest classified JPGs on the Pi were all `640x360`.
- The latest annotated JPGs on the Pi were all `640x360`.
- The pipeline log shows the current split clearly:
  - `pipeline.frame_capture`: `PyAV stream open: 640x360 ...`
  - `pipeline.hls_segmenter`: `segmenter input open: 1920x1080 ...`

### Code Evidence

- `bird_pipeline_v3.py` wires `FrameCapture` to `detect_url = CAMERAS_DETECT[name]`, currently `rtsp://127.0.0.1:8554/feeder-sub`.
- `FrameCapture._handle_frame()` sets `bgr_full = av_frame.to_ndarray(...)`; if the decoded stream is already `640x360`, `bgr_detect` and `bgr_full` are the same frame size.
- `SnapshotWriter._write_one()` treats any non-`None` `hires_frame` as `hires_ok`, rescales the bbox, and writes it. That is honest for a true main-stream frame, but misleading when the input stream is the low-res substream.
- `HlsSegmenter` is already demuxing the main stream and writing `~/bird-snapshots/hls/feeder/segments.json` with `pts_start` / `pts_end` for 1920x1080 `.ts` segments.

### Current Root Cause

`hires_ok` currently means "a frame was supplied in the `frame_bgr_full` slot", not "a true high-resolution frame was saved." Because the active capture stream is `feeder-sub`, the supplied full frame is still `640x360`.

### Current Hypothesis

The lowest-load path to real high-res snapshots is to extract one frame from the already-running 1920x1080 HLS segment that covers the lock-time PTS, instead of decoding a separate 1080p ring continuously or asking go2rtc for a current keyframe after the bird has moved.

## Implementation: HLS-Backed High-Res Snapshots

- Added `SnapshotWriter(hls_root=..., hls_wait_timeout_s=...)`.
- Added `_locate_hls_segment(camera, pts)`:
  - Reads `~/bird-snapshots/hls/<camera>/segments.json`.
  - Finds the segment whose `pts_start <= pts <= pts_end`.
  - Returns the segment path and relative offset within that segment.
- Added `_fetch_hls_frame_for_pts(camera, pts)`:
  - Returns immediately if PTS is missing/invalid or the sidecar does not exist.
  - Otherwise waits briefly for the segmenter to close the segment containing the lock-time PTS.
  - Extracts one frame with ffmpeg from the finalized `.ts` segment.
- Changed `SnapshotWriter._write_one()`:
  - Uses inline `frame_bgr_full` only when it is truly larger than the detector frame.
  - If inline "full" frame is still detector-sized, tries HLS extraction by PTS.
  - Counts `hires_ok` only for true high-res frames.
  - Adds counters: `hires_inline_ok`, `hires_hls_ok`, `hires_hls_miss`, `hires_lowres_fallback`.

## Verification: HLS Snapshot Path

- Wrote failing tests first in `tests/pipeline/test_snapshot_writer_hls.py`.
- Red failure: `SnapshotWriter.__init__()` did not accept `hls_root`.
- Implemented the HLS lookup/extract path.
- Pi focused suite:
  - `tests/pipeline/test_snapshot_writer_hls.py`
  - `tests/pipeline/test_snapshot_writer_rc3.py`
  - `tests/pipeline/test_hires_ring.py`
  - `tests/test_dashboard_live_video_proxy.py`
  - `tests/test_pipeline_events_ws.py`
  - Result: `28 passed, 4 warnings`.
- Live helper probe on Pi:
  - Extracted a real frame from current `~/bird-snapshots/hls/feeder/segments.json`.
  - Result shape: `(1080, 1920, 3)`.
  - Runtime: about `0.764s`.
- Restarted `bird-pipeline`.
- Post-restart health:
  - Service active.
  - Detect stream reopened at `640x360`.
  - HLS segmenter reopened at `1920x1080`.
  - New snapshot counters present in `/api/pipeline/health`.
- Waited 150s for a natural lock; no new lock occurred, so no live production snapshot was available for inspection in that window.
- Ran a non-destructive end-to-end writer probe using:
  - Real HLS segment extraction.
  - Temporary classified/annotated roots.
  - Fake DB insert.
  - Detector-sized inline frame.
  - Result:
    - raw JPG `1920x1080`
    - annotated JPG `1920x1080`
    - bbox scaled from `[100, 100, 300, 300]` to `[300.0, 300.0, 900.0, 900.0]`
    - `hires_hls_ok=1`

### User Confirmation and Demo Boundary

David clarified that the prior "can't confirm" message was a typo: the live pipeline high-res snapshot behavior can be confirmed. The separate caveat is that the demo source is low-resolution, so demo-mode snapshots are expected to be low-resolution and should not be used as the acceptance surface for high-res capture.

What is verified:

- The deployed writer path can locate an HLS segment by PTS.
- The deployed writer path can extract a high-resolution frame when the HLS source is high-resolution.
- The deployed writer path correctly scales the bbox and writes high-resolution raw/annotated JPGs in an end-to-end probe.
- Live pipeline high-res snapshots are confirmed outside the low-res demo context.

What remains true:

- If the demo overrides both detect and main/HLS streams to the same low-res file, the HLS-backed path can only produce a frame at the demo file's resolution.

High-res acceptance surface:

- Real camera / real high-res main stream, not the current low-res demo loop.

## Plan: Live Label Sync After Snapshot Fix

- Saved detailed plan at `/Users/vives/bird-classifier-pi/docs/superpowers/plans/2026-05-12-live-label-sync-plan.md`.
- Remaining path:
  1. Add video/event sync telemetry.
  2. Add an event buffer and browser-side clock bridge behind a flag.
  3. Add interpolation/prediction so labels stay attached during motion.
  4. Reuse the annotated demo for replay/timing gates.
  5. Tune label-only UX after boxes are no longer needed for debugging.
  6. Accept on real camera and Cloudflare/LAN browsers separately.

## Implementation: Sync Telemetry Slice

- Added `tests/test_dashboard_sync_diagnostics.py`.
- Red test confirmed the dashboard did not expose `requestVideoFrameCallback`/sync telemetry yet.
- Added diagnostic-only browser metrics in `dashboard/pi_dash.html`:
  - `lastVideoMediaTime`
  - `videoFrameHz`
  - `lastEventPts`
  - `eventAgeMsRough`
  - `clockDeltaMs`
- Exposed the metrics in the `?syncdiag=1` chip and `window.__overlayDebug.sync`.
- No label placement behavior changed in this slice.

## Verification: Sync Telemetry Slice

- Pi test command:
  - `./venv/bin/python -m pytest tests/test_dashboard_sync_diagnostics.py tests/test_dashboard_live_video_proxy.py -q`
  - Result: `5 passed, 4 warnings`.
- Browser smoke:
  - URL: `https://pi5.vivessato.com/?syncdiag=1&cb=sync-telemetry-20260512`
  - Diagnostic text included:
    - `video: 4 1920x1080`
    - `vclock: 15.808s @ 24.3fps`
    - `evt pts: n/a delta: n/a`
  - Browser console warnings/errors: none.
- No active track events appeared during the smoke sample, so event/video delta remained `n/a`. That is expected until a bird/track event arrives.

## Fix: Demo/Live Switching Without Manual Refresh

David clarified the actual requirement: the dashboard should switch between live and demo in-place. If a full page refresh is required, it should happen automatically, not be left to the user.

Root cause:

- `dashboard/pi_dash.html` changed the `<video-stream>` source by assigning `video.src`.
- `VideoRTC.src` updates `wsURL` and calls `onconnect()`.
- `VideoRTC.onconnect()` returns early if an existing WebSocket or PeerConnection is still alive.
- Result: the dashboard state could flip to live/demo while the old media session kept playing until a manual refresh destroyed it.

Fix:

- Added `reconnectLiveVideo(next)` in `dashboard/pi_dash.html`.
- On source changes, it clears any pending reconnect timer, calls `video.ondisconnect()` to close the existing VideoRTC session, then assigns the new same-origin `/api/ws?src=...` URL.
- No `location.reload` path was added.

Verification:

- Added regression coverage in `tests/test_dashboard_live_video_proxy.py`.
- Red test failed before the fix because the reconnect helper did not exist.
- Pi tests:
  - `tests/test_dashboard_live_video_proxy.py tests/test_dashboard_sync_diagnostics.py tests/test_pipeline_events_ws.py`
  - Result: `8 passed, 4 warnings`.
- Runtime switch smoke with terminal Playwright on `http://pi5.local:8099/?syncdiag=1&cb=switch-test-20260512`:
  - Before switch: page URL unchanged, `#live-video.wsURL = ws://pi5.local:8099/api/ws?src=feeder-main`.
  - After enabling demo via `/api/demo-mode`: same page URL, `#live-video.wsURL = ws://pi5.local:8099/api/ws?src=feeder-demo`, UI label `demo mode`.
  - After disabling demo: same page URL, `#live-video.wsURL = ws://pi5.local:8099/api/ws?src=feeder-main`, UI label `live`.
- Headless Playwright did not produce usable video dimensions for the WebRTC element, so this smoke verifies the in-page source switch/reconnect machinery, not visual WebRTC playback.

## Fix: Reconnect Guard Regression

David reported that after the source-switch fix, video persisted briefly, went black, came back, and repeated on both local and remote dashboard URLs. This matched a reconnect loop.

Root cause:

- `reconnectLiveVideo(next)` guarded on `next === currentSrc && video.src === url`.
- The `<video-stream>` custom element defines a `src` setter but no corresponding getter.
- Reading `video.src` therefore does not reliably return the current WebSocket URL.
- The 5-second demo/live poll kept calling `reconnectLiveVideo()` for the same source and repeatedly ran `video.ondisconnect()`.

Fix:

- Changed the guard to `if (next === currentSrc) return;`.
- Added regression coverage that forbids the broken `video.src` getter guard.

Verification:

- Red test failed against the deployed runtime before the fix.
- Pi tests:
  - `tests/test_dashboard_live_video_proxy.py tests/test_dashboard_sync_diagnostics.py tests/test_pipeline_events_ws.py`
  - Result: `8 passed, 4 warnings`.
- Browser automation check on `http://pi5.local:8099/?syncdiag=1&cb=reconnect-guard-20260512`:
  - Wrapped `#live-video.ondisconnect` with a counter.
  - Waited 12 seconds with the same selected source.
  - `disconnects` stayed `0`.
  - Current source during the check was `feeder-demo`; diagnostic video field showed `video: 4 640x360`.

User-eye check still needed:

- Reload the dashboard once to pick up the patched HTML.
- Confirm `/?syncdiag=1` no longer cycles black every few seconds in normal local and remote browsers.

## Fix: Demo/Live Recent Classification Routing

Date: 2026-05-15.

David reported that switching the Live view back from demo to live still left the Recent Classifications strip showing demo-loop rows. He also observed that live labels/bboxes were not visible after the first few seconds.

Findings:

- Live video was healthy during the audit (`video: 4 1920x1080`), but pipeline health reported `active_tracks: 0` and no current label events. So the lack of boxes at that moment was a detector/tracker/no-active-track condition, not evidence that the browser overlay renderer was broken.
- Recent classifications had a concrete data-routing bug: `dashboard/pi_review.py` always read `~/bird-snapshots/logs/classifications.db`.
- Demo rows written during the long demo interval were in the same DB as live rows and had no source-mode boundary, so live mode kept showing newer demo rows until live produced enough newer snapshots.

Fix:

- Added `classifications_db.resolve_db_path()`.
  - Default live writes go to `classifications.db`.
  - Pipeline processes with `PIPELINE_TEST_RTSP_URL` set now write to `classifications_demo.db`.
  - `PIPELINE_CLASSIFICATIONS_DB` remains an explicit override for tests/ops.
- `SnapshotWriter` now records `source_mode` and `source_stream` in `extra_json`.
- `GET /api/pi-review/recent` and `/stats` now accept `mode=live|demo` and read the matching classifications DB.
- Review verdict writes carry `source_mode`.
- The dashboard tracks `reviewMode`, requests recent/stats for the active demo/live mode, and refreshes that strip immediately when demo state changes.
- Source switches now clear stale overlay DOM nodes and event counters, so demo boxes/counters do not linger after switching video sources.

Data migration:

- Backed up:
  - `~/bird-snapshots/logs/backups/classifications.db.20260515-live-demo-split.bak`
  - `~/bird-snapshots/logs/backups/pi_reviews.db.20260515-live-demo-split.bak`
- Moved known demo-period rows from `classifications.db` to `classifications_demo.db`.
  - Live DB after migration: 31,861 rows.
  - Demo DB after migration: 15,817 rows.

Verification:

- Red tests failed before implementation:
  - missing `classifications_db.resolve_db_path`
  - missing `dashboard.pi_review.DEMO_CLASSIFICATIONS_DB_PATH`
  - missing overlay-state reset.
- Pi test command:
  - `./venv/bin/python -m pytest tests/test_demo_mode_classifications_routing.py tests/test_dashboard_live_video_proxy.py tests/pipeline/test_snapshot_writer_rc3.py tests/test_classifications_db.py -q`
  - Result: `32 passed, 4 warnings`.
- Backend endpoint checks:
  - `mode=live` returned current live rows such as `feeder_2026-05-15_10-27-25_1.jpg`, `feeder_2026-05-15_10-14-26_2.jpg`, and `feeder_2026-05-15_10-12-17_1.jpg`.
  - `mode=demo` returned the demo-loop `_644xx` rows.
- Browser automation:
  - In live mode, Recent Classifications showed live rows at the top.
  - Switching to demo without refresh changed the strip to demo rows.
  - Switching back to live without refresh changed the strip back to live rows.
  - Final system state was left in live mode with demo loop inactive.

Open follow-up:

- Live labels/bboxes only appear when the pipeline has active tracks. At the time of this audit the live pipeline was healthy but had `active_tracks: 0`. If birds are visibly present while health still reports zero active tracks, the next investigation is detector/tracker/motion-gate sensitivity and snapshot-lock coverage, not the dashboard renderer.

## Fix: Pi AIY raw-score normalization

Date: 2026-05-15.

David reported that live boxes were present at least some of the time, but labels were often improbable species such as Northern Flicker, grackles, and blue jays when the feeder birds were mostly finches with one chickadee.

Findings:

- Recent live `classifications.db` rows showed lock-time labels like `Northern Flicker`, `Chipping Sparrow`, `American Tree Sparrow`, and `Red Crossbill` at confidence `1.0`.
- The same rows' saved-frame authoritative pass often disagreed and usually preferred `House Finch` or `House Sparrow` at modest confidence.
- Re-running the classifier on the 1080p saved crops confirmed that those crops did not support the live `1.0` labels.
- Root cause in `pipeline/pi_classifier.py`: AIY/Hailo registry `raw_score` values are integer 0-255, but the code normalized with `raw / 255 if raw > 1 else raw`. That treats integer `raw_score == 1` as confidence `1.0` instead of `1/255`, allowing the weakest nonzero AIY ties to become perfect live votes and lock bad species.

Fix:

- Added `_normalize_raw_score()` in `pipeline/pi_classifier.py`.
- Integer raw scores, including `1`, now always divide by `255`.
- Float scores already in `[0, 1]` are still accepted as normalized values.

Verification:

- Added regression tests in `tests/pipeline/test_pi_classifier.py`.
- Red test on Pi before implementation:
  - `raw_score=1` produced `ClassificationResult(species='Northern Flicker', confidence=1.0, ...)`.
- Green tests after implementation:
  - `./venv/bin/python -m pytest tests/pipeline/test_pi_classifier.py -q`
  - Result: `3 passed`.
  - `./venv/bin/python -m pytest tests/pipeline/test_pi_classifier.py tests/pipeline/test_process_thread.py tests/pipeline/test_snapshot_writer_rc3.py -q`
  - Result: `20 passed`.
- Restarted `bird-pipeline.service`.
- Post-restart backend health was `overall: ok`, frames advanced, and no drops/restarts were reported.
- Watched the live pipeline for 60 seconds after restart. No detections occurred during that window, so there were no new live classification rows to validate against real birds.

User-eye check still needed:

- When birds are visibly in frame, confirm labels no longer jump to high-confidence unlikely species.
- If birds are visible but `detections_total` and `active_tracks` stay flat, the next backend investigation is detector/tracker sensitivity rather than classifier voting.
