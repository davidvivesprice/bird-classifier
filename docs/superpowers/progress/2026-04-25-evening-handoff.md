# Evening handoff — 2026-04-25

Sparse pointers. Future-Claude: investigate each link to recover full context.

## Read first (in order)

1. `~/.claude/projects/-Users-vives/memory/MEMORY.md`
2. `docs/superpowers/progress/2026-04-25-self-handoff.md` — earlier in the day
3. THIS doc — picks up where that one left off
4. `docs/superpowers/progress/2026-04-25-detection-snapshot-audit-findings.md` — the 4 root causes
5. `docs/superpowers/progress/cross-claude-comms.md` — Pi-Claude messages

## Where we are

**Mission:** clean training data → cleanlab → train flagship classifier.

**Today's pivot:** discovered ~30% of saved rows are classifier noise. Built RC3 to make noise identifiable, then started a UI refactor so David can clean efficiently.

**Stopped mid-execution** of `docs/superpowers/plans/2026-04-25-review-ui-shared-helpers.md` — 5 of 12 tasks landed, 7 remaining.

## Commits this session (chronological, most recent last)

Run `git log --oneline -40` to see them all. Highlights:

- `8cd8963` — RC3 + Pi 5 + airtight review (big start-of-session commit)
- `8d79e70` — iMac as-built spec + Pi handoff
- `bf5fd8a`/`fafaa56`/`7916d3c`/`ed499a6` — doc cleanup, status banners
- `94a0a8e`/`9c4998f`/`169fe09` — repo-split delegation to Pi-Claude
- `00dd8bc`/`7f1634b`/`2bb9a55` — RC3 Tasks 1-2 (preserve lock-time vote)
- `bfaa389` — RC3 Task 4 watershed (id 756294, 2026-04-25 14:06:38 ET)
- `5773551` — pre-existing test_pipeline_classifier failures noted
- `a50466f` — RC3 plan
- `787a77d` — `tools/calibrate_disagreement.sh` interactive calibration
- `c9c4bca`/`e3a4c13` — Review-UI Task 1 (`applyVerdictToUI` + backward-compat)
- `3352948` — Task 2 (data-file on Classified cards)
- `652859f` — Task 4 (loadQueue helpers)
- `50aaf38` — Task 5 (Classified loader migrated)

Pi-Claude commits also in log; they were pushing to this repo until ~13:15 ET when David delegated repo split to them. They've been told to stop pushing here.

## Review-UI refactor — STATE

Plan: `docs/superpowers/plans/2026-04-25-review-ui-shared-helpers.md` (12 tasks).

**Done:** 1, 2, 3 (no-op), 4, 5.

**Remaining:** 6 (Skipped), 7 (Missed), 8 (Batch), 9 (Classify+Lightbox by-name migration), 10 (server camera/multibird filter), 11 (UI dropdowns), 12 (spec doc), then final-reviewer + simplify pass.

Tasks 6-9 follow the SAME pattern as Task 5. Mechanical. Use Task 5's commit `50aaf38` as the template.

David is **actively reviewing data on the Classified tab** — Task 5's effects are live for him after browser hard-refresh (no dashboard restart needed for static-file changes).

## RC2 + RC4 status (deferred but still on the docket)

- **RC2** (confidence floor at write boundary): noise rows get `extra_json.suspect=true` for filtering. Easy now that RC3 metadata is in place. Plan not yet written.
- **RC4** (multi-bird annotation): pipeline writes per-track rows but JPGs only mark one bbox. Audit confirmed live. Plan not yet written.
- **RC1** (YOLO retrain): months of work; explicitly tabled per David. NOTE: 22K culled hallucinations are NOT verified-bird-free; cannot be used as training negatives without manual verification (David called this out — see chat log).

## Acute issues David surfaced today

- **Review buttons didn't advance** → fixed via dashboard restart + python-multipart install. Post-mortem: `docs/superpowers/progress/2026-04-25-review-ui-debug-log.md`
- **Bbox around background no bird** → confirmed via JPG eyeball; root cause RC1+RC2+RC3 stacked. Audit findings doc has details.
- **Multi-bird shots only see one bird** → confirmed; RC4 territory; pipeline DOES write per-track rows but annotation marks only one
- **Labels lagging on /live** → diagnosed as iMac YOLO at 134ms avg / 499ms p99 + the offset between sub-stream wall_time stamping and main-stream segment-completion stamping. Side-finding — not blocking cleanup. See conversation around the `htop` discussion.
- **Trash needs to be GONE not gray** → fixed in Task 1
- **Pagination must not lose rows** → fixed in Task 5 for Classified; Tasks 6-8 propagate to other tabs
- **Need camera + multibird filters on Classify and Classified** → Tasks 10-11

## Pi-Claude (parallel session) status

