# Idea-extraction record — 2026-06-15

> **What this is:** a single compressed map of every distinct idea, decision,
> and open question mined from the ~23 accumulated `docs/working/{progress,plans,specs}/`
> files, deduped against the current `ROADMAP.md`. It exists so we can (a) keep
> a record of the good wisdom, (b) compress the sprawl, and (c) let David
> adjudicate "still holds / dead" without re-reading everything.
>
> **Status guesses are the miner's, not verdicts.** David marks these up.
> Once adjudicated, the source progress files this distills can move to
> `docs/historical/`.

---

## Top candidates to revive

1. **Spatial-subtitle / ClockBridge + deliberate-delay architecture**
   (`2026-05-11-spatial-subtitle-overlay-architecture.md`) — ROADMAP names it
   the most complete answer to Chapter 1's timing fork; the shipped WebRTC+DOM
   path is a stop-gap that likely can't hit 1a's "no perceptible lag."
2. **HLS-backed high-res snapshot-by-PTS** (`2026-05-12-codex-live-log.md`) —
   unblocks Chapter 2's "high-quality crop" chip; resolves the 640×360 snapshot
   regression; partly implemented.
3. **Empirical offset harness + frame-by-frame annotations**
   (`sync_replay_assert.py`, `annotation_parser.py`, the demo annotations) —
   ROADMAP's measurement principle depends on it; exists, just needs
   re-pointing at the off-Pi feed.
4. **AIY raw_score normalization fix + authoritative-on-1080p-crop finding**
   (`codex-work-log.md`) — cheap, validated accuracy win; feeds Chapter 2.
5. **Off-Pi RTSP demo feed** — ROADMAP's own "next concrete move." NOTE: the
   shipped on-Pi `bird-demo-loop` + `feeder-demo` stream directly contradicts
   it; reconciling these is the literal next step.

## One contradiction for David's eye

