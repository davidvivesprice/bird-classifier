> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Lit Review 2: Calibration & OOD for Edge-TPU Bird Classifier

**Date:** 2026-04-23
**Scope:** State of the art in confidence calibration and out-of-distribution detection for image classifiers, filtered by our constraint: INT8 TFLite on Coral Edge TPU, ≤5 ms per 224×224 crop, pycoral + tflite_runtime.
**Goal:** Pick one main method + one fallback that solves our three failure modes — (a) confidently wrong, (b) bird-hallucinated empty frames, (c) unseen species force-mapped into 12 known classes.

---

## 1. Calibration

### 1.1 How we measure it

- **ECE (Expected Calibration Error)** — bin predictions by confidence, compare mean confidence to mean accuracy per bin, take a weighted L1 average. Standard: 15 equal-width bins. Guo et al. 2017 popularized this as *the* calibration metric.
- **MCE** — max gap across bins; worst-case calibration. Useful when one high-confidence bin is catastrophically wrong (our failure mode (a)).
- **Reliability diagrams** — confidence vs accuracy scatter/bars; the diagonal is perfect calibration.
- **Adaptive ECE** — equal-*count* bins. More stable when most predictions pile up at high confidence, as they will for our model. We should report this too.
- **Proper scoring rules** — NLL, Brier. Complement ECE (which is binned and can be gamed).

Compute ECE on the 1,673-sample hold-out. Report per-class reliability — aggregate ECE hides species-specific miscalibration.

### 1.2 Methods ranked for our constraints

