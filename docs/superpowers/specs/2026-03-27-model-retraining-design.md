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

Minimum 10 images per species, target 20+. Skip species with fewer than 10.

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

In `classify.py`, ~20 lines of change in the classification step:

```python
# After YOLO detection and bird crop:
aiy_predictions = aiy_classifier.classify(crop)
yard_predictions = yard_classifier.classify(crop)  # new

# Pick winner
if yard_predictions and yard_predictions[0].confidence > 0.70:
    final = yard_predictions[0]
    final.source = "yard_model"
elif aiy_predictions[0].confidence > 0.80:
    final = aiy_predictions[0]
    final.source = "aiy"
else:
    final = aiy_predictions[0]
    final.source = "aiy_uncertain"
```

Result dict gets a `model_source` field so the dashboard can show which model made the call.

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

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Yard model is worse than AIY | Confidence threshold ensures AIY wins when yard isn't sure |
| Too few images for a species | Minimum 10 images enforced, species below threshold skipped |
| Coral USB busy during training | Training is a one-time batch job, classifier pauses briefly |
| Model file corruption | Old model kept as backup, rollback button in UI |
| Imprinting model less accurate than full transfer learning | Phase 2: TF Lite Model Maker for deeper retraining if needed |
