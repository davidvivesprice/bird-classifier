# Autonomous Session Handoff — 2026-04-23 (extended)

Full log of the autonomous block. Originally written after 15 commits; extended as the block continued. Read top-to-bottom for chronology.

## Commits on `main` since checkpoint (23 in this session)

```
40966fc tier2_eval/split: visit-grouped StratifiedGroupKFold + real-data demo
87a8332 Tier 2 Phase 0: evaluation harness + first real baseline numbers
229777d Tier 2: four lit reviews + unified training plan v1
0ce490e Airtight review system spec + gamified /review-ideas mockup
1bca6c2 Tier 2 data audit: 1673 ground-truth samples, 13 trainable species, 158× imbalance
de1c36c 1b wire-up (env-gated, off by default): HiResCapture + ring + sidecar
5e83b79 Audit: close walk/select race; exit 0 regardless of orphan count
b071b43 gitignore: ignore tool runtime logs + macOS Spotlight marker
6373a9b Refresh dry-run summaries with current data
4ac34d8 handoff doc: update with final session state
5542913 CameraProcessThread: class-level defaults fix 4 test failures
cf0bf36 test_camera_config: update asserts to Sonoma-era thresholds
6bcf5f4 Session handoff note for 2026-04-23
cea6a99 Audit tool: legacy-underscored vs canonical orphans
3e05086 1b Part 1: HiResRingBuffer + score_frame tests
dde78e2 Add data-integrity audit tool + plist (not installed)
5b281c7 Add hallucination-window cull tool + dry-run
962e6c4 O-cluster: hls.js + 1s fade + O1 diag + prune log
99d1340 Add O-cluster plan for /live overlay fixes
5df60cf Fix Live tab regressions (0a)
46b21fd Add Tier 0-1 repair plans
84201bb Checkpoint: Sonoma migration + pipeline/dashboard hardening
```

Plus post-return commits when David flipped approvals:
- 0b cull executed (11,293 rows quarantined to `~/bird-snapshots/culled/2026-04-23/`)
- 1a LaunchAgent installed (hourly integrity audit running)
- 1a race bug caught ON FIRST RUN, audit fixed, one row casualty salvaged

## Live status (as of handoff)

**Production:** healthy.
- Pipeline (PID 40808), go2rtc (29092), uvicorn (62924) all alive.
- /api/pipeline/health returns 200, snapshot_writer still writing.
- /review-ideas route added but **not yet served** (uvicorn needs restart to pick it up). Meanwhile viewable by opening `dashboard/review-ideas.html` directly.
- 1a integrity LaunchAgent running hourly. Last run: 0 orphans (post-fix). Watch `~/bird-classifier/tools/audit_data_integrity.log` for evidence of the next orphan-bug occurrence.

**Filesystem salvage:**
- `~/bird-snapshots/salvage_audit_race_2026-04-23/` — one JPG the 1a race bug orphaned. Includes `README.txt` with recovery options.

**Working tree:** clean.

---

## What David needs to review, ranked

### Must-review / high-impact

1. **Tier 2 training plan** (`docs/superpowers/specs/2026-04-23-tier2-training-plan-v1.md`).
   - 16-class label set (Part C) — **David's sign-off needed**.
   - Hairy/Downy specialist-head fallback — **David's sign-off needed**.
   - Shadow-mode duration — **David's pick: 3 days? 7?**
   - All six open questions at the end of the doc.

2. **Airtight review system spec** (`docs/superpowers/specs/2026-04-23-airtight-review-system.md`) + **/review-ideas mockup** (`dashboard/review-ideas.html`).
   - Six current-system bugs documented (A-F).
   - Full design for `review_history` append-only table, keyset pagination, idempotency via client_id.
   - Mockup is a working interactive prototype (generative SVG birds; keyboard Y/N/T; streaks; confetti; undo). Open the file directly or restart uvicorn to hit `/review-ideas`.
   - **Not yet implemented.** Spec + mockup only.

