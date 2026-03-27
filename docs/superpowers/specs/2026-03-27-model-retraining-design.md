# Model Retraining — Design Spec

## Problem

The AIY Birds V1 species classifier is 82% accurate on reviewed images. It confuses sparrow species, misidentifies Rock Pigeons as Chickadees, and swaps Downy/Hairy Woodpeckers. The intelligence layers (yard prior, visit voting, audio corroboration) help flag errors, but the root cause is the model — it was trained on generic bird photos, not YOUR cameras, YOUR lighting, YOUR feeder.

## Goal

Train a yard-specific model from confirmed review images using Coral weight imprinting. Deploy it alongside AIY Birds V1 as a "second opinion" that overrides on species it knows well. Big accuracy win, zero risk — AIY stays as the safety net.

## Architecture

```
Image → YOLO (find bird) → crop 224x224
    ↓
    ├── AIY Birds V1 (964 species, generalist)
    │     prediction + confidence
    │
    ├── Yard Model (your species, specialist)
    │     prediction + confidence
    │
    └── Pick Winner
          yard > 70% → use yard model
          yard < 70%, AIY > 80% → use AIY
          both low → flag uncertain
```

### Why Two Models (not replace)

- AIY knows 964 species → catches rare visitors (Pileated Woodpecker)
- Yard model knows ~20 species → deadly accurate on YOUR common birds
- If yard model is bad → AIY catches it. Zero risk.
- Yard model runs on the same Coral USB → negligible extra latency

## Training Pipeline

### Step 1: Export Training Data

Pull from confirmed reviews + classified images:

```python
for species in confirmed_species:
    images = get_confirmed_images(species)  # review verdict='correct'
    for img_path in images:
        crop = crop_bird(img_path, bounding_box)  # 224x224
        save to training_data/{species}/
```

Minimum 15 images per species, target 20+. Skip species with fewer than 15.

For "wrong" verdicts with `correct_species` set, locate the image file under `classified/{original_wrong_species}/` and map it to the corrected species for training. Reuse logic from `export_yolo_dataset.py` which already handles this file lookup.

### Step 2: Weight Imprinting

Uses Coral's `ImprintingEngine` with the MobileNet V1 L2Norm model (already tested, works on our Coral USB):

```python
from pycoral.learn.imprinting.engine import ImprintingEngine

engine = ImprintingEngine(base_model_path, keep_classes=False)

for species_name, images in training_data.items():
    engine.train(images, class_id=species_id)

engine.save(output_model_path)
```

Takes seconds on the Coral USB. No GPU needed.

### Step 3: Test

Hold out 20% of images as test set. Run both models on the test set:

```
Species          | AIY Accuracy | Yard Accuracy | Winner
Song Sparrow     | 71%          | 95%           | Yard
Downy Woodpecker | 80%          | 92%           | Yard
House Finch      | 83%          | 97%           | Yard
Pileated Woodpecker | 85%       | N/A           | AIY (not in yard model)
```

### Step 4: Deploy

Save yard model to `models/yard_model.tflite` + `models/yard_model_labels.txt`. The classifier loads both models at startup.

## Classifier Integration

In `classify.py`, changes in the classification step:

### Confidence Calibration

AIY Birds V1 returns raw integer scores (0-255 quantized logits). The ImprintingEngine model returns L2-normalized cosine similarity scores (0.0-1.0 range). These CANNOT be compared directly. Both must be normalized before the pick-winner logic:

- AIY scores: apply softmax over top-3 to get probabilities
- Yard scores: already in probability space from L2-norm, use directly

Threshold calibration happens during Step 3 (test phase): run both models on the holdout set and find the optimal confidence threshold for each by maximizing accuracy. Store thresholds in a config file, not hardcoded.

### Label Mapping

The yard model uses canonical common names (e.g., "Song Sparrow") matching `normalize_species()` output. The label file `yard_model_labels.txt` is written during training with the same names used in the review system. AIY predictions are already normalized through `parse_label()` → `normalize_species()`. Both models output the same label space after normalization.

### Pick Winner Logic

```python
# After YOLO detection and bird crop:
aiy_preds = aiy_classifier.classify(crop)
aiy_conf = softmax(aiy_preds[:3])  # normalize to probabilities

yard_preds = yard_classifier.classify(crop) if yard_classifier else None

# Pick winner (thresholds calibrated during training)
if yard_preds and yard_preds[0].confidence > YARD_THRESHOLD:
    final_species = yard_preds[0].common_name
    model_source = "yard"
elif aiy_conf[0] > AIY_THRESHOLD:
    final_species = aiy_preds[0].common_name
    model_source = "aiy"
else:
    final_species = aiy_preds[0].common_name
    model_source = "aiy_uncertain"
```

