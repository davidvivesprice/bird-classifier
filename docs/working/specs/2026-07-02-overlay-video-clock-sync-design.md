# Consolidated timing audit + sync & verification design — Pi live bird overlay

Audience: implementing engineer. Sources: 4 sub-audits (client overlay, iMac reference, Pi server path, browser video-clock primitives). Confidence markers: **[M]** measured on this system, **[S]** source-verified (Chromium/WebKit/go2rtc/local code), **[E]** estimate from typical figures — unmeasured here.

---

## 0. The invariant (read first)

The entire design reduces to the iMac lesson re-based to a new shared clock:

> **Labels are a pure function of T = the camera-clock time of the video frame currently on glass.**

On the iMac the shared clock was one OS clock (SSE `wall_time_ms` vs HLS-segment mtime). On the Pi rig the shared clock is the **camera's 90 kHz RTP timeline**, and it is already plumbed end-to-end **[S]**:

- go2rtc passes camera RTP timestamps through **unmodified** to WebRTC (`pkg/webrtc/track.go WriteRTP` rewrites SSRC/seq/PT, never Timestamp) and builds the MSE fMP4 timeline purely from RTP deltas rebased to 0 (`pkg/mp4/muxer.go`).
- Browser: rVFC `metadata.rtpTimestamp` = raw camera RTP ts on Chrome **[S: Chromium `media_stream_remote_video_source.cc`]** and WebKit **[S: `RealtimeIncomingVideoSource.cpp`, device-unverified]**; on MSE, rVFC `metadata.mediaTime` = camera PTS rebased (exact by spec + muxer construction).
- Pipeline: SSE `pts` = PyAV `frame.time` from the same RTSP timeline (session-rebased) — `pipeline/sse_events.py` docstring already declares pts canonical.

Both sides ride the same camera crystal → **zero drift**; only per-session epoch constants need calibration. Cross-machine NTP (iPad clock skew) becomes structurally irrelevant, same as the iMac one-clock trick.

**Today's code does none of this**: labels apply at SSE-arrival time (`pi_dash.html:1616-1638`), the only video-clock read is `mediaTime` (meaningless on Chrome WebRTC — it's local playout elapsed since first frame, arbitrary epoch), smoothing is a retriggered 240 ms CSS transition, and no instrument measures label-vs-glass offset at all.

**Do NOT use**: `mediaTime` on Chrome WebRTC (local elapsed) • `wall_time_ms` for any sync decision (cross-machine skew; docstring already forbids it) • `estimatedPlayoutTimestamp` (effectively Firefox-only) • Safari `captureTime` (unconverted clock domain in WebKit source) • file-path replay (`PIPELINE_TEST_RTSP_URL=/path.mp4`) for anything timing-related (decodes at ~16× realtime, 90% frame drops — **[M]**, this produced the "128 events in 2.5 s" trace).

---

## 1. End-to-end timing map

All times relative to camera exposure of frame F. The camera→go2rtc leg is **common-mode** (shared by both paths) and cancels entirely once both sides are keyed to the camera timeline — it only matters for absolute glass latency.

### 1a. Label path (camera → pipeline → SSE → browser JS)

| # | Hop | Latency | Evidence |
|---|-----|---------|----------|
| L1 | Camera encode (640×360 sub) + LAN → go2rtc ingest | ~50–150 ms **[E]** | common-mode with video path |
| L2 | go2rtc RTSP re-serve → PyAV demux reorder | 0–100 ms (hard cap: `max_delay=100000`) **[S]** | `frame_capture.py:119-126` |
| L3 | Decode + BGR convert → **pts/wall stamp** | 5–15 ms **[E]** — stamped AFTER decode, so decode cost never appears as label offset | `frame_capture.py:186-210` |
| L4 | frame_q handoff (2-slot, drop-oldest) | 0–66 ms; 2.5% dropped-oldest **[M]** | `bird_pipeline_v3.py:263` |
| L5 | Hailo YOLO detect | 18 ms avg / 19 ms p99 **[M]** | health endpoint |
| L6 | Norfair + classify + emit | 2–10 ms **[E]**; emit <1 ms | `process_thread.py:137-199` |
| L4–L8 | **Total decode→SSE-delivery, localhost** | **p50 34 / p95 60 / max 101 ms [M]** | 30 s probe, direct :8105 |
| L9 | Dashboard proxy :8099 | +1–5 ms normal; **one 2.52 s stall in 45 s [M]**; upstream 32-slot queue = only ~1.1 s @ 30 ev/s, drops silent | `api.py:5086-5114` |
| L10 | LAN SSE +1–5 ms; Cloudflare WS bridge +30–100 ms **[E]**, burst-prone | | `api.py:5154-5175` |