3. **1b hi-res ring wire-up** (commit `de1c36c`). Env-gated, off by default.
   - `PIPELINE_HIRES_RING=1` → shadow mode (sidecar JSONs, no behavior change)
   - Any other truthy → ring authoritative
   - **Do not flip on the iMac.** Load avg 22+ already. Wait for Pi 5 hardware.

### Good-data deliverables to read

4. **Data audit** (`docs/superpowers/specs/2026-04-23-tier2-data-audit.md`). Per-species counts, Hairy/Downy confusion quantified, multi-bird filter needed, dual-dir situation explained. Reproducible SQL/python at the bottom of the doc.

5. **Baseline numbers** (commit `87a8332`, artifact `tier2_eval/baseline.report.json`).
   - AIY on 1,670-review hold-out: top-1 **68.0%**, macro-F1 **75.2%**, ECE **16.3%**.
   - Hairy/Downy confusion confirmed in the numbers.
   - not_a_bird recall 0.0% (AIY has no non-bird class).
   - This is what flagship must beat.

6. **Four lit reviews** (2026-04-23-litreview-1..4). Each is 1,400-1,500 words of citations + concrete recommendations. Skim if you want the detail behind the training plan's decisions.

### Phase 0 evaluation harness

7. **`tier2_eval/` package** (commits `87a8332`, `40966fc`). 26 tests green.
   - `metrics.py` — macro-F1, per-class recall/precision, confusion matrix, ECE, OOD AUROC, FPR@TPR, bootstrap CIs. Pure functions.
   - `baseline.py` — scores AIY from the DB against the hold-out. No Coral, no model loading.
   - `split.py` — visit-grouped K-fold that provably avoids the camera-trap ML's dominant leakage mode.
   - Run `python3 -m tier2_eval.baseline` to regenerate the baseline numbers anytime.

### Maintenance items from earlier in the session

8. **0a Live-tab fix** (commit `5df60cf`). 4-of-4 verified by you. Done.
9. **O-cluster** (commits `99d1340`, `962e6c4`). Needs your browser to verify O2 (17s → 10-12s), O3 (fade 1s). O1 instrumentation opt-in via `window.__o1Capture = true` in console.
10. **0b cull** (commit `5b281c7`, executed after approval). 11,293 rows quarantined; reversible for 30 days.
11. **1a integrity audit** (commit `dde78e2` + `5e83b79` race fix). LaunchAgent loaded; logs every orphan before culling.

---

## Agent dispatches this session

Four parallel general-purpose agents ran for the Tier 2 lit review. All four delivered files to `docs/superpowers/specs/`:
- agent a820742f51881d109 — bird classifiers
- agent a061cc09f40fb553d — calibration + OOD
- agent a30741fc462782324 — small/noisy/imbalanced training
- agent ac6a6232d95dbb4d6 — quantization + deployment

All committed in `229777d`.

---

## Known-damage inventory

1. **One DB row lost** to the 1a race bug before I caught it. File salvaged to `~/bird-snapshots/salvage_audit_race_2026-04-23/`. README explains recovery options.
2. **`/review-ideas` route added but not live** until uvicorn restarts.
3. **Pipeline log line from commit `962e6c4`** also not live until pipeline restarts (same reason). Both are harmless until restart.

---

## Anything pending that CAN'T continue without David

- Tier 2 implementation (Phase 1+): need the label-set sign-off and hardware plan (iMac vs Colab vs Pi 5).
- Airtight review backend: schema change to production DB — David approves and supervises migration.
- 1b flip-the-switch: explicit authorization to add a second FFmpeg decode on an already-saturated iMac.

All other work described above shipped cleanly or is waiting as specs for review.

---

## If I'm gone when you return

Everything mission-critical is either:
- **verified in commits** (0a, 0b, 1a mostly)
- **env-gated off** (1b)
- **spec-only** (Tier 2, airtight review)

No service is running code that depends on my being here. No timed rollbacks. No pending tasks that must fire. The audit LaunchAgent runs hourly and is self-contained.

`git log --oneline` tells the full story. Happy to dig deeper on any commit.
