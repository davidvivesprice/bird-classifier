# Documentation Audit

**Date:** 2026-04-26
**Repo:** /Users/vives/bird-classifier-pi (Pi-side)
**Book:** ~/docs/bird-observatory-pi/ (chapters live outside the repo per David's directive — parallel to iMac's ~/docs/bird-observatory/)
**Docs audited:** CLAUDE.md, GUIDE.md, ~/docs/bird-observatory-pi/00-overview.md … 08-deployment.md, ~/docs/bird-observatory-pi/README.md, docs/README.md (in-repo stub)
**Code roots scanned:** bird_pipeline_v3.py, pipeline/ (frame_capture, hires_ring, hailo_engine, hailo_detector, hailo_classifier, model_registry, pi_classifier, process_thread, motion_gate, tracker, snapshot_writer, classifier), dashboard/api.py, dashboard/pi_dash.html, dashboard/pi_review.py, tools/pi5_thermal_watch.py, tools/bench_hailo_multimodel.py, models/, ~/.config/systemd/user/* on pi5.local, runtime state via ssh

## What was reorganised

Beyond claim-level fixes, the full docs/ tree was restructured (Phase 1):

- **CLAUDE.md** — preamble path drift fixed (comms file moved); body rewritten from iMac architecture to Pi architecture (services count, Coral → Hailo NPU + AIY-CPU, two cameras → one, vote-lock now lists the conf threshold, ground-cam removed, references to the chapter book added).
- **GUIDE.md** — full rewrite (was the iMac Reference Guide top to bottom — Docker go2rtc, LaunchAgents, two cameras, Coral USB, defunct `live_detector.py`). Replaced with a Pi-side guide pointing at the chapter book + the in-repo working/historical docs.
- **9 reference chapters authored** at `~/docs/bird-observatory-pi/00-overview.md` through `08-deployment.md` (Phase 2). 759 lines total. Originally drafted in-repo at `docs/`, then moved to `~/docs/bird-observatory-pi/` per David's "the docs for the book need to live in ~/docs/bird-observatory-pi" directive (mirroring iMac's `~/docs/bird-observatory/` setup).
- **62 docs reorganised** (Phase 1):
  - `docs/superpowers/{plans,specs,progress,reviews}/` flattened into:
    - `docs/working/{plans,specs,progress}/` — 4 active reference docs
    - `docs/historical/{plans,specs,progress,reviews}/` — 60 retired docs, each banner-prepended with the iMac-format `> **HISTORICAL**` line
- **Empty `docs/superpowers/` tree removed.**
- **`docs/README.md` created** as the chapter-index entry point.

**Active doc set after audit:**
- Repo root: CLAUDE.md, GUIDE.md, DOC_AUDIT.md
- Repo docs/: README.md (stub pointing at the book), working/, historical/
- Repo docs/working/: 1 spec (hailo-playbook), 0 plans, 3 progress (pi5-handoff, pi-repo-split, cross-claude-comms)
- Book at `~/docs/bird-observatory-pi/`: 9 chapters (00-08) + README.md

---

## Summary

| Bucket | Count |
|---|---|
| ✅ Verified | 36 |
| ⚠️ Drift | 3 (all auto-fixed) |
| ❌ Hallucination | 1 file (GUIDE.md, full rewrite) + 1 section (CLAUDE.md body, full rewrite) |
| 🐛 Smell | 0 |
| ⏭ Skipped | 0 |

---

## ✅ Verified

36 substantive claims matched the code or runtime. Listed below for completeness.

<details>
<summary>Show verified claims</summary>

**Pipeline / classifier semantics**
- `docs/03-pipeline.md` — vote-lock `≥3 votes ∧ ≥0.35 conf ∧ ≥60% agreement` → `pipeline/process_thread.py:306-308`
- `docs/03-pipeline.md` — `MAX_CLASSIFICATION_ATTEMPTS = 5` → `pipeline/classifier.py:16`
- `docs/03-pipeline.md` — Norfair `distance_threshold = 2.0` (raised from 1.0 on 2026-04-17) → `pipeline/tracker.py:84,86`
- `docs/03-pipeline.md` — `WATCHDOG_STALL_MS = 10_000`, `WATCHDOG_CHECK_S = 2.0` → `pipeline/frame_capture.py:23-24`
- `docs/03-pipeline.md` — substream "no `-vf fps=N` filter" claim → `pipeline/frame_capture.py:86-99` (verbatim comment in code)
- `docs/03-pipeline.md` — `HiResRingBuffer(max_seconds=2.0, expected_fps=5.0)` defaults → `pipeline/hires_ring.py:33`
- `docs/03-pipeline.md` — SnapshotWriter queue `maxsize=32` → `pipeline/snapshot_writer.py:124`
- `docs/03-pipeline.md` — only feeder camera enabled, ground commented out → `bird_pipeline_v3.py:30-39`
- `docs/03-pipeline.md` — `extra_json.model_source` recorded per row in `classifications.db` → confirmed via `sqlite3 SELECT json_extract`

**Hailo engine**
- `docs/04-hailo-engine.md` — singleton VDevice with `scheduling_algorithm=ROUND_ROBIN`, `group_id="SHARED"` → `pipeline/hailo_engine.py:124-130`
- `docs/04-hailo-engine.md` — bench numbers (DET 58.9 → 45.5 FPS, CLS 47.7 → 44.2 FPS, 22.4 iters/s combined) → `working/specs/2026-04-25-hailo-playbook.md:§12` + raw bench output
- `docs/04-hailo-engine.md` — flat YOLO output shape `(40080,) = 80 × 501` → confirmed via direct invocation against `yolov8s_h8l.hef`
- `docs/04-hailo-engine.md` — `set_format_type(FormatType.FLOAT32)` on outputs before configure → `pipeline/hailo_engine.py:_ensure_configured`
- `docs/04-hailo-engine.md` — pre-compiled HEFs bake norm layer; pass UINT8 → `pipeline/hailo_classifier.py:classify` (post-fix)
- `docs/04-hailo-engine.md` — `is_classifier=False` blocks live switch → `dashboard/api.py:_pi_update_env_classifier` defense-in-depth check

**Services**
- `docs/02-services.md` — 4 user services running → `systemctl --user list-units`
- `docs/02-services.md` — bird-pipeline `Restart=always RestartSec=10` → `~/.config/systemd/user/bird-pipeline.service`
- `docs/02-services.md` — bird-pipeline env: `PI_MODE=1`, `PIPELINE_HEALTH_PORT=8100`, `PIPELINE_SSE_PORT=8105` → unit file
- `docs/02-services.md` — bird-pipeline sources `~/.bird-observatory-env` → `EnvironmentFile=%h/.bird-observatory-env`
- `docs/02-services.md` — `loginctl enable-linger vives` set → `systemctl --user is-active` works without login session
- `docs/07-thermal.md` — thermal-watch service: `Type=oneshot`, `Nice=10`, `IOSchedulingClass=best-effort`, `IOSchedulingPriority=7` → unit file
- `docs/07-thermal.md` — thermal-watch timer: `OnBootSec=2min`, `OnUnitActiveSec=1min`, `AccuracySec=5s` → unit file
- `docs/07-thermal.md` — CSV column list → `tools/pi5_thermal_watch.py:COLUMNS`

**Dashboard**
- `docs/05-dashboard.md` — `BirdAPIRewriteMiddleware` rewrites `/bird-api/*` → `/api/*` → `dashboard/api.py:58-90`
- `docs/05-dashboard.md` — themes `observatory / fieldguide / minimalist / dusk` → `dashboard/pi_dash.html` `[data-theme]` selectors
- `docs/05-dashboard.md` — image-crop produces square crop with 25% padding → `dashboard/api.py:get_image_crop` (post-fix)
- `docs/05-dashboard.md` — Live view uses `<video-stream>` from go2rtc + SSE labels + CSS smoothing → `dashboard/pi_dash.html:setupLiveView`

**Pi-Review**
- `docs/06-pi-review.md` — endpoints `POST /api/pi-review/{file}`, `DELETE /api/pi-review/{file}`, `GET /api/pi-review/recent`, `GET /api/pi-review/stats` → `dashboard/pi_review.py`
- `docs/06-pi-review.md` — SQLite schema with `file PRIMARY KEY`, `verdict CHECK ('yes','no')`, `model_source` → `dashboard/pi_review.py:init_db`
- `docs/06-pi-review.md` — model_source captured at click time from `classifications.db.extra_json.model_source` → `_lookup_model_source`
- `docs/06-pi-review.md` — PI_MODE-gated mount → `dashboard/api.py:90+`

**Hardware / runtime**
- `docs/01-hardware.md` — Pi 5, 4 GB MemTotal → `cat /proc/meminfo` → `MemTotal: 4146880 kB`
- `docs/01-hardware.md` — aarch64 → `uname -m`
- `docs/01-hardware.md` — Raspberry Pi OS Lite (Trixie), Python 3.13 → confirmed earlier
- `docs/01-hardware.md` — feeder-sub 640×360, feeder-main 1920×1080 → ffmpeg invocations in `frame_capture.py` and `hires_ring.py`
- `docs/02-services.md` — cloudflared tunnel UUID `bf725288-989b-4ae4-9d71-ea457310a8d4` → `~/.cloudflared/config.yml` on pi5

</details>

---

## ⚠️ Drift (auto-fixed)

### 1. BOOT_ORDER value wrong in 01-hardware.md

- **Doc:** `docs/01-hardware.md:25` (chapter draft)
- **Original claim:** `BOOT_ORDER = 0xf41`
- **Code reality:** Pi reports `BOOT_ORDER=0xf14` via `vcgencmd bootloader_config` — USB-MSD-first hex order. (The iMac head-start doc `~/docs/bird-observatory/historical/34-pi5-migration.md:40` had the wrong nybble order; my chapter inherited it.)
- **Fix applied:** Updated to `0xf14` in `docs/01-hardware.md` and clarified the order semantics ("USB-MSD first").

### 2. CLAUDE.md preamble pointed at the pre-reorg comms path

- **Doc:** `CLAUDE.md:8,12`
- **Original claim:** Cross-cutting fixes flow via `docs/superpowers/progress/cross-claude-comms.md`. See `docs/superpowers/progress/2026-04-25-pi-repo-split.md` for the full split context.
- **Code reality:** Phase 1 of this audit moved both files to `docs/working/progress/`. The old paths no longer exist.
- **Fix applied:** Both paths updated to `docs/working/progress/...`. Added a pointer to `docs/README.md` for the new chapter index.

### 3. Cloudflared service description over-claimed a tunnel name

- **Doc:** `docs/02-services.md:11` (chapter draft)
- **Original claim:** Cloudflare tunnel `pi5-observatory` (UUID `bf725288-989b-4ae4-9d71-ea457310a8d4`) → `pi5.vivessato.com`
- **Code reality:** The tunnel UUID is correct (in `~/.cloudflared/config.yml`), but the human-readable name `pi5-observatory` only exists on the Cloudflare account dashboard — not anywhere in the repo or systemd unit. Per the skill's "Hallucination if not verifiable" rule, the name is unsupported.
- **Fix applied:** Dropped the name; rephrased to cite the UUID + the config file path.

---

## ❌ Hallucination (auto-fixed)

### 1. GUIDE.md — entirely the iMac Reference Guide

- **Doc:** `GUIDE.md` (whole file, 266 lines)
- **Original content:** "A real-time bird identification system running on a single iMac. Two cameras watch the yard …" — described iMac architecture top to bottom: CloudKey Gen 2+, Docker go2rtc, ten LaunchAgents (`com.vives.bird-{audio,capture,classifier,dashboard,enhanced-audio,health-monitor,livedetect,pipeline,rtsp-sync,tunnel}`), Coral USB, `venv-coral`/`venv` split, defunct `live_detector.py`, etc.
- **Code reality:** Pi-side has none of this. 4 systemd-user services (no LaunchAgents), native go2rtc binary (no Docker), one camera (no ground), Hailo-8L (no Coral), single venv, `bird_pipeline_v3.py` (not `bird_pipeline.py`), `pi_classifier.py` (not `classify.py --watch`), no `health_monitor.py`, no `refresh_rtsp.py`.
- **Fix applied:** Replaced the entire file with a chapter-index stub pointing at `docs/00-overview.md` through `docs/08-deployment.md`, the working/ deep-references, and the historical/ archive. 19 lines, all verifiable.

### 2. CLAUDE.md — Architecture / Services / Pipeline / Video sections

- **Doc:** `CLAUDE.md:41-68` (the four sections under `## Architecture`)
- **Original content:** "Single 2017 iMac (i5-7400, 8GB RAM). CloudKey Gen 2+ manages two UniFi cameras." Services table listed `audio_analyzer`, `enhanced_audio`, `cloudflared` as "tunnel: birds.vivessato.com" + an `rtsp-sync` cron — five wrong entries vs. the Pi-side reality. Detection Pipeline described `SmartClassifier (yard model on Coral TPU → AIY fallback)`.
- **Code reality:** Pi runs 4 systemd-user services + 1 timer; no audio services on Pi yet (placeholder); cloudflared maps `pi5.vivessato.com` (not `birds.vivessato.com`); no rtsp-sync cron (UNIFI_API_KEY is stable on Pi); pipeline uses `PiClassifier` (registry-based) with AIY ONNX on CPU as the active candidate, not `SmartClassifier`/yard/Coral.
- **Fix applied:** Replaced the whole `## Architecture` block with a Pi-accurate version: 4 services + timer, Pi-side detection pipeline (HailoDetector → BirdTracker → PiClassifier with vote-lock thresholds spelled out), Pi-side video path (WebRTC + SSE + CSS smoothing, with the rationale-vs-iMac pointer to `docs/05-dashboard.md`). Added explicit pointers to the new `docs/` chapters.

---

## 🐛 Smells

None this pass. The Pi-side code surface is recent (most of it written this session) and the dashboard code paths backing the audited claims read cleanly.

---

## ⏭ Skipped

None. Every chapter, root MD, and the working/* docs were inspected.

`cross-claude-comms.md` was deliberately not audited — append-only message bus, content is intentionally a chronological log of cross-Claude messages, not architectural claims.

The `historical/` tree was not audited (per the doc-audit brief: historical content gets a banner, not a re-verification pass).

---

## Cross-Claude note

iMac-Claude is doing a parallel audit on `/Users/vives/bird-classifier/` (their own repo, post-split). Their audit landed before this one (their `DOC_AUDIT.md` at the iMac repo root); the structures are intentionally similar but the Pi side opted for top-level chapters at `docs/00-08.md` plus `working/` and `historical/` as the brief from David specified — vs. iMac's `historical/` subfolders under each of `specs/`, `plans/`, `progress/`, `reviews/`.

Cross-cutting follow-ups (would belong on either side):

- The Pi-side cloudflared service ExecStart relies on `~/.cloudflared/config.yml` lookup-by-default rather than passing the tunnel UUID explicitly. Same on iMac side. Not a "smell" per skill rules (no doc claim hinges on it) — flagging here as a future-resilience note.
- The `historical/specs/2026-04-25-imac-live-classify-as-built.md` was authored by iMac-Claude pre-split and lives in BOTH repos. iMac-Claude may have updated their copy; my Pi-side copy is now historical and frozen. If David wants the as-built kept current, iMac-side is the canonical copy.
