# Transfer Learning Pipeline for Yard Bird Classifier

**Date:** 2026-04-08
**Status:** Approved
**Author:** Claude + David

## Problem

The AIY Birds V1 model (964 species, Google-trained on iNaturalist photos) classifies birds from typical bird photography angles. Our feeder cam produces overhead close-ups with consistent background. A model trained on OUR images should beat AIY for common feeder species.

Weight imprinting failed twice (0/14 contaminated data, 31% with clean data). It only updates the last layer — insufficient for distinguishing 12 similar small bird species from the same camera angle.

## Solution

Real transfer learning using EfficientNet-Lite0 fine-tuned on confirmed feeder-cam images, with the iNaturalist bird model as the feature extractor base (not generic ImageNet). Training runs in Google Colab. The compiled Edge TPU model deploys alongside AIY Birds V1 using the existing dual-model `_pick_winner()` architecture.

## Glossary

- **Transfer learning**: Taking a model that already "understands seeing" (trained on millions of images) and teaching it YOUR specific task with YOUR images. Much faster and more accurate than training from scratch.
- **Fine-tuning**: The second phase of transfer learning where you "unfreeze" deeper layers and let them adjust to your data (not just the classification head).
- **EfficientNet-Lite0**: A neural network architecture optimized for mobile/edge devices. Good accuracy for its size. Fits in the Coral Edge TPU's 8MB SRAM.
- **Feature extractor**: The lower layers of the model that detect edges, textures, shapes, colors. These transfer between tasks — a model that learned to see dogs can use those same low-level skills to see birds.
- **iNaturalist bird model**: A MobileNetV2 trained specifically on bird photos (~900 species). Better starting point than generic ImageNet because it already knows bird-specific features (plumage patterns, beak shapes).
- **Quantization**: Converting model weights from 32-bit floats to 8-bit integers. Makes the model 4x smaller and much faster, with minimal accuracy loss. Required for Edge TPU.
- **Post-training quantization (PTQ)**: Quantizing after training is complete, using a representative dataset to calibrate the conversion. Simpler than quantization-aware training.
- **Edge TPU compilation**: Converting a quantized TFLite model into one that runs on the Coral USB Accelerator hardware. One-way process — the compiled model can't be edited.
- **Out-of-distribution (OOD)**: When the model sees something it wasn't trained on (e.g., a Blue Jay when it only learned 12 species). Good OOD detection means the model says "I don't know" instead of confidently guessing wrong.
- **Temperature scaling**: A calibration technique that adjusts how "confident" the model's predictions are. Higher temperature = less confident = better at admitting uncertainty on unknown species.
- **Confusion matrix**: A table showing which species get mixed up with which. Rows = actual species, columns = predicted species. Perfect model has all counts on the diagonal.
- **Holdout set**: Images set aside before training that the model never sees. Used to test accuracy on "fresh" data.
- **Data augmentation**: Creating variations of training images (flipped, rotated, brightness-adjusted) to teach the model that a bird is still a bird even from slightly different angles or lighting.
- **Softmax**: Converts raw model scores into probabilities that sum to 1.0. Higher score = more confident.
- **Abstention rate**: How often the model correctly says "I don't know" when shown a species it wasn't trained on. We want ≥95%.

## Architecture

Three cleanly separated components:

### 1. Data Exporter (`train_export.py` — runs on iMac)

Queries the review database for confirmed feeder-cam, single-bird images. Crops each image using its bounding box (15% padding). Organizes into `species_name/` folders. Splits 80/20 train/test (stratified). Also creates an OOD test set from untrained species.

**Outputs:**
- `training_data.zip` containing:
  - `train/{species}/` — 80% of images per species
  - `test/{species}/` — 20% holdout per species
  - `ood_test/{species}/` — all confirmed images of untrained species
  - `manifest.json` — species list, counts, export date, camera filter, DB stats

