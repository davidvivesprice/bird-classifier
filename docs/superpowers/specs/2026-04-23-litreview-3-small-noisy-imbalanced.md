# Lit Review 3 — Small, Noisy, Imbalanced: A Training Recipe for the Yard Model

**Date:** 2026-04-23
**Scope:** 46-species bird classifier, ~36K crops (13 species ≥ 500), 158× imbalance, ~34K weak AIY labels + 1,673 clean human-verified hold-out, Coral Edge TPU INT8 target, Python 3.9 / pycoral.

This is not a survey. It is a recommendation with citations. Where numbers exist, they are quoted inline so you can sanity-check the delta versus your last attempt (weight-imprinted MobileNet-V1: 0/14 then 31%).

---

## TL;DR — The Recipe

1. **Treat the 1,673 clean labels as a gold set, never train on them directly** until a final post-hoc classifier-only refit (step 7). They are your calibration + evaluation anchor.
2. **Clean the 34K weak pool first** with Confident Learning (cleanlab) using an ensemble of 3 cross-validated models trained on a bootstrap of AIY-labeled data; prune the bottom ~15–25% by self-confidence. Do NOT trust AIY's label where its own confidence is low OR where pruning flags it.
3. **Base model:** start from an ImageNet-pretrained EfficientNet-Lite0 or MobileNetV2 (both Edge-TPU friendly after QAT). Do NOT start domain-specific — iNat pretraining is tempting but most public iNat checkpoints are not Edge-TPU-compilable without surgery, and the gain is smaller than fixing label noise.
4. **Decouple representation from classifier (Kang 2020):** train the backbone with instance-balanced sampling (natural frequency) + standard cross-entropy + label smoothing 0.1 + MixUp α=0.2. Then freeze the backbone and re-train only the classifier head with class-balanced sampling (cRT). This is the single highest-leverage change over your previous pipeline.
5. **Loss during head re-training:** logit-adjusted softmax (Menon 2021) with the adjustment computed from the *cleaned* training frequencies — NOT focal loss, NOT naive class weights. Focal loss is worse than logit adjustment on long-tailed benchmarks and is famously sensitive to γ. Keep it as a fallback only.
6. **Semi-supervised exploit of remaining unlabeled / low-confidence pool:** one round of Noisy Student self-training — teacher generates pseudo-labels on the *weak* pool, student trained on {clean+cleaned-weak} ∪ {high-confidence pseudo-labeled}. One iteration. Not FixMatch (FixMatch assumes truly unlabeled data and is operationally heavier; your pool has useful priors from AIY).
7. **Final step:** with the decoupled model's features frozen, refit the classifier head on cleaned-weak + a *held-in* subset of the 1,673 (e.g. 80/20 inner split, see §6). Final eval on the untouched 1,673 hold-out. Never let the hold-out gradient-leak.
8. **QAT at the end, not the beginning.** Train in float, fine-tune with QAT for ~5 epochs, then `edgetpu_compiler`.

The two changes that matter most, in order: **(a) cleaning the 34K labels with confident learning before any training**, and **(b) decoupling representation learning from classifier learning**. Everything else is a rounding-error improvement on those two.

---

## 1. Imbalance — what to actually use

**Don't use class-weighted cross-entropy naively** on 158× imbalance from epoch 1. It destabilizes representation learning (Cao 2019, Kang 2020). Two approaches dominate the 2020–2024 literature:

### LDAM + Deferred Re-Weighting (Cao et al., NeurIPS 2019)
Label-distribution-aware margins (larger margin for rare classes) + standard CE for the first K epochs, then class-reweight (∝ 1/n_j) for the last epochs. On CIFAR-10-LT imbalance=100, LDAM-DRW hits 77.0% vs. 70.4% for vanilla CE. It is widely reproduced. Reasonable choice if you want one-stage training.

### Decoupling (Kang et al., ICLR 2020) — recommended
Kang's finding is the one to internalize: **data imbalance does not hurt representation learning; it only hurts the classifier.** Train the backbone with ordinary instance-balanced sampling, then freeze and retrain *only* the classifier with class-balanced sampling (cRT) or learnable weight scaling (LWS). On iNaturalist 2018 (the most yard-bird-like benchmark at 8,142 classes, heavy tail), cRT matched or beat carefully tuned one-stage methods. cRT is ~2 lines of code on top of a standard classifier. Use it.

### Logit Adjustment (Menon et al., ICLR 2021) — recommended for the head loss
Instead of re-weighting, *shift* logits by log(π_y) where π_y is class prior. Fisher-consistent for balanced error, unlike re-weighting / margin tricks. Menon reports an 8% relative reduction in balanced error on CIFAR-10-LT over weight normalization. In-loss version beats post-hoc. Pair with cRT: use logit-adjusted softmax as the loss during head re-training.

### What about Class-Balanced Loss / Effective Number (Cui 2019)?
CB-loss with β=0.9999 + focal on iNaturalist 2018 hit 36.12% top-1 error vs. 42.57% softmax baseline (ResNet-101). It works, but its improvements are mostly subsumed by decoupling + logit adjustment and it adds a hyperparameter. Skip unless you can't afford two-stage training.