**Temperature Scaling** (Guo et al. 2017). Divide logits by a single scalar `T` learned on a held-out set by minimizing NLL. One parameter, no retraining, preserves argmax. On CIFAR-100/ResNet it drops ECE from ~16% to <2% in the original paper. **Edge TPU implication:** learn `T` in FP32 against the *dequantized INT8 logits* from the on-device model, then either (i) bake `1/T` into the final dense layer weights before re-quantizing, or (ii) apply division post-pycoral in Python (free — it's one scalar multiply on 12 numbers). Option (ii) is what we want: keeps the TFLite artifact unchanged so re-training doesn't re-invalidate the calibration.

**Focal loss as calibrator** (Mukhoti et al., NeurIPS 2020). Training-time: replace CE with focal loss (`γ ≈ 3`, or their sample-adaptive schedule). Focal loss implicitly regularizes predicted entropy, so the model comes out *already* better-calibrated — they show SOTA ECE when focal is combined with post-hoc TS. **Bonus for us:** the same paper shows focal-trained models detect OOD better than CE+TS models. This is a training-time change, so it costs us one retrain, but it's free at inference and quantizes fine (the loss is gone by inference).

**Label smoothing** (ε = 0.05–0.1). Cheap, helps ECE modestly, but Müller et al. 2019 showed it can *hurt* distillation and can over-smooth — and can actively hurt OOD detection by flattening logits. Skip unless focal loss doesn't work out.

**Dirichlet calibration** (Kull et al., NeurIPS 2019). Learns a full `K×K` linear map on log-probs. More expressive than TS, can fix class-specific miscalibration — relevant for us since our failure is concentrated on a few species. Cost at inference: one `12×12` matmul. Trivial. Requires more hold-out data than TS (~1k+ for K=12 is fine; our 1,673 works). Worth evaluating as a TS upgrade.

**Histogram binning / Isotonic regression.** Non-parametric, per-class. Isotonic is expressive but data-hungry and can break argmax; histogram binning needs careful bin counts. Guo 2017 found both underperform TS on deep nets. Skip.

**Quantization and calibration interact.** Recent work (ACL 2025, arxiv 2509.21173) shows INT8 quantization can *change* ECE meaningfully — sometimes better (stochastic regularization), sometimes +49% on noisy data. **Takeaway:** fit `T` on the *quantized, on-device* model's outputs, not the FP32 checkpoint. Otherwise the calibration map is for the wrong model.

### 1.3 Calibration recommendation

Train with **focal loss (γ=3)**, then fit **temperature scaling** on the quantized model using the 1,673 hold-out. This is the Mukhoti combo, it's cheap, it stays accurate, and it's well-supported empirically.

---

## 2. OOD detection

OOD is the bigger lever for us. Failure modes (b) and (c) are OOD problems, not calibration problems — a well-calibrated in-distribution classifier still hallucinates on an empty feeder, because every softmax must sum to 1.

### 2.1 Methods ranked by Edge-TPU compatibility

**MSP — max softmax probability** (Hendrycks & Gimpel 2017). The baseline. Free. Works poorly because neural nets are overconfident; it's precisely failure mode (a). Keep as a reference.

**ODIN** (Liang et al. 2018). Temperature scaling + input perturbation via FGSM-style gradient. The input perturbation requires an FP32 backward pass — **incompatible with pycoral inference**. Drop.

**Energy score** (Liu et al., NeurIPS 2020). `E(x) = -T · logsumexp(logits/T)`. A single `logsumexp` over 12 logits — free at inference, operates on the same logits we already have. The paper reports energy reduces FPR95 by 18.0% on CIFAR-10 and 10.6% on CIFAR-100 vs MSP on a WideResNet; ~+5–10 AUROC points over MSP on most benchmarks. Even better: **"energy fine-tuning"** adds a margin loss at training time that pushes ID energies down and OOD (outlier-exposure) energies up; on CIFAR-10 this drops FPR95 another 5.2 pp. This is the sweet spot for our training budget.

**Outlier Exposure** (Hendrycks et al., ICLR 2019). Train with an auxiliary "not-the-task" dataset and a loss that pushes OOD outputs toward uniform (or, combined with energy, pushes their energy up). Orthogonal to score choice — you can use OE with MSP, energy, or anything. **We already have the data**: 374 confirmed non-birds + 22K culled JPGs as hard outliers, plus any empty-feeder frame grab we want. This is the single biggest lever available to us.

**ReAct** (Sun et al., NeurIPS 2021). Clip the penultimate activations at a percentile (e.g. the 90th) computed on ID data. Reduces FPR95 by 25.05 pp on ImageNet vs prior SOTA. Stacks with energy scoring. **Edge-TPU:** a `min(x, c)` op is fine in TFLite (it's just ReLU6-shaped), *if* you can insert it in the graph pre-quantization. Doable. Requires access to penultimate features — we have those (it's the last layer before our 12-class head).

**DICE** (Sun & Li, ECCV 2022). Sparsify the final FC weights by keeping only the top-k by contribution, then energy-score. Stacks with ReAct (ReAct+DICE is ~SOTA on ImageNet). Free at inference. **Edge-TPU:** bake sparsified weights into the final dense layer — trivial, just masks weights before re-quantizing. Strong "free lunch" candidate.

**Mahalanobis** (Lee et al., NeurIPS 2018). Per-class Gaussian in feature space; score is distance to nearest class centroid. Needs FP32 (or carefully quantized) features, per-class means (12 vectors), and a tied covariance inverse. For a 1280-dim MobileNetV2 feature, that's a 1280×1280 inverse-covariance — one matmul per inference. ~1.5 MB if stored FP32, or a sparse/low-rank approximation. Feasible off-TPU in numpy on the Pi; <1 ms for 12 classes with a 1280-d vector. *Relative Mahalanobis* (RMD, Ren et al. 2021) and *Mahalanobis++* (2505.18032) fix the known near-OOD failure mode of vanilla Mahalanobis. On modern backbones RMD is competitive with energy on near-OOD tasks — and it directly attacks failure mode (c): a Tufted Titmouse (unseen) will be far from all 12 centroids even if its top-1 softmax is 0.95.

**KNN-OOD** (Sun et al., ICML 2022). Non-parametric: score = distance to k-th nearest ID training embedding. No Gaussian assumption. On ImageNet, drops FPR95 by 24.77 pp vs Mahalanobis. Requires storing all training embeddings (~50k × 1280 FP16 ≈ 128 MB) and a KNN lookup per inference. With FAISS-CPU on the Pi, 10k samples × 1280-d is sub-millisecond. **Feasible but operationally heavier** than Mahalanobis (embedding store must be updated on every retrain). Worth keeping as a future upgrade.

**OpenMax** (Bendale & Boult 2016). Fits a Weibull to per-class activation distributions. Predates and is dominated by energy/Mahalanobis on modern benchmarks. Skip.

**Deep ensembles / MC-Dropout / Bayesian.** N-way inference. We have a ≤5 ms budget and one accelerator. An ensemble of 5 is 25 ms. **Out of budget.** Mention only because the literature keeps citing them as the SOTA upper bound on uncertainty quality — in a no-budget setting, a 5-model ensemble beats any single-model method. Not us.

### 2.2 What do we need at training time?

| Method | Training-time cost |
|---|---|
| MSP | None |
| Energy (scoring only) | None |
| **Energy + OE fine-tune** | Add 22k outlier mini-batch + energy-margin loss. One retrain. |
| ReAct | None (threshold fit on ID val set post-hoc) |
| DICE | None (sparsify post-hoc) |
| Mahalanobis / RMD | One forward pass over training set to fit class means + covariance |
| KNN-OOD | Store all training embeddings; re-embed on every retrain |
| Focal loss (for calibration) | One retrain |

### 2.3 Cheapest reliable gate for "I don't know this"

Ranked:
1. **Energy score, with OE training** — best cost/benefit. Free at inference. Attacks (b) and (c) directly if we put empty frames + non-birds in the outlier set.
2. **Energy + ReAct** — free. Clip penultimate activations at the 90th-percentile threshold; stacks additively. Expect another few AUROC points.
3. **Mahalanobis (or RMD) in feature space** — cheap, attacks failure (c) especially well because it's a *distance* not a *probability*. Orthogonal signal to energy. Costs one matmul on the Pi CPU.
4. *Top-1 softmax threshold* — don't. It's the baseline we're trying to escape.
5. *Entropy* — monotonically related to MSP for small K, so adds little.

The key insight: energy and Mahalanobis capture different kinds of OOD. Energy is a "is this in the trained data density?" score derived from logits. Mahalanobis is a "is this near any class centroid in feature space?" score. **Combining them** (simple weighted sum, weights fit on a held-out OOD val set made from our 374 non-birds) is a standard trick that empirically outperforms either alone and costs one extra matmul.

---

## 3. Concrete recommendation for our setup

### Main: Energy scoring + Outlier Exposure, trained with focal loss

**Training recipe:**
- Loss: focal loss (γ=3, Mukhoti 2020) on the 12 bird classes.
- Add an **outlier-exposure branch**: sample a minibatch from the 374 non-birds + 22k culled (call this set `OE`). Add energy-margin loss (Liu 2020 §3.3):
  `L_energy = E_ID[max(0, E(x) − m_in)²] + E_OE[max(0, m_out − E(x))²]`
  with `m_in = −25`, `m_out = −7` as starting points (paper defaults).
- Otherwise standard training: 224×224 crops, MobileNetV2 / EfficientNet-Lite backbone, INT8 PTQ at export.

**Inference recipe (on Pi):**
1. Run TFLite via pycoral → get 12 logits.
2. Argmax → predicted class.
3. Compute `energy = -logsumexp(logits)` (numpy, ~1 µs).
4. Apply `softmax(logits / T)` with `T` fit post-hoc — gives calibrated probability.
5. Gate decision:
   - `energy > τ_OOD` → emit `unknown`/`not_bird`.
   - `energy ≤ τ_OOD` AND `calibrated_top1 > τ_conf` → emit class.
   - else → emit `uncertain`.
   Set `τ_OOD` at TPR=95% on the 1,673 ID hold-out. Set `τ_conf` per-class from the reliability diagram.

**Why this:** zero extra inference cost beyond what we already pay; directly attacks all three failure modes (calibration fixes (a), OE + energy fixes (b), energy's density-awareness plus "unknown" emission fixes (c)); and the entire apparatus fits in pycoral + numpy — no new deps.

### Fallback: RMD (Relative Mahalanobis) in penultimate features

If energy+OE doesn't separate cleanly on our hold-out, add Mahalanobis as a second gate. One-time setup: forward the training set, compute per-class means (`12 × 1280`) and a shared covariance inverse (`1280 × 1280`), store as float16 npz. Per-inference: pull the penultimate layer out of the TFLite model (requires a second interpreter call or tapping the graph), compute `min_c (x − μ_c)ᵀ Σ⁻¹ (x − μ_c)`. A bird unlike any of our 12 classes will have a large Mahalanobis distance even when energy doesn't flag it. Gate on the max of normalized(energy) and normalized(mahalanobis).

**Not recommended now:** deep ensembles (budget), ODIN (needs gradients), KNN-OOD (operational complexity), OpenMax (dominated).

### What to measure

On the 1,673-sample hold-out plus a synthetic "OOD eval set" (374 non-birds + held-out empty-feeder frames + any iNaturalist images of species outside our 12):

- **ECE / adaptive ECE** on ID predictions — target <3%.
- **AUROC** for ID vs OOD — target >0.90 (MSP baseline will likely be 0.75–0.80).
- **FPR95** — fraction of OOD wrongly accepted at 95% ID acceptance. Target <20%.
- **Per-class reliability** — no class should have ECE > 10%.
- **End-to-end latency** — verify still ≤5 ms/crop on Edge TPU.

---

## 4. Sources

- Guo et al., *On Calibration of Modern Neural Networks*, ICML 2017 (arxiv 1706.04599).
- Mukhoti et al., *Calibrating Deep Neural Networks using Focal Loss*, NeurIPS 2020 (arxiv 2002.09437).
- Kull et al., *Beyond temperature scaling: Dirichlet calibration*, NeurIPS 2019.
- Hendrycks & Gimpel, *A Baseline for Detecting Misclassified and OOD Examples* (MSP), ICLR 2017.
- Liang et al., *ODIN*, ICLR 2018.
- Lee et al., *A Simple Unified Framework for Detecting Out-of-Distribution Samples* (Mahalanobis), NeurIPS 2018.
- Hendrycks et al., *Deep Anomaly Detection with Outlier Exposure*, ICLR 2019.
- Liu et al., *Energy-based Out-of-distribution Detection*, NeurIPS 2020 (arxiv 2010.03759).
- Sun et al., *ReAct: Out-of-distribution Detection With Rectified Activations*, NeurIPS 2021.
- Sun & Li, *DICE: Leveraging Sparsification for OOD Detection*, ECCV 2022.
- Sun et al., *Out-of-Distribution Detection with Deep Nearest Neighbors*, ICML 2022.
- Ren et al., *A Simple Fix to Mahalanobis Distance for Near-OOD* (RMD), 2021 (arxiv 2106.09022).
- *Mahalanobis++: Improving OOD Detection via Feature Normalization*, 2025 (arxiv 2505.18032).
- *Out-of-Distribution Detection: A Task-Oriented Survey of Recent Advances*, ACM CSUR 2025 (arxiv 2409.11884).
- *Quantized Can Still Be Calibrated*, ACL 2025 (aclanthology 2025.acl-long.1473); arxiv 2509.21173 on ECE drift under quantization.