Result dict gets a `model_source` field stored in `extra_json` (no schema migration needed).

## Dashboard Training Tab

New tab between "Audio" and "Review":

### Species Readiness
| Species | Confirmed | Status |
|---------|-----------|--------|
| Song Sparrow | 79/20 | Ready |
| Downy Woodpecker | 18/20 | Almost |
| European Starling | 2/20 | Needs review |

### Controls
- **"Train Model"** button — starts training, shows spinner + log
- **Results card** — per-species accuracy comparison after training
- **"Deploy"** button — activates yard model
- **Status line** — "Active: AIY Birds V1 + Yard Model v2 (trained Mar 27)"

### API Endpoints
- `GET /api/training/status` — readiness per species, active model info
- `POST /api/training/start` — kick off training (background job)
- `GET /api/training/progress` — SSE stream of training progress
- `GET /api/training/results` — accuracy comparison after training
- `POST /api/training/deploy` — activate the trained model

## Files

| File | Action |
|------|--------|
| `train_yard_model.py` | Create — training pipeline (export, imprint, test) |
| `yard_classifier.py` | Create — wrapper for yard model inference |
| `classify.py` | Modify — dual-model classification |
| `dashboard/api.py` | Modify — training endpoints |
| `dashboard/index.html` | Modify — Training tab |
| `models/yard_model.tflite` | Generated — trained model |
| `models/yard_model_labels.txt` | Generated — species labels |
| `models/mobilenet_v1_1.0_224_l2norm_quant_edgetpu.tflite` | Add — base imprinting model |

## Success Criteria

1. Yard model trains in under 60 seconds on Coral USB
2. Per-species accuracy improves by 10%+ on confused species (sparrows, woodpeckers)
3. No accuracy regression on species the yard model doesn't know (AIY handles them)
4. Training can be triggered from the dashboard with one click
5. Model can be rolled back to AIY-only with one click

## Coral USB Contention

The Coral USB is a single-device resource. During training, the live classifier cannot use it.

**Handling:**
1. Training endpoint sets a flag file (`/tmp/yard-model-training.lock`)
2. `classify.py` checks the flag before each inference — if set, skips Coral classification and queues images in `incoming/` (they accumulate safely)
3. Training completes in <60 seconds, flag is removed
4. Classifier resumes and processes the backlog
5. Timeout: if flag exists for >5 minutes, classifier ignores it (training assumed crashed)

## Rollback Mechanism

**File versioning:**
- `models/yard_model.tflite` — current active yard model
- `models/yard_model_prev.tflite` — previous version (auto-backed up before each training)
- `models/yard_model_labels.txt` — current labels
- `models/yard_model_prev_labels.txt` — previous labels

**Rollback API:** `POST /api/training/rollback`
- Swaps current ↔ prev model files
- Restarts classifier
- If no prev model exists, removes yard model entirely (AIY-only mode)

**Graceful missing model:** The classifier checks if `yard_model.tflite` exists at startup. If not, runs AIY-only mode — no crash, no error, just logs "Yard model not found, using AIY only."

## Test Methodology

With ~500 images across 16 species, a fixed 20% holdout gives too few samples per species for meaningful evaluation. Instead:

**Leave-one-out cross-validation:** For each species, train on all-but-one image, test on the held-out image, repeat. This maximizes both training and test data. Per-species accuracy is the fraction of held-out images correctly classified.

**Pre-deploy preview:** After training, automatically run the yard model on the last 100 live-classified images and show a "what would have changed" comparison — catches real-world drift that holdout sets miss.

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Yard model is worse than AIY | Calibrated confidence thresholds ensure AIY wins when yard isn't sure |
| Too few images for a species | Minimum 15 images enforced, species below threshold skipped |
| Coral USB busy during training | Lock file pauses classifier, 5-min timeout watchdog |
| Model file corruption | Previous model auto-backed up, rollback endpoint |
| Confidence scales don't match | Softmax normalization on AIY, thresholds calibrated on test set |
| Labels don't match between models | Both use normalize_species() canonical names |
| Imprinting model less accurate than full transfer learning | Phase 2: TF Lite Model Maker for deeper retraining if needed |
