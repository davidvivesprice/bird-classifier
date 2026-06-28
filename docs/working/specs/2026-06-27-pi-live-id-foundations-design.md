# Pi Live-Identification — Foundations Sub-Project (Design)

**Date:** 2026-06-27
**Status:** Design for review (brainstorming → writing-plans next)
**Author:** Claudette (Pi-side), from a 6-agent deep-hunt + direct py-spy verification on the live Pi

**Goal:** Make live identification actually work — birds get identified quickly, with real confidence, tracked without losing them, on a Pi that no longer thermally throttles — by fixing the *upstream* causes first.

**Approach:** Foundations-up. Fix the root causes (heat, crashes, a starvation-grade classifier gate) before tuning downstream symptoms (tracker, snapshots). Each item ships independently with a measurable, visible result. Software-only (no hardware changes), per David's call.

**Scope of THIS sub-project:** the foundations + zero-cost UX wins. Tracker tuning, snapshot quality, Mac↔Pi parity, and the flagship model are **subsequent sub-projects** (listed at the end), each getting its own spec→plan when we reach it.

---

## Corrected root-cause story (what's actually happening)

The deep hunt produced a roadmap, but two of its load attributions were **wrong**, and I corrected them by reading the code and profiling the live Pi with py-spy. The corrected, evidence-based story:

