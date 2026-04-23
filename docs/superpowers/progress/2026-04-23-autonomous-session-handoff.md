# Autonomous Session Handoff — 2026-04-23

David went offline after saying "make us proud." Below is everything that landed during the autonomous block, what's verified, what needs his eye, and what I deliberately did not do.

## What shipped (commits on `main`)

Ten commits this session, in order:

1. `84201bb` — Checkpoint: Sonoma migration + pipeline/dashboard hardening. (One-time git reset to make everything else atomic.)
2. `46b21fd` — Add Tier 0-1 repair plans.
3. `5df60cf` — Fix Live tab regressions (0a-1…0a-4). **4-of-4 David-verified.**
4. `99d1340` — Add O-cluster plan for /live overlay fixes.
5. `962e6c4` — O-cluster: hls.js catch-up config + 1s fade + O1 diagnostic capture + prune log. **Not visually verified.** David needs to `/live` hard-refresh to confirm O2 (17s → 10-12s) and O3 (1s fade).
6. `5b281c7` — Add hallucination-window cull tool + captured dry-run. **NOT executed.** 11,189 rows ready for cull when David approves.
7. `dde78e2` — Add data-integrity audit tool + LaunchAgent plist (not installed). **NOT installed.** Plist is in `tools/`, not `~/Library/LaunchAgents/`.
8. `3e05086` — 1b Part 1: HiResRingBuffer + score_frame. **14 unit tests green.** Not wired into pipeline (deferred for David's review).
9. `cea6a99` — Audit tool: separate legacy-underscored-dir orphans from real concerns.
10. *(this doc, next commit)*

## Verified (evidence in same message at commit time)

- **0a: all 4 symptoms cleared.** David confirmed 1-3 over chat, 4 ("phone check is good") late morning. Commit `5df60cf` records that.
- **1b tests green.** `pytest tests/pipeline/test_hires_ring.py` → 14 passed.
- **0b dry-run clean.** Script runs, no errors, 11,189 rows flagged, 0 pre-orphan. Captured in `tools/cull_hallucination_window.dry-run.json`.
- **1a dry-run clean.** 0 true orphan rows, 0 canonical orphan files. 336 "orphans" are all legacy-underscored-dir stragglers (known deferred). Captured in `tools/audit_data_integrity.dry-run.json`.
- **Dashboard serves new live.html** — `curl -sS http://localhost:8099/live | grep STALE_MS` returns the new value.

## Not verified (awaiting David's browser)

- **O2 (17s → 10-12s)** — hls.js config tightened. Needs eye-check on `/live`.
- **O3 (labels linger → ~1s fade)** — STALE_MS 1200→600. Note: the pipeline-side Norfair `hit_counter_max=15` = 3s Kalman coasting will still extend the *effective* fade by up to 3s. I did not change that (would risk vote-lock behavior). Documented in the O-cluster plan.
- **O1 (label drift)** — the clock code is correctly ported (verified in code). I added an opt-in diagnostic capture (`window.__o1Capture = true`) for the next drift incident, rather than patching blind.

## What David needs to decide

**Execute 0b cull?**
- Script: `tools/cull_hallucination_window.py`
- Dry-run summary: `tools/cull_hallucination_window.dry-run.json`
- 11,189 unreviewed rows from the Apr 19+ hallucination window. Moves JPGs to `~/bird-snapshots/culled/2026-04-23/`, marks DB rows `action='culled_hallucination'`. Reversible within 30 days (per plan's 2-phase retention).
- Command: `python3 tools/cull_hallucination_window.py --execute`

**Install 1a hourly integrity audit?**
- Script: `tools/audit_data_integrity.py`
- Plist: `tools/com.vives.bird-integrity-audit.plist`
- Current state: clean. Installing means "catch the next occurrence of the orphan bug with evidence."
- Install: `cp tools/com.vives.bird-integrity-audit.plist ~/Library/LaunchAgents/` then `launchctl load …`.

**Wire 1b HiResRingBuffer into the pipeline?**
- Module: `pipeline/hires_ring.py` (tested).
- Integration point: `bird_pipeline_v3.py` where `FrameCapture` and `SnapshotWriter` are instantiated.
- FrameCapture's contract (verified 2026-04-23): constructor is `FrameCapture(camera_name, rtsp_url, out_queue, width=1920, height=1080, fps=5)`. It pushes `Frame(bgr, wall_time_ms, camera, width, height)` objects to `out_queue` via `put_nowait` (drops oldest on backpressure). No callback API.
- Suggested wire-up for the plan's Task 3 (David: this needs your eye before it ships):
  1. Create a dedicated `queue.Queue(maxsize=20)` for the hi-res feed.
  2. Create a FrameCapture for the `-main` stream pushing to that queue.
  3. Spawn a consumer thread that drains the queue → `ring.push(frame.bgr, frame.wall_time_ms)`.
  4. Pass `ring` into `SnapshotWriter(..., hires_ring=ring, shadow_mode=True)`.
  5. Shadow-mode soak 3-4 days with sidecar JSONs per crop.
  6. Flip to `shadow_mode=False`.
- RAM overhead estimate: ~150 MB (2s × 5 fps × 1920×1080×3 + ffmpeg buffers). Fine on the 8 GB iMac.

**Fix the 13 pre-existing pipeline test failures?**
- All of them predate today's work — drift from the Sonoma checkpoint commit (thresholds changed 0.6→0.25 etc., tests weren't updated).
- Listed in `memory/project_system_repair_plan.md` forget-me-nots.
- Straightforward to fix but needs judgment calls on a few (the classifier-decision-tree ones). Not urgent.

## Deferred and why

- **0b execution** — data change. Not mine to make without approval.
- **1a LaunchAgent install** — installs a recurring autonomous action. Needs David's "yes."
- **1b pipeline wire-up** — touches the live detection circuit. David explicitly asked me to "add the system in safely, don't break it." I'd rather have him eyeball the integration than guess.
- **Norfair `hit_counter_max` change** — vote-lock interaction needs a soak. Documented as a future spec.
- **Pipeline restart to pick up the new prune log line** — I didn't restart the production pipeline. New log line will appear on next organic prune cycle (hourly).

## Known state at handoff time

- 10 commits pushed to local `main` (not pushed to remote — David's call on that).
- Production pipeline + dashboard + go2rtc: still running on their pre-restart code for the pipeline; dashboard is live-reloading the new `live.html` on every request.
- `/live` should be BETTER RIGHT NOW for O2/O3 even without any restart (uvicorn serves from disk).
- Disk: HLS segments ~370 MB (steady state, prune runs hourly). `~/bird-snapshots` has `culled/` dir created empty (no cull executed).

## If something looks wrong

`git log --oneline | head -20` shows the session's work. Any single commit can be reverted with `git revert <hash>`. The Sonoma checkpoint is the largest and the only one I'd advise against reverting — it captures weeks of work.
