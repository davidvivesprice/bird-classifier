# RC2 — informed by calibration data, not the original hypothesis

**Date:** 2026-04-26
**Skill:** writing-plans
**Status:** plan, not yet executed
**Calibration source:** 263 verdicts across 4 strata, recorded 2026-04-26 via the
new Classify-tab calibration UI (commit `5e2c8d0`). Watershed = id 756294.

## What the calibration actually showed

| Bucket | description | reviewed | lock-time precision | when lock wrong: auth right |
|---|---|---|---|---|
| A | disagree + auth.conf < 0.1 | 67 | **55%** (37/67) | 47% (14/30) |
| B | disagree + auth.conf ≥ 0.1 | 32 | **31%** (10/32) | **85%** (17/20) |
| C | agree + auth.conf < 0.1 | 61 | **85%** (52/61) | n/a |
| D | agree + auth.conf ≥ 0.5 | 103 | **98%** (101/103) | n/a |

(`not_a_bird` count = 0 across all buckets — David corrected to a real species
when the system was wrong, didn't trash. So "wrong" here means
"different-but-real species," not "no bird in bbox.")

## What this overturns

The **original RC2 hypothesis** was *"add a confidence floor at the write
boundary; rows below threshold are noise."* The calibration disproves it:

- Bucket C (low auth confidence + agreement): **85% precise.** A confidence
  floor would over-prune real birds.
- Bucket A (low auth confidence + disagreement): **55% — coin flip.** Not
  pure noise; throwing out everything below auth.conf < 0.1 sacrifices ~half
  the real birds in this stratum.
- Bucket B (high auth + disagreement): the lock-time species is wrong 69% of
  the time, but **auth was right 85% of the time** in those wrong cases. The
  audit's framing — *"auth overwrites lock-time with noise"* — is **wrong for
  this bucket.** Auth was correcting lock-time. We just lost the ability to
  know it.

## What this means

The disagreement flag isn't a noise filter. It's a **"lock-time-species-might-
be-wrong"** filter, and when auth is confident, **auth is usually the better
label.** RC2 has to encode that, not just drop rows.

## RC2 — three scoping options

### v1 (minimal — ~30 min): just the `suspect` flag

Add `extra_json.suspect=true` only to bucket A rows. Cleanlab can use this as
a hint to deprioritize them in training. No pipeline behavior change beyond
the new field.

- Pros: tiniest possible change, reversible, doesn't touch the canonical
  species. Buys us a filter signal immediately.
- Cons: leaves bucket B's 85% auth-wins finding on the table.

### v2 (recommended — ~1.5 hr): suspect flag + per-row training-label hint

Add three new fields to `extra_json` at write time, computed from the bucket:

| field | bucket A | bucket B | bucket C | bucket D |
|---|---|---|---|---|
| `suspect` | `true` | `false` | `false` | `false` |
| `training_label` | (none) | `<auth.species>` | `<lock.species>` | `<lock.species>` |
| `training_label_source` | `manual_review` | `auth_corrected` | `lock_time` | `lock_time` |

The `training_label` field is the recommended label for downstream training.
For bucket B we override with auth's species (85% wins). For buckets C+D we
use lock-time's species (already 85%/98% precise). Bucket A gets no
auto-label — it goes to manual review.

The **canonical `c.common_name` column doesn't change.** This is metadata
only; the dashboard, live-feed UI, and existing review flows stay identical.
Only cleanlab + future training-data exporters consume the new field.

- Pros: encodes the calibration finding directly. Cleanlab can train on
  ~85K+ rows of high-precision labels (D ≥98%, C ≥85%, B-corrected ≥85%
  if we trust auth, A excluded). Reversible.
- Cons: more fields in extra_json; one more thing to document.

### v3 (full — ~3 hr): also rewrite canonical species at write time

Same as v2 but also overwrite `c.common_name` with the new `training_label`.
Bucket B rows would store `auth.species` as canonical going forward.

- Pros: dashboards + reviewer UI immediately reflect the better species
  choice; less downstream confusion.
- Cons: changes user-visible data semantics; can't be reverted without a
  reclassification rerun. Risk of regret if the 85% auth-wins finding doesn't
  generalize beyond this sample.

