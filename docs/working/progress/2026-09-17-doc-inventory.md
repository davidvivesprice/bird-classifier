> Read-only documentation inventory from the 2026-09-17 deep pass. Steps 1–2 of its cleanup plan were executed that day (tracker/envelope/workflow rewrites, pointers, JSX book); steps 3–9 await David's yes/no — see the deep-pass report §7.

# Documentation Inventory — Bird Observatory (Pi + iMac), 2026-09-17

_Read-only inventory. No doc was edited. Scope: `~/docs/bird-observatory-pi/`, `~/docs/bird-observatory/`, both repos' `CLAUDE.md` / `GUIDE.md` / `ROADMAP.md` / `DOC_AUDIT.md` / `docs/` trees / deploy READMEs / test_clips READMEs / the may10 annotations. Excluded: `vendor/`, `venv*/`, `_additional_datasets/` (2.3 GB CUB-200 copy — not docs), node/browser bundles._

## Headline numbers

| | |
|---|---|
| Files inventoried | **275** (Pi book 17 · iMac docs 81 · Pi repo 92 · iMac repo 85) |
| CURRENT (describes live behaviour, at most cosmetic drift) | 55 |
| STALE (contradicts code today) | 9 files — but they are the 9 most-read: `03-pipeline.md`, Pi `chapters.jsx` (served live at `/book/`), `00-overview.md`, Pi `GUIDE.md`, Pi `docs/README.md`, `systemd-services-phase0.md`, iMac `README.md` framing, iMac `23-live-detection.md` (one claim), `working/reference` |
| FLUFF (dups, empty scaffolding, unbannered aspirational filler) | 26 files (~33 MB, most of it `docs-book-codex/`) |
| HISTORICAL | 185 — **100 % carry a banner** (`HISTORICAL` / `SUPERSEDED` / `Deprecated`) except the ~30 unbannered "working/" files I list below that are historical in substance |
| Byte-identical duplicate pairs | 18 (iMac repo `docs/superpowers/*` ↔ `~/docs/bird-observatory/working/*`; `docs-book` ↔ `docs-book-codex` HANDOFF/DOC_AUDIT) plus ~70 near-duplicates (1-byte banner diffs) between `bird-classifier-pi/docs/historical/{plans,specs,progress}` and `bird-classifier/docs/superpowers/*/historical` |

## What changed under the docs today (why the tracker sections are stale as of 14:22)

Commit `86ba8ed` (2026-09-17 14:22) landed **Tracker v4** (`pipeline/tracker_v4.py`: Kalman + ByteTrack two-stage + colour/size/vote ReID, `PIPELINE_TRACKER=v4` default, Norfair v3 selectable). Deployed and identical on the Pi (`rsync -c` dry-run: `process_thread.py`, `tracker_v4.py` match; only `CLAUDE.md`, legacy `hires_ring.py`, `av_log_guard.py` differ — Pi copies are OLDER, no runtime effect). Only `bird-classifier-pi/CLAUDE.md` line 71 mentions v4. Every other tracker description in the corpus still says Norfair.

## Spot-check: top 25 operational claims vs. code (2026-09-17)

| # | Claim (where) | Code truth | Verdict |
|---|---|---|---|
| 1 | Tracker = Norfair + `_frigate_distance`, threshold 2.5 (`03-pipeline.md` §Tracker, §Tracker-threshold; Pi `chapters.jsx:238,538`; `11-the-demo-lab.md` §Ground truth) | `bird_pipeline_v3.py:323-326` → `make_tracker()`; default `PIPELINE_TRACKER=v4` → `pipeline/tracker_v4.py` (Kalman, gates `PIPELINE_TRACK_GATE_*`, no distance threshold) | **STALE** |
| 2 | `hit_counter_max` 150 ≈ 5 s coast (`03-pipeline.md`, Pi `CLAUDE.md:73`, `07-02 sync design`) | v4: `_COAST_MAX=45` (1.5 s visible) then `_LOST_MAX=300` (10 s hidden) — `tracker_v4.py:63,65` | **STALE** (v3-only knob) |
| 3 | Non-goals: "no ByteTrack swap without ID-switch measurement", "no ReID" (`03-pipeline.md` §non-goals; iMac `23-live-detection.md:256-257`) | v4 IS ByteTrack-style + ReID, measured on the may10 reel (identities 29→8, switches 5→0) | **STALE** (now contradicted) |
| 4 | Detector conf default 0.30 (implied everywhere) | `PIPELINE_DET_CONF` default **0.15** (`bird_pipeline_v3.py:312,334`); 0.15-0.30 is the rescue band (`tracker_v4.py:52-53`) | **STALE** (undocumented) |
| 5 | Vote-lock ≥3 votes, ≥0.70 **calibrated**, ≥60 % (`03-pipeline.md`, Pi `CLAUDE.md`) | `process_thread.py:31` `PIPELINE_LOCK_CONF=0.70` | ✅ |
| 6 | Pi `chapters.jsx:575` vote-lock ≥0.35 | 0.70 calibrated on Pi (0.35 is the iMac raw gate, `bird-classifier/pipeline/process_thread.py:307`) | **STALE** (book) |
| 7 | Classify pacing every 2 hit-frames; 12 consecutive no-votes → plurality + 90-frame cooldown (`03-pipeline.md`) | `process_thread.py:62-63` `CLASSIFY_EVERY=2`, `CLASSIFY_COOLDOWN=90` | ✅ |
| 8 | Lock re-verify every 15; 4 disagreements ≥0.60; 20 vote-less releases (`03-pipeline.md`) | `process_thread.py:64-66,75` | ✅ |
| 9 | Classifier floor 0.16, attempts 12 (`03-pipeline.md`, `CLAUDE.md`) | `PIPELINE_CLASSIFIER_FLOOR` / `MAX_CLASSIFICATION_ATTEMPTS` (audited 07-17) | ✅ |
| 10 | Hi-res ring buffer default-on / `PIPELINE_HIRES_RING=authoritative` (Pi `chapters.jsx:281,335,591,1851,2501`; `07-thermal.md` observed-range para) | `SnapshotWriter(hires_ring=None)`; nothing reads the env var; snapshots come from HLS-by-PTS (`03-pipeline.md` says so correctly) | **STALE** (book + ch07 framing) — live Pi env file still carries the dead line (known smell) |
| 11 | Snapshot = 1080p frame demuxed from HLS segment by lock PTS, then `authoritative_classify()` (`03-pipeline.md`) | `snapshot_writer.py` (audited 07-17) | ✅ |
| 12 | Decode + Hailo detect in a supervised child process, SHM ring, idle stride 2 (`03-pipeline.md`, `CLAUDE.md`, `04-hailo-engine.md`) | `frame_capture_proc.py` (audited 07-17) | ✅ |
| 13 | MOG2 bypassed on the Hailo path (`03`, `CLAUDE.md`) | `uses_motion_regions=False` | ✅ |
| 14 | "5-FPS substream rate", "600 ms minimum lock latency at 5 fps", "200 ms substream interval" (`03-pipeline.md` levers §2, targets; Pi `chapters.jsx:495,715,883`) | Pi decodes the native ~30 fps substream (child at full rate when tracks active) | **STALE** (Coral-era numbers) |
| 15 | Video-clock overlay engine, buffered playout, Smooth ≈0.8 s jitter buffer, +5 ms certified (`05`, `10`, `CLAUDE.md`) | `pi_dash.html` rVFC engine (audited 07-17); rig numbers in `11` | ✅ (will change with identify-then-render) |
| 16 | Coasting tracks render dashed with bbox frozen (`05`, `10:68`) | v3 froze bbox; v4 **predicts** the box while coasting (Kalman) — SSE `coasting` flag unchanged | ⚠️ partially stale |
| 17 | Health counters: `tracker.id_switches` (`03-pipeline.md` §threshold fitness) | v4 adds `lost_tracks`, `reid_revivals`, `vote_merges`, `vote_splits` (commit msg); `id_switches` semantics differ | **STALE/incomplete** |
| 18 | 5 systemd services + 4 timers, journald logging (`02`, deploy README, `CLAUDE.md`) | verified 07-17; `02-services.md` still fine | ✅ (Pi book `README.md` row still says "4 services") |
| 19 | Pi book = chapters 00-12 (`CLAUDE.md`) vs "00 through 10" (Pi `GUIDE.md:7`, `docs/README.md:3`) | 13 chapters exist | **STALE** (GUIDE/README) |
| 20 | Repo-split doc at `docs/working/progress/2026-04-25-pi-repo-split.md` (Pi `GUIDE.md:22`, `docs/README.md:12`) | file is at `docs/historical/2026-04-25-pi-repo-split.md` | **STALE** (broken link) |
| 21 | "Cross-cutting fixes flow via cross-claude-comms.md; David relays" (`00-overview.md:87`, `08-deployment.md`, Pi book `README.md:56`, `03-pipeline.md:331`) | single operator since 07-01; file frozen | **STALE** (process claim) |
| 22 | Tunnel exposes `go2rtc.vivessato.com` (`00-overview.md` table; iMac `README.md`, `CLAUDE.md` services row) | Pi tunnel exposes only pi5→8099; iMac hostname "retired" in `index.html` but still routed in `~/.cloudflared/config.yml` (open smell) | **STALE** on Pi; ⚠️ iMac |
| 23 | iMac tracker threshold "2.0 for both iMac and Pi" (`23-live-detection.md:123,351`) | iMac `bird_pipeline_v3.py:262` = **2.5**; Pi = v4 | **STALE** |
| 24 | Integrity audit hourly (`working/reference/systemd-services-phase0.md`) | daily 03:40 (`deploy/systemd/README.md`, verified live 07-17) | **STALE** |
| 25 | ROADMAP Ch1 residual = "appearance ReID polish" (`ROADMAP.md:20,308`; `00-overview.md:55`) | landed today (v4 hue/sat-histogram + vote ReID) | **STALE** as of 14:22 |

