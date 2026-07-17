# Doc Audit — iMac system (CLAUDE.md, ~/docs/bird-observatory operational pages)

_2026-07-17, code-to-doc-verifier. 102 claims: ✅ 71 verified · ⚠️ 23 drift (fixed) · ❌ 0 hallucination · 🐛 3 smells._

## ✅ Verified (71)
Evidence records retained in the audit run; every table row, port, path, and threshold below the drift list checked clean.

## ⚠️ Drift — fixed in place (23)
- **pi CLAUDE.md:6** — iMac repo is iMac-Claude's territory — don't push to it
  - now: Single operator works both repos (per user); don't-push rule kept, ownership framing qualified
- **pi CLAUDE.md:8** — Cross-cutting fixes flow via cross-claude-comms.md patches; David relays
  - now: Historical two-Claude workflow; file remains as the patch log (memory 2026-07-01: cross-claude-comms is gone)
- **pi CLAUDE.md:12** — Split context at docs/working/progress/2026-04-25-pi-repo-split.md
  - now: File moved to docs/historical/2026-04-25-pi-repo-split.md
- **pi CLAUDE.md:13,54** — Pi reference book is chapters 00-08
  - now: Book grew to 00-12 (09-unified-brain, 10-overlay-sync, 11-demo-lab, 12-audio); both mentions fixed
- **pi CLAUDE.md:17,19** — Local `main` tracks imac-origin/pi-main; do not push `main` to imac-origin/main
  - now: Local branch is `pi-main` (git branch -vv); both mentions renamed
- **pi CLAUDE.md:52** — SQLite data store = classifications.db, pipeline.db, pi_reviews.db
  - now: Audio port added ~/bird-snapshots/birdnet-audio/birdnet_local.db (audio_analyzer.py:68); appended to list
- **pi CLAUDE.md:56-64** — 4 long-running systemd-user units; full set in deploy/systemd/
  - now: 5 running units (bird-audio.service ported 2026-07-07, confirmed running on Pi); thermal watcher units live in tools/; header updated + bird-audio table row added
- **pi CLAUDE.md:70** — HailoDetector YOLOv8s full-frame every frame
  - now: PIPELINE_IDLE_STRIDE=2: idle frames strided (every 2nd), full rate while birds detected (frame_capture_proc.py:80,326-335)
- **pi CLAUDE.md:74** — Hailo classifiers (ResNet50, YOLOv8s, YOLOv6n) cohabit with the YOLOv8 detector
  - now: Registry has ResNet50 classifier + YOLOv6n detector candidate (model_registry.py:239,290) alongside the YOLOv8s pipeline detector; reworded
- **pi CLAUDE.md:74** — see docs/04-hailo-engine.md
  - now: Chapter lives at ~/docs/bird-observatory-pi/04-hailo-engine.md; path fixed
- **pi CLAUDE.md:78-79** — Local WebRTC direct to go2rtc:1984; remote MSE via wss://go2rtc.vivessato.com
  - now: Both go through the dashboard's same-origin /api/ws go2rtc proxy (pi_dash.html:1818,1873,1922; api.py:4552); Pi tunnel only exposes pi5.vivessato.com→8099
- **imac CLAUDE.md:35** — Services (7 active + 1 cron)
  - now: 10 launchd units: 6 long-running + 4 scheduled (bird-pipeline-watchdog every 30 min and bird-log-rotate daily 3:30 added 2026-07-17); header + two table rows added
- **imac CLAUDE.md:44** — bird-integrity-audit — periodic (StartInterval)
  - now: Hourly (StartInterval 3600 in com.vives.bird-integrity-audit.plist); precision added
- **imac CLAUDE.md:46** — rtsp-sync (cron) refreshes tokens daily 3:10 AM
  - now: launchd StartCalendarInterval 3:10 (crontab is empty; com.vives.bird-rtsp-sync.plist)
- **imac CLAUDE.md:52** — feeder uses yard(Coral)+AIY, ground uses AIY only
  - now: Ground detection disabled in CAMERAS_DETECT (bird_pipeline_v3.py:30-35); Coral auto-degrades to AIY-only via crash-loop breaker DISABLE_CORAL=1 (~/bin/bird-pipeline-run.sh)
- **imac CLAUDE.md:56-57** — Local WebRTC direct to go2rtc:1984; remote MSE via wss://go2rtc.vivessato.com
  - now: Same-origin /api/ws proxy through the dashboard (index.html:3613-3621 — 'retires go2rtc.vivessato.com as a public hostname'); fallback chain webrtc,mse,hls,mp4
- **25-audio-analyzer.md:392** — Level 6 (DOWN): sleep DOWN_RETRY_INTERVAL=300 s, then re-run the whole ladder
  - now: Describes today's self-healing hold: every 300 s actively resync RTSP tokens (_trigger_sync, rate-limited) + reload URLs from disk, then retry the preferred stream — the code does 
- **25-audio-analyzer.md:622** — On RTSP disconnect, RTSPStreamManager escalates through 6 recovery levels before giving up
  - now: escalates through 6 levels and never permanently gives up — Level 6 self-heals by resyncing tokens/reloading URLs every 300 s (rtsp_stream.py:188-195)
