# Tier 2 Yard Model Revamp — Training Plan v1 (synthesis)

**Date:** 2026-04-23
**Status:** Superseded in part — see caveat below. Architecture and augmentation recipe still valid; label-sourcing strategy replaced by calibration findings.

> **2026-04-26 calibration update:** 263 human verdicts across 4 RC3 strata showed AIY's softmax is 85% correct when it disagrees with the yard model (bucket B), and agree+low-confidence rows (bucket C) are 85% precise — disproving the confidence-floor hypothesis. The "clean the 34K weak AIY labels with Confident Learning" step (Part B §2) remains valid but should use the `training_label` field from RC2 metadata once that ships, not raw AIY labels. The base architecture should use **AIY distillation** (train EfficientNet-Lite0 to mimic AIY's 965-class softmax on our images) rather than fine-tuning from ImageNet; the ImageNet starting checkpoint is a weaker prior than AIY's bird-specific knowledge. RC2 plan: `docs/superpowers/plans/2026-04-26-rc2-from-calibration.md`.

**Inputs:**
- `2026-04-23-tier2-data-audit.md` — what the data actually looks like
- `2026-04-23-litreview-1-bird-classifiers.md` — architecture + augmentation
- `2026-04-23-litreview-2-calibration-ood.md` — uncertainty + OOD
- `2026-04-23-litreview-3-small-noisy-imbalanced.md` — training on noisy long-tail
- `2026-04-23-litreview-4-quantization-deployment.md` — Coral + Hailo deployment

Where the reviews disagree, I've chosen the path with the most evidence behind it for **our specific situation** (158× imbalance, class-conditional label noise, INT8 deployment, 1,673 clean labels, Hairy-vs-Downy as the dominant confusion pair).

---

## Part A: The architecture decision

**Backbone: EfficientNet-Lite0 at 224×224 input.**

Unanimous across three reviews. Rationale:
- MobileNetV3 / EfficientNet-B0 use hard-swish + SE blocks that collapse under INT8 PTQ (77.4% → 33.9% top-1 measured). Lite drops these for INT8-friendly ReLU6.
- Coral Edge TPU compiles Lite cleanly with 100% ops on TPU (verified by community reports; must re-verify with `edgetpu_compiler -s` before shipping).
- Hailo 8L compiles Lite from ONNX equally cleanly.
- MobileViT / FastViT / RepViT are tempting but Coral punts attention ops to CPU — 10× latency penalty.
- MobileNetV2 is the runner-up if Lite0 has any compile issue. Comparable accuracy, slightly bigger.

**Input size: 224×224.** Sweet spot for Edge TPU memory footprint + detail. 160×160 would be faster but loses the eye-visibility feature that matters for Hairy/Downy.

**Output: 16 classes.** 14 species (the 13 with ≥500 files plus Hairy Woodpecker which has 236 files but strong review coverage and is half of the dominant confusion pair), plus `not_a_bird` explicit class, plus `unknown` explicit class. Rationale in Part C.

---

## Part B: The training recipe

This is the hand-waving-free version. Steps are in execution order.

### Step 1: Build visit-grouped splits (BEFORE anything else)

**Pipeline bug risk: temporal leakage.** Two crops from the same 5-minute visit (same bird, same lighting, same bbox region) must not straddle train/test. This is camera-trap ML's most common failure (iWildCam post-mortems).

Implementation: add a `visit_id` column to the classifications query, or derive it: `group by (camera, floor(timestamp/300))` with adjacent-bucket merging. Use `StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42)` from sklearn ≥1.0.

**Hard constraint:** No visit in the 1,673 hold-out may appear in any training fold. Check this *explicitly* before each training run — write an assert, not a comment.

### Step 2: Clean the 34K weak AIY labels with Confident Learning

**This is THE fix for the "31% everything is Goldfinch" failure mode.** The agents agree this is the single highest-ROI step.

