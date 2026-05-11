# Overnight Execution Plan — Pi Bird Observatory Bedrock Restoration + Optimization

**Date:** 2026-05-11 (night)
**Operator:** Claude (autonomous, David has gone to sleep)
**Mandate:** working demo with synced labels at sunrise; optimized + beautiful + scalable as the actual bar; audited every step

## North-star definitions

- **Working** = demo loop plays on iMac LAN; labels render on birds within 500ms of detection event; pipeline stable for ≥15 min without restart; thermals below 80°C sustained; CPU ≤120% sustained
- **Optimized** = all Track B cheap wins landed (threads=1, letterbox pool, snapshot copy elim, INTER_LINEAR), each individually verified
- **Beautiful** = label rendering smooth (Adaptive Lock kernel or CSS transitions), no jank, toggle for labels-vs-boxes works
- **Scalable** = code shape supports N=4 cameras without architectural surgery (single shared decoder pattern in place, even if only one camera attached)
- **Audited** = every phase ends with an independent parallel agent verifying the work matches spec

## Phases (each gated by verification before advance)

### Phase 0: Safety net + baseline (10 min)

- [ ] Git status clean, current branch noted
- [ ] Identify the pre-`1503435` commit hash for reference (cherry-pick source, not full revert)
- [ ] Capture baseline screenshot via Playwright on iMac LAN (`/tmp/dash_probe.py`) → `/tmp/baseline_dash.png`
- [ ] Record baseline: pipeline CPU, top thread CPU, temp, throttle, RAM
- [ ] Confirm services: bird-pipeline, bird-dashboard, bird-demo-loop, go2rtc all active
- [ ] Confirm go2rtc serves `feeder-sub` stream (substream must exist before flip)

**Audit gate 0:** Bash sanity check — every baseline item captured and noted in this plan as evidence.

### Phase 1A: Restore browser live-view per CLAUDE.md (40 min)

CLAUDE.md documents: "Labels rendered client-side as DOM elements with CSS transform transitions, synced via SSE wall_time_ms (no HLS+sidecar smoothing)". The session's HLS rewrite drifted off this.

