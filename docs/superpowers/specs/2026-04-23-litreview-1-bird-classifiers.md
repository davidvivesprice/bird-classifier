# Literature Review: Fine-Grained Bird Classification for Embedded Inference

**Date:** 2026-04-23
**Scope:** State of the art 2024-2026, biased toward what ships on a Coral Edge TPU today and a Hailo-8L tomorrow, for the Vives Bird Observatory flagship yard classifier (≤46 classes, ~36K crops, 158× imbalance, dominant Hairy/Downy confusion).

---

## 1. Top 5 backbones for INT8 TFLite / Edge TPU

Ranked for our constraint set: 224×224, ≤5 ms on Edge TPU, fully INT8-quantizable, 46-way fine-grained.

### 1. **EfficientNet-Lite0** — the safe pick
- ~4.7M params, 75.1% FP32 / 74.4% INT8 on ImageNet. Designed explicitly *for* INT8 TFLite and Edge TPU. No squeeze-and-excite (SE) blocks, no `swish` — the two things that cause Edge TPU fallback to CPU. Compiles cleanly end-to-end.
- On Coral, 224×224 inference is typically ~2-4 ms with everything on TPU.
- **Why it's the safe pick:** every op maps; quantization is characterized; training recipes are well-understood (TF `lite_model_maker`, `timm` `efficientnet_lite0`). Hailo-8L also lists it as a first-class model.
- **Con:** older architecture (2020). A well-trained MobileNet-V3 can match it, but MNV3's hard-swish / SE need care.

### 2. **MobileNet-V3-Large (no SE variant, or SE re-implemented as HardSigmoid)** — the competitive pick
- ~5.4M params, 75.2% FP32 on ImageNet. Best accuracy/FLOP on CPU; on Edge TPU it *can* be fastest if you strip hard-swish and fold SE properly. Coral ships a version; many third-party compiles fail because of `hard_swish` being delegated to CPU.
- **Con:** risk of op fallbacks that quietly tank latency. If we pick this, we build a compile-smoke-test in CI.

### 3. **EfficientNet-Lite1 / Lite2** — more headroom
- Lite1 (~5.4M, 76.7%) and Lite2 (~6.1M, 77.6%) at 240/260 input give measurable accuracy gains for ~1.5-2× the latency of Lite0. Worth benchmarking on Coral; Lite2 often sits right at our 5 ms bound at 224 crops.
- **Recommendation:** train Lite0 *and* Lite1 on the same recipe; pick by held-out accuracy if Lite1 comes in under 5 ms.