### Focal Loss (Lin et al., 2017) — de-emphasized
Focal loss (α=0.25, γ=2.0 canonical) was designed for *dense object detection*, not classification with severe long tails. It under-weights the already-rare tail after balancing. Literature consistently finds it inferior to logit adjustment / LDAM on long-tail classification. Use only as a fallback baseline.

---

## 2. Label noise — your AIY labels are ~20–40% wrong on some classes

Your specific noise profile (squirrel → Rock Pigeon, empty frame → Flicker, Chickadee → Titmouse) is **instance-dependent, asymmetric**, class-conditional noise. That is the hardest kind. Symmetric-noise-tuned methods (vanilla label smoothing, bootstrap) will underperform.

### Step A — prune before training (Confident Learning, Northcutt 2021 / cleanlab)
Required. Pipeline:
1. Train 3 models on 3 cross-validated folds of the AIY-labeled 34K (small EfficientNet-Lite0, 10 epochs, no fancy tricks).
2. Get out-of-fold predicted probabilities P(ĉ|x).
3. Run cleanlab `find_label_issues` with `method="prune_by_noise_rate"`.
4. Drop flagged examples. Expected pruning rate for weak-supervision pipelines: 10–30% (Northcutt et al. 2021).

This is the highest-ROI single step in the whole recipe and directly addresses what broke your last attempt — 31% "everything is Goldfinch" is the signature of a model memorizing a dominant wrong mode in contaminated data. Confident learning removes that mode.

### Step B — robust training on the cleaned pool
The classic method families:

- **Co-teaching / Co-teaching+ (Han 2018; Yu 2019):** two networks exchange small-loss examples. Robust to symmetric noise up to 50%. Slower, 2× compute.
- **DivideMix (Li et al., ICLR 2020):** GMM on per-sample loss → clean/noisy split → semi-sup on the noisy half. State-of-the-art on CIFAR-N, ~94% on CIFAR-10 at 50% symmetric noise vs. ~62% for vanilla CE. Non-trivial to implement (two networks, MixMatch inner loop). Use only if post-cleanlab accuracy is still poor.
- **Label smoothing ε=0.1 (Lukasik et al., ICML 2020):** cheap, competitive with loss-correction under noise. Keep it on by default.
- **MixUp α=0.2 (Zhang 2018):** reduces CE under noise by 6.5–12.5% on CIFAR-10 (Zhang et al. 2018). Makes memorization of wrong labels harder. Keep it on.

### Recommended recipe for YOUR noise profile
Prune with cleanlab → train with CE + label smoothing 0.1 + MixUp α=0.2. Do NOT start with DivideMix. DivideMix shines when you have *no* clean anchors; you have 1,673. If your balanced accuracy on the clean hold-out after step 7 is still <60% on the tail, escalate to DivideMix.

---

## 3. Semi-supervised use of the weak pool — Noisy Student > FixMatch here

FixMatch (Sohn 2020) assumes genuinely unlabeled data and thresholds pseudo-labels at τ=0.95. It doesn't exploit your AIY prior. For class-imbalanced SSL, CVPR-2024 "Distribution-Aware ML-FixMatch" boosts pseudo-label accuracy 40% → 90% — but that requires custom distribution alignment and is overkill here.