Verdict: 14 of 25 stale/partial. All 14 cluster around the tracker, the pre-30fps performance envelope, the dead two-Claude workflow and the retired ring buffer. The snapshot flow, vote-lock, process cage and services are accurate.

## Files / sections that MUST be rewritten after tracker-v4 + identify-then-render land

Ordered by reader exposure (the Pi book is served live at `http://pi5.local:8099/book/`).

| Priority | File | Sections | Why |
|---|---|---|---|
| P0 | `~/docs/bird-observatory-pi/03-pipeline.md` | data-flow diagram box "BirdTracker (Norfair + Frigate dist)"; §"Tracker (`pipeline/tracker.py`)"; §"Classifier" (add `note_vote` ReID hook, split/merge semantics); lever #4 "Tracker distance threshold"; watch-out #8; §"Tracker threshold and the ID-switch honesty contract" (whole section); §"What we're choosing NOT to do" (ByteTrack/ReID lines); §"What 'as good as we possibly can' looks like" (5 fps numbers); refs "Norfair (current tracker)"; SSE paragraph (add v4 health counters + any new `verified_at`/render-delay fields) | v3 → v4; identify-then-render adds a "verified label" stage before SSE |
| P0 | `~/docs/bird-observatory-pi/docs-book/book/chapters.jsx` (+ rebuild `book.bundle.js`, redeploy per README-BUILD) | lines ~202, 238-240, 281, 335-345, 388-400, 427, 464-471, 495, 538-545, 575, 591, 625-629, 715, 883, 1237, 1533, 1595, 1690, 1718-1730, 1851, 2443-2501 | JSX duplicate of ch03/04/07 — every Norfair / ring-buffer / 0.35 / 5-fps claim |
| P0 | `bird-classifier-pi/CLAUDE.md` | line 73 "2026-06-29 foundations" (drop v3 knobs or label "v3 only"); line 8 comms sentence; Video Path bullet (buffered playout of *verified* labels) | one-screen truth for every session |
| P0 | `bird-classifier-pi/ROADMAP.md` | header date; Ch1 status cell + step 5 residuals; "A fork we'll hit (decide later)" → decided: deliberate display delay / identify-then-render; add Ch1c "identity persistence" chip with the tracker_ab numbers | the spine ledger |
| P1 | `~/docs/bird-observatory-pi/00-overview.md` | "honest ledger" paragraph; "What runs" tunnel row; "Where the code lives" Claude-persona wording; add move #6 (tracker v4) to the arc | landing page |
| P1 | `~/docs/bird-observatory-pi/10-overlay-sync.md` | add "Act IV — identify-then-render" (labels rendered only after a verified ID, on the buffered video); update avenues table (the "deliberate display delay" row from DEFERRED → LIVE); line 68 coasting sentence (v4 predicts) | overlay/buffer semantics change |
| P1 | `~/docs/bird-observatory-pi/05-dashboard.md` | "Live view" bullets: track lifecycle (revival/merge/split of `track_id`), coasting render, Smooth-mode buffer depth if the render delay grows | overlay consumer |
| P1 | `~/docs/bird-observatory-pi/11-the-demo-lab.md` | §Ground truth ("Norfair tracker"; refresh the 6/9 baseline); add `tools/tracker_ab.py` to the stack table + `lab` CLI list; add identity metrics (identities/visit, handoffs, switches) as first-class census outputs | the acceptance harness |
| P1 | `bird-classifier-pi/docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` | promote to the identify-then-render spec (or write the new spec citing it) | it already describes the target architecture |
| P2 | `~/docs/bird-observatory-pi/04-hailo-engine.md` | "5 FPS" scheduler prose | numbers |
| P2 | `~/docs/bird-observatory-pi/07-thermal.md` | "Observed range" paragraph → banner as April window; add post-F1 envelope (69 °C) | ring-buffer framing |
| P2 | `~/docs/bird-observatory/23-live-detection.md` | tracker paragraph (iMac 2.5, Pi diverged to v4 — cross-ref); non-goals | cross-system pointer |
| P2 | `~/docs/bird-observatory/07-aiy-classification.md` | add Pi section: isotonic calibration (`pipeline/calibration.py`), hi-res relabel, why "buffer time" helps AIY | the model David wants to lean on |
| P2 | `bird-classifier-pi/docs/working/2026-06-15-idea-extraction-record.md` | mark spatial-subtitle + ReID items as adopted | keeps the ledger honest |

## Ranked cleanup plan (for a later phase — nothing executed)

Each step is independent; the coordinator can stop after any step. Estimated effort is for one agent.

| Rank | Action | Files | Effort | Risk |
|---|---|---|---|---|
| 1 | **Rewrite** the P0/P1 sections above once the tracker + identify-then-render work is measured (use the `tracker_ab.py` / `lab grade` numbers as the evidence in the prose). Rebuild + redeploy `book.bundle.js`. | 9 files | 3-4 h | none |
| 2 | **Fix broken pointers** now (no dependency on the mission): Pi `GUIDE.md:7,22`, `docs/README.md:3,12` ("00 through 10", moved split doc); Pi book `README.md` ch02 row "4 services" → 5, ch03 row add "(v4 tracker)"; iMac `README.md`/`CLAUDE.md`/`00-overview.md` — add a one-line banner "**Pi is the production node; the iMac is a frozen reference (ROADMAP 2026-07-03)**"; `~/docs/_index.md:31` Bird Observatory blurb (still says iMac/Coral/Adaptive-Lock). | 7 files, ~12 lines | 20 min | none |
| 3 | **Delete** pure fluff: `_daves_observations_mac.md`, `_daves_observations_pi.md` (0 B); `~/docs/bird-observatory/.pytest_cache/`; `_truth-staging-2026-06-11.md` after confirming its 6 items landed in ch02/ch08 (they did on 07-17 per DOC_AUDIT); `working/reference/systemd-services-phase0.md` (superseded by `deploy/systemd/README.md`); `training videos/playlist.txt` (April concat list). | 6 | 10 min | none (git-tracked in `~/docs`) |
| 4 | **Collapse duplicates**: (a) fold `docs-book-codex/book/ERRATA.md` §D into `docs-book/book/ERRATA.md`, then delete `docs-book-codex/` (33 MB, otherwise byte-identical); (b) delete the 18 byte-identical copies under `bird-classifier/docs/superpowers/{plans,progress,specs}/*.md` and leave the existing REDIRECT `README.md` (its own text says the `~/docs` copy wins); (c) decide ONE home for the pre-split historical archive — `bird-classifier-pi/docs/historical/` (75 files) and `bird-classifier/docs/superpowers/*/historical/` (~70) differ only by banner bytes; keep the Pi copy (active repo), replace the iMac tree with a pointer. | ~160 files | 45 min | low — verify with `diff -rq` before each rm |
| 5 | **Banner + move to `historical/`** the ~30 unbannered working docs that are historical in substance: Pi `docs/working/plans/*` (all 5), `docs/working/progress/*` except `2026-07-03-native-crash-isolation.md`, `docs/working/specs/2026-05-10-*`, `2026-06-27-*`, `docs/superpowers/plans/2026-05-12-*` (stray dir — remove `docs/superpowers/`), `cross-claude-comms.md` (365 KB; banner its header, move, update the 4 docs that call it "the patch log"); `~/docs/bird-observatory/working/{plans,progress}/*` and `working/specs/2026-04-25-imac-live-classify-as-built.md`, `2026-04-29-pi5-migration-plan.md`, `2026-04-23-tier2-training-plan-v1.md`. Keep in `working/`: hailo playbook, 07-02 sync design, spatial-subtitle memo, flagship dossier, idea-extraction record, lit reviews 1-4. | ~30 | 40 min | needs David only for the idea-extraction "still holds/dead" marks — bank that |
| 6 | **Banner as HISTORICAL/ASPIRATIONAL**: `09-the-unified-brain.md` (45 KB — or cut to a 1-page "decision + what's left": ground camera, iMac retirement, audio consolidation), `28-yard-model-training.md` (Coral path; superseded by the flagship dossier), `wildlife-pipeline-plan.md` (FUTURE), `23-live-detection.md` levers section (iMac frozen). | 4 | 30 min | none |
| 7 | **Refresh dated meta-docs**: `working/README.md` (historical/ "currently empty" is false), `DOC_AUDIT.md` ×2 — either regenerate with a fresh `code-to-doc-verifier` run after step 1, or add a "superseded by 2026-09 audit" line; drop the 22 Pi rows copy-pasted into the iMac audit. | 3 | 15 min (or 1 h with a re-run) | none |
| 8 | **Relocate non-docs**: `~/docs/bird-observatory/_additional_datasets/` (2.3 GB CUB-200 text dumps) out of the docs repo/Syncthing folder; `training videos/` (1 GB of MP4) is fine where it is but `may10_demo_video.annotations.md` should be listed in the Pi book README as the ground-truth asset. | 2 dirs | 5 min + copy | check `.stignore` first |
| 9 | **Conventions gap** (`~/docs/CONVENTIONS.md` rule 5): neither bird folder has a `_pulse.md`; `~/docs/bird-observatory/` has no `gotchas.md` at Pi level (Pi gotchas live inside ch01/ch03/ch08). Bootstrap a pulse via the `docs-discipline` skill after step 1 so the dashboard at dash.vivessato.com reflects the mission. | 2 | 15 min | none |