**Label for camera-time T is in browser JS at ≈ T + 150–400 ms (LAN), T + 200–500 ms (tunnel).** Event rate: 29.6/s **[M]** (one event per processed frame with active tracks; 98.2% of frames during demo).

### 1b. Video path (camera → go2rtc → browser glass)

| # | Hop | Latency | Evidence |
|---|-----|---------|----------|
| V1 | Camera encode (1080p main, or demo stream) + ingest | ~50–150 ms **[E]** (common-mode) | |
| V2 | go2rtc repacketize (no transcode) | <5 ms **[S]** | `track.go`, `rtp.go` |
| V3a | **Chrome WebRTC**: jitter buffer | 30–150 ms LAN default, adaptive **[E]**; controllable 0–4000 ms via `receiver.jitterBufferTarget` (Chrome 124+, a hint) **[S]**; measurable as Δ`jitterBufferDelay`/Δ`jitterBufferEmittedCount` | |
| V3b | **MSE (iPad tunnel + pre-WebRTC window)**: mux <5 ms + WS/tunnel 30–100 ms + SourceBuffer gap | **~1.0 s equilibrium [S-derived]**: `playbackRate = gap` law → d(gap)/dt = 1−gap → stable at 1.0 s; rate varies continuously | `video-rtc.js:475-479` |
| V4 | Decode | 2–10 ms; measurable `totalDecodeTime/framesDecoded` | |
| V5 | Composite → glass | ≤1–2 vsync (8–33 ms); reported exactly by rVFC `expectedDisplayTime` | |

**Frame F displays at ≈ T + 100–350 ms (Chrome WebRTC default), ≈ T + 1.1–1.4 s (MSE via tunnel).** Anchor exactness: `rtpTimestamp` = camera RTP, exact; MSE `mediaTime` = camera PTS rebased, exact.

### 1c. Lookahead budget (the number the whole design keys on)

Lookahead = (video display latency) − (label availability latency), i.e., how far past the displayed moment the event buffer extends:

| Config | Lookahead | Consequence |
|---|---|---|
| Chrome WebRTC, default buffer | **−300 … +200 ms** — a race; label for the displayed frame often hasn't arrived | past-only rendering; no symmetric smoothing |
| Chrome WebRTC, `jitterBufferTarget=800 ms` | **+400 … +800 ms** | bracketing interpolation always; symmetric kernels (retuned σ) mostly |
| iPad MSE via tunnel | **+600 … +1200 ms** (free, from the playbackRate=gap law) | full smoothing; the buffer becomes the feature |
| Safari WebRTC (non-target) | −300 … +200 ms, **no knob exists** | past-only, permanently |

---

## 2. Ranked actual lag sources