**Noisy Student (Xie et al., CVPR 2020)** is the right fit:
1. Train teacher on cleaned labeled set.
2. Generate pseudo-labels on the weak pool (keep examples where teacher confidence > 0.7 AND teacher top-1 matches AIY's top-1 — a two-witness rule that exploits your AIY prior).
3. Train student on {labeled} ∪ {pseudo-labeled} with noise (dropout, MixUp, augmentation) on the student side.

Xie reports 88.4% → from 87.0% on ImageNet; per-iteration gains of 0.5–1.0% were typical. One iteration is almost always enough in practice. **Iterate at most twice.** More iterations compound pseudo-label bias on a long tail.

Mean Teacher (Tarvainen 2017) is reasonable but strictly weaker than Noisy Student on ImageNet-scale benchmarks. Skip.

---

## 4. Transfer learning recipe

**Start from ImageNet.** iNaturalist-pretrained checkpoints exist but: (a) most are too big for Edge TPU without heavy surgery; (b) Kornblith 2019 showed ImageNet features transfer competitively even to fine-grained species tasks when the target dataset is small-to-medium. Your 36K (cleaned: ~28K) is right in that zone.

**Architecture:** MobileNetV2 1.0x (224×224) or EfficientNet-Lite0. Both compile cleanly for Edge TPU via QAT. MobileNet-V1 (your prior base) is fine but V2 has a meaningful accuracy bump (~2-3% ImageNet) at similar latency.

**Freeze schedule (TensorFlow / Keras canonical, per Keras docs):**
- **Stage 1 — head only, 10 epochs.** Freeze backbone entirely, BatchNorm in inference mode. LR = 1e-3 on the head (Adam).
- **Stage 2 — discriminative unfreeze, 30 epochs.** Unfreeze top block(s), keep early blocks frozen. Backbone LR = 1e-5, head LR = 1e-4. Keras explicitly warns: when unfreezing a model with BN layers, pass `training=False` at call time so BN stats don't drift on the small dataset.
- **Stage 3 — cRT classifier re-training, 10 epochs.** Freeze *everything* except the final dense layer; class-balanced sampling; logit-adjusted CE.
- **Stage 4 — QAT, 5 epochs.** Enable fake-quant nodes (`tf.quantization`), LR 1e-5, then convert to TFLite INT8. Coral's docs recommend QAT over PTQ for final accuracy.

Total wall time on a single RTX-class GPU: hours, not days.

---

## 5. Don't touch these

- **Weight imprinting (Qi 2018)** — this was your previous recipe. It is known to be fragile for long tails with noisy labels; the imprint vectors absorb noise directly. Do not return to it.
- **Large re-sampling ratios (e.g. oversample tail 20×)** before representation learning — this is precisely what Kang showed hurts.

---

## 6. Cross-validation that doesn't leak

You correctly identified the problem: naive random split lets two crops from the same VISIT (same bird, 2 seconds apart, same lighting, same background) end up one-in-train-one-in-test. This inflates test accuracy and is why your 31% run on clean data looked "not that bad" internally but was actually random.

**Use `sklearn.model_selection.GroupKFold` (or `StratifiedGroupKFold` for imbalance-aware stratification).** Grouping key, in order of preference:

1. **Visit ID** — contiguous run of detections on one camera within e.g. 5 minutes. If you don't have this field, derive it: group by `(camera_id, floor(timestamp / 300))`, then merge adjacent buckets with the same species.
2. **Camera × day** — weaker but easy. Prevents same-day-same-camera leakage.
3. **Camera × hour** — weakest useful grouping.

The iWildCam convention (Beery et al.): hold out *entire camera locations* for test. Overkill for 2 cameras; the visit-level group is the right granularity for you.

**Stratified Group K-Fold** (scikit-learn ≥1.0) gives you balanced class distribution *across* folds without breaking groups. Use k=5. Report mean ± std macro-F1 and per-class recall on the tail.

The 1,673 human-verified labels should be split by visit too — ensure no visit in the hold-out appears in the 34K weak pool. Check this explicitly; it is the single most common pipeline bug in camera-trap ML (per Beery 2018 iWildCam post-mortems).

---

## 7. What success looks like

Concrete numbers to expect, based on the benchmarks cited:

- Baseline (CE, natural sampling, no cleaning): ~40–50% macro-F1 on tail, ~70% on head. (Consistent with your "31% everything is Goldfinch" failure mode.)
- + cleanlab pruning: +5–10% macro-F1.
- + decoupling / cRT: +3–6% balanced accuracy (Kang 2020 reports this range on iNat).
- + logit adjustment: +2–4% balanced error reduction (Menon 2021).
- + Noisy Student (one round): +1–2%.

Cumulative: a well-executed version of this recipe should put you at 65–75% macro-F1 on the human-verified hold-out, with tail-class recall ≥50% — versus 0% on your last run. Do not promise the user more.

---

## References

Cao et al. 2019. *Learning Imbalanced Datasets with LDAM Loss.* NeurIPS. (https://arxiv.org/abs/1906.07413)
Cui et al. 2019. *Class-Balanced Loss Based on Effective Number of Samples.* CVPR. (https://arxiv.org/abs/1901.05555)
Han et al. 2018. *Co-teaching.* NeurIPS.
Kang et al. 2020. *Decoupling Representation and Classifier for Long-Tailed Recognition.* ICLR. (https://arxiv.org/abs/1910.09217)
Kornblith et al. 2019. *Do Better ImageNet Models Transfer Better?* CVPR.
Li et al. 2020. *DivideMix.* ICLR. (https://arxiv.org/abs/2002.07394)
Lin et al. 2017. *Focal Loss for Dense Object Detection.* ICCV.
Lukasik et al. 2020. *Does Label Smoothing Mitigate Label Noise?* ICML. (https://arxiv.org/abs/2003.02819)
Menon et al. 2021. *Long-Tail Learning via Logit Adjustment.* ICLR. (https://arxiv.org/abs/2007.07314)
Northcutt et al. 2021. *Confident Learning.* JAIR. cleanlab library: https://github.com/cleanlab/cleanlab
Qi et al. 2018. *Low-Shot Learning with Imprinted Weights.* CVPR.
Ren et al. 2020. *Balanced Meta-Softmax.* NeurIPS.
Sohn et al. 2020. *FixMatch.* NeurIPS. (https://arxiv.org/abs/2001.07685)
Xie et al. 2020. *Self-Training with Noisy Student.* CVPR. (https://arxiv.org/abs/1911.04252)
Zhang et al. 2018. *mixup: Beyond Empirical Risk Minimization.* ICLR.

Coral Edge TPU QAT guide: https://www.coral.ai/docs/edgetpu/retrain-classification/
sklearn StratifiedGroupKFold: https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.StratifiedGroupKFold.html

---

**Word count:** ~1,470.