## Banked for David (needs his call, not the coordinator's)

1. Adjudicate the `2026-06-15-idea-extraction-record.md` "still holds / dead" marks so step 5 can archive its ~15 source files.
2. Keep or kill the Pi **JSX book** (`chapters.jsx` + bundle). It is a second copy of every chapter that has drifted three times in five months; the alternative is serving the markdown chapters directly at `/book/`.
3. Whether the iMac doc set (`~/docs/bird-observatory/`, 81 files) stays "CURRENT for a frozen machine" or is bannered wholesale as reference-only.
4. The 2.3 GB CUB dataset copy inside the synced docs folder.

## Open smells carried forward from the 07-17 audits (still true today, code-side, not docs)

- Pi `~/.bird-observatory-env` still says `PIPELINE_HIRES_RING=authoritative` with an affirmative comment (nothing reads it).
- Retired `bird-demo-loop.service` still installed (disabled) on the Pi.
- `go2rtc.service`, `cloudflared.service`, `bird-audio.service` lack `OnFailure=bird-alert@%N`.
- iMac `~/.cloudflared/config.yml` still routes `go2rtc.vivessato.com` → :1984 (unauthenticated go2rtc API on the internet) although the dashboard calls the hostname retired.
- Pi deployed `pipeline/hires_ring.py` and `pipeline/av_log_guard.py` are older than the mirror (mirror carries the "proven ineffective" tombstone docstring; the Pi still carries "THE KILL" text) — harmless, but the "deployed == mirror" claim is not literally true; one `rsync` fixes it.

---

## Full file table

### PI-BOOK — `docs/bird-observatory-pi` (17 files)