**Off-Pi feed (ROADMAP's next move) vs. on-Pi `bird-demo-loop` + `feeder-demo`
(shipped in May).** The docs point opposite directions. Resolving this decides
whether we retire the on-Pi loop and where the new feed lives (NAS or M4 Mac).

---

## Full deduped map (8 themes)

### 1. Overlay / label timing / sync
- **PTS is the sole clock for sync; wall-clock only for logs/filenames.** — bedrock-design, spatial-subtitle, ROADMAP — LIKELY-LIVE.
- **Spatial-subtitle / ClockBridge: labels are timecoded cues rendered against the displayed frame's media time via a measured DetectionClock→MediaClock bridge.** — spatial-subtitle, handoff — LIKELY-LIVE.
- **Deliberate 8-12s display delay so classifier/tracker settle before the viewer sees the frame; backfill labels once locked.** — spatial-subtitle, ROADMAP — LIKELY-LIVE.
- **Borrow from broadcast telestration / Frigate-Viseron NVR overlay / ARKit anchoring / WebVTT cue model.** — ROADMAP, spatial-subtitle — LIKELY-LIVE.
- **Symmetric Adaptive-Lock Gaussian smoothing (past+future, zero phase lag; velocity-blended sigma 380/190ms).** — bedrock-design §C3 — UNCLEAR; kernel reusable, tied to reverted HLS path.
- **Pre-arrival fade-in: read first event ~5-8s ahead in the buffer; label fades up exactly as the bird lands.** — bedrock-design — UNCLEAR; needs buffered/delayed path.
- **PDT-as-fake-NTP (encode PTS in EXT-X-PROGRAM-DATE-TIME) to unify hls.js + native iOS.** — bedrock-design — LIKELY-SUPERSEDED; HLS live path reverted, useful for replay.
- **Two sync tolerances: 500ms detection-match vs 1000ms species-match (vote-lock lags); median-lag gate ±50ms.** — bedrock-design, runbook — LIKELY-LIVE; reusable for the harness.
- **Shipped live path is "approximate" (~200-500ms lag): WebRTC + DOM divs + CSS 240ms transitions, synced via SSE wall_time_ms.** — overnight-result, CLAUDE.md — UNCLEAR; stop-gap vs ROADMAP 1a's "no perceptible lag."
- **WebRTC rejected as label clock authority (no presented-frame timeline) but fine as low-latency raw view.** — spatial-subtitle — LIKELY-LIVE.

### 2. Detection / tracking
- **No identity churn: a perched bird keeps one track; handle multiple birds at once.** — ROADMAP 1a — LIKELY-LIVE.
- **Tracker threshold 2.0 on Pi (vs 1.0 iMac); can fuse crossing birds; monitor `id_switches`, revisit if >5/hr.** — session-summary, pi5-handoff — LIKELY-LIVE.
- **HailoDetector.detect() ignores the motion gate and runs full-frame — "MOG2 starving YOLO" was wrong.** — codex-work-log — LIKELY-LIVE (correction).
- **Within-track disagreement detector: flag >60% species disagreement → AIY fallback (fixes yard overconfidence).** — session-summary — UNCLEAR; module shipped, needs SmartClassifier integration.
- **"active_tracks: 0 while birds visible" is a detector/tracker/motion-gate sensitivity investigation, not a renderer bug.** — codex-live-log — LIKELY-LIVE (open).

### 3. Model / classification / data quality
- **AIY raw_score normalization bug fixed (`raw==1`→conf 1.0 locked bad species).** — codex-live-log/work-log — LIKELY-LIVE (landed).
- **Authoritative reclass on 1080p crops beats 640×360 → keep high-res as the review/training path.** — codex-live-log — LIKELY-LIVE (Ch2).
- **Tier-2 flagship: EfficientNet-Lite0, 16 classes, x86 train → hailo8l DFC compile; cleanlab→head→OOD-gate→QAT→shadow→cutover.** — tier2-readiness, hailo-playbook — UNCLEAR; ROADMAP Ch2 asks "buy vs train," uncommitted.
- **AIY baseline to beat: 67.96% top-1 / 75.2% macro-F1 / 16.3% ECE on 1,670 hold-out; targets ≥75/≥65/≤5 + ≥0.85 OOD AUROC.** — tier2-readiness — LIKELY-LIVE (measurement anchor).
- **HARD GATE: eyeball ≥5 crops/species before training (the yard-0/14 disaster); cleanlab-prune 34K weak labels first.** — tier2-readiness — LIKELY-LIVE.
- **EfficientNet-Lite0 softmax offloaded to CPU; HEF emits raw logits.** — hailo-playbook §5.1 — LIKELY-LIVE.
- **Regional species filter active; bad labels were region-plausible-but-context-wrong, not tropical leakage.** — codex-work-log — LIKELY-LIVE.
- **Honest "unknown" is the Ch2 chip; label-states unknown/candidate/locked/human_confirmed/retracted.** — ROADMAP, spatial-subtitle — LIKELY-LIVE.

### 4. Pipeline architecture / performance
- **Move the demo feed OFF the Pi (RTSP from NAS or M4 Mac).** — ROADMAP, cpu-audit — LIKELY-LIVE; contradicts shipped on-Pi loop.
- **Pi 5 has HEVC decode but NO HW H.264 decoder and NO encoder of either; SW-decoding 1080p ~76% of a core.** — cpu-audit, overnight-result — LIKELY-LIVE (constraint).
- **FrameCapture reads 640×360 substream (~14% vs ~76%); detector inputs don't need more res.** — overnight-result — LIKELY-LIVE.
- **End-to-end H.265 only if the camera emits HEVC (G3 Dome does NOT; G4 Pro/G5 do).** — cpu-audit, handoff — UNCLEAR (camera upgrade, open).
- **Single-shared-decoder: demux once, forward packets to segmenter via bounded queue (~150 lines).** — handoff Option 1 — UNCLEAR; revisit if segmenter returns.
- **Landed memory-bandwidth wins: PyAV threads=1, preallocated Hailo buffer + in-place BGR→RGB, removed defensive copies. 213%→~125% CPU, no throttle.** — overnight-result — LIKELY-LIVE.
- **Scalability target N=4-8 cameras, ≤50% CPU/camera; today ~125%/camera → N=2 saturates; N=8 = camera H.265 + HW HEVC, or federated Pis.** — cpu-audit — UNCLEAR; ROADMAP scopes N=1, paused-not-dead.
- **HLS-backed high-res snapshot: extract the frame from the 1080p .ts covering the lock PTS; `hires_ok` must mean true high-res.** — codex-live-log — LIKELY-LIVE (Ch2).
- **Snapshot regression: snapshots are 640×360 since the substream flip; HLS-extract is the fix.** — overnight-result, handoff — LIKELY-LIVE (open in Ch2).
- **Hailo multi-model: ONE VDevice, shared group_id + ROUND_ROBIN, run_async; co-sched cost measured, fits both at 5 FPS.** — hailo-playbook — LIKELY-LIVE (reference).
- **DFC compile only on x86_64 Ubuntu 22.04/24.04; Model Zoo v2.18, --hw-arch hailo8l, ≥1024 calib images.** — hailo-playbook §4 — LIKELY-LIVE.

### 5. Infra / resilience / ops
- **UniFi RTSP tokens DO rotate (~2-day life); a refresh stub that only restarts go2rtc lets tokens go stale → silent zero-frame "active" service.** — cross-claude-comms — LIKELY-LIVE (trap).
- **Config-rewriters must preserve unmanaged streams (refresh-rtsp wiped feeder-demo; now reads+preserves via PyYAML).** — handoff, memory — LIKELY-LIVE.
- **Partial-rsync landmine: keep `pipeline/` fully synced or modules crash-loop.** — cross-claude-comms — LIKELY-LIVE.
- **Watchdog: check proc.poll() before stall-age, or dead-on-startup ffmpeg becomes an un-respawned zombie.** — pi5-handoff — LIKELY-LIVE.
- **integrity-audit + refresh-rtsp timers were left DISABLED on the live Pi (only thermal-watch fired).** — cross-claude-comms — UNCLEAR; confirm current state.
- **HLS recorder declared vestigial (827MB, dashboard uses WebRTC); deactivate pending proper sync design.** — cross-claude-comms — UNCLEAR; may be re-introduced by the replay segmenter.
- **Never kill -9 a Hailo process; graceful restart + SIGTERM → vdevice.release().** — hailo-playbook, pi5-handoff — LIKELY-LIVE.
- **WebSocket label mirror for Cloudflare (SSE is buffered, 0 events through tunnel); video same-origin via /api/ws.** — codex-takeover — LIKELY-LIVE.
- **Pi 5 boot: SD = bootloader+kernel, NVMe = rootfs.** — handoff, memory — LIKELY-LIVE.

### 6. Presentation / UX
- **Labels-only is the target UI; bounding boxes are debug-mode scaffolding; anchor near bird center/top with collision avoidance.** — spatial-subtitle, codex-takeover — LIKELY-LIVE.
- **Label toggle controls rendering ONLY — never stops ingestion/tracking/classification/snapshots/telemetry.** — spatial-subtitle — LIKELY-LIVE.
- **Prettier presentations (name card above feeder, zoomed bird-photo card) deferred to Ch3; pixel-glued label stays as the instrument.** — ROADMAP — LIKELY-LIVE.
- **Demo/live data isolation: demo writes classifications_demo.db (gated by PIPELINE_TEST_RTSP_URL); dashboard mode=live|demo; switch clears stale overlay + Recent strip.** — codex-live-log — LIKELY-LIVE.
- **In-place demo/live switch: reconnectLiveVideo() must close the old VideoRTC session; <video-stream> src has a setter, no getter (guard on next===currentSrc).** — codex-live-log — LIKELY-LIVE.
- **HARD RULE: UI work needs headless Playwright screenshot + Read the PNG; Chrome MCP doesn't render. Use dash_probe.py.** — overnight-result, memory — LIKELY-LIVE.

### 7. Testing / measurement / harness
- **THE measurement principle: eye = binary judge, not analog instrument; timecode + frame annotations → offset in ms; no timing work judged by description.** — ROADMAP, bedrock-design — LIKELY-LIVE.
- **Existing harness: sync_replay_assert.py + annotation_parser.py + may10 annotations; tolerant parser handles hand-annotation quirks.** — bedrock-plan, runbook — LIKELY-LIVE.
- **Greedy 1:1 annotation↔event match (Hungarian upgrade flagged); IoU≥0.5; LAN (2a) + CF-tunnel (2b) both pass before merge.** — bedrock-design — UNCLEAR; tunnel-token mode was a follow-up.
- **C5 replay result: harness correct; failures were classifier accuracy + annotation-format mismatch, NOT sync; median lag <±5ms.** — runbook — LIKELY-LIVE.
- **Every gate runs with realistic adjacent load + becomes a committed regression script; failed gate halts progress (foundations-up; the prior spec failed because gates were correctness-shaped not load-shaped).** — cpu-audit, overnight-execution — LIKELY-LIVE.
- **Adversarial two-independent-reads (Claude + Codex on same tracks, trust only on agreement); per-phase audit+verification gates.** — cpu-audit, codex-audit-prompts — UNCLEAR; worked but heavy; ROADMAP favors lighter single-chapter focus.
- **PyAV mpegts passthrough mux preserves PTS byte-exact across keyframes (proven).** — bedrock-design C1 — LIKELY-LIVE (reusable for replay/snapshot).

### 8. Meta
- **Pi/iMac repo split: edit in bird-classifier-pi (pi-main), rsync to Pi; iMac frozen reference; never push pi main to imac-origin/main.** — repo-split, ROADMAP — LIKELY-LIVE.
- **Mac = frozen known-good reference; Pi = sole active surface; M4 Mac + NAS available for off-Pi RTSP + load-vs-code.** — ROADMAP — LIKELY-LIVE.
- **Load-vs-code ladder: (1) move feed off Pi + re-measure; (2) run same pipeline on M4 — perfect = Pi load, bad on both = code; (3) only then a Coral (libedgetpu flaky).** — ROADMAP — LIKELY-LIVE.
- **Self-heal/power stack landed: watchdog + service-canary + iMac pi-watch + NUT graceful shutdown.** — ROADMAP — LIKELY-LIVE.

---

## Miner's "safe to archive wholesale" (pure status logs, ideas already captured above)
- `2026-04-25-pi-repo-split.md` — split mechanics (rule survives in CLAUDE.md/ROADMAP).
- `2026-04-30-docs-state.md` — a docs-rewrite to-do snapshot.
- `DOC_AUDIT_PI_BOOK.md` — one-time doc-verification report.
- `2026-04-29-session-summary.md` — task log (substantive nuggets captured above).
- `phase1-daily-validation.md`, `phase1-shadow-handoff-gates.md` — Pi↔iMac shadow-cutover runbooks overtaken by the Pi-as-sole-surface model.
