# Pi repo split — 2026-04-25

**Trigger:** David, via iMac-Claude (cross-claude-comms.md, 2026-04-25 ~13:45 ET):
> "we need to start a new repo for him and move all his work there."

**Status:** Done. This file lives in the new Pi-side repo.

## What happened

Two Claudes were pushing to `/Users/vives/bird-classifier/` on iMac:
- iMac-Claude — owns the iMac live-classify subsystem (review2, dashboard/api.py for iMac, snapshot writer, Tier 2 prep on iMac side)
- Pi-Claude — owns Pi 5 + Hailo-8L (this repo)

The shared `main` produced commit churn, ambiguity about who owns shared files, and a growing conflict surface as iMac-Claude started RC3 work in the snapshot writer. David called it: split.

## The split

| | Before | After |
|---|---|---|
| Repo path on iMac | `/Users/vives/bird-classifier/` (shared) | `/Users/vives/bird-classifier/` (iMac-Claude only) |
| Pi-Claude home | (same path, with comms protocol) | `/Users/vives/bird-classifier-pi/` (this repo) |
| Pi runtime | `vives@pi5.local:/home/vives/bird-classifier/` (rsync target) | unchanged — still rsync target |
| Git history | shared linear history on `main` | shared up through commit `5773551`, diverges from there |
| Cross-cutting fixes | implicit — last writer wins | explicit — patches in `cross-claude-comms.md`, David relays |

## How Pi-Claude works now

1. Edit files in `/Users/vives/bird-classifier-pi/` on iMac (same editor ergonomics as before).
2. Commit in `/Users/vives/bird-classifier-pi/` (this is the canonical Pi repo).
3. rsync working files to `vives@pi5.local:/home/vives/bird-classifier/` for deployment.
4. Pi's `~/bird-classifier/.git/` is no longer authoritative — leave alone or delete; not used.
5. **NEVER push to `/Users/vives/bird-classifier/`** — that's iMac-Claude's repo.

## Remote `imac-origin`

The new repo has the original iMac-side `origin` (David's GitHub at `github.com/davidvivesprice/bird-classifier.git`) renamed to `imac-origin` to prevent accidental pushes. If David later wants this repo on a separate GitHub remote, add a new `origin` then.

## Move list (what's Pi-Claude territory)

These files were authored by Pi-Claude or are Pi-only behavior:

**Pi-only modules (only imported when `PI_MODE=1`):**
- `pipeline/hailo_engine.py`
- `pipeline/hailo_detector.py`
- `pipeline/hailo_classifier.py`
- `pipeline/pi_classifier.py`
- `pipeline/model_registry.py`
- `dashboard/pi_dash.html`

**Pi-only tools + tests:**
- `tools/bench_hailo_multimodel.py`
- `tools/pi5_thermal_watch.py` + `tools/pi5-thermal-watch.{service,timer}`
- `tests/pipeline/test_hailo_engine.py`
- `tests/pipeline/test_hailo_detector_engine.py`
- `tests/pipeline/test_hailo_classifier_engine.py`

**Pi-only docs:**
- `docs/superpowers/specs/2026-04-25-hailo-playbook.md`
- `docs/superpowers/plans/2026-04-25-hailo-multimodel-path1.md`
- `docs/superpowers/progress/2026-04-25-pi5-handoff.md`
- This file
- `docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` (authored by iMac-Claude — kept here as reference for how Pi mirrors/diverges)

**Pi-only data:**
- `models/imagenet_labels.txt`

**Shared files Pi-Claude has touched (changes already in iMac repo at split point — no action needed):**
- `pipeline/frame_capture.py` — watchdog `proc.poll()` fix (commit `07dd21d`)
- `pipeline/hires_ring.py` — same watchdog fix
- `bird_pipeline_v3.py` — `exclude_hailo` kwarg drop (commit `4514ea5`)
- `dashboard/api.py` — `exclude_hailo` plumbing drop (commit `4514ea5`); detector-as-classifier guard + non-blocking switch restart (commit `b81c493`); David's path/box fixes (commit `9da2c59`)

iMac-Claude can clean Pi-only files out of the iMac tree at their convenience — already offered.

## Cross-cutting fixes after split

Watchdog fix (`pipeline/frame_capture.py`, `pipeline/hires_ring.py`): already in iMac repo via commit `07dd21d` — no action needed.

Future cross-cutting fixes: post diff to `cross-claude-comms.md` with subject `[patch]`, body = unified diff, attribute target file. David relays; iMac-Claude applies.

## What's NOT in this repo

The iMac-only stuff stays in `/Users/vives/bird-classifier/`:
- `dashboard/index.html` (iMac dashboard)
- `dashboard/live.html` (iMac live overlay with HLS+sidecar smoothing)
- `pipeline/snapshot_writer.py` RC3 work (in flight by iMac-Claude)
- iMac LaunchAgents (`com.vives.*.plist`)
- iMac-side audio_analyzer.py, classifications.db, etc.

These files exist in this repo too (cp -a captured them) but Pi-Claude doesn't edit them. If they become a maintenance burden, we can clean them out later.
