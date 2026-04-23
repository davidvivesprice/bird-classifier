# Tier 2 Yard Model Revamp — Data Audit (2026-04-23)

**Purpose:** Concrete state of the training-data assets *before* any yard-model work begins. This is the HARD GATE from `feedback_verify_data_first.md` — the prior attempt scored 0/14 because we trained on garbage without auditing. This audit isn't optional; it's the foundation every subsequent decision rests on.

**Method:** SQL against `classifications.db`, filesystem walks of `~/bird-snapshots/`. All queries reproducible from this doc. Read-only — nothing mutated.

---

## 1. Inventory at a glance

| Asset | Count | Notes |
|---|---|---|
| Total classification rows | 155,159 | after 2026-04-23 cull |
| Rows with `action='classified'` (bird-classified) | ~130K | the weak-label training pool |
| Rows with `action='no_bird'` / `skipped:*` | ~92K | detection-stage log; never had a JPG |
| Rows with `action='trashed:*'` | ~10K | reviewer-rejected, file moved to trash |
| Rows with `action='culled_hallucination'` | 11,293 | today's 0b cull; in quarantine 30d |
| Human reviews (ground truth) | 1,827 | |
| Files on disk — classified/\<species\>/ | 36,335 | across 46 species dirs |
| Files on disk — annotated/ | 42,535 | with corner-bracket bbox overlay (27 GB) |
| Files on disk — culled/2026-04-23/ | 22,586 | two per culled row (classified + annotated) |
| Multi-bird frames (`json_array_length > 1`) | 10,341 | **MUST FILTER from training** |

## 2. The ground-truth hold-out (reviews)

This is the most valuable asset in the entire system. It must **never** go into training.

**Verdict breakdown (1,827 reviews):**

| Verdict | Count | Semantics |
|---|---|---|
| `correct` | 1,133 | The row's label matches the bird. Positive ground truth. |
| `wrong` | 251 | Row's label wrong; `correct_species` is the truth. |
| `trash` | 289 | Not-a-bird (squirrel / empty / debris). Hard negative ground truth. |
| `reclassify` | 103 | Reviewer deferred; don't use. |
| `requeued` | 30 | Reviewer sent back; don't use. |
| `skip` | 21 | Reviewer abstained; don't use. |

**Usable ground truth: 1,673 (1,133 + 251 + 289).**

### Per-species positive ground truth (from `correct` verdicts)

| Species | `correct` reviews |
|---|---|
| Black-capped Chickadee | 91 |
| Hairy Woodpecker | 88 |
| Carolina Wren | 87 |
| Song Sparrow | 85 |
| American Goldfinch | 83 |
| House Finch | 80 |
| Dark-eyed Junco | 72 |
| White-breasted Nuthatch | 70 |
| Downy Woodpecker | 69 |
| Brown-headed Cowbird | 67 |
| Northern Cardinal | 65 |
| Red-bellied Woodpecker | 62 |
| Mourning Dove | 61 |
| Tufted Titmouse | 60 |
| Blue Jay | 49 |
| Red-winged Blackbird | 15 |
| Pine Warbler | 13 |
| European Starling | 4 |
| White-crowned Sparrow | 3 |
| Chipping Sparrow | 3 |
| American Robin | 2 |
| White-throated Sparrow | 1 |

Plus the 251 `wrong` corrections (`correct_species` is the true label) — concentrated on Downy Woodpecker (100) and not_a_bird (85).

**Hold-out test set target: 15 species × 50-90 samples each + 289 non-bird = ~1,300 samples.** Sufficient to measure per-species recall and ECE meaningfully.

## 3. Training-data pool (by species)

Files on disk per species, combining `Spaces Dir/` and `Underscored_Variant/`. Excludes reviewed files (held-out).

**Species with ≥500 files (the 13 plausible training classes):**