## Recommendation: v2

v1 leaves too much value on the table. v3 risks regretting a one-shot
rewrite. v2 is the sweet spot: encodes everything calibration taught us
without touching user-visible behavior, and v3 stays available later as a
"promote training_label to canonical" follow-up if v2 proves out.

## Implementation steps for v2

- [ ] **Step 1: Refactor the bucket logic into a single helper**

  In `pipeline/snapshot_writer.py`, add `_classify_rc3_bucket(disagreement,
  auth_conf)` that returns `'A'|'B'|'C'|'D'|None`. Same predicates used by
  `reviews_db.RC3_BUCKETS`. Keep the predicates colocated with definitions in
  `reviews_db.py` to avoid drift — import-and-reuse if simple, otherwise
  duplicate with a doc-string cross-reference.

- [ ] **Step 2: Add the three new entry-dict keys**

  In `_write_one`, after the existing `lock_time` / `authoritative` /
  `disagreement` block, compute the bucket and set:

  ```python
  bucket = _classify_rc3_bucket(entry["disagreement"],
                                 (auth or {}).get("confidence"))
  entry["suspect"] = (bucket == "A")
  if bucket == "B" and auth and auth.get("species"):
      entry["training_label"] = auth["species"]
      entry["training_label_source"] = "auth_corrected"
  elif bucket in ("C", "D"):
      entry["training_label"] = lock_time_species
      entry["training_label_source"] = "lock_time"
  # bucket A or unclassified → no training_label (excluded from training pool)
  ```

- [ ] **Step 3: Tests**

  Add to `tests/pipeline/test_snapshot_writer_rc3.py`:
  - `test_bucket_A_marked_suspect_no_training_label`
  - `test_bucket_B_uses_auth_species_as_training_label`
  - `test_bucket_C_uses_lock_species_as_training_label`
  - `test_bucket_D_uses_lock_species_as_training_label`

- [ ] **Step 4: Add a `suspect` filter to `/api/review/pending` + Classify UI**

  The existing bucket dropdown is for calibration. Add a separate small
  toggle: "Hide suspect rows" (default on). Reviewers shouldn't have to
  re-process bucket-A noise unless they opt in.

- [ ] **Step 5: Backfill stats query**

  Add a one-off SQL note to the audit-findings doc showing what % of
  post-watershed rows would have which `training_label_source`. Helps size
  the cleanlab pool.

- [ ] **Step 6: Pipeline restart + verify on first new row**

  ```bash
  launchctl kickstart -k "gui/$(id -u)/com.vives.bird-pipeline"
  # wait for a new classified row, then:
  sqlite3 ~/bird-snapshots/logs/classifications.db \
    "SELECT id, common_name, json_extract(extra_json,'\$.suspect'),
            json_extract(extra_json,'\$.training_label'),
            json_extract(extra_json,'\$.training_label_source')
     FROM classifications ORDER BY id DESC LIMIT 5;"
  ```

  Expect: every row has a non-null `suspect` (true or false); buckets B/C/D
  have a `training_label`.

- [ ] **Step 7: Update the as-built spec**

  `docs/superpowers/specs/2026-04-25-imac-live-classify-as-built.md` —
  add the new fields to the extra_json reference + a one-paragraph "RC2:
  what training-label-source means" section.

## Side-finding (not blocking)

`AVG(authoritative.confidence)` for bucket B's wrong rows is **1.022** — i.e.
some rows have `auth.confidence > 1`. AIY's softmax shouldn't return >1; one
of these is true:
1. `_authoritative_species` is returning `raw_score` not `confidence` for
   some path
2. There's a numpy-scalar serialization issue producing exotic values
3. The float math is overflowing somehow

Worth a 15-minute audit but NOT a blocker for RC2. Logging in
`docs/superpowers/progress/2026-04-25-side-findings.md`.

## Open questions for David

1. v1 / v2 / v3? (recommendation: v2)
2. Any objection to the `suspect=true` UI default-hiding bucket A from
   normal review?
3. Trust the 85%-auth-wins finding from n=20? It's a small sample for that
   specific cell; another 30 verdicts in bucket B would tighten the
   confidence interval.