Recipe (Northcutt 2021 / cleanlab):
1. Split the 34K AIY-labeled pool into 3 cross-validation folds.
2. Train 3 EfficientNet-Lite0 models (10 epochs each, vanilla CE, no fancy tricks) — each on 2 folds, predicting on the held-out fold. Get out-of-fold probability estimates `P(ĉ|x)`.
3. Run `cleanlab.filter.find_label_issues(labels, pred_probs, method="prune_by_noise_rate")`.
4. Drop flagged examples from the training pool. Expected prune rate: 10–30%.

Save the pruned-in and pruned-out lists. Commit them. Treat the pruned pool as the new training substrate.

### Step 3: Add explicit OOD / non-bird training data

From the data audit:
- 374 confirmed non-bird samples (289 `trash` verdicts + 85 `wrong → not_a_bird`)
- 22,586 culled-hallucination JPGs in quarantine — many are empty-feeder crops labeled as birds. Sample 1,000 and hand-verify a subset (or use an AIY-low-confidence heuristic) to build a second non-bird set.

Label all of these `not_a_bird`. They join the training pool as a 15th class.

### Step 4: Stage-1 training — backbone (instance-balanced sampling)

Per Kang 2020 (Decoupling): imbalance hurts the classifier, not the representation. Train the backbone with natural frequency sampling (Mourning Dove gets 7,125 slots, Common Grackle gets 45).

Recipe (combining reviews 1 and 3):
- **Start from ImageNet** (NOT iNat — public iNat checkpoints are hard to Edge-TPU-compile, and Kornblith 2019 shows ImageNet transfers competitively on fine-grained ≤36K datasets).
- **Freeze schedule:**
  - Epoch 0–9: backbone frozen, train classifier head only. LR 1e-3 Adam.
  - Epoch 10–39: unfreeze top blocks, discriminative LR (backbone 1e-5, head 1e-4). BatchNorm in `training=False` mode (critical per Keras docs — small dataset = stats drift).
- **Augmentation (timm A2 recipe):** RandAugment (N=2, M=9), MixUp α=0.2, CutMix α=1.0, RandomErasing p=0.25. Mixup mitigates memorization of any residual label noise.
- **Loss:** vanilla cross-entropy + label smoothing ε=0.1. Class weighting OFF in this stage.
- **Optimizer:** AdamW, weight decay 0.05, cosine schedule, EMA of weights.
- **Auxiliary loss:** Pairwise Confusion (Dubey ECCV 2018) + Center Loss, weighted 0.1. Pushes feature embeddings of confused-pair species apart. One-line addition. Cited +1–3% on lookalike pairs.

### Step 5: Stage-2 training — classifier head (class-balanced sampling, logit-adjusted CE)

Freeze backbone. Re-train the final dense layer only.