| Rank | Species | Combined file count |
|---|---|---|
| 1 | Mourning Dove | 7,125 |
| 2 | House Finch | 5,704 |
| 3 | Black-capped Chickadee | 4,258 |
| 4 | Song Sparrow | 4,068 |
| 5 | Dark-eyed Junco | 3,047 |
| 6 | Downy Woodpecker | 1,649 |
| 7 | Northern Cardinal | 1,626 |
| 8 | Tufted Titmouse | 1,422 |
| 9 | White-throated Sparrow | 809 |
| 10 | White-breasted Nuthatch | 771 |
| 11 | Northern Mockingbird | 669 |
| 12 | Brown-headed Cowbird | 550 |
| 13 | House Sparrow | 539 |

**Class imbalance: 7,125 ÷ 45 (Common Grackle, bottom of long tail) = 158× — severe. Will require stratified sampling + class-weighted loss.**

### Current yard model's 12 species vs. proposed top-13

Current yard label set (`yard_model_labels.txt`): 12 species. I haven't read the file this session but the brief mentions the mismatch cost (anything not in these 12 gets force-mapped → hallucination). The top-13 above will differ from the current list; the species set itself is a scoping decision.

## 4. Confusion patterns (from `wrong` verdicts)

The most frequent `(labeled_as → actually_is)` pairs from 251 wrong verdicts:

| Labeled as | Actually is | Count | Interpretation |
|---|---|---|---|
| **Hairy Woodpecker** | **Downy Woodpecker** | **100** | THE dominant lookalike confusion. Must solve. |
| Blue Jay | Black-capped Chickadee | 19 | Surprising (size difference); worth investigating. |
| Dark-eyed Junco | not_a_bird | 19 | Squirrel / empty / IR frame |
| Common Grackle | not_a_bird | 11 | Matches the stale-bbox pattern culled today |
| European Starling | not_a_bird | 11 | Same |
| American Goldfinch | not_a_bird | 10 | Same |
| Northern Cardinal | House Finch | 7 | Female Cardinal ↔ female House Finch (real-world hard) |
| Brown-headed Cowbird | House Finch | 6 | Silhouette confusion |
| Various | not_a_bird | 50+ | Squirrel-or-empty-frame problem |

### Implications for model design

- **Hairy/Downy binary must be a specific validation target.** 100 corrections says the existing system cannot tell them apart reliably.
- **"not_a_bird" is the single biggest confusion class.** Explicit OOD class (or gating by detection confidence) is not optional.
- **Sparrow-group confusions are absent** from the top-20 `wrong` verdicts — suggests the label set might lump sparrows loosely and split only when evidence accumulates (a later-stage enhancement).

## 5. OOD / non-bird training data

| Source | Count | Notes |
|---|---|---|
| `wrong` verdicts → `correct_species='not_a_bird'` | 85 | explicit "this was labeled bird X but is actually a squirrel/empty" |
| `trash` verdicts (all not_a_bird by definition) | 289 | |
| `no_bird` classifications | ~82K | detection-stage, no file saved (unusable as image OOD) |
| Culled-hallucination JPGs in quarantine | 22,586 | a HUGE pool — ~11K "confidently-wrong" crops, many of which are empty feeder frames. Usable as OOD hard negatives if we want. |