| File | Size | Modified | Class | Purpose / evidence | Rewrite after tracker + identify-then-render? |
|---|---|---|---|---|---|
| `00-overview.md` | 8.1K | 2026-07-17 | STALE | Pi overview, 'where we are 2026-07-03' arc, honest ledger. Ledger still says 'remaining polish is appearance ReID' (landed today as v4); tunnel row still lists go2rtc.vivessato.com; 'Pi-Claude/iMac-Claude' framing. | YES |
| `01-hardware.md` | 14.6K | 2026-07-17 | CURRENT | Pi 5/Hailo/NVMe/camera inventory, boot architecture (RTL9210 history bannered SUPERSEDED), filesystem, thermal feel. | no |
| `02-services.md` | 11.0K | 2026-07-17 | CURRENT | 5 systemd services + 4 timers + canary/watchdog, env, demo mode, logs (journald). Verified 2026-07-17. | no |
| `03-pipeline.md` | 57.9K | 2026-07-17 | STALE | The spine: frame->detect->track->classify->snapshot. Tracker section (§'Tracker', §'Tracker threshold and the ID-switch honesty contract', diagram box, lever #4, watch-out #8, non-goals 'no ByteTrack swap / no ReID', refs 'Norfair (current tracker)') all describe the retired v3 Norfair tracker; '5-FPS substream', '600 ms lock latency at 5 fps', '~76% precision' are pre-30fps numbers; process-boundary + snapshot + vote-lock sections are accurate. | YES — primary rewrite target |
| `04-hailo-engine.md` | 44.7K | 2026-07-17 | CURRENT | Shared VDevice / ROUND_ROBIN scheduler architecture, HEF quirks, bench numbers, levers. '5 FPS substream target' in scheduler prose is stale (30 fps). | minor |
| `05-dashboard.md` | 10.9K | 2026-07-17 | CURRENT | pi_dash.html surface, video-clock overlay engine, demo-mode toggle, Model Lab switch. Coasting-track rendering + track lifecycle will need a sentence once identify-then-render (buffered playout of verified labels) lands. | YES (overlay section) |
| `06-pi-review.md` | 6.7K | 2026-07-17 | CURRENT | Pi-review v2 API (5 verdicts, history/undo), schema, per-classifier accuracy. Verified 2026-07-17. | no |
| `07-thermal.md` | 8.6K | 2026-07-17 | CURRENT* | Thermal envelope + watch tool. *'Observed range' paragraph still frames load as 'hi-res ring buffer at 5 fps' and 'after PIPELINE_HIRES_RING=authoritative' (ring retired 2026-05-12) — should be bannered as the April window. | minor |
| `08-deployment.md` | 12.1K | 2026-07-17 | CURRENT* | rsync workflow, runbook, don't-do list. *Repo-split table still says 'iMac-Claude only / Pi-Claude' and 'cross-cutting fixes flow as [patch] posts in the comms file' (dead workflow). | minor |
| `09-the-unified-brain.md` | 44.6K | 2026-07-03 | FLUFF (bannered) | 45 KB 'destination, not what runs today' migration essay (decided 04-26). Bannered honestly, but the Pi IS the primary brain now and the iMac is a frozen reference — the plan's premise has been overtaken; long lever/research sections duplicate ch03/ch04 patterns. | no — banner as HISTORICAL/ASPIRATIONAL or cut to 1 page |
| `10-overlay-sync.md` | 32.4K | 2026-07-04 | CURRENT (layered) | Three-act overlay-sync history; Act III = live video-clock engine. Acts I/II preserved as history with SUPERSEDED banner. Coasting/dashed rendering + the 'deliberate display delay' fork will change with identify-then-render. | YES (Act IV or new avenues row) |
| `11-the-demo-lab.md` | 8.6K | 2026-07-07 | CURRENT* | Sim camera, timecode, ground truth, sync rig, `tools/lab` CLI, ten-minute recipe. *'Ground truth' section says 'Norfair tracker'; the 6/9 lock number predates cadence/v4 changes; tools/tracker_ab.py (A/B harness) not listed. | YES (small) |
| `12-audio.md` | 4.3K | 2026-07-07 | CURRENT | BirdNET port to the Pi (auto-gain, silence-trim, self-heal). | no |
| `README.md` | 5.2K | 2026-07-07 | CURRENT* | Pi book chapter index + where-things-live. *Says ch02 = '4 services' (is 5), lists cross-claude-comms as live bus, 'Pi-Claude commits'. | minor |
| `_truth-staging-2026-06-11.md` | 2.0K | 2026-06-12 | FLUFF | 2 KB staging note of facts to fold into ch02/ch07/ch08/ch10 (canary, watchdog, journald persistence). Check each item landed (most did on 07-17), then delete. | no |
| `docs-book/README-BUILD.md` | 1.9K | 2026-07-08 | CURRENT | How to rebuild book.bundle.js with esbuild and deploy /book/ to the Pi. | no |
| `docs-book/book/chapters.jsx` | 162.1K | 2026-07-17 | STALE (duplicate surface) | 166 KB JSX copy of the Pi chapters, served LIVE at pi5:8099/book/. Still says: Norfair + threshold 2.0/2.5, 'hi-res ring buffer default-on' (x6), PIPELINE_HIRES_RING=authoritative, vote-lock >=0.35, '5 FPS substream', 'Lock-time latency 600 ms at 5 fps'. Every chapter fix must be re-applied here + bundle rebuilt. | YES — or retire the JSX copy |

### IMAC-DOCS — `docs/bird-observatory` (86 files)

| File | Size | Modified | Class | Purpose / evidence | Rewrite after tracker + identify-then-render? |
|---|---|---|---|---|---|
| `00-how-it-works.md` | 8.1K | 2026-05-06 | CURRENT (iMac) | Plain-English overview of the iMac observatory. | no |
| `01-architecture.md` | 12.3K | 2026-07-17 | CURRENT (iMac) | iMac system overview; Norfair tracker + 0.35 lock are correct FOR THE iMac. | no |
| `02-hardware.md` | 6.0K | 2026-05-06 | CURRENT (iMac) | iMac, Coral, cameras, mics. | no |
| `03-network.md` | 5.7K | 2026-07-17 | CURRENT (iMac) | Ports/tunnel map. Verified 07-17. | no |
| `06-yolov8-detection.md` | 4.8K | 2026-04-30 | CURRENT (iMac) | YOLOv8n ONNX/CoreML detector on iMac. | no |
| `07-aiy-classification.md` | 24.8K | 2026-04-30 | CURRENT (shared) | AIY Birds V1 965-class model — the classifier BOTH systems run. Best single reference for the model David called 'our best ally'. Written iMac-side; Pi calibration (isotonic map) not mentioned. | YES (add Pi calibration + hi-res relabel section) |
| `09-regional-filter.md` | 28.2K | 2026-04-30 | CURRENT (shared) | 62-species Chilmark allowlist + range validation. | no |
| `10-jsonl-data.md` | 8.9K | 2026-04-30 | CURRENT (iMac) | classifications.db schema + JSONL backup. | no |
| `11-api-endpoints.md` | 17.0K | 2026-04-30 | CURRENT (iMac) | Every FastAPI route on the iMac dashboard. | no |
| `12-dashboard-ui.md` | 14.5K | 2026-04-30 | CURRENT (iMac) | iMac SPA tabs. | no |
| `13-species-images.md` | 5.6K | 2026-04-30 | CURRENT (shared) | Species-image cache pipeline. | no |
| `16-sse-streaming.md` | 9.1K | 2026-04-26 | CURRENT (iMac) | SSE track-event protocol (iMac shape; Pi adds pts/seq/emit_ms/coasting). | minor |
| `17-auth.md` | 4.3K | 2026-05-01 | CURRENT (iMac) | No auth on birds.vivessato.com; CORS list. | no |
| `18-launchagents.md` | 14.8K | 2026-07-17 | CURRENT (iMac) | 10 LaunchAgents. Verified 07-17. | no |
| `20-deployment.md` | 4.5K | 2026-04-30 | CURRENT (iMac) | verify.sh / deploy.sh. | no |
| `22-gotchas.md` | 40.8K | 2026-04-30 | CURRENT (log) | 42 KB lessons log; audit banner says which entries reference retired infra. | no |
| `23-live-detection.md` | 51.5K | 2026-05-01 | STALE (one claim) / CURRENT (iMac) | iMac v3 pipeline reference + strategic levers. Says tracker threshold '2.0 for both iMac and Pi' — iMac code is 2.5 (bird_pipeline_v3.py:262), Pi runs v4 (no Norfair threshold). Non-goals 'not switching tracker / not adding ReID' are now contradicted by the Pi. | YES (cross-ref note: Pi diverged to v4) |
| `24-custom-yolo-training.md` | 36.6K | 2026-04-30 | CURRENT (iMac) | YOLO retraining workflow. | no |
| `25-audio-analyzer.md` | 49.0K | 2026-07-17 | CURRENT (iMac) | 50 KB BirdNET analyzer reference incl. long 'how to think about acoustic ID' essay (aspirational, flagged in 07-17 audit). | no |
| `26-enhanced-audio.md` | 5.7K | 2026-04-30 | CURRENT (iMac) | Bandpass MP3 stream. | no |
| `28-yard-model-training.md` | 40.9K | 2026-04-30 | HISTORICAL-ish | 42 KB Coral 12-species yard model training pipeline. Coral path is iMac-only and auto-degrades; flagship design (Pi repo 2026-06-29) supersedes the direction. Not bannered. | no (banner: superseded by flagship dossier) |
| `31-label-motion-adaptive-lock.md` | 21.5K | 2026-04-30 | CURRENT (iMac) | Browser-side Adaptive Lock smoothing on iMac /live.html (5-7 Hz events). Pi replaced this with the video-clock engine — say so at top. | minor (cross-ref) |
| `DOC_AUDIT.md` | 30.3K | 2026-04-27 | HISTORICAL | 2026-04-26 audit report of the ~/docs tree (moves/rewrites). Dated record; no banner but self-dated. | no |
| `README.md` | 8.7K | 2026-07-17 | STALE (framing) | iMac doc index. Presents the iMac as 'everything' with no banner that the Pi is the primary/production node and the iMac a frozen reference (ROADMAP: 'do not develop'). Architecture diagram: vote-lock >=0.35 (true on iMac), tunnel lists go2rtc.vivessato.com (retired hostname, still routed — smell). | no (banner) |
| `_daves_observations_mac.md` | 0B | 2026-04-25 | FLUFF | 0 bytes. Empty scaffolding since 04-25. | no (delete) |
| `_daves_observations_pi.md` | 0B | 2026-04-25 | FLUFF | 0 bytes. Empty scaffolding since 04-25. | no (delete) |
| `appendix/data-flow-diagram.txt` | 10.4K | 2026-04-26 | CURRENT (iMac) | ASCII data-flow for the iMac. | no |
| `appendix/env-reference.md` | 14.7K | 2026-05-01 | CURRENT (iMac) | IPs/ports/paths/env for the iMac. | no |
| `appendix/file-manifest.md` | 11.0K | 2026-04-26 | CURRENT (iMac) | iMac file manifest; 'Norfair tracker' rows are correct for iMac. | no |
| `cross-claude-comms-history.md` | 16.7K | 2026-05-01 | HISTORICAL | Condensed archive of the 04-25..05-01 comms bus. Fine. | no |
| `docs-book-codex/DOC_AUDIT.md` | 7.5K | 2026-04-30 | FLUFF (dup) | Byte-identical to docs-book/DOC_AUDIT.md. | no |
| `docs-book-codex/HANDOFF.md` | 15.0K | 2026-05-01 | FLUFF (dup) | Byte-identical to docs-book/HANDOFF.md. | no |
| `docs-book-codex/book/ERRATA.md` | 8.7K | 2026-05-06 | FLUFF (near-dup) | docs-book copy with a 05-06 hardware-corrections section D that the main ERRATA lacks — fold D into docs-book/book/ERRATA.md then delete the 33 MB codex tree. | no |
| `docs-book/DOC_AUDIT.md` | 7.5K | 2026-04-30 | HISTORICAL | 04-30 book audit. | no |
| `docs-book/HANDOFF.md` | 15.0K | 2026-05-01 | HISTORICAL | 04-27 handoff to the book-design Claude. | no |
| `docs-book/book/ERRATA.md` | 7.7K | 2026-05-01 | HISTORICAL | Book errata; section A resolved. | no |
| `docs-book/book/chapters.jsx` | 316.1K | 2026-04-30 | STALE (duplicate surface) | iMac book JSX copy of the iMac chapters; frozen with the iMac. Not served by the Pi. | no |
| `frigate-lessons-for-bird-observatory.md` | 11.0K | 2026-04-14 | HISTORICAL (bannered) | Frigate patterns that shaped v3; status banner says all implemented. | no |
| `product-compass.md` | 22.9K | 2026-05-09 | CURRENT (values) | David's stated values/frustrations/direction (05-09). Values current; tech mentions (ring buffer, hit_counter 90) dated. | no |
| `training videos/may10_demo_video.annotations.md` | 10.8K | 2026-05-10 | CURRENT (ground truth) | David's frame-by-frame annotations of the may10 reel — the acceptance bar for the lab (9 visits, in-frame + identifiable windows). | no — protect |
| `training videos/playlist.txt` | 143B | 2026-04-12 | FLUFF | 4-line ffmpeg concat playlist for the April clips. | no |
| `wildlife-pipeline-plan.md` | 11.8K | 2026-03-20 | FLUFF | Mammal/SpeciesNet plan (03-20), never started, no status banner. Aspirational. | no (banner FUTURE or move to working/) |
| `working/README.md` | 1.8K | 2026-04-26 | CURRENT (meta) | Explains working/ scaffolding. Says historical/ 'currently empty' — it holds one plan. | minor |
| `working/plans/2026-04-26-rc2-from-calibration.md` | 7.9K | 2026-04-26 | HISTORICAL (unbannered) | RC2 plan 'pending David's v1/v2/v3 decision' since 04-26; iMac frozen, Pi calibration (06-29) superseded the idea. Byte-identical dup in iMac repo. | no |
| `working/plans/README.md` | 511B | 2026-04-26 | FLUFF (dup) | Byte-identical to bird-classifier/docs/superpowers/plans/README.md. | no |
| `working/progress/2026-04-25-side-findings.md` | 4.6K | 2026-04-26 | HISTORICAL (unbannered) | iMac side-findings queue (dup of repo copy). | no |
| `working/progress/2026-04-26-evening-handoff.md` | 18.4K | 2026-04-26 | HISTORICAL (unbannered) | iMac session handoff (dup of repo copy). | no |
| `working/progress/2026-04-29-imac-claude-compact-prompt.md` | 15.4K | 2026-04-29 | HISTORICAL (unbannered) | iMac-Claude compaction prompt — three-Claude era. | no |
| `working/progress/README.md` | 802B | 2026-04-26 | FLUFF (dup) | Byte-identical to repo copy. | no |
| `working/specs/2026-04-23-airtight-review-system.md` | 17.9K | 2026-04-26 | HISTORICAL (unbannered, dup) | iMac review2 system spec, shipped. | no |
| `working/specs/2026-04-23-litreview-1-bird-classifiers.md` | 13.5K | 2026-04-23 | CURRENT (research, dup) | Lit review feeding the flagship; still relevant input to the flagship dossier. | no |
| `working/specs/2026-04-23-litreview-2-calibration-ood.md` | 14.1K | 2026-04-23 | CURRENT (research, dup) | Calibration/OOD lit review — the Pi's isotonic calibration came from this line. | no |
| `working/specs/2026-04-23-litreview-3-small-noisy-imbalanced.md` | 14.0K | 2026-04-23 | CURRENT (research, dup) | Lit review. | no |
| `working/specs/2026-04-23-litreview-4-quantization-deployment.md` | 12.6K | 2026-04-23 | CURRENT (research, dup) | Lit review (Coral-targeted; Hailo section needed). | no |
| `working/specs/2026-04-23-tier2-training-plan-v1.md` | 18.0K | 2026-04-26 | HISTORICAL (self-marked superseded, dup) | Tier-2 training plan v1; header says superseded in part. | no |
| `working/specs/2026-04-25-imac-live-classify-as-built.md` | 26.3K | 2026-04-26 | HISTORICAL (unbannered, dup x3) | iMac live-classify as-built 04-25 — also in both repos' historical/. Three copies. | no |
| `working/specs/2026-04-25-review-ui-helpers.md` | 4.6K | 2026-04-26 | CURRENT (iMac, dup) | Review-tab JS helpers. | no |
| `working/specs/2026-04-29-pi5-migration-plan.md` | 31.6K | 2026-04-29 | HISTORICAL (unbannered) | 32 KB iMac->Pi migration plan 'draft, pre-review'. Overtaken: Pi is primary, iMac frozen. | no |
| `working/specs/README.md` | 1.7K | 2026-04-26 | FLUFF (dup) | Byte-identical to repo copy. | no |
| `historical/04-snapshot-capture.md` | 4.5K | 2026-04-26 | HISTORICAL | Archived 04 snapshot capture. | no |
| `historical/05-snapshot-sync.md` | 482B | 2026-03-27 | HISTORICAL | Archived 05 snapshot sync. | no |
| `historical/08-classify-pipeline.md` | 10.8K | 2026-04-25 | HISTORICAL | Archived 08 classify pipeline. | no |
| `historical/14-birdnet-go.md` | 3.6K | 2026-03-15 | HISTORICAL | Archived 14 birdnet go. | no |
| `historical/15-birdnet-export.md` | 4.2K | 2026-03-19 | HISTORICAL | Archived 15 birdnet export. | no |
| `historical/19-nas-services.md` | 8.1K | 2026-03-27 | HISTORICAL | Archived 19 nas services. | no |
| `historical/21-syncthing.md` | 3.5K | 2026-04-26 | HISTORICAL | Archived 21 syncthing. | no |
| `historical/27-quality-audit.md` | 6.0K | 2026-04-26 | HISTORICAL | Archived 27 quality audit. | no |
| `historical/29-infrastructure-overhaul-april-2026.md` | 6.3K | 2026-04-26 | HISTORICAL | Archived 29 infrastructure overhaul april 2026. | no |
| `historical/30-player-overlay-audit.md` | 12.2K | 2026-04-25 | HISTORICAL | Archived 30 player overlay audit. | no |
| `historical/30-session-handoff-april-15-2026.md` | 9.8K | 2026-04-26 | HISTORICAL | Archived 30 session handoff april 15 2026. | no |
| `historical/30a-player-overlay-audit.md` | 11.5K | 2026-04-26 | HISTORICAL | Archived 30a player overlay audit. | no |
| `historical/32-monterey-migration.md` | 22.6K | 2026-04-26 | HISTORICAL | Archived 32 monterey migration. | no |
| `historical/34-pi5-migration.md` | 11.7K | 2026-04-26 | HISTORICAL | Archived 34 pi5 migration. | no |
| `historical/35-pi5-prep-runbook.md` | 8.3K | 2026-04-26 | HISTORICAL | Archived 35 pi5 prep runbook. | no |
| `historical/migration/HANDOFF.md` | 12.7K | 2026-04-26 | HISTORICAL | Archived HANDOFF. | no |
| `historical/migration/archive/MEMORY-full.md` | 5.2K | 2026-04-26 | HISTORICAL | Archived MEMORY full. | no |
| `historical/migration/archive/conversation-context.md` | 18.2K | 2026-04-26 | HISTORICAL | Archived conversation context. | no |
| `historical/migration/archive/file-tree.md` | 18.0K | 2026-04-26 | HISTORICAL | Archived file tree. | no |
| `historical/migration/archive/project-memories.md` | 1.5K | 2026-04-26 | HISTORICAL | Archived project memories. | no |
| `historical/migration/archive/system-state-2026-03-19.md` | 1.9K | 2026-04-26 | HISTORICAL | Archived system state 2026 03 19. | no |
| `historical/migration/archive/system-state-2026-03-20.md` | 2.4K | 2026-04-26 | HISTORICAL | Archived system state 2026 03 20. | no |
| `historical/migration/model-retraining-spec.md` | 9.5K | 2026-04-26 | HISTORICAL | Archived model retraining spec. | no |
| `historical/migration/system-state-2026-03-22.md` | 11.8K | 2026-04-26 | HISTORICAL | Archived system state 2026 03 22. | no |
| `historical/migration/system-state-2026-03-23.md` | 11.8K | 2026-04-26 | HISTORICAL | Archived system state 2026 03 23. | no |
| `historical/migration/system-state-2026-03-27.md` | 7.3K | 2026-04-26 | HISTORICAL | Archived system state 2026 03 27. | no |
| `working/historical/plans/2026-04-22-data-integrity-audit.md` | 12.9K | 2026-04-26 | HISTORICAL | Archived data integrity audit. | no |

### PI-REPO — `bird-classifier-pi` (97 files)

| File | Size | Modified | Class | Purpose / evidence | Rewrite after tracker + identify-then-render? |
|---|---|---|---|---|---|
| `CLAUDE.md` | 7.6K | 2026-09-17 | CURRENT* | Mission + Pi architecture + rules; pipeline line already names BirdTrackerV4 (updated today). *Line 73 '2026-06-29 foundations' still lists v3-only knobs (hit_counter_max 150, distance_threshold 2.5, PIPELINE_TRACK_*) as if live; comms-file sentence describes a dead workflow. Deployed copy on the Pi is an older revision (no run-time effect). | YES (small) |
| `DOC_AUDIT.md` | 17.5K | 2026-07-17 | HISTORICAL | 2026-07-17 audit report (183 claims). Dated record. Its 5 smells are still open (env PIPELINE_HIRES_RING line on the Pi, retired bird-demo-loop unit installed, 3 units without OnFailure, untracked nice drop-in, go2rtc.vivessato.com still routed on iMac). | no |
| `GUIDE.md` | 2.1K | 2026-07-03 | STALE | Repo-side pointer guide. Says book is 'chapters 00 through 10' (is 00-12); links docs/working/progress/2026-04-25-pi-repo-split.md (moved to docs/historical/). | no (fix 2 lines) |
| `ROADMAP.md` | 16.7K | 2026-07-04 | CURRENT* | The spine ledger (chapters 1/2/3). *Header 'Last updated 2026-07-03' while body cites 07-04; Ch1 residual 'appearance ReID polish' landed today (v4); nothing yet about identify-then-render (the display-delay fork it defers is now the plan). | YES |
| `deploy/systemd/README.md` | 5.7K | 2026-07-17 | CURRENT | systemd units source of truth (07-17). Honest about 3 units lacking OnFailure. | no |
| `docs/README.md` | 1.4K | 2026-07-03 | STALE | In-repo docs map. 'chapters 00 through 10'; points at progress/2026-04-25-pi-repo-split.md (moved). | no (fix) |
| `docs/superpowers/plans/2026-05-12-live-label-sync-plan.md` | 8.0K | 2026-05-21 | HISTORICAL (unbannered, stray dir) | Only file left in the legacy docs/superpowers/ path; superseded by the 07-02 engine. | no (move) |
| `docs/working/2026-06-15-idea-extraction-record.md` | 12.8K | 2026-06-15 | CURRENT (meta) | Compressed map of ideas mined from working/ files, awaiting David's still-holds/dead adjudication so the sources can be archived. Names ROADMAP's spatial-subtitle/deliberate-delay idea as top revival candidate — exactly the identify-then-render direction. | YES (mark adjudicated items) |
| `docs/working/plans/2026-05-10-pi-overlay-sync-bedrock.md` | 97.6K | 2026-05-10 | HISTORICAL (unbannered) | 100 KB HLS+canvas overlay plan — Act I, superseded 05-11 and again 07-02. | no |
| `docs/working/plans/2026-05-11-codex-audit-prompts.md` | 11.0K | 2026-05-11 | HISTORICAL (unbannered) | Paste-ready Codex prompts for the 05-11 CPU audit. | no |
| `docs/working/plans/2026-05-11-overnight-execution.md` | 9.8K | 2026-05-11 | HISTORICAL (unbannered) | 05-11 overnight plan; result file exists. | no |
| `docs/working/plans/2026-05-11-pipeline-cpu-audit-plan.md` | 18.8K | 2026-05-11 | HISTORICAL (unbannered) | 05-11 CPU audit plan (213% CPU era; fixed by F1 06-28). | no |
| `docs/working/plans/2026-06-28-pi-live-id-foundations-plan.md` | 24.3K | 2026-06-28 | HISTORICAL (unbannered) | F0-F3 foundations plan — shipped 06-29. | no |
| `docs/working/progress/2026-04-25-pi5-handoff.md` | 25.7K | 2026-04-25 | HISTORICAL (unbannered) | Pi bring-up handoff; 5-hour outage story referenced by ch03. | no |
| `docs/working/progress/2026-05-10-bedrock-overlay-sync-runbook.md` | 3.8K | 2026-05-10 | HISTORICAL (unbannered) | Replay harness runbook, port 8654 mediamtx era (dead per ch02). | no |
| `docs/working/progress/2026-05-10-overlay-sync-handoff.md` | 10.3K | 2026-05-10 | HISTORICAL (unbannered) | 05-10 late handoff. | no |
| `docs/working/progress/2026-05-11-handoff-to-conversational-claude.md` | 17.9K | 2026-05-11 | HISTORICAL (unbannered) | Cold-start brief for a Claude.ai session. | no |
| `docs/working/progress/2026-05-11-overnight-result.md` | 15.0K | 2026-05-11 | HISTORICAL (unbannered) | 05-11 overnight result. | no |
| `docs/working/progress/2026-05-12-codex-live-log.md` | 20.1K | 2026-05-21 | HISTORICAL (unbannered) | Codex takeover running log. | no |
| `docs/working/progress/2026-05-12-codex-takeover-control.md` | 4.8K | 2026-05-21 | HISTORICAL (unbannered) | Codex control note (branch/pi-main rules — the still-true bits live in CLAUDE.md). | no |
| `docs/working/progress/2026-05-15-codex-work-log.md` | 7.6K | 2026-05-21 | HISTORICAL (unbannered) | Codex 05-15 log. | no |
| `docs/working/progress/2026-07-03-native-crash-isolation.md` | 3.4K | 2026-07-03 | CURRENT | The two-crasher SEGV forensics; paired with ch03 §process boundary. | no |
| `docs/working/progress/cross-claude-comms.md` | 356.1K | 2026-07-03 | HISTORICAL (365 KB, unbannered) | Dead message bus; the file header still says 'two Claudes ... both sides poll it'. Condensed archive exists in ~/docs. Referenced as 'the patch log' by CLAUDE.md/GUIDE/README. | no (banner + move to historical/) |
| `docs/working/progress/tier2-readiness-checkpoint.md` | 7.3K | 2026-04-29 | HISTORICAL (unbannered) | 04-29 Tier-2 readiness (AIY 67.96% top-1 baseline) — superseded by the 06-29 flagship dossier. | no |
| `docs/working/reference/systemd-services-phase0.md` | 4.5K | 2026-04-29 | STALE | Phase-0 unit definitions (04-29): says integrity audit hourly (now daily 03:40), points to cross-claude-comms; superseded by deploy/systemd/README.md. | no (delete or archive) |
| `docs/working/specs/2026-04-25-hailo-playbook.md` | 31.7K | 2026-04-25 | CURRENT (reference) | Deep Hailo-8L API/scheduler/DFC playbook — canonical pairing for ch04. | no |
| `docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md` | 48.8K | 2026-05-10 | HISTORICAL (unbannered) | 50 KB Act-I overlay design, superseded twice. | no |
| `docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` | 21.6K | 2026-05-11 | CURRENT (design input) | Codex 'labels as spatial subtitles over a media clock + deliberate delay' memo — ROADMAP calls it the most complete answer to the timing fork; it IS the identify-then-render architecture. Promote, don't archive. | YES (becomes the spec basis) |
| `docs/working/specs/2026-06-27-pi-live-id-foundations-design.md` | 11.7K | 2026-06-27 | HISTORICAL (unbannered) | F0-F3 design, shipped 06-29. Mentions Norfair/hit_counter tuning as follow-ups (now v4). | no |
| `docs/working/specs/2026-06-29-flagship-classifier-design.md` | 13.9K | 2026-06-29 | CURRENT (DRAFT) | Flagship classifier dossier — Phase 1 data manifest done, Phase 2 blocked on cloud GPU + David's data cleaning. The doc David needs for 'why was the flagship crap' (data audit section). | no (status refresh) |
| `docs/working/specs/2026-07-02-overlay-video-clock-sync-design.md` | 25.6K | 2026-07-02 | CURRENT | Video-clock overlay engine design + certified numbers; pairs with ch10 Act III. Mentions hit_counter_max coast (v3) in passing. | minor |
| `test_clips/README.md` | 3.2K | 2026-03-22 | HISTORICAL-ish | Mock RTSP clip serving (03-22, iMac-era ports). Contains live camera RTSP token URLs. | no |
| `docs/historical/2026-04-25-pi-repo-split.md` | 4.6K | 2026-06-15 | HISTORICAL | Archived pi repo split. | no |
| `docs/historical/2026-04-29-session-summary.md` | 8.5K | 2026-06-15 | HISTORICAL | Archived session summary. | no |
| `docs/historical/2026-04-30-docs-state.md` | 2.3K | 2026-06-15 | HISTORICAL | Archived docs state. | no |
| `docs/historical/DOC_AUDIT_PI_BOOK.md` | 11.4K | 2026-06-15 | HISTORICAL | Archived DOC_AUDIT_PI_BOOK. | no |
| `docs/historical/phase1-daily-validation.md` | 8.8K | 2026-06-15 | HISTORICAL | Archived phase1 daily validation. | no |
| `docs/historical/phase1-shadow-handoff-gates.md` | 8.4K | 2026-06-15 | HISTORICAL | Archived phase1 shadow handoff gates. | no |
| `docs/historical/plans/2026-03-21-phase-0-1-quick-wins-test-infra-shared-inference.md` | 67.5K | 2026-04-26 | HISTORICAL | Archived phase 0 1 quick wins test infra shared inference. | no |
| `docs/historical/plans/2026-03-22-phase-2-reviews-sqlite.md` | 8.6K | 2026-04-26 | HISTORICAL | Archived phase 2 reviews sqlite. | no |
| `docs/historical/plans/2026-03-22-phase-3-visit-model.md` | 10.6K | 2026-04-26 | HISTORICAL | Archived phase 3 visit model. | no |
| `docs/historical/plans/2026-03-22-rtsp-resilience-plan.md` | 54.8K | 2026-04-26 | HISTORICAL | Archived rtsp resilience plan. | no |
| `docs/historical/plans/2026-03-23-audio-accuracy-plan.md` | 29.2K | 2026-04-26 | HISTORICAL | Archived audio accuracy plan. | no |
| `docs/historical/plans/2026-03-26-nas-independence-plan.md` | 8.4K | 2026-04-26 | HISTORICAL | Archived nas independence plan. | no |
| `docs/historical/plans/2026-03-28-review-system-overhaul.md` | 30.2K | 2026-04-26 | HISTORICAL | Archived review system overhaul. | no |
| `docs/historical/plans/2026-03-28-yard-model-integration.md` | 36.7K | 2026-04-26 | HISTORICAL | Archived yard model integration. | no |
| `docs/historical/plans/2026-03-30-unified-classification-query.md` | 25.3K | 2026-04-26 | HISTORICAL | Archived unified classification query. | no |
| `docs/historical/plans/2026-04-03-unified-bird-pipeline.md` | 25.9K | 2026-04-26 | HISTORICAL | Archived unified bird pipeline. | no |
| `docs/historical/plans/2026-04-08-transfer-learning-pipeline.md` | 50.5K | 2026-04-26 | HISTORICAL | Archived transfer learning pipeline. | no |
| `docs/historical/plans/2026-04-10-live-detection-v2.md` | 127.2K | 2026-04-26 | HISTORICAL | Archived live detection v2. | no |
| `docs/historical/plans/2026-04-11-live-detection-v3-phase1.md` | 97.8K | 2026-04-26 | HISTORICAL | Archived live detection v3 phase1. | no |
| `docs/historical/plans/2026-04-22-data-integrity-audit.md` | 12.9K | 2026-04-26 | HISTORICAL | Archived data integrity audit. | no |
| `docs/historical/plans/2026-04-22-garbage-data-cull.md` | 11.8K | 2026-04-26 | HISTORICAL | Archived garbage data cull. | no |
| `docs/historical/plans/2026-04-22-hires-ring-buffer.md` | 21.3K | 2026-04-26 | HISTORICAL | Archived hires ring buffer. | no |
| `docs/historical/plans/2026-04-22-live-tab-fix.md` | 10.6K | 2026-04-26 | HISTORICAL | Archived live tab fix. | no |
| `docs/historical/plans/2026-04-23-live-overlay-fixes.md` | 10.9K | 2026-04-26 | HISTORICAL | Archived live overlay fixes. | no |
| `docs/historical/plans/2026-04-25-hailo-multimodel-path1.md` | 32.8K | 2026-04-26 | HISTORICAL | Archived hailo multimodel path1. | no |
| `docs/historical/plans/2026-04-25-rc3-preserve-lock-time-vote.md` | 19.3K | 2026-04-26 | HISTORICAL | Archived rc3 preserve lock time vote. | no |
| `docs/historical/progress/2026-04-11-v3-progress.md` | 18.2K | 2026-04-26 | HISTORICAL | Archived v3 progress. | no |
| `docs/historical/progress/2026-04-11-v3-ready-for-cutover.md` | 15.6K | 2026-04-26 | HISTORICAL | Archived v3 ready for cutover. | no |
| `docs/historical/progress/2026-04-11-v3-self-audit.md` | 22.9K | 2026-04-26 | HISTORICAL | Archived v3 self audit. | no |
| `docs/historical/progress/2026-04-23-autonomous-session-handoff.md` | 7.4K | 2026-04-26 | HISTORICAL | Archived autonomous session handoff. | no |
| `docs/historical/progress/2026-04-24-pi5-overnight.md` | 3.7K | 2026-04-26 | HISTORICAL | Archived pi5 overnight. | no |
| `docs/historical/progress/2026-04-25-compact-prompt-v2.md` | 8.1K | 2026-04-26 | HISTORICAL | Archived compact prompt v2. | no |
| `docs/historical/progress/2026-04-25-compact-prompt.md` | 3.4K | 2026-04-26 | HISTORICAL | Archived compact prompt. | no |
| `docs/historical/progress/2026-04-25-detection-snapshot-audit-findings.md` | 8.1K | 2026-04-26 | HISTORICAL | Archived detection snapshot audit findings. | no |
| `docs/historical/progress/2026-04-25-morning-brief.md` | 9.4K | 2026-04-26 | HISTORICAL | Archived morning brief. | no |
| `docs/historical/progress/2026-04-25-review-ui-debug-log.md` | 5.7K | 2026-04-26 | HISTORICAL | Archived review ui debug log. | no |
| `docs/historical/progress/2026-04-25-self-handoff.md` | 12.7K | 2026-04-26 | HISTORICAL | Archived self handoff. | no |
| `docs/historical/progress/2026-04-25-side-findings.md` | 4.0K | 2026-04-26 | HISTORICAL | Archived side findings. | no |
| `docs/historical/reviews/2026-04-10-live-detection-v2-review.md` | 65.5K | 2026-04-26 | HISTORICAL | Archived live detection v2 review. | no |
| `docs/historical/specs/2026-03-21-foundations-design.md` | 26.9K | 2026-04-26 | HISTORICAL | Archived foundations design. | no |
| `docs/historical/specs/2026-03-22-rtsp-resilience-design.md` | 12.8K | 2026-04-26 | HISTORICAL | Archived rtsp resilience design. | no |
| `docs/historical/specs/2026-03-23-audio-accuracy-design.md` | 9.5K | 2026-04-26 | HISTORICAL | Archived audio accuracy design. | no |
| `docs/historical/specs/2026-03-26-nas-independence-design.md` | 3.6K | 2026-04-26 | HISTORICAL | Archived nas independence design. | no |
| `docs/historical/specs/2026-03-27-model-retraining-design.md` | 9.3K | 2026-04-26 | HISTORICAL | Archived model retraining design. | no |
| `docs/historical/specs/2026-03-28-review-system-overhaul-design.md` | 9.4K | 2026-04-26 | HISTORICAL | Archived review system overhaul design. | no |
| `docs/historical/specs/2026-03-28-yard-model-integration-design.md` | 10.9K | 2026-04-26 | HISTORICAL | Archived yard model integration design. | no |
| `docs/historical/specs/2026-03-30-unified-classification-query-design.md` | 5.5K | 2026-04-26 | HISTORICAL | Archived unified classification query design. | no |
| `docs/historical/specs/2026-03-31-second-opinion-api-design.md` | 4.7K | 2026-04-26 | HISTORICAL | Archived second opinion api design. | no |
| `docs/historical/specs/2026-04-01-live-detection-research.md` | 5.6K | 2026-04-26 | HISTORICAL | Archived live detection research. | no |
| `docs/historical/specs/2026-04-03-unified-bird-pipeline-design.md` | 14.5K | 2026-04-26 | HISTORICAL | Archived unified bird pipeline design. | no |
| `docs/historical/specs/2026-04-08-transfer-learning-pipeline-design.md` | 12.1K | 2026-04-26 | HISTORICAL | Archived transfer learning pipeline design. | no |
| `docs/historical/specs/2026-04-10-live-detection-v2-design.md` | 60.2K | 2026-04-26 | HISTORICAL | Archived live detection v2 design. | no |
| `docs/historical/specs/2026-04-11-live-detection-v3-design.md` | 43.6K | 2026-04-26 | HISTORICAL | Archived live detection v3 design. | no |
| `docs/historical/specs/2026-04-15-delayed-playback-overlay-design.md` | 19.9K | 2026-04-26 | HISTORICAL | Archived delayed playback overlay design. | no |
| `docs/historical/specs/2026-04-16-name-that-call-game-design.md` | 6.8K | 2026-04-26 | HISTORICAL | Archived name that call game design. | no |
| `docs/historical/specs/2026-04-16-overlay-sync-ground-truth-verification.md` | 30.6K | 2026-04-26 | HISTORICAL | Archived overlay sync ground truth verification. | no |
| `docs/historical/specs/2026-04-17-smooth-label-overlay-design.md` | 3.6K | 2026-04-26 | HISTORICAL | Archived smooth label overlay design. | no |
| `docs/historical/specs/2026-04-23-airtight-review-system.md` | 17.6K | 2026-04-26 | HISTORICAL | Archived airtight review system. | no |
| `docs/historical/specs/2026-04-23-litreview-1-bird-classifiers.md` | 13.6K | 2026-04-26 | HISTORICAL | Archived litreview 1 bird classifiers. | no |
| `docs/historical/specs/2026-04-23-litreview-2-calibration-ood.md` | 14.2K | 2026-04-26 | HISTORICAL | Archived litreview 2 calibration ood. | no |
| `docs/historical/specs/2026-04-23-litreview-3-small-noisy-imbalanced.md` | 14.1K | 2026-04-26 | HISTORICAL | Archived litreview 3 small noisy imbalanced. | no |
| `docs/historical/specs/2026-04-23-litreview-4-quantization-deployment.md` | 12.8K | 2026-04-26 | HISTORICAL | Archived litreview 4 quantization deployment. | no |
| `docs/historical/specs/2026-04-23-tier2-data-audit.md` | 10.8K | 2026-04-26 | HISTORICAL | Archived tier2 data audit. | no |
| `docs/historical/specs/2026-04-23-tier2-training-plan-v1.md` | 17.3K | 2026-04-26 | HISTORICAL | Archived tier2 training plan v1. | no |
| `docs/historical/specs/2026-04-25-imac-live-classify-as-built.md` | 26.0K | 2026-04-26 | HISTORICAL | Archived imac live classify as built. | no |

### IMAC-REPO — `bird-classifier` (75 files)

| File | Size | Modified | Class | Purpose / evidence | Rewrite after tracker + identify-then-render? |
|---|---|---|---|---|---|
| `CLAUDE.md` | 3.5K | 2026-07-17 | CURRENT (iMac) | Mission + iMac architecture (10 launchd units, YOLO ~5-7 fps, lock >=0.35 — matches iMac code). No banner that the iMac is a frozen reference. | no (banner) |
| `DOC_AUDIT.md` | 9.0K | 2026-07-17 | HISTORICAL | 07-17 iMac audit report; first 22 drift rows are copy-pasted from the Pi audit (pi CLAUDE.md lines) — duplicated content. | no |
| `GUIDE.md` | 11.1K | 2026-04-26 | CURRENT (iMac) | iMac reference guide (04-26). | no |
| `docs/superpowers/README.md` | 1.4K | 2026-04-26 | CURRENT (redirect) | Redirect: content consolidated to ~/docs/bird-observatory/working/ on 04-26; copies kept 'for now'. | no |
| `docs/superpowers/plans/2026-04-26-rc2-from-calibration.md` | 7.9K | 2026-04-26 | FLUFF (dup) | Byte-identical to ~/docs working copy. | no |
| `docs/superpowers/plans/README.md` | 511B | 2026-04-26 | FLUFF (dup) | Identical to ~/docs copy. | no |
| `docs/superpowers/progress/2026-04-25-side-findings.md` | 4.6K | 2026-04-26 | FLUFF (dup) | Byte-identical to ~/docs working copy. | no |
| `docs/superpowers/progress/2026-04-26-evening-handoff.md` | 18.4K | 2026-04-26 | FLUFF (dup) | Byte-identical to ~/docs working copy. | no |
| `docs/superpowers/progress/README.md` | 802B | 2026-04-26 | FLUFF (dup) | Identical to ~/docs copy. | no |
| `docs/superpowers/specs/2026-04-23-airtight-review-system.md` | 17.9K | 2026-04-26 | FLUFF (dup) | Byte-identical to ~/docs working copy. | no |
| `docs/superpowers/specs/2026-04-23-litreview-1-bird-classifiers.md` | 13.5K | 2026-04-23 | FLUFF (dup) | Byte-identical. | no |
| `docs/superpowers/specs/2026-04-23-litreview-2-calibration-ood.md` | 14.1K | 2026-04-23 | FLUFF (dup) | Byte-identical. | no |
| `docs/superpowers/specs/2026-04-23-litreview-3-small-noisy-imbalanced.md` | 14.0K | 2026-04-23 | FLUFF (dup) | Byte-identical. | no |
| `docs/superpowers/specs/2026-04-23-litreview-4-quantization-deployment.md` | 12.6K | 2026-04-23 | FLUFF (dup) | Byte-identical. | no |
| `docs/superpowers/specs/2026-04-23-tier2-training-plan-v1.md` | 18.0K | 2026-04-26 | FLUFF (dup) | Byte-identical. | no |
| `docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` | 26.3K | 2026-04-26 | FLUFF (dup) | Byte-identical to ~/docs working copy (3rd/4th copies in both repos' historical/). | no |
| `docs/superpowers/specs/2026-04-25-review-ui-helpers.md` | 4.6K | 2026-04-26 | FLUFF (dup) | Byte-identical. | no |
| `docs/superpowers/specs/README.md` | 1.7K | 2026-04-26 | FLUFF (dup) | Identical to ~/docs copy. | no |
| `test_clips/README.md` | 3.2K | 2026-03-22 | HISTORICAL-ish (dup) | Same as Pi repo copy. | no |
| `docs/superpowers/plans/historical/2026-03-21-phase-0-1-quick-wins-test-infra-shared-inference.md` | 67.5K | 2026-04-26 | HISTORICAL | Archived phase 0 1 quick wins test infra shared inference. | no |
| `docs/superpowers/plans/historical/2026-03-22-phase-2-reviews-sqlite.md` | 8.6K | 2026-04-26 | HISTORICAL | Archived phase 2 reviews sqlite. | no |
| `docs/superpowers/plans/historical/2026-03-22-phase-3-visit-model.md` | 10.6K | 2026-04-26 | HISTORICAL | Archived phase 3 visit model. | no |
| `docs/superpowers/plans/historical/2026-03-22-rtsp-resilience-plan.md` | 54.7K | 2026-04-26 | HISTORICAL | Archived rtsp resilience plan. | no |
| `docs/superpowers/plans/historical/2026-03-23-audio-accuracy-plan.md` | 29.2K | 2026-04-26 | HISTORICAL | Archived audio accuracy plan. | no |
| `docs/superpowers/plans/historical/2026-03-26-nas-independence-plan.md` | 8.4K | 2026-04-26 | HISTORICAL | Archived nas independence plan. | no |
| `docs/superpowers/plans/historical/2026-03-28-review-system-overhaul.md` | 30.2K | 2026-04-26 | HISTORICAL | Archived review system overhaul. | no |
| `docs/superpowers/plans/historical/2026-03-28-yard-model-integration.md` | 36.7K | 2026-04-26 | HISTORICAL | Archived yard model integration. | no |
| `docs/superpowers/plans/historical/2026-03-30-unified-classification-query.md` | 25.3K | 2026-04-26 | HISTORICAL | Archived unified classification query. | no |
| `docs/superpowers/plans/historical/2026-04-03-unified-bird-pipeline.md` | 25.9K | 2026-04-26 | HISTORICAL | Archived unified bird pipeline. | no |
| `docs/superpowers/plans/historical/2026-04-08-transfer-learning-pipeline.md` | 50.5K | 2026-04-26 | HISTORICAL | Archived transfer learning pipeline. | no |
| `docs/superpowers/plans/historical/2026-04-10-live-detection-v2.md` | 127.2K | 2026-04-26 | HISTORICAL | Archived live detection v2. | no |
| `docs/superpowers/plans/historical/2026-04-11-live-detection-v3-phase1.md` | 97.8K | 2026-04-26 | HISTORICAL | Archived live detection v3 phase1. | no |
| `docs/superpowers/plans/historical/2026-04-22-data-integrity-audit.md` | 12.9K | 2026-04-26 | HISTORICAL | Archived data integrity audit. | no |
| `docs/superpowers/plans/historical/2026-04-22-garbage-data-cull.md` | 11.8K | 2026-04-26 | HISTORICAL | Archived garbage data cull. | no |
| `docs/superpowers/plans/historical/2026-04-22-hires-ring-buffer.md` | 21.3K | 2026-04-26 | HISTORICAL | Archived hires ring buffer. | no |
| `docs/superpowers/plans/historical/2026-04-22-live-tab-fix.md` | 10.6K | 2026-04-26 | HISTORICAL | Archived live tab fix. | no |
| `docs/superpowers/plans/historical/2026-04-23-live-overlay-fixes.md` | 10.9K | 2026-04-26 | HISTORICAL | Archived live overlay fixes. | no |
| `docs/superpowers/plans/historical/2026-04-25-hailo-multimodel-path1.md` | 32.8K | 2026-04-26 | HISTORICAL | Archived hailo multimodel path1. | no |
| `docs/superpowers/plans/historical/2026-04-25-rc3-preserve-lock-time-vote.md` | 19.3K | 2026-04-26 | HISTORICAL | Archived rc3 preserve lock time vote. | no |
| `docs/superpowers/plans/historical/2026-04-25-review-ui-shared-helpers.md` | 40.0K | 2026-04-26 | HISTORICAL | Archived review ui shared helpers. | no |
| `docs/superpowers/progress/historical/2026-04-11-v3-progress.md` | 18.2K | 2026-04-26 | HISTORICAL | Archived v3 progress. | no |
| `docs/superpowers/progress/historical/2026-04-11-v3-ready-for-cutover.md` | 15.6K | 2026-04-26 | HISTORICAL | Archived v3 ready for cutover. | no |
| `docs/superpowers/progress/historical/2026-04-11-v3-self-audit.md` | 22.9K | 2026-04-26 | HISTORICAL | Archived v3 self audit. | no |
| `docs/superpowers/progress/historical/2026-04-23-autonomous-session-handoff.md` | 7.4K | 2026-04-26 | HISTORICAL | Archived autonomous session handoff. | no |
| `docs/superpowers/progress/historical/2026-04-24-pi5-overnight.md` | 3.7K | 2026-04-26 | HISTORICAL | Archived pi5 overnight. | no |
| `docs/superpowers/progress/historical/2026-04-25-compact-prompt-v2.md` | 8.1K | 2026-04-26 | HISTORICAL | Archived compact prompt v2. | no |
| `docs/superpowers/progress/historical/2026-04-25-compact-prompt.md` | 3.4K | 2026-04-26 | HISTORICAL | Archived compact prompt. | no |
| `docs/superpowers/progress/historical/2026-04-25-detection-snapshot-audit-findings.md` | 9.8K | 2026-04-26 | HISTORICAL | Archived detection snapshot audit findings. | no |
| `docs/superpowers/progress/historical/2026-04-25-evening-handoff.md` | 8.7K | 2026-04-26 | HISTORICAL | Archived evening handoff. | no |
| `docs/superpowers/progress/historical/2026-04-25-morning-brief.md` | 9.4K | 2026-04-26 | HISTORICAL | Archived morning brief. | no |
| `docs/superpowers/progress/historical/2026-04-25-pi5-handoff.md` | 25.8K | 2026-04-26 | HISTORICAL | Archived pi5 handoff. | no |
| `docs/superpowers/progress/historical/2026-04-25-review-ui-debug-log.md` | 5.7K | 2026-04-26 | HISTORICAL | Archived review ui debug log. | no |
| `docs/superpowers/progress/historical/2026-04-25-self-handoff.md` | 12.7K | 2026-04-26 | HISTORICAL | Archived self handoff. | no |
| `docs/superpowers/progress/historical/cross-claude-comms.md` | 21.1K | 2026-04-26 | HISTORICAL | Archived cross claude comms. | no |
| `docs/superpowers/reviews/historical/2026-04-10-live-detection-v2-review.md` | 65.5K | 2026-04-26 | HISTORICAL | Archived live detection v2 review. | no |
| `docs/superpowers/specs/historical/2026-03-21-foundations-design.md` | 26.9K | 2026-04-26 | HISTORICAL | Archived foundations design. | no |
| `docs/superpowers/specs/historical/2026-03-22-rtsp-resilience-design.md` | 12.8K | 2026-04-26 | HISTORICAL | Archived rtsp resilience design. | no |
| `docs/superpowers/specs/historical/2026-03-23-audio-accuracy-design.md` | 9.5K | 2026-04-26 | HISTORICAL | Archived audio accuracy design. | no |
| `docs/superpowers/specs/historical/2026-03-26-nas-independence-design.md` | 3.6K | 2026-04-26 | HISTORICAL | Archived nas independence design. | no |
| `docs/superpowers/specs/historical/2026-03-27-model-retraining-design.md` | 9.3K | 2026-04-26 | HISTORICAL | Archived model retraining design. | no |
| `docs/superpowers/specs/historical/2026-03-28-review-system-overhaul-design.md` | 9.4K | 2026-04-26 | HISTORICAL | Archived review system overhaul design. | no |
| `docs/superpowers/specs/historical/2026-03-28-yard-model-integration-design.md` | 10.9K | 2026-04-26 | HISTORICAL | Archived yard model integration design. | no |
| `docs/superpowers/specs/historical/2026-03-30-unified-classification-query-design.md` | 5.5K | 2026-04-26 | HISTORICAL | Archived unified classification query design. | no |
| `docs/superpowers/specs/historical/2026-03-31-second-opinion-api-design.md` | 4.7K | 2026-04-26 | HISTORICAL | Archived second opinion api design. | no |
| `docs/superpowers/specs/historical/2026-04-01-live-detection-research.md` | 5.6K | 2026-04-26 | HISTORICAL | Archived live detection research. | no |
| `docs/superpowers/specs/historical/2026-04-03-unified-bird-pipeline-design.md` | 14.5K | 2026-04-26 | HISTORICAL | Archived unified bird pipeline design. | no |
| `docs/superpowers/specs/historical/2026-04-08-transfer-learning-pipeline-design.md` | 12.1K | 2026-04-26 | HISTORICAL | Archived transfer learning pipeline design. | no |
| `docs/superpowers/specs/historical/2026-04-10-live-detection-v2-design.md` | 60.1K | 2026-04-26 | HISTORICAL | Archived live detection v2 design. | no |
| `docs/superpowers/specs/historical/2026-04-11-live-detection-v3-design.md` | 43.6K | 2026-04-26 | HISTORICAL | Archived live detection v3 design. | no |
| `docs/superpowers/specs/historical/2026-04-15-delayed-playback-overlay-design.md` | 19.9K | 2026-04-26 | HISTORICAL | Archived delayed playback overlay design. | no |
| `docs/superpowers/specs/historical/2026-04-16-name-that-call-game-design.md` | 6.8K | 2026-04-26 | HISTORICAL | Archived name that call game design. | no |
| `docs/superpowers/specs/historical/2026-04-16-overlay-sync-ground-truth-verification.md` | 30.6K | 2026-04-26 | HISTORICAL | Archived overlay sync ground truth verification. | no |
| `docs/superpowers/specs/historical/2026-04-17-smooth-label-overlay-design.md` | 3.6K | 2026-04-26 | HISTORICAL | Archived smooth label overlay design. | no |
| `docs/superpowers/specs/historical/2026-04-23-tier2-data-audit.md` | 10.8K | 2026-04-26 | HISTORICAL | Archived tier2 data audit. | no |
| `docs/superpowers/specs/historical/2026-04-25-hailo-playbook.md` | 31.8K | 2026-04-26 | HISTORICAL | Archived hailo playbook. | no |
