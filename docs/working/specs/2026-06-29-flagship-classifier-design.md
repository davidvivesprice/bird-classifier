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
- **Training images** in `~/bird-snapshots/classified/` are abundant for the core set (House Finch ~35k, Chickadee ~16k, Junco ~14k, Nuthatch ~10k, Mourning Dove ~7k, Carolina Wren ~5k, Downy ~5k, Goldfinch ~5k, Song Sparrow ~7k, Cardinal ~4.6k, Titmouse ~4.3k, Hairy/Cowbird ~1.4k) — but these are **AIY-weak-labeled (noisy)**, to be cleaned, not trusted as ground truth.
- **DATA-PREP BLOCKER (confirmed 2026-06-29):** the classified/ folders have **dual directories** (space vs underscore) for most species — e.g. `House Finch/` (30,087) AND `House_Finch/` (5,180); same for Song Sparrow, Chickadee, Junco, Cardinal, Downy, Titmouse. Must consolidate before training.
- Also available: `~/bird-snapshots/annotated/` (~bbox-drawn JPGs), `models/chilmark_feeder_species.txt` (62 plausible MV species), `models/species_ranges.json`.

**Implications:** the **core class set is the top ~15 species** (≥20 clean labels, thousands of weak labels each). The tail (<20 reviews) folds into "unknown/other" until more reviews accumulate. The **OOD negative set is real and free**: 339 trash (non-bird) + 388 wrong (mis-IDs).

## 3. Eval harness (already exists — reuse)

`tier2_eval/` (`split.py`, `metrics.py`, `baseline.py`, `baseline.report.json`) already computes top-1, macro-F1, **ECE**, per-class recall/precision, and a confusion matrix on the reviews hold-out. The flagship must **beat the baseline on this harness**: accuracy ↑, **ECE ↓ (target TBD by David)**, per-species recall floor (TBD), and a new **OOD AUROC** metric (the harness needs an OOD test split built from the 339 trash + held-out untrained species).

## 4. Recommended approach (from the 2026-06-29 Hailo-targeted research synthesis)

One small INT8-friendly CNN emitting **raw logits + a penultimate feature vector**, with all calibration/OOD done **post-hoc on the Pi CPU**, trained weak-then-clean, shipped only after re-measuring on the **quantized** model. (Aligns with the prior `docs/historical/specs/2026-04-23-tier2-training-plan-v1.md`, which already chose EfficientNet-Lite0 — this dossier updates pretraining + adds the calibration/OOD/Hailo specifics.)