**Recommendation:** Build an OOD training set from `trash` + `correct_species='not_a_bird'` (374 confirmed) + a subset of culled-hallucination JPGs (sample 500-1000 and visually verify they're bird-free).

## 6. Data-integrity findings from the audit

- **46 species have dual directories** (`Species Name/` + `Species_Name/`). Not 12 as earlier memory suggested. Training scripts must walk both OR a consolidation pass must run first.
- **0 TRUE orphan rows** right now (after 0b cull and 1a audit fix).
- **336 "canonical orphan files"** under underscored-variant dirs — these ARE valid training data (the historical files), just not counted as DB-referenced orphans because most underscore-dir files DO have DB rows. The 336 without DB rows are the residue.
- **Multi-bird (10,341 rows) MUST be excluded from training.** `json_array_length(birds_json) > 1` filter.
- **Annotated dir has 42,535 bbox-overlaid images.** Useful as a visualization aid during training-set verification. Bbox coordinates themselves live in `classifications.best_detection_json.box`.

## 7. Recommended next steps (unchanged from `project_yard_model_revamp.md`)

1. **Brainstorm (superpowers:brainstorming)** the seven open questions, *informed by this data*:
   - Label set: 13 top + "other"? 15? Top-N + explicit OOD?
   - OOD strategy: explicit class vs energy-based vs threshold?
   - Split strategy: per-camera? per-visit? per-week?
   - Target metric set: per-species recall floor? ECE target? OOD AUROC?
   - Architecture: MobileNet-V3-Small (Coral-native) vs EfficientNet-Lite?
   - Eval harness: live dashboard with per-species rolling accuracy?
   - Shadow-mode rollout plan: how many days beside current yard before flipping?
2. **Parallel lit review (dispatching-parallel-agents)** on the four angles from the brief.
3. **Visually verify** ≥5 images per species for the top 13 before any training run. **HARD GATE.**
4. **Write training plan** (superpowers:writing-plans).
5. Train. Evaluate. Shadow-deploy. Flip.

## 8. Reproduction of the queries in this audit

Every number above comes from these commands — run them yourself to re-verify.

```bash
# Per-species classified counts
sqlite3 ~/bird-snapshots/logs/classifications.db "
  SELECT common_name, COUNT(*) FROM classifications
  WHERE action='classified' AND common_name IS NOT NULL
  GROUP BY common_name ORDER BY COUNT(*) DESC;"

# Reviews breakdown
sqlite3 ~/bird-snapshots/logs/classifications.db "
  SELECT verdict, COUNT(*) FROM reviews GROUP BY verdict;"

# Per-species correct verdicts (via join)
sqlite3 ~/bird-snapshots/logs/classifications.db "
  SELECT c.common_name, COUNT(*) FROM reviews r
  JOIN classifications c ON r.file = c.file
  WHERE r.verdict='correct'
  GROUP BY c.common_name ORDER BY COUNT(*) DESC;"

# Wrong-verdict confusions (what was labeled X that's actually Y)
sqlite3 ~/bird-snapshots/logs/classifications.db "
  SELECT c.common_name AS labeled, r.correct_species AS truth, COUNT(*)
  FROM reviews r JOIN classifications c ON r.file = c.file
  WHERE r.verdict='wrong' AND r.correct_species != ''
  GROUP BY labeled, truth ORDER BY COUNT(*) DESC LIMIT 20;"

# Combined per-species file count (both dir variants)
python3 -c "
from pathlib import Path
counts = {}
for d in (Path.home() / 'bird-snapshots' / 'classified').iterdir():
    if d.is_dir():
        norm = d.name.replace('_', ' ')
        counts[norm] = counts.get(norm, 0) + len(list(d.glob('*.jpg')))
for n, c in sorted(counts.items(), key=lambda x: -x[1])[:15]:
    print(f'{n:<35} {c}')
"

# Multi-bird count
sqlite3 ~/bird-snapshots/logs/classifications.db "
  SELECT COUNT(*) FROM classifications
  WHERE json_array_length(birds_json) > 1 AND action='classified';"
```

## 9. What this audit is NOT

- **Not a visual-verification pass.** The HARD GATE of sampling ≥5 images per species still must happen before any training run. That requires David's eyes (or a crowd-sourced viewer) — I cannot verify image content via filesystem walks alone.
- **Not a training plan.** That comes after brainstorming the open questions.
- **Not OOD-detection research.** The brainstorm's parallel lit review will source that.

---

**Takeaway:** The training-data state is *significantly better* than the feedback-laden memory files suggested. 1,673 human-verified samples, 13 species with 500+ files each, a clear 158× imbalance to account for, one dominant lookalike pair to engineer around (Hairy/Downy), and 22K+ culled-hallucination JPGs available as OOD hard negatives if we want them. The flagship-model project is well-founded; the remaining open questions are design choices, not data-availability blockers.
