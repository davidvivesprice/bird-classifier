# Lit Review 4: INT8 Quantization + Deployment (Coral + Hailo 8L)

Date: 2026-04-23
Scope: MobileNet / EfficientNet-Lite class, 224x224, 13-15 classes, <=5 ms.
Two targets today and tomorrow: **Coral USB Edge TPU** (pycoral 2.0, tflite_runtime 2.5.0, Py3.9) now, **RPi5 + Hailo 8L** later.

Bottom line: the two runtimes consume **different artifacts and different tooling**. One trained PyTorch/TF graph, two parallel compile paths. Do not expect a single "quantized model file" to run on both — you'll ship a `.tflite` (Edge TPU) and a `.hef` (Hailo). Plan the repo and CI around that fact from day one.

---

## 1. Pick the backbone with quantization in mind (before training)

The single biggest accuracy pitfall is **architecture, not calibration**. Two traps to avoid:

- **MobileNetV3 hard-swish + squeeze-excite.** Naive PTQ on V3 collapses accuracy; hard-swish is not a supported Edge TPU op and falls back to CPU, which both wrecks latency and amplifies quantization error. [1][2]
- **Swish / SiLU anywhere (stock EfficientNet, EfficientNet-B0).** Stock EfficientNet-B0 under PTQ has dropped from ~77.4% to ~33.9% top-1 in published results; only QAT brings it back. EfficientNet-Lite exists precisely to fix this — it replaces swish with ReLU6 and removes SE blocks, and the PTQ gap shrinks to <1% (75.1% -> 74.4% on ImageNet). [3][4]

**Recommendation for this project:** use **EfficientNet-Lite0/1** or **MobileNetV2 (1.0)** as the backbone. Both are INT8-friendly by construction on Edge TPU and Hailo. Avoid MobileNetV3, stock EfficientNet, and any custom activation (GELU, swish, Mish) in the classifier head.

---

## 2. Quantization strategy: PTQ vs QAT

Decision rule, in order:

1. **Start with full-integer PTQ** (weights + activations + I/O all INT8). It's the only thing the Edge TPU actually executes, and it's the path both Hailo and Coral officially support. [5][6]
2. **Measure the FP32 -> INT8 accuracy gap** on your held-out 1,673 clean images. If the gap is **<=1.5% top-1** and per-class recall doesn't drop more than **3 pp on any class**, ship PTQ.
3. **Escalate to QAT** only if the gap exceeds that threshold on the held-out set, or if a rare class loses >5 pp recall. Budget a week — QAT requires training-loop changes, a fresh fine-tune, and re-export. QAT typically recovers 2-5% over PTQ on hard backbones. [7]

Why this order: with an EfficientNet-Lite backbone and clean calibration, PTQ is almost always sufficient. Going straight to QAT is premature optimization that costs a week.

**Do not use dynamic-range or float16 quantization.** Edge TPU requires full-integer — dynamic-range models compile but run weights-on-CPU, which kills the whole point. [8]

---

## 3. Calibration set construction

This is where PTQ lives or dies. Concrete recipe:

- **Size:** TFLite official guidance is "a few dozen to a few hundred" — in practice 100-500 samples gives stable min/max for a 15-class classifier. [5] **Hailo's guidance is different: their DFC expects ~1024+ samples, with several thousand recommended.** [9][10]
- **Composition:** sample from your **held-out 1,673 clean images** (stratified by class, plus a small OOD/background slice). Do **not** sample from the 34K weak-label pool — you want the activation statistics to match what the deployed model will see. The TFLite docs explicitly say representativeness matters more than volume. [5]
- **Preprocessing must match inference exactly.** Same resize, same color order (RGB for both Hailo DFC and Coral), same normalization. A silent BGR/RGB mismatch in calibration is the #1 cause of "model compiled fine but predicts garbage."
- **Leakage:** the calibration slice must never be used for eval. Pre-split 1,673 -> 256 calibration / 1,417 evaluation, freeze the split in a CSV, and commit it.

---

## 4. Coral Edge TPU path

### 4.1 Artifact pipeline

**Recommended path (matches the existing pipeline you already have working):**

```
Keras/TF model
  -> TFLiteConverter w/ representative_dataset + INT8 I/O
  -> model_int8.tflite
  -> edgetpu_compiler model_int8.tflite
  -> model_int8_edgetpu.tflite
```

**PyTorch path is possible but lossy:** PyTorch -> ONNX -> onnx2tf (or onnx-tf) -> TFLite -> edgetpu_compiler. Known breakage: channel-last conversion, unsupported op rewrites, and the edgetpu_compiler rejecting graphs because of a single leftover `Transpose` or `Cast`. If your training code is PyTorch, seriously consider porting to TF/Keras for the Coral target only, or accept that every PyTorch change will need a conversion-debug day.