**Architecture:** **EfficientNet-Lite0**, 224×224×3, **ReLU6 + no squeeze-and-excitation (HARD requirement** — swish/SE backbones collapse ~75%→46% under INT8 PTQ; Lite0 loses <1pp). Head emits **logits only** (softmax on CPU) + a feature tap for Mahalanobis. On the Hailo-8L zoo Lite0 is ~1057 FPS / 0.78 GOPs (sub-ms) — co-resides with the YOLOv8s detector on the shared VDevice for almost nothing. Reject MobileNetV3 (hard-swish+SE), stock EfficientNet/V2 (SiLU+SE), ViT/hybrids (overkill). Stay at 224 (the detector hands a tight crop — square-pad it, don't distort). Runner-up: RegNetX-800MF; fallback MobileNetV2-1.0.

**Pretraining CHANGE:** switch **ImageNet → iNaturalist-2021 (iNat-Aves)**. The historical ImageNet choice was justified only by Coral/EdgeTPU compile friction, **now obsolete on Hailo (compiles from ONNX)**. iNat pretraining is the single biggest fine-grained-bird lever (~+20pp on CUB-class tasks). Source a Lite0/MobileNet iNat checkpoint via `timm`, else intermediate iNat-Aves fine-tune first.

**Data/training — strict two-stage (clean reviews quarantined):**
- *Stage 1:* warm-start the backbone on the ~246k **weak AIY labels** (noisy pretraining signal ONLY, never the decision boundary), first pruned via confidence-trajectory / Confident-Learning (drop persistently-erratic-loss; keep hard high-but-decreasing; visually spot-check 50+ before deleting — `feedback_verify_data_first`). Natural-frequency sampling. Optionally distill the logged AIY top-3 softmax via KL.
- *Stage 2:* freeze most of the backbone, fine-tune the head (+ optional last 1–2 blocks) on **only the 1,353+138 clean reviews**; class-balanced sampling; **post-hoc logit adjustment** (Menon 2021) as the primary imbalance fix (directly attacks the measured Blue Jay 0.39 / Hairy 0.43 / Cardinal 0.62 precision collapse; swap the live visitor prior per season). SnapMix (NOT CutMix — it deletes the diagnostic eye-ring/wing-bar), MixUp α0.2, light RandAugment, RandomErasing p≤0.25, label-smoothing 0.1, AdamW wd0.05, cosine, EMA.
- *Splits:* visit-grouped StratifiedGroupKFold **+ a chronological hold-out** (random splits inflate accuracy 5–20%; thresholds tuned in June mis-fire in December).

**Calibration + OOD — three decoupled post-hoc jobs (one forward pass, cheap CPU):**
- *Calibration:* temperature scaling fit on clean reviews → ECE 0.163→≤0.05 target; **per-class temperature vector** (long-tailed support).
- *OOD:* **Mahalanobis++** (L2-normalize penultimate features, class-conditional Gaussians, shared covariance) as the primary distance gate (catches near-OOD unseen warblers that softmax forces into a known class); retrain a real `not_a_bird` negative class on `dataset_negatives/` + the 339 trash reviews (current `not_a_bird` recall is 0.0); energy/MSP only as a coarse pre-filter.
- *Decision rule:* lock only if **calibrated_prob ≥ cut1 AND Mahalanobis ≤ cut2**, both read off risk-coverage curves on clean reviews, re-derived per season. (No ensembles/MC-dropout — N passes, wrong for one Hailo.)

**Hailo compile:** train FP32 on an **x86 + NVIDIA-GPU VM** (DFC needs it) → ONNX (opset 11–17) → `hailomz` parse→optimize→compile, `--hw-arch hailo8l`. **CRITICAL: grep the optimize log for "Reducing optimization level to 0 / no available GPU"** — the #1 silent quantization-collapse (would recreate the yard 0/14 disaster). Calibration set = ≥1024 (prefer ≥2000) **real Chilmark feeder crops**, verified, spanning lighting/season/species. Bake normalization (raw uint8), softmax off-chip, `bias_correction` ON. **Fit temperature + Mahalanobis + thresholds on the QUANTIZED outputs, never FP32** (INT8 shifts ECE). Register the HEF in the existing `HailoEngine` (shared VDevice, ROUND_ROBIN, Pattern B); re-run `tools/bench_hailo_multimodel.py`. **Ship gate:** INT8 top-1 within 1.5pp of FP32, no class −5pp recall, FP32↔INT8 logit corr ≥0.98, calib/eval disjoint.

**Drop-in:** replace `pi_classifier.py`'s `raw_score/255` single-threshold path with `(calibrated_prob, mahalanobis_distance)` from the registry; keep the vote-lock as an orthogonal temporal smoother but gate it on calibrated_prob and short-circuit to "unknown" on the distance gate. Re-derive the lock threshold; do NOT carry the AIY-era 0.35 forward.

## Implementation phases (after David approves the decisions below)
1. **Data prep:** consolidate the dual directories (space/underscore), build the cleaned Stage-1 weak set + the quarantined clean hold-out, build the OOD test split (trash + held-out species), extend `tier2_eval` with OOD AUROC.
2. **Train Stage 1 → Stage 2** (cloud GPU VM) → FP32 eval on the harness (beat 68%/ECE 0.16).
3. **Post-hoc calibration + OOD** fit on clean reviews.
4. **Hailo compile** (GPU VM) → re-measure on the quantized HEF → fit calibration/thresholds on quantized outputs.
5. **Shadow-deploy** alongside AIY (3–7 days), compare on live reviews, then cutover via the registry.

## 5. DAVID DECISIONS (each with a research-backed recommended default — approve or adjust)

1. **Class set** — *Rec:* ship the **15 species (≥20 clean reviews) + `not_a_bird` + `unknown`**; let the unknown gate make adding tail species (Grackle/Mockingbird/Robin/Crow/White-throated Sparrow) non-disruptive as their reviews cross ~50. (Alt: add the tail now, accept weaker per-class numbers.)
2. **Metric acceptance targets** — *Rec (your risk call):* per-species recall floor **≥0.85**, ECE **≤0.05**, OOD AUROC **≥0.85** (stretch 0.92), FPR@95TPR **≤0.25**, plus a max wrong-lock rate.
3. **OOD risk posture** — *Rec:* the **two-gate** design (calibrated prob for species + Mahalanobis distance for "is this even known"), over single-threshold or explicit-class-only. The tradeoff is "never hallucinate a known species" vs "don't over-abstain" — your call on which error you tolerate.
4. **Lock/abstain policy** — *Rec:* drop the AIY-era fixed 0.35; set the operating point from the new model's risk-coverage curve (e.g. "a locked species must be right ≥95%"), re-derived per season. **Interim option available now:** an env-gated lower of the *current* lock gate 0.35→~0.30 to surface more labels on the correct-but-under-confident AIY model (some wrong-lock risk) — say the word and I'll add `PIPELINE_LOCK_CONF`.
5. **Training compute & cadence** — *Rec:* train + DFC-compile on a **cloud x86+GPU VM** (the iMac is at load ~22 and the DFC *needs* a GPU to avoid silent quantization collapse); one-shot now, periodic retrain as reviews grow; shadow-deploy 3–7 days before cutover.
6. **iNat pretraining** — *Rec:* approve **iNaturalist-2021 over ImageNet** (the ImageNet choice was justified only by obsolete Coral friction); if no Lite0 iNat checkpoint exists, OK to add the intermediate iNat-Aves fine-tune for the ~20pp gain?

## 6. Non-negotiables carried forward
- Visually verify training data before training (`feedback_verify_data_first` — the prior attempt failed on data, not model).
- Honesty over optimism — real numbers on the hold-out, never "should work."
- Drop-in via the existing classifier interface; degrade gracefully per-inference; never stall the live path.
- Reproducible recipe — "flagship" includes someone else being able to follow it.