- Pushed 12+ commits to this repo this morning (Hailo Path 1, watchdog fix, etc.)
- David decided ~13:15 ET that Pi-Claude needs its own repo
- Comms message at end of `cross-claude-comms.md` delegates the repo split execution to Pi-Claude
- They were "going to David next" per their last comms message
- iMac repo should not see new commits from Pi-Claude going forward
- Their watchdog fix to `pipeline/frame_capture.py` + `pipeline/hires_ring.py` is already in this repo and was loaded when bird-pipeline was restarted at RC3 Task 4

## Key system state

- iMac dashboard PID: was 65240 → restarted to current. Run: `pgrep -f 'uvicorn dashboard.api'`
- iMac bird-pipeline: was restarted at 14:06 ET for RC3 Task 4. PID may have changed since.
- Pi all 4 services: per Pi-Claude comms, were active and processing
- iMac classifications.db: ~756K+ rows. Watershed at id 756294 (RC3 metadata starts here).
- 22K culled hallucinations: NOT verified bird-free. Treat with skepticism.

## Calibration tool ready but not yet run

`tools/calibrate_disagreement.sh` — interactive 4-bucket stratified sample of disagreement-flag accuracy. David has not yet run a real sample. Worth doing tomorrow once we have more post-watershed data. Default `N_PER_BUCKET=5` → 20 sample rows.

## Open observations / side-findings

`docs/superpowers/progress/2026-04-25-side-findings.md` — David's observation log. Has:
- review_history legacy backfill missing on iMac (1827 rows pre-watershed)
- debug test row in review_history (id=1, harmless)
- iMac YOLO 2× slower than docs claim (CoreML may not be active)
- 4 pre-existing test_pipeline_classifier.py failures

## Next sensible steps (priority order)

1. **Test Tasks 1-5 on Classified tab** — David hard-refreshes, clicks trash on a card, expects animate-out + remove + stable Older→ pagination.
2. **Push through Tasks 6-12** if Tasks 1-5 are clean — mechanical work, ~30-40 min subagent-driven.
3. **Final code-reviewer + simplify pass** on the whole refactor diff once 12 lands.
4. **RC3 final code-reviewer** (deferred from today; commits 00dd8bc through bfaa389).
5. **Run `tools/calibrate_disagreement.sh`** for real disagreement-flag calibration.
6. **Decide on RC2 vs RC4 next** — RC2 (noise filter) is cheap; RC4 (multi-bird annotation) helps cleanup UX.

## Key file pointers

| What | Path |
|---|---|
| iMac as-built (canonical system reference) | `docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` |
| Hailo playbook | `docs/superpowers/specs/2026-04-25-hailo-playbook.md` |
| Audit findings (the 4 root causes) | `docs/superpowers/progress/2026-04-25-detection-snapshot-audit-findings.md` |
| Review-UI plan (mid-execution) | `docs/superpowers/plans/2026-04-25-review-ui-shared-helpers.md` |
| Tier 2 training plan | `docs/superpowers/specs/2026-04-23-tier2-training-plan-v1.md` |
| Side-findings ledger (David's queue) | `docs/superpowers/progress/2026-04-25-side-findings.md` |
| Cross-Claude comms | `docs/superpowers/progress/cross-claude-comms.md` |
| RC3 plan | `docs/superpowers/plans/2026-04-25-rc3-preserve-lock-time-vote.md` |
| Today's review-UI debug post-mortem | `docs/superpowers/progress/2026-04-25-review-ui-debug-log.md` |
| Pi 5 handoff (for parallel session) | `docs/superpowers/progress/2026-04-25-pi5-handoff.md` |
| Earlier self-handoff | `docs/superpowers/progress/2026-04-25-self-handoff.md` |

## Skills used today

- systematic-debugging (review UI bug + audit Phase 1)
- writing-plans (RC3 plan + Review-UI plan)
- subagent-driven-development (mid-execution on Review-UI Tasks 1-5)
- brainstorming (audit scoping)
- using-superpowers (meta)

Skills NOT yet invoked but should be when relevant:
- `simplify` — for the final pass over the Review-UI refactor
- `test-driven-development` — for RC2 / RC4 / future code work
- `verification-before-completion` — always-on

## David's working preferences (observed)

- Pushes back on stale-memory / vibes-based answers — VERIFY against code
- Pushes back on solo grind — invoke skills, dispatch subagents
- Strategic about session-arc handoffs (asks for them BEFORE compaction)
- Direct, terse, no preamble
- Says "still try your hardest" when asked about effort level
- "Slow and right" affirmation = systematic-debugging working

## Compact prompt

Use the same shape as `docs/superpowers/progress/2026-04-25-compact-prompt-v2.md` but bias the new summary toward THIS session's review-UI refactor + the audit findings. The earlier prompt is now stale on the work-in-flight.