Approach: rewrite `setupLiveView()` in `dashboard/pi_dash.html` to use go2rtc `<video-stream>` + SSE-driven DOM overlay. **Do NOT touch theme CSS or theme switcher JS** (David's parallel work).

Reference the pre-1503435 state via `git show <hash>:dashboard/pi_dash.html` to see what worked.

- [ ] Read current `setupLiveView()` (~1212-1640 in pi_dash.html)
- [ ] Read pre-1503435 setupLiveView for the working WebRTC + DOM overlay pattern
- [ ] Rewrite setupLiveView: video-stream element + DOM label overlay + SSE listener, no hls.js, no canvas
- [ ] Keep `window.__overlayDebug` hook (used by dash_probe.py)
- [ ] Preserve labels toggle UI
- [ ] Commit: "fix(dashboard): restore WebRTC+DOM overlay live-view per CLAUDE.md"
- [ ] Restart bird-dashboard

**Audit gate 1A — parallel agent:** dispatch reviewer to verify (a) setupLiveView uses video-stream not hls.js, (b) no theme code modified, (c) SSE listener subscribes correctly, (d) DOM overlay positioning math correct.

**Verification gate 1A:**
- [ ] Playwright probe loads dashboard
- [ ] Video element shows non-black frames (videoWidth > 0)
- [ ] Within 30s of page load, at least one `[data-label]` DOM node appears (= label visible)
- [ ] Console clean (no JS errors)

### Phase 1B: Flip FrameCapture to substream (20 min)

- [ ] Read SnapshotWriter to understand bgr_full dependency
- [ ] Edit `bird_pipeline_v3.py:246` from `main_url = CAMERAS_MAIN[name]` to use `CAMERAS_DETECT[name]` for FrameCapture; keep `CAMERAS_MAIN[name]` for HlsSegmenter (it needs main-quality for replay)
- [ ] Read FrameCapture to confirm `bgr_full` semantic — at substream res it's 640×360. SnapshotWriter will save lower-res snapshots tonight. Documented as known-issue, fix tomorrow.
- [ ] rsync to Pi
- [ ] Restart bird-pipeline
- [ ] Commit: "perf(pipeline): switch FrameCapture to feeder-sub per CLAUDE.md"

**Audit gate 1B — parallel agent:** dispatch reviewer to verify (a) one-line minimal change, (b) HlsSegmenter still uses main, (c) snapshots gracefully degrade not crash.

**Verification gate 1B:**
- [ ] Pipeline service active (no restart loop)
- [ ] Journal log shows "PyAV stream open: 640x360 codec=h264" for FrameCapture
- [ ] Journal log shows "segmenter input open: 1920x1080" for HlsSegmenter
- [ ] CPU drops from ~213% to ~110% within 60s
- [ ] Temp drops from 84°C to <75°C within 5 min
- [ ] SSE events still flow (curl test)

### Phase 2: Track B optimizations (90 min, 4 commits)

Each optimization is its own commit. Each gets independent verification.

#### Phase 2A: `threads=1` on PyAV decoders (15 min)

- [ ] Add `options={"threads": "1"}` (or codec_context tweak) to FrameCapture `av.open()` call
- [ ] Same for HlsSegmenter
- [ ] Verify locally that diff is small
- [ ] rsync + restart pipeline
- [ ] Commit: "perf(pipeline): pin PyAV decoder threads=1 to free slice workers"

**Verification 2A:** thread count in `top -H -p PID` shows fewer worker threads; total CPU drops further; no decoder errors.

#### Phase 2B: HailoDetector letterbox buffer pool (20 min)

- [ ] Read `pipeline/hailo_detector.py:114-128` `_letterbox`
- [ ] Preallocate 640×640×3 uint8 buffer in `__init__`
- [ ] Reuse it via `cv2.copyMakeBorder(..., dst=preallocated)` or direct slice fill
- [ ] BGR→RGB in-place via `cv2.cvtColor(..., cv2.COLOR_BGR2RGB, dst=...)`
- [ ] rsync + restart
- [ ] Commit: "perf(hailo): preallocate letterbox buffer to eliminate per-frame allocation"

**Verification 2B:** detection still works (snapshots created on visit); no exceptions in journal; memory growth flat over 5 min.

#### Phase 2C: SnapshotWriter eliminate defensive copies (15 min)

- [ ] Read `pipeline/snapshot_writer.py:197` — confirm `frame.bgr_full.copy()` is defensive
- [ ] Remove the .copy() (the producer doesn't mutate after put_nowait)
- [ ] Remove redundant `p['frame'].copy()` in encode path
- [ ] Single JPEG encode reused for raw + annotated paths if both still produced
- [ ] rsync + restart
- [ ] Commit: "perf(snapshot): eliminate defensive frame copies; reuse JPEG bytes"

**Verification 2C:** snapshots still write on lock; RSS lower or stable.

#### Phase 2D: INTER_LINEAR resize if still needed (10 min)

After 1B, substream is 640×360 native — most resize gone. But check process_thread for any remaining resize and switch INTER_AREA→INTER_LINEAR.

- [ ] grep INTER_AREA in pipeline/
- [ ] Replace with INTER_LINEAR for non-display resizes
- [ ] rsync + restart
- [ ] Commit: "perf: INTER_LINEAR for detect-path resize"

**Verification 2D:** detection still hits on demo loop birds (Hailo not regressing).

**Audit gate 2 — parallel agent:** dispatch reviewer to verify all four optimizations are correct, atomic, and don't break the pipeline.

### Phase 3: Demo loop end-to-end (30 min)

- [ ] Confirm bird-demo-loop.service is feeding `rtsp://localhost:8654/feeder-main`
- [ ] Confirm `PIPELINE_TEST_RTSP_URL` env still set on bird-pipeline
- [ ] Run Playwright probe for 60s recording: how many tracks detected, how many labels rendered, smoothness
- [ ] Take screenshot showing labels on birds
- [ ] 15-min stability test: pipeline doesn't crash/restart, CPU stable, temp stable

**Verification 3:**
- [ ] Demo loop visible in browser
- [ ] At least 3 distinct labels appear over the 15-min test (matching annotations)
- [ ] No service restarts during the 15-min test
- [ ] Final temp ≤ 75°C
- [ ] Final CPU ≤ 120%

**Audit gate 3 — parallel agent:** dispatch reviewer to interpret Playwright results + verify against `may10_demo_video.annotations.md`.

### Phase 4: Beauty + capability (60 min if time allows)

Stretch goals if time remains before sunrise:

- [ ] Smooth label motion: port Adaptive Lock kernel or use CSS transitions properly
- [ ] Labels-only mode (toggle to hide boxes, show only species text)
- [ ] Confidence-gated rendering: only show locked tracks by default
- [ ] Add diagnostic chip with bridge offset + cue freshness (per Codex's spec hint)

### Phase 5: Sunrise handoff (15 min)

- [ ] Write `docs/working/progress/2026-05-11-overnight-result.md` summarizing what was done, what works, what's pending
- [ ] Update todos
- [ ] Take final dashboard screenshot
- [ ] Leave one-paragraph TL;DR at top for David's first read

## Audit & verification methodology

Each phase has TWO gates:
1. **Audit gate:** independent parallel agent reviews the code change against the phase's spec
2. **Verification gate:** I run real commands (Bash, SSH, Playwright) and check measured outcomes

**Verification rule (from `verification-before-completion`):** I do not mark a phase complete without fresh evidence from a real command in the same flow. No "should work." No previous-output recall.

## Failure escalation

If a phase fails verification:
1. Diagnose: read logs, check journal, run smaller test
2. Attempt single repair
3. If still failing: rollback that phase's commit, document the failure, move to next phase if independent
4. If blocking: stop, write status to handoff doc, do NOT continue stacking changes

## Rollback safety

Every phase is its own git commit. Each commit reverted independently if needed. Pi gets rsynced from working tree, so reverting + rsync is enough.

## What I will NOT do tonight

- Implement Codex's full spatial-subtitle architecture (that's the multi-day bedrock plan, not the overnight)
- Touch theme CSS / theme switcher JS in pi_dash.html
- Modify dashboard panels other than live-view
- Push uncommitted code to Pi
- Skip a verification gate
- Claim completion without evidence
- Fight the Pi if a phase blocks — escalate to handoff doc and continue with what's independent

## Skills I am using

- `verification-before-completion` (the iron law)
- `subagent-driven-development` (for audit gates; me-as-implementer for tight-context tasks)
- `dispatching-parallel-agents` (for independent audits per phase)
- `executing-plans` (this document is the spec being executed)
- `debugging` (when verification fails)