1. **Thermal throttling is the upstream cause.** Live Pi sits at 82–84.5°C, `throttled=0xe000x` (frequency capped), process at ~130% CPU.
2. **The heat is wasted motion detection.** `process_thread._process_frame()` calls `self.motion_gate.regions(frame.bgr)` **every frame** (process_thread.py:98). That runs MOG2 background subtraction + AOI mask + 2× morphology + contour detection, which OpenCV parallelizes across **all 4 cores** (`cv2.getNumThreads()=4`, TBB). py-spy caught the hottest thread (`proc-feeder`) inside `motion_gate.regions()`, and `top -H` shows four near-equal continuous threads = the dominant CPU.
3. **That motion output is thrown away.** `HailoDetector.detect()` (hailo_detector.py:89-93) documents: *"motion_regions / forced_full are accepted for compatibility but not used — Hailo is fast enough that we always run full-frame."* The `regions` result is used only to populate a stat counter (`yolo_actually_ran`, process_thread.py:115). The AOI polygon (built inside motion_gate) is therefore **also not enforced on detections** — it only ever filtered the discarded motion mask.
4. **The cascade:** throttle → variable CPU clock → jittery frame cadence + reader-loop restarts → detections arrive on an unstable clock → Norfair tracks lose association and die at ~3 s (`hit_counter_max=90 ÷ ~30 fps`) → a track must collect ≥3 votes before it dies, but…
5. **the classifier gate sits at the model's confidence median.** `PiClassifier.confident_threshold=0.25` (pi_classifier.py:51,72) vs the AIY model's measured median (~0.23) → ~82% of crops rejected as "unlabeled" → votes rarely reach the ≥3 / ≥0.35 / ≥60%-agreement lock (process_thread.py:309-321) → **"sits on identifying," low confidence, slow.**
6. **Snapshots** come from the **cheap** HLS passthrough mux (hls_segmenter.py is `demux`→`add_stream_from_template`→`mux`, **no decode** — NOT the 76%-core cost the deep hunt claimed). They reach 1080p ~94% of the time. The real defects are *frame selection* (closest-PTS, not best-quality) and a *bbox desync* (the 640×360 box is scaled ×3 onto an independently-encoded 1080p frame, assuming pixel alignment that doesn't hold).
7. **A SEGV every few hours** (likely Hailo VDevice/driver, possibly heat-aggravated) resets counters and drops live ID for the restart window. The self-heal stack already auto-restarts it.

**Consequence for the plan:** the thermal fix is *tiny and software-only* (stop computing motion nothing uses), snapshots **stay 1080p with no added latency** (segmenter is cheap and stays), and the live view is **already 1080p at ~zero Pi cost** (go2rtc passthrough → browser decodes; audio is already in the stream, just muted in JS).

---

## Foundation work items (ordered for fastest visible result while staying safe)

### F0 — Safety net for measurement (tiny, do first)
- **Current:** systemd `--user` `bird-pipeline.service` is auto-restarted by the self-heal stack; no SEGV backtrace is captured.
- **Change:** confirm `Restart=on-failure` + `RestartSec` backoff; enable `faulthandler` (and/or `coredumpctl`/`PYTHONFAULTHANDLER=1`) so the next SEGV leaves a Python+C backtrace. No behavior change.
- **Acceptance:** a deliberate restart shows the service self-heals; faulthandler is armed (verified in env/logs).
- **Risk:** none (observability only). **Thermal:** neutral.

### F1 — Kill the wasted MOG2 motion computation (THE thermal fix)
- **Current:** `motion_gate.regions()` runs every frame (process_thread.py:98); output ignored by `HailoDetector`. Dominant CPU consumer.
- **Change:** stop computing per-frame motion on the Hailo path. Make the motion call conditional on whether the detector actually consumes it (it doesn't, on Pi) — e.g., skip `motion_gate.regions()` when the detector is the Hailo full-frame detector, passing `regions=None`. Replace the `yolo_actually_ran` stat with a constant `True` (YOLO always runs). **Preserve the AOI intent cheaply** if desired: apply a point-in-polygon filter on Hailo detection centroids (microseconds) instead of the MOG2 mask. Verify (grep + runtime) no other live consumer of `regions` before removing.
- **Acceptance (self-validating, measured with py-spy + vcgencmd before/after over a 20-min window):** process CPU drops from ~130% toward ~50–60%; `vcgencmd get_throttled` clears the active-throttle bit; sustained temp drops below the soft limit (~84°C → target ~68–72°C). Live overlay visibly smoother; tracks already hold longer before any tracker tuning.
- **Risk:** low — removing a no-op-for-Hailo computation. Mitigation: AOI re-added as a cheap detection filter; A/B behind an env flag so it's reversible. **Thermal:** this *is* the fix.

### F2 — Classifier gate fix (the biggest downstream win)
- **Current:** `confident_threshold=0.25` ≈ model median; `MAX_CLASSIFICATION_ATTEMPTS=5`; ~82% crops unlabeled.
- **Change:** lower the **vote-eligibility** floor to ~0.15–0.18 (env-var A/B) so more crops vote, while keeping the **lock** gate (≥3 votes, ≥0.35, ≥60% agreement) as the noise guard. Raise `MAX_CLASSIFICATION_ATTEMPTS` 5→~12 so a track gets enough attempts in its lifetime. Quick side-test: raw top-1 vs regional-species-filtered confidence on ~50 crops (rule out the regional filter depressing scores).
- **Acceptance:** unlabeled rate ~82% → ~40–50%; species labels appear within ~1–2 s far more often; shown confidence rises off the 0.25 floor. Watch the disagreement ratio for false locks.
- **Risk:** occasional brief wrong label before authoritative re-classify corrects it (David accepted this trade for labels appearing at all). Mitigation: agreement gate unchanged; env-var A/B. **Thermal:** negligible (event-driven, ~7.4 ms/crop, well within F1's freed headroom).

### F3 — Restore a truthful health signal + confirm the feed stabilizes
- **Current:** health reports `overall=broken` purely because `ffmpeg_restarts_last_hour>10` (the Pi uses PyAV, not ffmpeg — mislabeled); ~20 reader restarts/hr.
- **Change:** after F1, monitor reader restarts over a stable hour (hypothesis: thermal-induced decoder stalls subside). Rename the misleading key to `reader_restarts`. If restarts persist post-thermal-fix, investigate go2rtc RTSP / PyAV decode separately.
- **Acceptance:** `overall=ok` (or `degraded` with a true reason); restarts fall to single digits; no artificial detection gaps.
- **Risk:** restarts may be partly RTSP-driven (revealed by data, not assumed). **Thermal:** minor positive.

### F4 — Crash backtrace + harden (true iteration foundation)
- **Current:** SEGV ~6×/24h; self-heal restarts it; no root cause captured.
- **Change:** with F0 armed, capture the next backtrace; inspect `hailo_engine.py` async job lifecycle (run_async/wait/buffer release) for the VDevice theory. Strengthen restart backoff. Do the deep driver fix only if the backtrace warrants it.
- **Acceptance:** a captured backtrace pointing at the faulting frame; uptime exceeds the prior 2–6 h crash interval (target >12 h). (F1's thermal relief may itself reduce crash frequency if heat-aggravated.)
- **Risk:** root cause may live in Hailo C/driver space; auto-restart+backoff is the survivable fallback. **Thermal:** neutral.

### UX-A — Live-view quick wins (client-side, zero Pi cost, shippable in parallel)
- **Current:** `video-stream.js:9` `muted=true`, `:8` `controls=false`; stream already carries H.264 + Opus audio and is already 1080p.
- **Change:** add a click-to-unmute control (browser autoplay-audio policy needs a gesture); add a fullscreen button calling `requestFullscreen()` on the video element; optional HD/low-bandwidth toggle exposing the already-existing `feeder-sub` alongside `feeder-main` (no transcode). Do not touch theme CSS / theme switcher / non-live panels.
- **Acceptance:** David can unmute and hear the feeder, go fullscreen, and pick quality on the live view (verified via headless screenshot + manual check per the human-facing-verification rule).
- **Risk:** iOS Safari fullscreen quirks → graceful fallback. **Thermal:** none (client-side).

---

## Verification methodology

- **Thermal/CPU:** py-spy (now installed at `~/.local/bin/py-spy`) + `vcgencmd measure_temp`/`get_throttled` + `top -H`, measured **before/after** each change over a steady ≥20-min window. No claims without the paired numbers (per verification-before-completion).
- **Identification quality:** label rate and confidence distribution from the SSE stream (`:8105`) and `classifications.db`; unlabeled-vs-labeled ratio from health.
- **A/B:** env-var gates where possible so each change is reversible and individually attributable.
- **Deploy:** edit in `/Users/vives/bird-classifier-pi/`, rsync to `vives@pi5.local:/home/vives/bird-classifier/`, restart the `--user` service.
- **UX:** headless screenshot + Read the PNG + show David (human-facing-verification HARD RULE).

---

## Subsequent sub-projects (the path forward, each its own spec→plan)

- **S1 — Tracker tuning:** re-measure track lifetimes after F1 (cadence may already be fixed); then tune `hit_counter_max` (90→~120–150) and `distance_threshold` for the real ~37%-of-frames detection regime. Visible: one stable ID held for seconds, not re-acquired every 3 s.
- **S2 — Snapshot quality (segmenter stays):** wire in the already-written `hires_ring.score_frame()` (Laplacian sharpness + size + centering) to pick the best frame, and fix the substream→mainstream bbox desync (re-run the detector on the hi-res frame, or widen the crop). Visible: sharp, well-framed 1080p snapshots.
- **S3 — Mac↔Pi parity:** update the Mac's stale 2026-04-17 AOI trapezoid (clips Orioles) to match the Pi's 2026-05-09 rectangle *if framing matches*; document intentional hardware-driven differences (Mac ONNX region-gated @ fps=5 vs Pi Hailo full-frame @ ~30 fps) so they stop reading as bugs.
- **S4 — Flagship classifier (next major project):** the AIY model genuinely scores Chilmark feeder birds low; the threshold fix (F2) is interim. Train a calibrated, OOD-aware flagship on the ~16 yard species, compile to Hailo. (See `project_yard_model_revamp`.)

---

## Biggest risks

- **F1 thermal relief smaller than expected:** if removing MOG2 doesn't clear the throttle, escalate to dropping substream fps (30→15, halves decode) before revisiting the (declined) hardware option. Measured, not assumed.
- **SEGV resists a clean fix** (Hailo driver space): rely on auto-restart+backoff; F1's cooling may reduce frequency.
- **Classifier floor admits noisier votes:** agreement gate + authoritative re-classify mitigate; A/B and watch disagreement ratio.
