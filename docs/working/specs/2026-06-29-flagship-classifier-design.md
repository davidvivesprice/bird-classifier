# Flagship Feeder-Bird Classifier — Design Dossier (DRAFT)

**Date:** 2026-06-29
**Status:** DRAFT assembled autonomously for David's review. Data/baseline/problem sections are evidence-grounded and final-quality. Architecture / calibration-OOD / Hailo-compile sections are **pending the research workflow synthesis** (running) and will be appended. Key scope decisions are flagged as DAVID DECISIONS — not made unilaterally.

**This supersedes the Coral-era brief** (`memory/project_yard_model_revamp.md`, 2026-04). The hardware target is now **Pi 5 + Hailo-8L**, not Coral. The process from that brief (brainstorm → lit review → data audit → eval harness → plan) still holds; this dossier advances the lit-review + data-audit steps.

---

## 1. The problem (evidence-grounded)

The live identifier gets common species **right but under-confident**, so it rarely "locks" a label. Two independent measurements:

- **Live (2026-06-29):** a real Northern Cardinal was tracked steadily (frame_count 266 ≈ 9s after the S1 coast fix) and labeled "Northern Cardinal" via vote-plurality — but at confidence **0.27**, below the 0.35 lock gate, so it never locked.
- **Hold-out baseline** (`tier2_eval/baseline.report.json`, n=1670, the current AIY model — the model to beat):
  - top-1 accuracy **0.68**, macro-F1 0.75
  - **ECE 0.163** — badly mis-calibrated (this is THE problem: "says 80%" ≠ "right 80%")
  - **`not_a_bird` recall 0.0** — the OOD/non-bird path is completely broken; it cannot reject squirrels/empty frames
  - Downy Woodpecker recall 0.41, Hairy Woodpecker precision 0.43 — the classic Downy/Hairy confusion
  - Blue Jay precision 0.39, Northern Cardinal precision 0.62 — over-prediction on some classes

**Flagship success target** (David's words, from the old brief, still the bar): *"good at knowing what it should know AND what it doesn't."* Concretely: high per-species recall on the actual visitor distribution, **calibrated confidence (low ECE)**, and **proper OOD** (untrained bird / non-bird → "unknown", never a hallucinated known species).

## 2. Data foundation (the HARD GATE — audited 2026-06-29)

On the iMac data store `~/bird-snapshots/logs/classifications.db` (269 MB):

- **246,725 classifications** (weak AIY labels) — the training pool. Visitor distribution (top, by count): House Finch 38.6k, Black-capped Chickadee 17.2k, Dark-eyed Junco 14.7k, White-breasted Nuthatch 10.4k, Mourning Dove 8.5k, Song Sparrow 7.4k, Downy Woodpecker 6.7k, American Goldfinch 5.7k, Carolina Wren 5.6k, Northern Cardinal 5.0k, Tufted Titmouse 4.7k, then Cowbird/Hairy/House Sparrow/Grackle/Mockingbird/Robin/Crow/White-throated Sparrow (1–2k each). (102.6k rows are blank/unlabeled.)
- **2,285 human reviews = GROUND TRUTH** (never train on these): 1,353 `correct`, 138 `reclassify` (corrected label), 388 `wrong` (hard negatives), 339 `trash` (non-bird/empty → **OOD negatives**), plus skip/requeued.
- **Per-species clean labels** (correct-via-join + reclassify-corrected) — **15 species have ≥20 clean labels**, the viable eval class set:
  House Finch 190, Black-capped Chickadee 180, Carolina Wren 92, Hairy Woodpecker 88, Song Sparrow 85, American Goldfinch 83, Downy Woodpecker 79, Dark-eyed Junco 72, White-breasted Nuthatch 70, Tufted Titmouse 69, Northern Cardinal 68, Brown-headed Cowbird 67, Red-bellied Woodpecker 62, Mourning Dove 61, Blue Jay 41. (Tail: Red-winged Blackbird 15, Pine Warbler 13, then <5.)
- Also available: `~/bird-snapshots/classified/` per-species image folders, `~/bird-snapshots/annotated/` (~bbox-drawn JPGs), `models/chilmark_feeder_species.txt` (62 plausible MV species), `models/species_ranges.json`.

**Implications:** the **core class set is the top ~15 species** (≥20 clean labels, thousands of weak labels each). The tail (<20 reviews) folds into "unknown/other" until more reviews accumulate. The **OOD negative set is real and free**: 339 trash (non-bird) + 388 wrong (mis-IDs).

## 3. Eval harness (already exists — reuse)

`tier2_eval/` (`split.py`, `metrics.py`, `baseline.py`, `baseline.report.json`) already computes top-1, macro-F1, **ECE**, per-class recall/precision, and a confusion matrix on the reviews hold-out. The flagship must **beat the baseline on this harness**: accuracy ↑, **ECE ↓ (target TBD by David)**, per-species recall floor (TBD), and a new **OOD AUROC** metric (the harness needs an OOD test split built from the 339 trash + held-out untrained species).

## 4. Architecture / Calibration+OOD / Hailo compile

> **PENDING** — being produced by the `flagship-classifier-research` workflow (Hailo-targeted: backbone choice, calibration+OOD method, small-noisy-data fine-tuning, Hailo Dataflow Compiler path). Synthesis will be appended here.

## 5. DAVID DECISIONS (not made unilaterally — these are the brainstorming inputs)

1. **Class set:** top 15 (≥20 clean labels) + "unknown"? Or include the 1–2k-weak-label tail (Cowbird/Hairy already in the 15; Grackle/Mockingbird/Robin/Crow as added classes)?
2. **Metric acceptance targets:** per-species recall floor (e.g. ≥0.85?), ECE ceiling (e.g. ≤0.05?), OOD AUROC floor, max wrong-lock rate.
3. **OOD handling:** explicit "unknown" class vs calibrated-threshold abstain vs energy/feature-distance (research will recommend; you pick the risk posture).
4. **Lock-threshold policy:** keep the fixed 0.35, or set it from the calibration curve per the new model? (Interim option: a quick env-gated experiment lowering the *current* lock gate ~0.35→0.30, since the model is correct-but-under-confident — would surface more labels now, at some wrong-lock risk. Your accuracy-vs-coverage call.)
5. **Training compute & cadence:** train on the iMac (`venv-training/`) and compile to Hailo, or other; one-shot vs periodic retrain as reviews grow.

## 6. Non-negotiables carried forward
- Visually verify training data before training (`feedback_verify_data_first` — the prior attempt failed on data, not model).
- Honesty over optimism — real numbers on the hold-out, never "should work."
- Drop-in via the existing classifier interface; degrade gracefully per-inference; never stall the live path.
- Reproducible recipe — "flagship" includes someone else being able to follow it.