**Data filters (non-negotiable):**
- `c.camera = 'feeder'` — feeder cam only
- `json_array_length(c.birds_json) <= 1` — single-bird frames only
- Confirmed reviews only (`verdict = 'correct'` OR `verdict = 'wrong'` with `correct_species`)
- Minimum 15 images per species to be included in training

**Dashboard integration:**
- "Export Training Data" button in the Training tab
- Shows species counts, warns if any species is below 50 images
- Downloads the zip to `~/docs/bird-observatory/training-exports/`

### 2. Colab Training Notebook (`Bird_Observatory_Training.ipynb`)

A self-contained Google Colab notebook. Every cell has plain English comments explaining what it does and why.

**Notebook sections:**

1. **Setup** — Install TensorFlow, edgetpu_compiler. Mount Google Drive or upload zip.
2. **Unpack & Inspect** — Unzip, show species distribution, sample images per species. Visual verification checkpoint.
3. **Build Model** — Load EfficientNet-Lite0 with iNaturalist bird feature extractor base (not generic ImageNet). Replace classification head with our species count.
4. **Data Augmentation** — Horizontal flip, ±15% brightness/contrast, ±10° rotation, random crop (0.85-1.0 scale). No vertical flip (birds don't hang upside down). No heavy color distortion (plumage color is diagnostic).
5. **Train Phase 1** — Freeze base layers. Train classification head only for 10 epochs. Learning rate 0.01.
6. **Train Phase 2** — Unfreeze top 20% of layers. Fine-tune for 10 more epochs at learning rate 0.0001. This is where the model learns YOUR feeder's specific visual patterns.
7. **Evaluate: In-Distribution** — Test on holdout set. Per-species accuracy, confusion matrix, overall accuracy. Must hit ≥80% to proceed.
8. **Evaluate: Out-of-Distribution** — Test on untrained species. Measure abstention rate (% scoring below confidence threshold). Must hit ≥95% abstention. Calibrate temperature scaling.
9. **Quantize** — Full integer quantization (uint8 input/output, int8 weights). Use ~200 representative training images for calibration.
10. **Compile for Edge TPU** — `edgetpu_compiler` (runs natively in Colab's Linux environment). Verify 100% ops mapped to Edge TPU.
11. **Download** — Package `yard_model_edgetpu.tflite` + `yard_model_labels.txt` + `training_report.json` for download.

**Training report (`training_report.json`) contains:**
- Per-species accuracy on holdout set
- Overall accuracy
- Confusion matrix
- OOD abstention rate
- Recommended confidence threshold
- Training hyperparameters used
- Species list and image counts
- Timestamp

**Base model choice: iNaturalist bird features vs ImageNet**

The iNaturalist bird model (`mobilenet_v2_1.0_224_inat_bird`) was trained on ~900 bird species. Its lower layers already know bird-specific features: plumage patterns, beak shapes, body proportions. This is a better starting point than generic ImageNet (dogs, cars, flowers) because:
- The feature extractor already "thinks in birds"
- Less fine-tuning needed to specialize for our 12 species
- Better accuracy with fewer training images

If the iNaturalist base model can't be loaded as a Keras feature extractor (TF Hub compatibility issue), fall back to ImageNet-pretrained EfficientNet-Lite0. Both paths produce Edge TPU compatible output.

### 3. Model Deployer (runs on iMac)

**Manual deploy (v1):**
- Copy `yard_model_edgetpu.tflite` and `yard_model_labels.txt` to `models/`
- Old model auto-backed up as `yard_model_prev.tflite`
- Restart classifier: `launchctl unload/load com.vives.bird-classifier.plist`

**Dashboard deploy (future):**
- "Deploy Model" button: upload `.tflite`, backup old, restart
- "Rollback" button: swap back to `yard_model_prev.tflite`
- "Start Fresh" button: delete yard model, revert to AIY-only

**No code changes needed in classify.py or yard_classifier.py** — the existing dual-model architecture loads whatever `.tflite` is at `models/yard_model.tflite`. Label file at `models/yard_model_labels.txt`. The `_pick_winner()` logic handles confidence thresholds.

## Testing Suite

### Level 1: Holdout Accuracy Test (in Colab, automated)

- 20% of training images held back, never seen during training
- Per-species accuracy report
- Confusion matrix
- **Gate: ≥80% overall accuracy to proceed**

### Level 2: Out-of-Distribution Test (in Colab, automated)

- Feed images of UNTRAINED species through the model
- Verify ≥95% score below confidence threshold (model correctly abstains)
- Temperature scaling calibration: tune threshold so untrained species reliably fall through to AIY
- Also test with ground cam images if available
- **Gate: ≥95% abstention rate on untrained species**

### Level 3: Video End-to-End Test (on iMac)

A test harness (`test_video_pipeline.py`) that replays Protect video clips through the full bird pipeline:

- Decodes video frames (handles higher resolution from Protect export)
- Runs YOLO detection → crop → classify with BOTH models → IoU tracking
- Frame-by-frame timeline: species, confidence, which model won, track lifecycle
- Summary report: detections per species, false positives, missed detections

**Test videos (David provides):**
- 30-60 seconds each, 3-5 clips
- Mix of: common species, look-alike pairs, untrained species, empty feeder, multiple birds
- Exported from UniFi Protect app

**Regression testing:** Save results as baseline. Every retrain, run the same videos and compare. Accuracy should improve or stay stable, never regress.

## Deployment Gate

The model ONLY goes live if ALL of these pass:
1. Holdout accuracy ≥ 80%
2. OOD abstention rate ≥ 95%
3. Video test shows correct species identification with no confident wrong answers on untrained species

## Data Flow

```
iMac: train_export.py
  → queries DB (feeder, single-bird, confirmed)
  → crops images using bounding boxes
  → splits 80/20 train/test + OOD set
  → zips to training_data.zip

Upload to Colab (or Google Drive)

Colab: Bird_Observatory_Training.ipynb
  → unpack, inspect, visual verify
  → train EfficientNet-Lite0 (iNat bird base)
  → evaluate holdout (≥80% gate)
  → evaluate OOD (≥95% gate)
  → quantize (full int8)
  → compile for Edge TPU
  → download yard_model_edgetpu.tflite + labels + report

iMac: Deploy
  → copy to models/
  → restart classifier
  → run video test suite
  → go live
```

## What We're NOT Building (YAGNI)

- Dashboard Training tab UI (future — manual deploy is fine for now)
- Automated retraining pipeline (future — manual trigger via Colab)
- Ground cam model (separate project, feeder first)
- BirdNET custom audio classifier (parallel track, not this spec)
- Model versioning system (file backup is enough for now)

## Dependencies & Constraints

- **Python 3.9** in venv-coral (pycoral compatibility)
- **numpy < 2.0** (pycoral compiled against numpy 1.x)
- **Coral USB single-session** — classifier must be stopped during deployment
- **edgetpu_compiler** runs in Colab (Linux), not macOS
- **TensorFlow** only needed in Colab, not on iMac
- **No GPU required** on iMac — Colab provides free GPU for training

## References

- [Google Coral Official Retraining Tutorial (Colab)](https://colab.research.google.com/github/google-coral/tutorials/blob/master/retrain_classification_ptq_tf2.ipynb)
- [Autonomous AI Bird Feeder Paper (arXiv:2508.09398)](https://arxiv.org/abs/2508.09398) — Belgian researchers, 40 species, 99.5% val / 88% field accuracy
- [Coral Models - iNaturalist Bird Model](https://www.coral.ai/models/all/)
- [Coral Retrain Classification Guide](https://www.coral.ai/docs/edgetpu/retrain-classification/)
- [EfficientNet-EdgeTPU](https://research.google/blog/efficientnet-edgetpu-creating-accelerator-optimized-neural-networks-with-automl/)
- [TF Lite Model Maker Guide](https://www.tensorflow.org/lite/guide/model_maker)