- **Sampling:** class-balanced — each epoch, sample equally from each class (so Common Grackle's 45 examples appear 158× more often per epoch than in the natural distribution).
- **Loss:** logit-adjusted softmax (Menon 2021). During training, subtract `τ * log(π_y)` from the logit of class y, where π_y is the class's natural frequency and τ=1.0. This is Fisher-consistent for balanced error. Not focal loss — review 3 is explicit that focal underperforms here.
- **Epochs:** 10. LR 1e-3 on the head only.

**Conflict resolution:** Review 1 suggested focal loss + DRW. Review 3 argued logit adjustment. Decision: **logit adjustment** (Review 3's argument is more specific to our 158× long-tail setting; focal was designed for detection). Fall back to LDAM+DRW if macro-F1 plateaus.

### Step 6: Hairy-vs-Downy specialist head (the aux pattern)

A dedicated binary head triggered when the main head's top-2 includes both Hairy Woodpecker AND Downy Woodpecker. The lookalike is responsible for 100 known corrections — the dominant confusion in the dataset.

Implementation: on top of the shared backbone features, add a 2-class linear head trained exclusively on the ~173 confirmed Hairy-or-Downy samples (88 + 69 from the review hold-out + all wrong→Downy corrections). Pass hard-negatives from MixUp'd other-species samples with low weight to teach "neither."

Inference logic: if main head top-1 ∈ {Hairy, Downy} AND top-2 ∈ {Hairy, Downy}, run the binary head and override the main head's decision.

### Step 7: OOD + calibration head

From review 2:
- **During training:** fine-tune the already-trained model for 3 epochs with Outlier Exposure (Hendrycks 2019) using the culled-hallucination pool + not_a_bird examples. Loss = CE (on in-distribution) + λ * energy-margin loss (on OE data). Pushes logits lower-energy on OOD.
- **At inference:** compute energy `E(x) = -logsumexp(logits(x))`. Cheaper than any softmax-adjustment method (free, it's already computed).
- **Gate logic:** three zones
  - `E(x) < τ_confident` → return top-1 species with confidence
  - `τ_confident ≤ E(x) < τ_unknown` → return top-1 with `"uncertain"` flag (UI shows it muted)
  - `E(x) ≥ τ_unknown` → return `"unknown"` — never hallucinates a seen species
- **Calibrate post-hoc:** fit temperature T on the **quantized** model's logits (not FP32!) against the clean hold-out. INT8 shifts ECE by up to +49% — calibrating on FP32 is a known trap.
- **Fallback OOD gate:** compute class-wise means on the penultimate features during training. At inference, Mahalanobis distance from test point to nearest class mean. One matmul (~0.3 ms on Pi CPU). Use as a second gate that fires if energy alone is ambiguous.

### Step 8: Noisy Student iteration (one round)

After Stage 2 finishes:
1. Use the trained model as teacher.
2. Predict on the *original* 34K AIY pool (not the cleaned one — we want teacher to re-decide).
3. Keep examples where teacher confidence > 0.7 AND teacher top-1 matches AIY's top-1 (two-witness rule).
4. Add these to the training set.
5. Retrain student from scratch with the augmented set.

One iteration only. More iterations compound pseudo-label bias on long tails.

### Step 9: Quantization-aware training (final polish)

- PTQ first. If gap vs FP32 is ≤1.5% top-1 AND no class loses >5 pp recall, ship PTQ.
- Otherwise QAT: 5 epochs, LR 1e-5, fake-quant nodes enabled. Re-compile.
- Calibration set: 500 stratified samples from the 1,673 hold-out. **Do NOT calibrate on the same split you evaluate on.** Use folds-specific calibration/eval pairs.
- For Hailo: 1,500 samples minimum (DFC prefers 2,000+).

---

## Part C: The label-set decision

**Recommendation: 14 species + `not_a_bird` + `unknown` = 16-class output.**

| Class | Source | Rationale |
|---|---|---|
| Black-capped Chickadee | data audit top | 4,258 files, 91 reviews |
| House Finch | " | 5,704 files, 80 reviews |
| Northern Cardinal | " | 1,626 files, 65 reviews |
| Dark-eyed Junco | " | 3,047 files, 72 reviews |
| Mourning Dove | " | 7,125 files, 61 reviews |
| Song Sparrow | " | 4,068 files, 85 reviews |
| Downy Woodpecker | " | 1,649 files, 69 reviews + 100 "wrong" corrections |
| Hairy Woodpecker | " | 236 files, 88 reviews *— borderline on file count but REQUIRED for Hairy/Downy specialist* |
| Tufted Titmouse | " | 1,422 files, 60 reviews |
| White-breasted Nuthatch | " | 771 files, 70 reviews |
| American Goldfinch | " | 252 files, 83 reviews *— borderline file count, reviews are strong* |
| Carolina Wren | " | 271 files, 87 reviews *— borderline, high review count* |
| Blue Jay | " | 101 files, 49 reviews *— borderline, keep for coverage* |
| Brown-headed Cowbird | " | 550 files, 67 reviews |
| `not_a_bird` | non-bird training pool | 374 confirmed + sampled culled JPGs |
| `unknown` | energy gate, not trained | OOD threshold at inference |

Some of these (Hairy Woodpecker, American Goldfinch, Carolina Wren, Blue Jay) have <500 files but have strong review coverage. The reviews become training AFTER being replicated in a second augmentation pass (AugMix + RandomErasing + MixUp partners from higher-volume species) to bulk up the class effective size.

**Explicitly excluded from the closed set:** White-throated Sparrow (809 files, only 1 correct review), Northern Mockingbird (669 files, ~0 correct reviews), House Sparrow, Red-winged Blackbird (15 reviews — borderline, revisit after first iteration). These land in the `unknown` class until reviews accumulate. The flagship is narrower than the 46 species in `classified/`, because trying to train on all 46 with the current imbalance guarantees long-tail failure.

**Growth plan:** when any excluded species accumulates 50+ `correct` reviews, re-train with that species as a new explicit class. The `unknown` gate makes adding classes non-disruptive.

---

## Part D: Deployment

### Coral Edge TPU path (current production target)

1. Train FP32 (TF/Keras) → `saved_model/`
2. PTQ to INT8 with 500-sample calibration set → `model.tflite`
3. Validate: `edgetpu_compiler -s` must show 100% ops-on-TPU. **If any op partitions to CPU, STOP** — the resulting model runs an order of magnitude slower. Swap the op or pick a different backbone.
4. Benchmark: `edgetpu_compiler --benchmark` target ≤5 ms on 224×224 input.
5. Drop-in replace `~/bird-classifier/models/yard_model.tflite`.

### Hailo 8L path (future, Pi 5 deployment)

1. Export trained model to ONNX
2. `hailo compiler` with 1,500-sample calibration set → `model.hef`
3. Validate: Hailo fails fast on unsupported ops (unlike Coral's silent partition). Cleaner failure mode.
4. **Critical:** Hailo 8L and Hailo 8 produce different HEFs. Re-compile per target; CI should build both.

### Seven-test validation gate (before shipping)

All must pass:
1. Hold-out top-1 accuracy ≥ FP32 - 1.5%
2. Per-class recall drop ≤ 5 pp on every class
3. Confusion matrix diff: no new off-diagonal > 10 samples
4. Logit correlation between FP32 and INT8 model ≥ 0.98 on eval set
5. Calibration set and eval set have ZERO overlap (hard-check)
6. p95 inference latency ≤ 5 ms on target hardware
7. Preprocessing canary: 10 committed reference images produce identical tensors in FP32 and INT8 paths (catches drift in preprocessing code)

---

## Part E: Success criteria (numeric, verifiable)

From the reviews' numeric guidance and David's framing:

| Metric | Target | Stretch | Rationale |
|---|---|---|---|
| Macro-F1 on 1,673 hold-out | ≥ 65% | ≥ 75% | Review 3 projects 65-75% for well-executed recipe on this kind of dataset |
| Per-class recall floor (14 species) | ≥ 50% | ≥ 70% | Menon 2021 + Kang 2020 projections |
| Hairy/Downy disambiguation F1 | ≥ 85% | ≥ 92% | Specialist head target |
| ECE on hold-out (after T-scaling) | ≤ 5% | ≤ 3% | Review 2 benchmark |
| OOD AUROC (bird → not-a-bird) | ≥ 0.85 | ≥ 0.92 | Review 2 energy-method projection |
| FPR at 95% TPR on OOD | ≤ 25% | ≤ 15% | Review 2 benchmark |
| Inference p95 (Coral) | ≤ 5 ms | ≤ 3 ms | Current budget |
| Inference p99 (Coral) | ≤ 8 ms | ≤ 5 ms | Tail budget for 5 fps × 3-bird peak |

**What we will NOT promise David:** "feels like magic." Model quality must be measured, not vibed.

---

## Part F: Evaluation harness (build BEFORE training)

Per the brief's step 4: "Spec the evaluation harness BEFORE training anything."

Deliverables (Python package under `~/bird-classifier/tier2_eval/`):
1. `split.py` — visit-grouped StratifiedGroupKFold. Asserts no visit leakage.
2. `metrics.py` — macro-F1, per-class recall/precision, ECE, OOD AUROC, FPR95, confusion matrix, class-balanced accuracy. All quoted with 95% CIs via bootstrap.
3. `calibration.py` — temperature-scaling fit + reliability diagrams.
4. `ood.py` — energy and Mahalanobis scorers, threshold tuning.
5. `dashboard.py` — live-updating HTML report that compares two models side-by-side. Generates on-demand, no server needed.
6. `canary_preprocess.py` — the 10-image ref set, regression gate.
7. `test_no_leakage.py` — CI check that hold-out is never in train.

All seven ship with pytest coverage. `tier2_eval/` has zero production dependencies; it's pure analytics.

---

## Part G: Phase plan

| Phase | Duration | Deliverable |
|---|---|---|
| **0: Evaluation harness** | 2-3 days | `tier2_eval/` package, tests green, can score the current yard model as a baseline |
| **1: Data cleaning** | 1-2 days | Cleanlab pass on 34K weak labels, pruned lists committed, data audit of the cleaned pool |
| **2: Stage-1 training (backbone)** | 1 day wall-clock | FP32 backbone checkpoint, baseline metrics on 1,673 hold-out |
| **3: Stage-2 (decouple + head)** | 1 day | FP32 final model, full metrics report |
| **4: Specialist head** | 1 day | Hairy/Downy binary head, combined-model metrics |
| **5: OOD / calibration** | 1 day | Energy-gated model, ECE + AUROC hitting targets |
| **6: Quantization + deployment** | 1-2 days | `yard_model_v2.tflite` compiled, 7-test validation passed |
| **7: Shadow deployment** | 3-7 days | Side-by-side with current yard, disagreement log reviewed by David |
| **8: Cutover** | 1 day | Flip `SmartClassifier._run_yard` to v2, update `yard_model_labels.txt` |

Total: ~2 weeks active, ~3-4 weeks calendar with reviews. Each phase independently verifiable.

---

## Part H: What I'm uncertain about

Honest list:
- **Exact ECE target after INT8 quantization** on OUR data — the review 2 number (+49% ECE shift) is cifar-scale, might be different for us. Need to measure and adjust target.
- **Whether Hairy Woodpecker has enough samples** (236 files + 88 reviews + 100 wrong→Downy corrections = ~424 effective samples after augmentation). If specialist head can't hit 85% F1, fall back to merging Hairy/Downy into a single "Woodpecker-small" class for the main head. Honest failure mode.
- **Whether Pairwise Confusion aux loss actually helps HERE** — cited gains are on CUB-200 and Stanford Dogs. Our camera is narrower. Measure before keeping.
- **Whether Noisy Student iteration is worth the compute** — it's a +1-2% gain. If Stage-2 already hits 65% macro-F1, skip it.

These get resolved by measurement, not argument. The harness exists to surface them.

---

## Part I: What I need from David before I start

1. **Label-set sign-off.** The 16-class decision in Part C — can modify.
2. **Hairy/Downy fallback approval.** If specialist head fails, OK to merge into "Woodpecker-small"?
3. **Shadow-mode duration.** 3 days? 7?
4. **Anyone else on this?** Or me solo.
5. **Hardware.** Do we train on the iMac (slow, and it's already at load 22), or bring up a separate training machine / cloud? The data fits in RAM; a single 10GB GPU is enough. Maybe a Colab session with the data rsync'd.
6. **Species not in top 13 but with high review counts** (White-throated Sparrow 1 review but 809 files; Song Sparrow 85 reviews but skipped) — re-check my cut, add any you want.

Answer any; not blocking — I can start Phase 0 (evaluation harness) with zero approvals needed.