- **18-launchagents.md:11 / README.md:6 / 01-architecture.md:33,159** — Eight LaunchAgents run the bird observatory
  - now: Ten / (10) LaunchAgents — bird-pipeline-watchdog and bird-log-rotate added today (live plists present in ~/Library/LaunchAgents/)
- **18-launchagents.md:82** — bird-pipeline Command = venv-coral/bin/python3 -u bird_pipeline_v3.py
  - now: /bin/bash /Users/vives/bin/bird-pipeline-run.sh (crash-loop-breaker wrapper that execs the python command) — live bird-pipeline.plist ProgramArguments
- **18-launchagents.md:31-42 (Service Status table + run-mode summary + Log Files table)** — Service inventory lists only 8 agents; run-mode summary says 1 hourly + 1 daily cron
  - now: Added rows + per-agent detail sections for bird-pipeline-watchdog (StartInterval=1800, scripts/imac-pipeline-watchdog.sh, wedge detector) and bird-log-rotate (3:30 AM, scripts/rota
- **18-launchagents.md:130,142 / 25-audio-analyzer.md:573-578** — bird-audio / enhanced-audio Command = /usr/bin/python3 -u <script>
  - now: scripts/run-with-env.sh /usr/bin/python3 -u <script> — live plists wrap both in run-with-env.sh (sources project env, e.g. UNIFI_PROTECT_API_KEY needed by the Level-2/6 token resyn
- **03-network.md:28,53** — audio_analyzer :8098 — BirdNET SSE + WAV clip server; proxied by dashboard for /api/birdnet-*
  - now: 8098 is a metrics/health endpoint (/metrics, /health) that the dashboard polls (api.py:774); the /api/birdnet-* SSE/summary/clip routes are dashboard-native on :8099 reading the DB

## ❌ Hallucination (0)
None found.

## 🐛 Smells — for human review, no fix applied (3)
- `/Users/vives/.cloudflared/config.yml:5-6 (iMac)` [medium] cloudflared still publicly maps go2rtc.vivessato.com → localhost:1984 even though dashboard/index.html:3613 says that hostname is retired, and go2rtc.yaml:11-13
  - why: A retired-but-still-routed hostname exposes the unauthenticated go2rtc API (stream config, WebRTC signaling) to the public internet unless a
  - via claim: iMac CLAUDE.md video-path claim 'Remote: MSE via wss://go2rtc.vivessato.com' and the cloudflared services-tabl
- `dashboard/birdnet_sse.py:27` [medium] Dead legacy module hardcodes PORT=8098, the same port audio_analyzer.py's metrics server binds
  - why: api.py comments state it 'Replaces the separate birdnet_sse.py process' — birdnet_sse.py is unused, but if ever launched it would collide wi
  - via claim: audio_analyzer :8098 — BirdNET SSE + WAV clip server (03-network.md)
- `rtsp_stream.py:56` [high] Class docstring still says Level 6 = 'Down — retry full ladder periodically', but the implemented Level-6 branch resyncs tokens/reloads URLs and retries only th
  - why: The code comment (not editable here) now mis-describes the very self-heal behavior added today, the same inaccuracy the doc had; worth align
  - via claim: Level 6 DOWN — re-run the whole ladder (25-audio-analyzer.md:392)

## Skipped (13)
- Mission / Who it's for / What matters / NOT building / Technical principles sections (both — Aspirational and philosophy prose — not checkable claims
- Key Rules sections (both files) — Process guidance to the AI, not code-verifiable
- Remote branch etiquette (snapshot branches fine, merges via cherry-pick/PR) — Process/workflow guidance, not verifiable state
- UniFi G3 Dome camera model (pi:51) — External hardware; only RTSP URLs visible from code
- Crucial P3 NVMe brand (pi:51) — ASMedia USB bridge hides drive identity; size (1.8T) and USB transport verified instead
- 2017 iMac model year (imac:32) — Hardware year inferred from i5-7400 (verified via sysctl) but not directly checkable
- ~2-4 s child respawn coast timing (pi:70) — Runtime behavior — would require executing/crashing the pipeline
- Latency characterizations: 'sub-second' WebRTC, 'buffered' MSE, ~0.8s jitter depth in prac — Runtime performance claims; config knobs verified, delivered latency not measurable static
- 25-audio-analyzer.md:23-264 'How to think about acoustic bird ID' — levers, Perch 2.0, Bir — aspirational/research-prose and external-citation content; per protocol, planned/estimated
- 25-audio-analyzer.md ~76% / 88-92% precision and +N% lever estimates — aspirational/estimated performance claims with no in-repo eval script or benchmark log; no
- 25-audio-analyzer.md:195-220 'What transfers to the Pi 5 + Hailo build' — planned/forward-looking migration content; not current-state iMac behavior
- historical/, migration/, working/, docs-book*/, DOC_AUDIT.md, product-compass.md, cross-cl — historical/scaffolding/non-operational pages excluded by task scope (skip:historical)
- go2rtc :8554 RTSP relay and :8555 WebRTC ICE port (03-network.md:22-23,50-51) — go2rtc built-in default ports not declared in go2rtc.yaml (only :1984 listen present); not