### 4.2 Hard Edge TPU compile constraints [11][6]

- Tensors must be **1D, 2D, or 3D** (or 4D+ with only 3 innermost dims >1). `batch=1` is mandatory.
- **Tensor sizes constant at compile time** — no dynamic shapes, no `None` batch dim.
- **Weights/biases frozen.**
- Every op must be in the Edge TPU op table, or it partitions. A single unsupported op mid-graph forces everything downstream to CPU. This is the "order of magnitude slowdown" trap. [6]
- INT8 I/O strongly recommended (prevents CPU-side quantize/dequantize nodes).

### 4.3 Operator gotchas

- **HARD_SWISH, LEAKY_RELU, GELU, SWISH** — not Edge TPU supported. [1][2]
- **Squeeze-and-excite blocks** — the sigmoid+mul combo usually compiles but often lands on CPU.
- **NMS / postprocessing** — keep all postprocessing (argmax, softmax, NMS) **outside** the TFLite graph. Let the classifier output raw INT8 logits; dequantize + argmax in Python. A single FLOAT-typed NMS inside the graph can silently break quantization. [11]
- `edgetpu_compiler -s` prints the op-by-op mapping table. **Always inspect it** — reject any build where the "Operations successfully mapped" count isn't 100%.

### 4.4 Version pinning note

Your toolchain is `pycoral 2.0 / tflite_runtime 2.5.0 / Py3.9`. That combo is frozen — Google has not updated the Debian Edge TPU runtime since 2021. Training with newer TF (2.13+) is fine, but the TFLite converter schema must stay compatible with `tflite_runtime 2.5.0`. Pin `tensorflow==2.13.x` for conversion and keep a `convert_for_coral.py` script isolated from the Hailo path. [12]

---

## 5. Hailo 8L path

### 5.1 Artifact pipeline

```
PyTorch or TF model
  -> export to ONNX (PyTorch) or TFLite/ONNX (TF)
  -> Hailo Dataflow Compiler (DFC):
       parser -> optimizer (calibration+quant) -> compiler
  -> model.hef
```

Hailo officially consumes **ONNX and TFLite**; PyTorch is supported indirectly via ONNX export. [13][14] The clean path from PyTorch is: `torch.onnx.export` (opset 13-17 is the sweet spot) -> `hailo parser` -> `hailo optimize` (calibration happens here) -> `hailo compiler`. The Hailo Model Zoo ships scripts that do all three stages in one command with a YAML config — start from a Model Zoo example for your backbone and edit it. [13]

### 5.2 Hailo 8 vs 8L: not interchangeable

**An HEF compiled for Hailo 8 will NOT load on an 8L and vice versa.** The runtime refuses with `HEF arch: HAILO8, Device arch: HAILO8L` (or the reverse). You must pick the target at compile time: `hailo compiler --hw-arch hailo8l ...`. [15][16] The 8L is a lower-capacity, lower-cost variant (~13 TOPS vs ~26 TOPS), and HEFs are physically laid out for the device's compute cluster topology. Plan your CI matrix: **one training run, two compile targets** (`tflite_edgetpu` + `hef_hailo8l`).

### 5.3 Differences vs Edge TPU compile

- **Calibration set is bigger** — 1024+ images recommended, several thousand typical. [9][10] Your 256-sample plan for Coral is too small for Hailo; build a second, larger calibration pack (~1500 images from the clean set, rest of the held-out reserved for eval).
- **Quant error mitigations built in:** DFC exposes weight clipping, activation clipping, IBC (Iterative Bias Correction), and QFT (quantization fine-tuning). These are Hailo-specific and will recover accuracy that PTQ alone loses. [9] Turn them on before considering QAT.
- **Broader op support**: LSTM bidirectional, hard-swish, and more are supported natively — the Hailo 8L will compile graphs the Edge TPU can't. This means you can train one backbone for Hailo that would force you to re-architect for Coral, but if you're shipping both, target the **Coral op set as the lowest common denominator**.
- **Single HEF = whole graph**. Unlike Edge TPU, Hailo doesn't silently partition to CPU — unsupported ops fail compilation outright. Easier to reason about, harder to accidentally ship a slow model.

---

## 6. Post-quantization validation — tests that catch silent accuracy loss

Run all of these on every build, fail the pipeline on regression:

1. **Held-out top-1 and per-class recall.** Compare FP32 -> INT8 TFLite -> Edge TPU TFLite -> HEF. Fail if any stage drops >1.5% top-1 or >3 pp per-class recall from FP32.
2. **Per-class confusion matrix diff.** A single class swap (e.g., "house finch" -> "purple finch") often hides in aggregate top-1. Diff matrices, not scores.
3. **Logit-correlation test.** Feed the same 256 images through FP32 and INT8, compute Pearson correlation of raw logits per class. <0.98 on any class = investigate.
4. **Calibration vs eval overlap check.** Assert no filename intersection. Dumb bug, common bug.
5. **End-to-end latency budget.** Fail if `edgetpu_compiler -s` reports any op on CPU, or if measured latency exceeds 5 ms on either device. Use `pycoral.utils.edgetpu.run_inference` and `HailoRT`'s built-in profiler.
6. **I/O dtype assertion.** Confirm input is INT8 and output is INT8 on both artifacts. A float input tensor silently adds a CPU-side quantize op.
7. **Numerical spot check on a golden batch.** Keep 10 hand-labeled "canary" images committed to the repo. Every compiled artifact must produce the same argmax on all 10. This catches preprocessing drift across pipeline rewrites.

---

## 7. Deployment recipe, condensed

```
train: EfficientNet-Lite0 or MobileNetV2, ReLU6 only, 224x224, 15 classes
export:
  fp32_savedmodel/   (source of truth)
  splits/calib_256.csv   (stratified from 1,673 clean)
  splits/calib_1500.csv  (stratified from 1,673 clean)
  splits/eval.csv        (complement)
compile_coral.py:
  - TFLiteConverter INT8 full, INT8 I/O, representative_dataset=calib_256
  - edgetpu_compiler -s -o out/coral/  model_int8.tflite
  - assert 100% ops mapped
compile_hailo.py:
  - torch.onnx.export opset 15 (or TF -> ONNX)
  - hailo parser / optimize (calib_1500) / compiler --hw-arch hailo8l
  - assert compile succeeds, emit out/hailo/model.hef
validate.py (runs on both artifacts):
  - top-1 + per-class recall vs FP32 baseline (thresholds above)
  - logit correlation
  - canary batch argmax equality
  - latency <5 ms
```

Ship only when all seven validations pass. Two artifacts, one eval script, one regression gate. That's the recipe.

---

## Sources

1. [MobileNetV3 Hard_swish not supported (google-coral/edgetpu #353)](https://github.com/google-coral/edgetpu/issues/353)
2. [Will EdgeTPU support LeakyRelu and Hardswish? (#272)](https://github.com/google-coral/edgetpu/issues/272)
3. [Higher accuracy on vision models with EfficientNet-Lite (TF Blog)](https://blog.tensorflow.org/2020/03/higher-accuracy-on-vision-models-with-efficientnet-lite.html)
4. [HAWQ-V3: Dyadic Neural Network Quantization (PTQ vs QAT numbers)](https://assets.amazon.science/a5/a5/bc16183e477aabdb282bfbeea260/hawq-v3-dyadic-neural-network-quantization.pdf)
5. [Post-training quantization - Google AI Edge / LiteRT](https://ai.google.dev/edge/litert/conversion/tensorflow/quantization/post_training_quantization)
6. [TensorFlow models on the Edge TPU - Coral](https://www.coral.ai/docs/edgetpu/models-intro/)
7. [Achieving FP32 Accuracy for INT8 Inference with QAT (NVIDIA)](https://developer.nvidia.com/blog/achieving-fp32-accuracy-for-int8-inference-using-quantization-aware-training-with-tensorrt/)
8. [Post-training integer quantization (TFLite)](https://www.tensorflow.org/lite/performance/post_training_integer_quant)
9. [hailo_model_zoo OPTIMIZATION.rst (calibration, IBC, QFT)](https://github.com/hailo-ai/hailo_model_zoo/blob/master/docs/OPTIMIZATION.rst)
10. [Accuracy degradation after quantization for Hailo HW (Hailo Community)](https://community.hailo.ai/t/accuracy-degradation-after-quantization-for-hailo-hw/50)
11. [Edge TPU Compiler docs](https://www.coral.ai/docs/edgetpu/compiler)
12. [Coral FAQ (runtime version pinning)](https://www.coral.ai/docs/edgetpu/faq/)
13. [Hailo Model Zoo (ONNX/TFLite parser paths)](https://github.com/hailo-ai/hailo_model_zoo)
14. [Convert ONNX Models to Hailo8L (RidgeRun)](https://www.ridgerun.ai/post/convert-onnx-model-to-hailo8l)
15. [HEF format is not compatible with device (Hailo Community)](https://community.hailo.ai/t/hef-format-is-not-compatible-with-device-device-arch-hailo8l-hef-arch-hailo8/8162)
16. [Differences between Hailo-8 and Hailo-8L (Hailo Community)](https://community.hailo.ai/t/what-are-differences-between-hailo-8-and-hailo-8l/1675)
