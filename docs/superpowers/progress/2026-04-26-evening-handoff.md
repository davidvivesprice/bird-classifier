# Evening handoff — 2026-04-26

Sparse pointers + everything that landed today. Future-Claude: investigate
each link to recover full context.

## Read first (in order)

1. `~/.claude/projects/-Users-vives/memory/MEMORY.md`
2. `docs/superpowers/progress/2026-04-25-evening-handoff.md` — yesterday
3. THIS doc — picks up from there
4. `docs/superpowers/plans/2026-04-26-rc2-from-calibration.md` — the new RC2 plan, **awaiting David's choice of v1/v2/v3**
5. `docs/superpowers/progress/2026-04-25-detection-snapshot-audit-findings.md` — the 4 root causes
6. `docs/superpowers/progress/cross-claude-comms.md` — Pi-Claude messages

---

## Mission (constant — restated to make sure it doesn't drift)

**Two questions David named clearly today:**

1. **How do we get really good data?**
2. **How do we use that good data to train a fantastic model?**

**Critical clarification David made:** the yard model is **live-overlay only** (it's there for speed). AIY is the real classifier for everything that ends up in the database / training pool. The actual unsolved technical problem is **hi-res shots to AIY** — see the "Hi-res to AIY" section below.

**The flagship classifier work** lives at:
- `docs/superpowers/specs/2026-04-23-tier2-training-plan-v1.md` (existing plan)
- `~/.claude/projects/-Users-vives/memory/project_yard_model_revamp.md` (project brief)

**THE Tier 2 plan needs revision** — it was written before today's calibration data + before David's "AIY is the authority, train a model that learns from it" clarification. See the "AIY-as-base — the honest truth" section.

---

## Hard rules from David (don't violate, ever)

These survived from yesterday and were re-confirmed today. **Last-Claude
explicitly marked these for me; I am marking them for next-Claude.**

1. **iMac and Pi do NOT share a repo.** Pi-Claude was delegated the repo
   split. iMac repo = `/Users/vives/bird-classifier/`. Pi repo lives at
   `/Users/vives/bird-classifier-pi/` on the iMac (rsync'd to Pi).
   Cross-cutting fixes flow as patches relayed via David through
   `cross-claude-comms.md`.

2. **The 22K `culled_hallucination/` JPGs are NOT verified bird-free.** They
   were a TIME-WINDOW cull, not content-verified. Do NOT use them as YOLO
   training negatives without manual verification. Same pattern caused the
   yard 0/14 disaster. Real negatives live in: 289 trash verdicts + 85
   wrong→not_a_bird + manually-verified samples.

3. **RC1 (YOLO retrain) is TABLED.** Not the next move. Months of work.

4. **Trash + wrong verdicts must be GONE immediately** (animate out, not
   gray-out). Pagination must NOT lose rows. Both addressed in the
   Review-UI refactor (yesterday) — confirmed working today.

5. **Review tab + Classified tab need camera filter + multibird filter.**
   Filter OUT multi-bird shots = useless for training data. Both shipped
   yesterday/today (Tasks 10–11 of the refactor).

6. **Honesty over optimism.** No "should work." Verify or say you don't
   know. (`feedback_full_code_review.md`)

7. **VERIFY against code, not memory/vibes.** Last-Claude got told off for
   stale-memory operating mode. Read the actual code via parallel reads.

8. **Use skills + dispatch subagents.** David called out solo-grinding twice.

---

## Today's session arc

This session picked up where the 2026-04-25 evening handoff left off
(Review-UI refactor mid-execution: 5/12 tasks done per handoff doc, but
git showed Tasks 8–9 also landed because David reverted/restored them
to give me context).

**What happened, in order:**

1. **Verified actual refactor state** — Tasks 1–9 all in git; plan checkboxes
   were never updated (cosmetic only). Real remaining: Tasks 10, 11, 12.

2. **Finished Tasks 10–12 directly:**
   - `b4f9a80` — Task 10: server-side camera + multibird filters on
     `/api/review/classified`. Extended multibird from bool to tri-state
     (`"only" | "exclude" | ""`) in `reviews_db._build_classification_query`.
   - `2ebf8ce` — Task 11: UI dropdowns on Classify + Classified. Converted
     Classify's checkbox to a tri-state select.
   - `eaa64bd` — Task 12: spec doc at
     `docs/superpowers/specs/2026-04-25-review-ui-helpers.md`.

3. **Final code-reviewer pass** on the whole 11-commit refactor diff. Three
   fixes committed (`4af7458`):
   - `queueNextPage` off-by-one when verdictsSinceFetch ≥ pageSize
   - multibird truthy-trap (`"0"` was wrongly applying the filter); replaced
     with explicit allow-list
   - docstring drift on `/api/review/classified`

4. **Simplify pass** on three deferred reviewer findings (`2014a7b`):
   - Dropped `classifiedItems` / `skippedFiles` "thin reflection" arrays
     (lightboxes now read `state.lastResp.items` directly)
   - Dropped legacy innerHTML-substring fallback in `applyVerdictToUI` (all
     grids now have `data-file`)
   - Wrapped duplicated post-verdict block into `applyVerdictAndRecord`

5. **RC3 final code-reviewer** (deferred from yesterday). Came back "ship
   it" with 2 minor follow-ups. Landed `5d5332f`:
   - Disagreement flag now case-fold + whitespace-tolerant (so `"Northern
     Cardinal"` vs `"northern cardinal"` doesn't false-flag).
   - Added 2 tests covering case-folding + None-species edge case.
   - Pipeline restarted, guard live for new rows.

6. **Calibration UI** (`5e2c8d0`). Replaced the terminal-based
   `tools/calibrate_disagreement.sh` (which David rightly called out as the
   wrong tool — "we have a whole review UI"):
   - Added `RC3_BUCKETS` dict to `reviews_db.py` (4 strata as SQL
     predicates + labels) + `bucket` param threaded through
     `get_classifications` / `count_classifications` /
     `_build_classification_query`. Bucket implies `c.id >= RC3_WATERSHED_ID`.
   - `/api/review/pending` accepts `bucket=A|B|C|D`.
   - `/api/review/calibration-stats` returns per-bucket totals + verdict
     tallies for live precision computation.
   - Classify toolbar gained a "calibration bucket" dropdown next to the
     other filters; selecting one reveals an inline stats panel that
     refreshes after every verdict.
   - Bash script deleted.

7. **David ran calibration** through the UI. Generated **263 verdicts**
   across the 4 strata (vastly over-delivered the suggested 20/bucket).
   Results:

   | Bucket | description | reviewed | precision | when wrong: auth right |
   |---|---|---|---|---|
   | A | dis + auth.conf < 0.1 | 67 | **55%** | 47% |
   | B | dis + auth.conf ≥ 0.1 | 32 | **31%** | **85%** |
   | C | agr + auth.conf < 0.1 | 61 | **85%** | n/a |
   | D | agr + auth.conf ≥ 0.5 | 103 | **98%** | n/a |

   `not_a_bird` count = 0 across all buckets. David corrected to a real
   species when system was wrong, didn't trash. So "wrong" here means
   "different-but-real species," not "no bird in bbox."

8. **RC2 plan written from data** (`6e9b2d9`):
   `docs/superpowers/plans/2026-04-26-rc2-from-calibration.md`. The
   **original RC2 hypothesis is wrong.** Calibration disproves
   "confidence floor → drop" — bucket C (low auth.conf + agreement) is 85%
   precise, so a floor would over-prune. Three scoping options proposed:
   v1 (~30 min, just `suspect=true` flag), v2 (~1.5 hr, recommended —
   `suspect` + `training_label` + `training_label_source`), v3 (~3 hr,
   also rewrite canonical species). **Awaiting David's choice.**

9. **David's strategic clarification:** "feeder model is just for live
   labels because it's faster. what we really want to figure out is
   1: how to get really good data and 2: how to use that good data to
   train a fantastic model." Then: "i want to know if we can train on
   AIY as the base." See "AIY-as-base — the honest truth" below.

---

## Hi-res to AIY — THE actual unsolved data problem

**The single biggest "good data" lever we haven't pulled.**

Currently when `SnapshotWriter._authoritative_species` runs, it classifies
the **640×360 substream crop** that the lock-time frame came from. Same
low-res garbage that's poisoning ~30% of saved rows.

The hi-res ring is BUILT (`pipeline/hires_ring.py` — Pi-Claude's work,
already in this repo). On Pi it's ON
(`PIPELINE_HIRES_RING=authoritative`). On iMac the env flag is **not set**,
so the ring is dormant.

**To turn it on (one line + restart):**

```bash
# Edit ~/.bird-observatory-env, add:
export PIPELINE_HIRES_RING=authoritative

launchctl kickstart -k "gui/$(id -u)/com.vives.bird-pipeline"
# Watch for: "[hires_ring] ENABLED — shadow_mode=False"
```

Then verify in the next several rows:
- `pipeline/snapshot_writer.py` should pick from the ring (look for
  `hires_ok` / `hires_skipped` counts in `/snapshot-stats`)
- AIY classifies the hi-res crop, not the substream
- New rows' `extra_json.authoritative.confidence` should rise on average

**Risk:** if the ring is mis-configured (FFmpeg path, frame timing), you
get `hires_fail` errors and AIY falls back. Worth tailing pipeline logs
for ~5 min after the restart.

---

## AIY-as-base — the honest truth

David asked: "can we train on AIY as the base, that's been the best
model? i'd like to know the truth."

**You CANNOT fine-tune the `.tflite` file directly.** It's a compiled
INT8 binary, no trainable graph. Anyone saying "just fine-tune AIY"
without specifying *what* is hand-waving.

**You CAN use AIY's knowledge — three real ways:**

1. **Knowledge distillation (best).** Train a new small model to mimic
   AIY's softmax outputs on our images. Captures AIY's nuanced "bird"
   understanding. Pairs perfectly with our 900+ hand/pseudo-verified
   labels: distillation on the unlabeled mass + supervised loss on the
   labeled ones.

2. **Pseudo-labeling.** Use AIY's confident predictions
   (auth.conf > 0.5 = bucket-D regime) as labels. Calibration confirms
   98% precision in that regime. Already a primitive form of this.

3. **AIY logits as features.** Concatenate AIY's 965-vector with our
   image features in a small head. Smaller gain, more plumbing.

**You CAN'T find the original AIY float checkpoint.** Google never
published it. AIY = MobileNet-v1 trained on Google-internal bird data
(referenced in Van Horn iNat papers). Approximate substitutes exist on
HuggingFace but are not the same model.

**The Tier 2 plan deliberately rejected this approach** ("start from
ImageNet, not iNat"). That decision pre-dates today's calibration data
and David's clarification. **The Tier 2 plan needs revision** to use
AIY-distillation. With 900 high-quality labels + AIY-as-teacher, we
can almost certainly beat ImageNet-from-scratch.

---

## How to get good data — the actual path

Decomposed from David's question 1 ("how do we get really good data"):

1. **Turn on hi-res-to-AIY** (above). Single biggest lever.
2. **Fix `auth.confidence > 1` bug** (side-finding below). Trustworthy
   confidences gate everything downstream.
3. **Apply RC2** (whatever scope David picks) so future rows carry
   training-pool metadata.
4. **Build a "training pool" SQL view** filtering by calibration-derived
   rules: bucket D + C as gold, bucket B with auth-as-label, A excluded.
5. **Add explicit negatives:** David's 289 trash + 85 not_a_bird +
   manually-sampled hi-res empty-feeder frames. **NOT the 22K culled
   folder** (per Hard Rule #2).
6. **RC4 (multi-bird annotation)** so multi-bird shots stop generating
   "one row, one bbox, missing the others." Plan not yet written.

## How to train a fantastic model — the actual path

Decomposed from David's question 2 (mostly already specced in
`2026-04-23-tier2-training-plan-v1.md`, but needs revision):

- **Backbone:** EfficientNet-Lite0, 224×224 input (Coral-friendly INT8).
  Decision still good.
- **Starting point: REVISE — AIY distillation, not ImageNet from scratch.**
- **Augmentation:** RandAugment (N=2,M=9), MixUp α=0.2, CutMix α=1.0,
  RandomErasing p=0.25, horizontal flip. (David explicitly named all of
  these — "all of it.")
- **Class balance:** Two-stage training per Kang 2020 — instance-balanced
  backbone, then class-balanced classifier head with logit-adjusted CE.
- **Cleanlab** on the lower-precision tiers (bucket A residuals, weak
  pseudo-labels) to remove residual noise. NOT needed for bucket D.
- **Explicit OOD:** `not_a_bird` + `unknown` classes. Use David's verified
  negatives + manually-sampled empty frames.
- **Quantization-aware training** so INT8 deployment matches eval.
- **Calibration:** temperature scaling on the held-out set. Target
  Expected Calibration Error (ECE) < 0.05.
- **Visit-grouped train/test splits** (`StratifiedGroupKFold`) — temporal
  leakage is camera-trap ML's #1 silent failure.
- **Pairwise Confusion + Center Loss** (Dubey 2018) for lookalike pairs
  (Hairy/Downy etc.). +1–3% on confused pairs.
- **Eval harness already in place** at `tier2_eval/`.

---

## Repo state — git commits this session

```
6e9b2d9  plan: RC2 informed by 263-verdict calibration data
5e2c8d0  feat(review): RC3 calibration in the Classify UI (retires bash script)
5d5332f  fix(rc3): case-fold + whitespace-tolerant disagreement flag
2014a7b  refactor(review): simplify pass on shared-helpers refactor
4af7458  fix(review): code-reviewer fixups on shared-helpers refactor
eaa64bd  docs(review): spec for applyVerdictToUI + loadQueue shared helpers
2ebf8ce  feat(review): camera + multibird filters on Classify and Classified tabs
b4f9a80  feat(review): /api/review/classified accepts camera + multibird filters
```

(Yesterday's chain: `c9c4bca` … `38ba415` — the Review-UI refactor Tasks
1–9 plus yesterday's evening handoff.)

---

## Open side-findings

1. **`auth.confidence > 1` in 504 post-watershed rows** (max 2.5).
   `_authoritative_species` is leaking either a `raw_score` on some path,
   or a numpy-scalar serialization issue, or a float overflow. Logged in
   `docs/superpowers/progress/2026-04-25-side-findings.md`. **15-min audit
   before RC2 ships.** Doesn't break the bucket logic (both `>= 0.1` and
   `>= 0.5` thresholds still match), but the disagreement-flag math is
   nonsense for those rows.

2. **iMac YOLO at 134ms avg / 499ms p99.** Doc says ~98ms with CoreML.
   Either CoreML isn't active or iMac is under load. Side-finding from
   yesterday, not blocking.

3. **review_history legacy backfill missing on iMac** (1827 rows
   pre-watershed). Cosmetic; logged.

4. **4 pre-existing test_pipeline_classifier.py failures.** Logged.

---

## Pi-Claude state

- iMac repo should NOT see new commits from Pi-Claude.
- They were delegated the repo split + were doing Pi-side work.
- Last comms message in `docs/superpowers/progress/cross-claude-comms.md`.
- Pi 5 services were active per their handoff: `~/.bird-observatory-env`
  on Pi has `PIPELINE_HIRES_RING=authoritative` (Pi already runs hi-res).

---

## Calibration tool retired — DON'T re-create it

`tools/calibrate_disagreement.sh` was deleted in `5e2c8d0`. The right
tool is the **Calibration bucket dropdown on the Classify tab** + the
inline stats panel + `/api/review/calibration-stats` endpoint. If you
think you need a CLI calibration tool, you've forgotten this discussion.
Re-read `5e2c8d0`'s commit message.

---

## Decisions awaiting David

1. **RC2 scope** — v1 (just suspect flag) / v2 (recommended: full
   training-label metadata) / v3 (also rewrite canonical species).
2. **Trust the bucket-B 85%-auth-wins finding from n=20?** Or another
   ~30 verdicts to tighten?
3. **Hi-res-to-AIY** — pull the trigger? (One env line. Recommend yes.)
4. **AIY-distillation revision to Tier 2 plan** — write it now, or wait?
5. **Brainstorm distillation approach first** (student arch, loss mix,
   eval) or just write the updated plan?

---

## Skills used today

- `subagent-driven-development` (refactor Tasks 10–12)
- `code-reviewer` agent (refactor + RC3 reviews)
- `writing-plans` (RC2 plan)
- `verification-before-completion` (always-on)
- `using-superpowers` (meta)

Skills NOT yet invoked but should be when relevant:
- `brainstorming` — for the AIY-distillation revision discussion (per
  `project_yard_model_revamp.md`'s playbook)
- `test-driven-development` — for RC2 implementation
- `dispatching-parallel-agents` — possibly for the Tier 2 plan revision
  (lit-review on knowledge distillation specifically)

---

## David's working preferences (carried forward + reinforced today)

- Pushes back on stale-memory / vibes-based answers — VERIFY against code
- Pushes back on solo grind — invoke skills, dispatch subagents
- Pushes back on terminal tools when a UI exists ("we have the whole review tab")
- Pushes back on overcomplicating ("you keep solving the wrong problem")
- Asks for explanations at HIS level — not jargon, not data dumps,
  plain-English narrative. Iterate to clarity.
- Direct, terse, no preamble
- Says "still try your hardest" when asked about effort level
- "Slow and right" affirmation = systematic-debugging working
- Strategic about session-arc handoffs (asks for them BEFORE compaction)

---

## Compact prompt

When the next /compact comes, bias the summary toward:
- The calibration findings (the 4-bucket precision table is the hard
  artifact — keep it verbatim)
- The Tier 2 plan revision intent (AIY-distillation, not ImageNet-scratch)
- Hi-res-to-AIY as the next data lever
- The 5 awaiting-decisions list

Compress aggressively on:
- The Review-UI refactor (it's done; commits + spec doc capture it)
- The terminal calibration script saga (it's deleted; don't re-litigate)
- Yesterday's content (the 2026-04-25 handoff stays canonical for that)

---

## Key file pointers (consolidated)

| What | Path |
|---|---|
| **THIS handoff** | `docs/superpowers/progress/2026-04-26-evening-handoff.md` |
| Yesterday's handoff | `docs/superpowers/progress/2026-04-25-evening-handoff.md` |
| **RC2 plan (awaiting choice)** | `docs/superpowers/plans/2026-04-26-rc2-from-calibration.md` |
| Audit findings (the 4 root causes) | `docs/superpowers/progress/2026-04-25-detection-snapshot-audit-findings.md` |
| Review-UI helpers spec | `docs/superpowers/specs/2026-04-25-review-ui-helpers.md` |
| Review-UI plan (DONE) | `docs/superpowers/plans/2026-04-25-review-ui-shared-helpers.md` |
| Tier 2 training plan (NEEDS REVISION) | `docs/superpowers/specs/2026-04-23-tier2-training-plan-v1.md` |
| Yard revamp brief | `~/.claude/projects/-Users-vives/memory/project_yard_model_revamp.md` |
| iMac as-built | `docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` |
| Hailo playbook | `docs/superpowers/specs/2026-04-25-hailo-playbook.md` |
| Side-findings ledger | `docs/superpowers/progress/2026-04-25-side-findings.md` |
| Cross-Claude comms | `docs/superpowers/progress/cross-claude-comms.md` |
| RC3 plan | `docs/superpowers/plans/2026-04-25-rc3-preserve-lock-time-vote.md` |
| Pi 5 handoff | `docs/superpowers/progress/2026-04-25-pi5-handoff.md` |