1. **No label↔video clock alignment at all (client).** Labels render at network arrival; video at jitter-buffered playout. Offset −300…+200 ms on Chrome LAN, up to **−1.4 s on MSE**, time-varying. `pi_dash.html:1616-1638`. THE bug; everything else is second order.
2. **Wrong anchor primitives available to a naive fix.** `mediaTime` on Chrome WebRTC = local elapsed (silent 50–200+ ms error + arbitrary epoch); `wall_time_ms` matching = full receiver-pipeline latency + NTP skew swallowed. `pi_dash.html:1500`, iMac-port pattern `live.html:319`.
3. **Demo-rig split-brain (server).** Browser plays local `sim/current.mp4` exec loop; pipeline analyzes the NAS loop — two unrelated clocks/loop phases → unbounded drifting offset. **Invalidates every demo-based sync observation until fixed.** `api.py:5300` vs `go2rtc.yaml`; comments at `api.py:5297-5298` and `pi_dash.html:1355-1356` are stale.
4. **MSE ~1.0 s self-regulating buffer** — dominant on the iPad path; with clock keying it flips from bug to free lookahead. `video-rtc.js:475-479`.
5. **Chrome WebRTC jitter buffer** 30–150 ms, adaptive → variable offset when un-keyed; pure (controllable) latency when keyed.
6. **240 ms CSS transition chase**: steady-state trail ~30 ms at 30 Hz (+~16 ms mean event staleness ≈ 46 ms vs newest pipeline position); **95 ms to 90% / 240 ms to 100% on transients and size changes**. Becomes pure added lag under a per-frame interpolator → remove. `pi_dash.html:310-313, 333-334`.
7. **Ghosting**: client GC 1.5–2.1 s after last event (`pi_dash.html:1594-1614,1822`) + server Norfair coasting ~5 s (hit_counter_max 150) emitting drifting boxes. Most visible "wrongness" after the clock fix.
8. **Label availability latency** (150–400 ms LAN): after the fix this is NOT an offset (events carry pts) — it sets the minimum video buffer needed for lookahead.
9. **Event transport pathologies**: proxy 2.52 s stall **[M]**; Cloudflare buffers SSE outright (WS bridge exists for this); 32-slot queue = silent permanent loss for any client stalled >1.1 s; WS bursts defeat per-event rendering (structurally fixed by buffer+loop design).
10. **Render quantization**: style writes outside rAF → 0–16.7 ms vsync + rVFC callback misses under main-thread load (frame still displays; recompute-per-callback handles it).
11. **Epoch resets (failure mode, not steady lag)**: pipeline capture restart resets pts epoch; transport flip (WebRTC↔MSE, silent in video-rtc); RTP discontinuity (camera reboot, UniFi token resync — the June audio lesson); 32-bit RTP wrap every 13.25 h. Without auto re-anchor, "perfect sync" silently becomes seconds-off.
12. Minor: nighttime pause kills evening demos in ~10 s (demo API doesn't set `PIPELINE_NIGHT_BYPASS`) • per-track `getBoundingClientRect` layout thrash • 5 s demo-mode poll window.

---

## 3. Recommended sync design

### 3.1 Displayed-frame camera time, per transport

Run one rVFC-driven clock module against the inner `<video>` (`videoEl.video`); re-register on element/src change (fix the never-reset `videoFrameClockStarted` hazard).

- **Transport detection (runtime, mandatory):** `metadata.rtpTimestamp !== undefined` ⇒ WebRTC path; else MSE. Cross-check `videoEl.pcState/wsState` and `srcObject instanceof MediaStream`; surface in HUD. video-rtc races both and can switch silently — re-anchor on any flip.
- **Chrome WebRTC:** `V(f) = unwrap(metadata.rtpTimestamp) / 90000` (u64 unwrap, signed-delta mod 2³²; wrap every 47,721.86 s ≈ 13.25 h). Never `mediaTime`.
- **Safari WebRTC:** same per WebKit source; device-unverified (Q2 below). Not a target config; falls back to REALTIME mode regardless.
- **MSE (iPad Safari, and any pre-WebRTC window on all browsers):** `V(f) = metadata.mediaTime` (camera PTS rebased to 0). Exact.
- Between rVFC ticks extrapolate `V_now = V_last + (performance.now() − t_callback)/1000`, clamped to +100 ms. Use `expectedDisplayTime` for HUD latency math. Safari <15.4: rAF + `currentTime` fallback (±1 frame accuracy); require iPadOS ≥15.4 for full accuracy.

No video-rtc.js fork needed: `videoEl.video`, `videoEl.pc.getReceivers()`, `videoEl.mode` are all public properties.

### 3.2 Per-session offset C (label pts ↔ video camera time)

Both timelines are slope-1 in camera time; only the epoch constant `C = V − pts` (same camera frame) is unknown, per session per transport.

**Runtime estimator (uniform across transports):** at each event arrival (cap 4 Hz), sample `C_raw = V_disp_now − pts_newest`. Then
`C_est = rollingMedian₃₀ₛ(C_raw) + B_video − Δ̂_label`, where:
- `B_video` = measured video-side buffering, **auto-adaptive**: Chrome WebRTC = Δ`jitterBufferDelay`/Δ`emittedCount` + `totalDecodeTime/framesDecoded` + 12 ms (½ vsync); MSE = `video.buffered.end(last) − video.currentTime` + 12 ms. This makes C track jitterBufferTarget changes and MSE gap wobble automatically.
- `Δ̂_label` = quasi-static label availability constant (go2rtc→decode-stamp δ + in-Pi 34 ms p50 + transport), **measured once per (transport, path) by the verification rig (§4)** and stored (localStorage, seeded from a rig-published JSON). Expect ~70–150 ms LAN, ~120–250 ms tunnel.

Accuracy target after rig calibration: **±20–40 ms**, drift-free (camera crystal both sides). Chrome `captureTime` (valid after first RTCP SR — go2rtc/pion sends SRs, and go2rtc's NTP clock IS the Pi clock, same host as the pipeline) is used as an independent HUD cross-check, not load-bearing.

**Calibration state machine:** UNCALIBRATED → (≥20 samples, IQR < 80 ms) → LOCKED. While not LOCKED: render in REALTIME mode and show "sync: calibrating". Confidence = IQR, surfaced in HUD.

### 3.3 Label playout buffer + smoothing modes

**Event buffer:** single ring keyed by pts, span 10 s (~300 events @30 Hz; iMac's 120 s is unnecessary at this rate), prune on insert, per-track chronological index rebuilt (or cursor-advanced — T is monotonic) once per rendered frame. Insert-only in the SSE/WS handler — **zero style work at event arrival**, which also neutralizes Cloudflare burst delivery and the proxy stall pattern.

**Modes, auto-selected from measured lookahead** `L̂ = pts_newest − (V_disp − C_est)`, rolling p10 over 10 s, 100 ms hysteresis, current mode + reason in HUD (doc-31 rule: never silently shrink σ):

| Mode | Requires L̂_p10 | Rendering |
|---|---|---|
| SMOOTH | ≥ 3.2·σ_wide + 120 ms = **760 ms** | full dual-Gaussian Adaptive Lock (symmetric) |
| BRACKET | ≥ 120 ms | linear interpolation between the two straddling events + narrow-only Gaussian (σ=80 ms) |
| REALTIME | anything | newest event ≤ T, hold — **no extrapolation** (iMac v0.5 lesson); optional 60 ms past-only triangular smoothing |

**Buffer policy per config:** iPad MSE — free ~1 s buffer ⇒ SMOOTH, no action. **Chrome LAN — set `videoEl.pc.getReceivers()` video receiver `.jitterBufferTarget = 800` (ms, Chrome 124+; keep legacy `playoutDelayHint = 0.8` seconds for older Chrome)** on track attach ⇒ SMOOTH with ~0.9–1.1 s glass latency. It's a hint: trust only the getStats-measured achieved delay; mode selection uses L̂, not the requested value. Offer a "Realtime" toggle (target=0) that drops to REALTIME mode — see Decision Q1. Safari WebRTC: no knob ⇒ REALTIME always.

### 3.4 Interpolation: port Adaptive Lock, retuned for 30 Hz

Port verbatim-shape from `/Users/vives/bird-classifier/dashboard/live.html` (it's transport-agnostic JS; run it in pts-seconds): `gaussianAt` (449-465), `adaptiveAnchorAt` + chronologically-closest gap fallback (470-513), EMA state + pruning (444, 517-522). Catmull-Rom (git 8d46f8c) is NOT the recommendation — it lost to Gaussians on noise rejection (it reproduces YOLO's ±5 px jitter at every control point).

**Retuned constants (Pi emits 30 Hz vs iMac's 5–7 Hz — windows shrink, averaging improves):**
- `SIGMA_WIDE_MS = 200` (±640 ms window ≈ 38 events; iMac 450 @6 Hz ≈ 17)
- `SIGMA_NARROW_MS = 80` (±256 ms ≈ 15 events)
- Velocity blend: keep `VEL_LO/HI = 20/80 px/s` (same 640×360 pixel space), `VEL_LOOKBACK_MS = 150`, `ALPHA_EMA_GAIN = 0.1` as starting points; re-tune in the motion-sandbox with recorded Pi tracks (`PIPELINE_RECORD_EVENTS` replay data is the ready-made input).
- Smooth **4 channels** (cx, cy, w, h) as independent 1-D kernels — the Pi draws full boxes, not just a label pill.
- Cost: 3 kernels × 4 ch × ~40 events × 3 tracks ≈ 1.4 k `exp()`/frame ≈ well under 0.1 ms — fine for iPad.

### 3.5 Renderer

- One rVFC-driven render pass per displayed frame: compute `T = V(f) − C_est`, evaluate smoothers at T, write transforms. Keep the existing DOM nodes (labels/styling preserved, GPU-composited) but **kill the 240 ms transitions on transform/width/height** (`pi_dash.html:310-313, 333-334`) — keep opacity transitions for birth/death only. If profiling shows layout cost from width/height writes at 60 Hz ×3 tracks, fall back to the iMac's canvas overlay.
- Cache the stage rect via `ResizeObserver` on `#live-stage`; zero layout reads in the loop (removes the per-track-per-event `getBoundingClientRect`, `pi_dash.html:1547`).
- Stateless per callback (recompute absolute state; never accumulate) — rVFC misses under load then cause a skipped update, not divergence. Detect misses via `presentedFrames` gaps; snap instead of animate after a gap.

### 3.6 Birth / death / gaps / lock — all on the video clock T

- Birth: fade-in 150 ms of displayed-frame time; skip fade if `T − firstSeen > 300 ms` at first render. Young tracks (<~300 ms) run in the wide-kernel regime (velocity ≈ 0) — expected, per doc 31.
- Death: last event pts < T − 600 ms (STALE) → 300 ms fade → DOM removal. Replaces the wall-clock gcTracks 1.5–2.1 s ghost. Keep a wall-clock "paused (no events 4 s)" status separately.
- Gaps: chronologically-closest-event hold (never newest — with lookahead, newest is the bird's future). Never extrapolate past the last event.
- `is_locked`/species text flips when **T crosses the pts where the flag flipped** (per-event data already in the buffer) — otherwise lock flashes appear early by the buffer depth (~1 s on MSE).
- Coasting: ask the server for a `coasting` flag (§3.8); render coasted boxes dashed/50% — after the clock fix, drifting coast boxes are the biggest remaining "wrongness".

### 3.7 Drift correction & re-anchor

Drift is structurally zero; the monitor exists to catch epoch breaks:
- Continuous: recompute C estimator; alarm if |ΔC| > 50 ms per 30 s (should never fire between resets).
- **Re-anchor triggers:** event pts backward jump or > +1 s vs extrapolation (pipeline capture restart — pts epoch resets **[M]**); rtp/mediaTime discontinuity > 2 s (camera reboot, UniFi token resync, go2rtc restart, SourceBuffer re-init); transport flip (rtpTimestamp presence change); video element/src change (`resetOverlayState`).
- During re-anchor (~1–3 s): freeze boxes at last positions, dim 50%, HUD shows "sync: re-calibrating". Never render with a known-broken C.

### 3.8 Server-side changes (small)

**Required for any verification (do first):**
1. Demo same-stream: `PIPELINE_TEST_RTSP_URL=rtsp://127.0.0.1:8554/feeder-demo` — pipeline and browser then consume the one go2rtc exec producer (continuous pts across loops). Bake into `POST /api/demo-mode` (`dashboard/api.py:5300`), plus `PIPELINE_NIGHT_BYPASS=1` on demo-ON (evening demos currently die ≤10 s after dusk — proven Jul 01), plus fix the stale comments (`api.py:5297-5298`, `pi_dash.html:1355-1356`). Thermal note: the exec loop re-encodes 640×360 libx264 on the Pi — demo-only; watch temps (Pi recently recovered from thermal saga).

**Strongly recommended:**
2. Add `seq` (monotonic counter) and `emit_ms` to the SSE payload (`pipeline/sse_events.py:147-152`) + per-client dropped counter in stats — makes the silent 32-slot-queue drops and proxy stalls client-visible and separates transport lag from pipeline lag.
3. `coasting: true` on Norfair tracks with no fresh detection this frame (`process_thread.py`), and consider capping coast emission at ~1 s (vs current ~5 s) — Decision Q6.

---

## 4. Verification design

Prove sync separately from smoothing (feedback_sync_math_proof): the rig measures the clock; the sandbox tunes the math.

### 4.1 Ground truth

1. **Burn a machine-readable timecode into the demo video** (one-time re-encode of `sim/current.mp4`, 149.933 s = 4,498 frames @30 fps): 16-bit frame index + 8-bit XOR checksum as 24 blocks of 8×8 px pure black/white along the top edge (survives H264; decodable from one `getImageData` row) + human-readable `drawtext` corner. Neither WebRTC MediaStreams nor same-origin MSE taint the canvas — readback works on both paths. Update the NAS copy if the NAS path stays in use.
2. **Offline ground truth:** replay the timecoded file per-frame through the exact detect+track config with a synchronous feed (NOT via `PIPELINE_TEST_RTSP_URL` file mode — 16× decode drops 90% of frames). Output `gt.json: {frame_idx → [{track bbox, id, locked}]}`. Sanity: event count ≈ 4,498/loop.
3. Live-session pts ↔ frame_idx map: robust trajectory cross-correlation of live SSE bboxes against gt over a 20 s window (slope 1, offset = loop phase; sharp peak, ±1 frame).

### 4.2 Browser self-instrumentation + automated test

- `?synctest=1` mode in pi_dash: per rVFC, push `{perfNow, transport, anchorRaw, V_disp, C_est, L̂, mode, decodedTimecodeIdx, renderedBoxes[]}` into `window.__syncSamples` (ring, 2 min).
- **Playwright** (Mac, LAN, against pi5 with demo mode ON): Run A = Chrome WebRTC (assert transport via HUD state; set jitterBufferTarget per config). Run B = MSE forced via `?forcemse=1` (page sets `videoEl.mode='mse'` before src — the markup attribute is inert, so the page param is required). Collect 120 s, pull `__syncSamples`, compute metrics, assert thresholds, dump JSON + histogram artifact. Include a chaos step: `systemctl --user restart bird-pipeline` mid-run and assert recovery.
- iPad Safari via tunnel: not automatable — the HUD itself computes and displays live offset when it detects the timecode; manual spot-check, record the number.

### 4.3 Metrics + acceptance thresholds

- **O_total (user-perceived, ms):** B_label − B_video (mod loop), where B_video = decoded on-glass timecode and B_label = gt frame whose boxes best match the rendered boxes (nearest-neighbor search ±2 s, averaged over tracks). Includes smoothing chase.
- **O_clock (ms):** (T mapped through the pts↔idx correlation) − B_video. Splits clock error from smoothing: O_smooth = O_total − O_clock.
- **Spatial:** center distance + IoU between rendered boxes and gt(B_video).
- **PASS per transport:** |O_clock median| ≤ 40 ms; O_total p50 within ±40 ms, p95 ≤ 100 ms; center error mean ≤ 8 px, p95 ≤ 20 px @640×360; C drift < 20 ms over 30 min; re-anchor recovery < 3 s (transport flip), < 5 s (pipeline restart); zero rendered frames with UNCALIBRATED C after warm-up.
- First rig run against CURRENT code = the baseline number nobody has today (finding: the dominant offset has never been measured), and each rig run refreshes the Δ̂ constants of §3.2.

### 4.4 Live HUD (upgrade the `?syncdiag=1` chip)

Active transport + pcState • achieved video buffer (getStats delta / MSE gap) in ms • lookahead L̂ p10/p50 • C_est ± confidence + age + re-anchor count • render mode + reason • event rate, seq-gap loss count, emit→arrival transport delay (needs §3.8-2) • missed-rVFC count • **live offset-vs-timecode in ms whenever the demo timecode is on glass** — the single number David watches, e.g. "sync: −12 ms".

---

## 5. Decisions needed + risks

**Q1 (David — the only real product fork): latency vs smoothness on Chrome LAN.** SMOOTH default adds ~0.6–0.9 s of glass latency via jitterBufferTarget=800. Recommendation: SMOOTH default with latency shown in the HUD and a "Realtime" toggle (target=0, REALTIME mode, honest ~33 ms steps) — this is an observation UI, not a reaction UI, and it mirrors the iMac /live-vs-dashboard split. iPad MSE has ~1 s latency regardless.
**Q2 (engineer, before building):** 10-line rVFC probe page against live go2rtc on the actual devices — confirm `rtpTimestamp` populated end-to-end (Chrome + iPad), confirm mediaTime↔pts linearity on MSE, measure achieved jitterBufferTarget floor at 0 and 800. All source-level evidence says yes; nothing device-level does. Also confirm the Pi's pinned go2rtc 1.9.14 matches the timestamp-passthrough code read at master.
**Q3:** Where do rig-measured Δ̂ constants live (recommend: rig publishes `/api/sync-constants` JSON; client caches in localStorage keyed by transport+path) and re-measure cadence (each rig run).
**Q4 (David):** OK to re-encode `sim/current.mp4` with the 8 px timecode strip? (Slight demo aesthetic change; enables all objective measurement.)
**Q5:** Demo unification variant: local exec loop (recommended: continuous pts, self-contained; Pi pays one 360p x264 encode) vs PATCH feeder-demo→NAS relay (thermally free, but reverts on go2rtc restart, silently re-splitting the rig — the failure mode we just diagnosed). If NAS: verify its loop-wrap doesn't jump RTP backward every 149.93 s.
**Q6 (David/engineer):** coast rendering + server coast cap (~1 s vs current ~5 s) — trades brief-occlusion continuity against drifting-box wrongness.
**Q7:** GC/stale constants (600 ms + 300 ms fade proposed) and whether "identifying…" should be shown at all during the buffered delay.

**Standing risks:** jitterBufferTarget is a hint (trust getStats only) • RTP discontinuities from camera/UniFi token events (June lesson) make auto-re-anchor non-optional • video-rtc can silently switch transports (anchor must switch primitives) • σ/velocity constants need a sandbox pass on real Pi 30 Hz tracks • Safari WebRTC metadata unconfirmed on device (non-target; REALTIME fallback covers it) • the 32-slot SSE queue still silently drops on >1.1 s client stalls until §3.8-2 lands.

---

## 6. Implementation order

1. **Server prep** (§3.8-1, -2): demo same-stream + night bypass + seq/emit_ms. (~½ day) — prerequisite for everything.
2. **Timecoded demo video + offline ground truth** (§4.1). (~½–1 day)
3. **Client clock layer** (§3.1–3.2): transport detect, anchors, unwrap, C estimator, HUD v1. No rendering changes yet. (~1–2 days)
4. **Rig v1** (§4.2–4.3): measure the baseline offset of current code + calibrate Δ̂. First objective number. (~1 day)
5. **Buffered renderer** (§3.3, 3.5, 3.6): ring buffer, rVFC loop, kill transitions, fades/GC/lock on T, BRACKET mode. Rig must show |O_clock| ≤ 40 ms here, before any smoothing work. (~1–2 days)
6. **Adaptive Lock port + retune** (§3.4) via sandbox with recorded Pi tracks; mode auto-switch. (~1–2 days)
7. **jitterBufferTarget SMOOTH mode + re-anchor hardening + full acceptance runs** on Chrome-WebRTC, Chrome-MSE-forced, iPad-manual. (~1 day)

**Key files:** client — `/Users/vives/bird-classifier-pi/dashboard/pi_dash.html` (overlay: 310-313, 1486-1697, 1769-1822), `/Users/vives/bird-classifier-pi/dashboard/video-rtc.js` (read-only; 368-382, 475-479, 591-627), `/Users/vives/bird-classifier-pi/dashboard/api.py` (5086-5175 event proxies, 5297-5344 demo mode); server — `/Users/vives/bird-classifier-pi/pipeline/sse_events.py` (136-165), `/Users/vives/bird-classifier-pi/pipeline/frame_capture.py` (119-210), `/Users/vives/bird-classifier-pi/pipeline/process_thread.py` (177-199); reference implementation — `/Users/vives/bird-classifier/dashboard/live.html` (Adaptive Lock 422-522, buffer 305-318, fades 658-687) and `/Users/vives/docs/bird-observatory/31-label-motion-adaptive-lock.md` (tuning table, prerequisites 23-27, 139-143).