### 4. **MobileViT-V2-050 / -075** — the transformer dark horse
- MobileViT-V2-050 quantizes to ~1.7 MB INT8 and gets near-ViT accuracy at mobile cost ([MDPI 2024](https://www.mdpi.com/2076-3417/14/18/8115)). Lightweight attention is genuinely useful for fine-grained differences (bill length, crown texture).
- **Con for *us*:** Coral Edge TPU compiler often kicks transformer attention ops to CPU (softmax-over-axis, reshape-heavy attention). Expect partial delegation unless we use Keras reference implementations verified end-to-end. Hailo-8L is friendlier here but still not free. **Not our first pick for Edge TPU today; revisit for Hailo phase.**

### 5. **ShuffleNet-V2 1.0× / 1.5×** — the efficiency pick we should *not* take
- Cheap on CPU, but channel-shuffle ops have historically been poorly supported on Edge TPU and Hailo toolchains. Listing it for completeness; skip unless latency budget shrinks.

**Explicitly excluded:** FastViT, RepViT, EfficientFormer — all promising on phone NPUs but not officially supported on Coral's edgetpu-compiler today. [Efficient Deployment of Transformer Models on Edge TPU Accelerators](https://openreview.net/forum?id=PibYaG2C7An) documents the op-refactoring needed; not worth the yak-shave for v1.

---

## 2. Training recipes that actually work on small fine-grained datasets (2024-2026)

The modern consensus recipe ([Wightman et al., "ResNet strikes back"](https://arxiv.org/pdf/2110.00476); `timm` defaults; [TDS "Refined Training Recipe for FGVC"](https://towardsdatascience.com/a-refined-training-recipe-for-fine-grained-visual-classification/)):

| Component | Recommended setting | Why |
|---|---|---|
| **Optimizer** | AdamW, lr 1e-3, weight decay 0.02 | SGD-with-momentum still OK; AdamW is faster to tune and robust on ~36K samples. |
| **Schedule** | Cosine decay, 5-epoch linear warmup, 100-200 epochs | FGVC needs long schedules; Lite models benefit from warmup. |
| **Augmentation stack** | RandomResizedCrop(0.6-1.0), HorizontalFlip, **RandAugment(m=9, n=2)** or **TrivialAugment**, **Mixup (α=0.2)** + **CutMix (α=1.0)** with 50/50 switch, RandomErasing(p=0.25) | This is the `timm` "A2" recipe. Mixup+CutMix is non-negotiable on small FGVC. TrivialAugment matches RandAugment with zero tuning. |
| **Label smoothing** | 0.1 | Standard. Interacts well with mixup. |
| **Loss** | Cross-entropy + **class-balanced** re-weighting (β=0.9999) initially, then **deferred re-weighting (DRW)** in last 20% of epochs. | See §3. |
| **Regularization** | Stochastic depth 0.1 (if backbone supports), DropConnect 0.2 | |
| **EMA** | Model EMA decay 0.9999 | Consistently gains 0.5-1% on FGVC. |
| **Pretraining** | ImageNet-1k (mandatory) → iNat21 birds checkpoint if available, else straight to our data | iNat21 pretraining is the single biggest lever (~3-5%). |
| **Resolution** | Train at 224, optionally fine-tune last 10 epochs at 240-256 (FixRes) | Mild accuracy bump, free at inference if we stay at 224. |

**For INT8 deployment:** always finish with **quantization-aware training (QAT)** for the last ~10-20 epochs. Post-training quantization (PTQ) *works* on Lite backbones but loses 0.5-1.5% on fine-grained; for Hairy/Downy that matters.

---

## 3. How the literature handles "nearly identical species" (the Hairy/Downy problem)

Three families of techniques, ordered by how much engineering they cost us:

### A. Loss-level: Pairwise Confusion + focal variants (**cheap, ship first**)
[Dubey et al., ECCV 2018, "Pairwise Confusion"](https://openaccess.thecvf.com/content_ECCV_2018/papers/Abhimanyu_Dubey_Improving_Fine-Grained_Visual_ECCV_2018_paper.pdf): add an auxiliary loss that *minimizes* KL-divergence between predicted distributions of random same-batch pairs. Counter-intuitively, confusing the network on random pairs forces it to rely on *real* discriminative features. Adds one line to training, gives measured +1-3% on CUB-style datasets. **Directly applicable to Hairy/Downy.**

[Sun et al., "Learning Attentive Pairwise Interaction" (API-Net, AAAI 2020)](https://ojs.aaai.org/index.php/AAAI/article/view/7016): sample confused-pair mini-batches and learn a gating vector that highlights differences. Middling engineering cost; bigger gains on known-hard pairs. **Second thing to try** — we already know our hard pair, so we can explicitly mine (Hairy, Downy) pairs each batch.

### B. Attention-based part discovery (**medium cost, uncertain on-device**)
- **TransFG** ([He et al., AAAI 2022](https://cdn.aaai.org/ojs/19967/19967-13-23980-1-2-20220628.pdf)): uses ViT attention to select discriminative patches. SOTA on CUB-200-2011, NABirds. Heavy.
- **CAL** ([Rao et al., ICCV 2021](https://openaccess.thecvf.com/content/ICCV2021/papers/Rao_Counterfactual_Attention_Learning_for_Fine-Grained_Visual_Categorization_and_Re-Identification_ICCV_2021_paper.pdf)): counterfactual attention — ask "what if the attention were wrong?" as a supervisory signal. Works on CNNs. Cheap to add if our backbone exposes intermediate features.
- **GLSim** ([2024](https://arxiv.org/html/2407.12891v1)): global-local similarity beats CAL/TransFG on 8 FGVC benchmarks with a ViT backbone.
- All of these are training-time tricks that leave inference unchanged *if* you drop the attention branch at export. CAL is the only one that realistically bolts onto EfficientNet-Lite0 without pain.

### C. Hierarchical / two-stage classification (**higher cost, pays off**)
The camera-trap community has largely converged here. [Nature Sci Reports 2025](https://www.nature.com/articles/s41598-025-90249-z) report a two-stage pipeline (global classifier → cluster-specific expert models) hitting 96.2% F1 across 24 mammal species — well above single-model baselines. The "Two-phase training mitigates class imbalance" approach ([Ecology & Evolution 2021](https://www.researchgate.net/publication/357417207)) is simpler: train one model on all classes, then freeze the backbone and retrain the head on a balanced sampler.

**For us:** a tiny, dedicated **Hairy-vs-Downy binary head** triggered only when the main 46-way classifier's top-2 includes both is worth its weight in gold. It's the camera-trap "expert model" pattern, applied surgically to one known-hard pair.

---

## 4. Single-pose / feeder-cam specifics

Not a large literature — most FGVC benchmarks are deliberately diverse. But three robust findings:

1. **Pose consistency is a feature, not a bug.** [Schneider et al., Ecology & Evolution 2020](https://onlinelibrary.wiley.com/doi/full/10.1002/ece3.6147) ("Three critical factors affecting automated image species recognition performance for camera traps") shows that classifiers trained on one camera transfer poorly to others. We should *not* train on iNat-like diverse data and expect it to work on our feeder — **our own crops dominate**, with iNat data as a pretraining step only.
2. **Temporal consistency cheap-and-free.** At 5 fps, the same bird appears in a burst of frames. Even simple per-track majority-vote over 5-10 frames erases most single-frame flips. Not a training technique, a pipeline technique — but this is where feeder-cam deployments outperform FGVC benchmarks.
3. **Background leakage is real and hazardous.** A single-pose dataset means the background, feeder fixture, and lighting correlate with species (whichever species visit most). Training-time fixes: aggressive color jitter, random erasing *over the bird*, and — if we can — paste crops onto varied backgrounds. Audit: mask the bird at validation time; if accuracy stays high, the model learned the feeder.

---

## 5. Recommendation

**Ship EfficientNet-Lite0 at 224×224, INT8 via QAT, trained on the `timm` A2-style recipe with these three additions:**

1. **Class-balanced re-weighting with DRW** (cross-entropy β=0.9999, applied only in last 20% of epochs). This is the single highest-ROI fix for 158× imbalance and is in the standard [LDAM-DRW](https://github.com/kaidic/LDAM-DRW) repo. Do not use plain focal loss alone; [survey results](https://arxiv.org/pdf/2404.15593) consistently show LDAM/Balanced-Softmax beats focal by 5-9% on long-tailed benchmarks.
2. **Pairwise Confusion auxiliary loss**, weight 0.1. Trivial to add, genuinely helps similar-species pairs.
3. **A tiny Hairy-vs-Downy binary head** (2-layer MLP on pooled features) triggered only when the main head's top-2 are those species. This is the camera-trap "expert model" pattern. At inference, the cost is ~0.1 ms extra on CPU and only fires on <5% of detections.

**iNat21 birds checkpoint is the single biggest pretraining lever** — ~3-5% accuracy for zero inference cost. If we can't find a downloadable one, the `timm` `tf_efficientnet_lite0.in1k` ImageNet-1k checkpoint is our floor.

**Do not** invest in MobileViT / FastViT / CAL until we have a Lite0 baseline that's beating 85% balanced accuracy on the 1,673-label hold-out. They're future gains, not prerequisites. **Do not** revisit weight imprinting — the literature has not found a fine-grained recipe where k-NN-on-embeddings beats a well-trained softmax classifier on our data regime.

**Specific risks to pre-empt:**
- Background leakage: validate by masking the bird and confirming accuracy drops to chance.
- Train/test temporal leakage: split the 1,673 human-verified labels *by day*, not at random.
- Compile-time op fallbacks: verify on Coral with `edgetpu_compiler -s` that 100% of ops land on TPU before declaring a model shippable.

---

## Sources

- [Higher accuracy on vision models with EfficientNet-Lite (TF Blog)](https://blog.tensorflow.org/2020/03/higher-accuracy-on-vision-models-with-efficientnet-lite.html)
- [TensorFlow models on the Edge TPU (Coral)](https://www.coral.ai/docs/edgetpu/models-intro/)
- [Edge TPU performance benchmarks (Coral)](https://www.coral.ai/docs/edgetpu/benchmarks/)
- [Hailo-8L Models (Hailo Model Zoo)](https://github.com/hailo-ai/hailo_model_zoo)
- [MobileViT on-edge deployment study, MDPI Applied Sciences 2024](https://www.mdpi.com/2076-3417/14/18/8115)
- [Wightman, "ResNet strikes back"](https://arxiv.org/pdf/2110.00476)
- [timm mixup/cutmix docs](https://timm.fast.ai/mixup_cutmix)
- [A Refined Training Recipe for Fine-Grained Visual Classification (TDS)](https://towardsdatascience.com/a-refined-training-recipe-for-fine-grained-visual-classification/)
- [Dubey et al., Pairwise Confusion for FGVC, ECCV 2018](https://openaccess.thecvf.com/content_ECCV_2018/papers/Abhimanyu_Dubey_Improving_Fine-Grained_Visual_ECCV_2018_paper.pdf)
- [Sun et al., Learning Attentive Pairwise Interaction, AAAI 2020](https://ojs.aaai.org/index.php/AAAI/article/view/7016)
- [He et al., TransFG, AAAI 2022](https://cdn.aaai.org/ojs/19967/19967-13-23980-1-2-20220628.pdf)
- [Rao et al., Counterfactual Attention Learning, ICCV 2021](https://openaccess.thecvf.com/content/ICCV2021/papers/Rao_Counterfactual_Attention_Learning_for_Fine-Grained_Visual_Categorization_and_Re-Identification_ICCV_2021_paper.pdf)
- [GLSim: Global-Local Similarity for FGVC, 2024](https://arxiv.org/html/2407.12891v1)
- [Cao et al., LDAM-DRW, NeurIPS 2019](https://github.com/kaidic/LDAM-DRW)
- [A Survey of Deep Long-Tail Classification Advancements, 2024](https://arxiv.org/pdf/2404.15593)
- [Schneider et al., Three critical factors in camera-trap recognition, Ecology & Evolution 2020](https://onlinelibrary.wiley.com/doi/full/10.1002/ece3.6147)
- [Two-stage camera-trap pipeline, Nature Sci Reports 2025](https://www.nature.com/articles/s41598-025-90249-z)
- [Two-phase training for class imbalance in camera traps, 2021](https://www.researchgate.net/publication/357417207)
- [Efficient Deployment of Transformer Models on Edge TPU Accelerators (OpenReview)](https://openreview.net/forum?id=PibYaG2C7An)
