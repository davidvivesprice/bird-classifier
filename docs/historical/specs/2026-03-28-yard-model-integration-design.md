> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Yard Model Integration — Design Spec

## Problem

The trained yard model (`models/yard_model.tflite`, 44 species) exists but isn't being used. The classifier runs AIY-only. This spec covers integrating the yard model as a second opinion alongside AIY Birds V1.

**Scope:** Classifier integration only. Dashboard Training tab, Coral contention during training, and "start fresh" UI are separate specs.

## Goal

Run both AIY (generalist, 964 species) and yard model (specialist, 44 species trained on your cameras) on every bird crop. Pick the winner using calibrated confidence thresholds. Store which model won for auditability.

## Architecture

```
crop 224x224
  → AIY classifier (Coral Edge TPU)
      → raw int scores 0-255 → softmax top-3 → probabilities
  → Yard classifier (Coral Edge TPU)
      → L2-norm scores 0.0-1.0 → deduplicate aliases → top-3
  → Pick winner (calibrated thresholds)
  → result dict with model_source field
```

Both models run on the same Coral USB Accelerator, sequentially (not concurrent). YOLO detection runs on CoreML (iMac GPU/CPU) — no Coral contention with either classifier.

### Why Two Models (not replace)

- AIY knows 964 species — catches rare visitors the yard model has never seen
- Yard model knows 44 species — trained on YOUR cameras, YOUR lighting, YOUR feeder
- If yard model is wrong → AIY catches it. Zero risk.
- Extra latency: ~5ms per crop. Irrelevant at 10-second polling.

## Files

| File | Action | Purpose |
|------|--------|---------|
| `yard_classifier.py` | Create | Wrapper: load yard model on Coral, classify, handle labels |
| `classify.py` | Modify | Dual-model: run both classifiers, pick winner |
| `probe_yard_model.py` | Create | Verification script: validate scores, Coral sharing, labels |

No changes to: `bird_inference.py`, `dashboard/`, `train_yard_model.py`, DB schema.

## New File: `yard_classifier.py`

### Responsibilities

1. Load `yard_model.tflite` on Coral via `pycoral.utils.edgetpu.make_interpreter`
2. Load `yard_model_labels.txt`, normalize all labels via `normalize_species()`
3. Deduplicate alias pairs: when two classes map to the same canonical name (e.g. "Feral Pigeon" + "Rock Pigeon" → "Rock Pigeon"), sum their scores before ranking
4. Filter `not a bird` class (class 43) from results — yard model abstains
5. Return top-3 predictions as list of dicts matching `SpeciesClassifier` output format

### Interface

```python
class YardClassifier:
    def __init__(self, model_path, labels_path):
        """Load model on Coral. Raises if Coral unavailable."""

    def classify(self, crop):
        """Classify a bird crop (PIL Image or numpy array).

        Returns list of dicts: [{"common_name": str, "scientific_name": str, "confidence": float}, ...]
        Top-3, sorted by confidence descending.
        scientific_name is empty string (yard model doesn't know scientific names).
        Returns empty list if model abstains (top = "not a bird").
        """

    @property
    def enabled(self):
        """Whether the yard model is active. Can be set to False to pause."""
```

### Label Deduplication

At load time:
1. Read all labels from `yard_model_labels.txt`
2. Run each through `normalize_species()` → canonical name
3. Build `canonical_to_class_ids` mapping: `{"Rock Pigeon": [17, 33], "Dark-eyed Junco": [13, 37], ...}`
4. During inference: sum scores for classes sharing a canonical name

Known alias pairs in current model:
- "Feral Pigeon" (class 17) + "Rock Pigeon" (class 33) → "Rock Pigeon"
- "Slate-colored Junco" (class 37) + "Dark-eyed Junco" (class 13) → "Dark-eyed Junco"

## Score Calibration (Updated from Probe Results)

**Probe findings (2026-03-28):** Both models return float32 integer-valued scores, NOT what the original spec assumed.

- **AIY:** float32, shape (965,), values 0-151 observed. Very sparse — most classes score 0, winner scores 19-151.
- **Yard model:** float32, shape (44,), values 3-12 observed. Very compressed — all classes score 3+, winner scores 8-12.

**Normalization:** Apply softmax to BOTH models' top-N scores to get comparable probabilities. This handles the different score ranges automatically — softmax turns "big gap = confident" into high probability regardless of absolute scale.

```python
# Both models: softmax over top scores → probabilities
aiy_probs = softmax(aiy_top3_scores)    # e.g. [0.85, 0.10, 0.05]
yard_probs = softmax(yard_top3_scores)  # e.g. [0.72, 0.18, 0.10]
```

**`not a bird` class:** Probe showed it's unreliable as a gate (scores 4-7, same range as real species). Strategy: filter it from results before softmax. If after filtering, yard model's top probability is below threshold, yard abstains.

## Pick-Winner Logic

```python
if yard_result and yard_result[0]["confidence"] >= YARD_THRESHOLD:
    winner = yard_result[0]
    model_source = "yard"
elif aiy_confidence >= AIY_THRESHOLD:
    winner = aiy_result
    model_source = "aiy"
else:
    winner = aiy_result
    model_source = "aiy_uncertain"

# Special case: both models agree on species
if yard_result and aiy_species == yard_result[0]["common_name"]:
    model_source = "both_agree"
```

Starting thresholds (updated from probe results — both models use softmax now):
- `YARD_THRESHOLD = 0.45` — yard model softmax probability must exceed this to override (yard scores are compressed 3-12, so softmax spreads are narrower)
- `AIY_THRESHOLD = 0.50` — AIY softmax probability when yard defers

These are constants at the top of `yard_classifier.py`, not hardcoded in logic. Will be tuned further based on real-world accuracy.

### When yard model abstains

If yard model is disabled/missing/errors, or if its top softmax probability is below YARD_THRESHOLD after filtering `not a bird` — AIY wins. `model_source = "aiy_only"`.

Note: `not a bird` class is filtered from scores before softmax (probe showed it's unreliable as a gate — scores 4-7, same range as real species).

## Coral Device Sharing

Both AIY and yard model load as separate `make_interpreter()` instances on the same Coral USB. Sequential inference (never concurrent) — pycoral supports this.

**Must verify:** The probe script loads both interpreters and runs inference back-to-back to confirm no device conflicts.

## Error Handling

Every failure mode degrades gracefully to AIY-only:

| Failure | Behavior | model_source |
|---------|----------|-------------|
| `yard_model.tflite` missing at startup | Log info, skip yard model entirely | `aiy_only` |
| Coral unavailable for yard model | Log warning, AIY-only mode | `aiy_only` |
| Yard model inference throws per-crop | Catch, log warning, use AIY for that crop | `aiy_only` |
| Yard model scores all near zero | Yard abstains via threshold, AIY wins | `aiy` |

**No crash paths.** The classifier never stops working because of the yard model.

## What Gets Stored

New fields in the result dict (stored in `extra_json` column, no schema migration):

```python
"model_source": "yard" | "aiy" | "both_agree" | "aiy_uncertain" | "aiy_only"
"yard_prediction": {"species": "Song Sparrow", "confidence": 0.85}
"aiy_confidence": 0.72  # softmax-normalized
```

This enables:
- Dashboard showing which model identified each bird
- Auditing disagreements between models
- Tuning thresholds based on real-world accuracy

## Probe Script: `probe_yard_model.py`

Run before writing production code. Verifies all assumptions:

1. **Score ranges** — Load both models, run same crop, print raw outputs
2. **Duplicate labels** — Run all 44 labels through `normalize_species()`, print collisions
3. **`not a bird` class** — Verify class ID and score behavior
4. **Two interpreters on one Coral** — Load both, run inference back-to-back
5. **Score distribution** — Run on 20 recent classified images, print score histograms

Results inform final threshold values.

## Modifications to `classify.py`

### At startup (in `main()`)

After loading `_classifier` (AIY), attempt to load yard model:

```python
global _yard_classifier
try:
    from yard_classifier import YardClassifier
    yard_model = MODEL_DIR / "yard_model.tflite"
    yard_labels = MODEL_DIR / "yard_model_labels.txt"
    if yard_model.exists() and yard_labels.exists():
        _yard_classifier = YardClassifier(str(yard_model), str(yard_labels))
        logging.info("Yard model loaded: %d species", len(_yard_classifier.labels))
    else:
        _yard_classifier = None
        logging.info("Yard model not found, AIY-only mode")
except Exception as e:
    _yard_classifier = None
    logging.warning("Could not load yard model: %s — AIY-only mode", e)
```

### In `process_file()`, after AIY classification

For each detected bird crop, also run yard classifier. Apply pick-winner logic. Store `model_source` and yard prediction in result dict.

### Impact on existing features

- **Yard prior** — runs after pick-winner, same as before
- **Visit voter** — runs after pick-winner, same as before
- **Range filter** — runs after pick-winner, same as before
- **Annotation** — shows winner species, no change needed
- **Review system** — sees final species, can inspect `extra_json` for model_source

No breaking changes to any existing feature.

## Future Work (not in this spec)

- Dashboard Training tab with "Train Model" and "Start Fresh" buttons
- Coral USB contention handling during training (lock file)
- Threshold auto-tuning from review feedback
- Training tab showing per-species accuracy comparison

## Verified Results (2026-03-28)

All probe results and smoke tests confirmed:

- **Label collisions:** 2 found as expected — Rock Pigeon (classes 17+33), Dark-eyed Junco (classes 13+37). Handled by score summing.
- **Two Coral interpreters:** Confirmed working in same process. Process-level lock only (one process at a time).
- **AIY scores:** float32 integers 0-151 (not 0-255 uint8 as originally assumed)
- **Yard scores:** float32 integers 3-12 (not 0.0-1.0 as originally assumed). Same scale type as AIY.
- **Softmax normalization:** Applied to both models for comparable probabilities. Works correctly.
- **`not a bird` class:** Index 43, scores 4-7 (same range as real species). Filtered before softmax.
- **Smoke test:** Yard model overrode AIY's "Black-capped Chickadee" with "Dark-eyed Junco" on first real image. `model_source=yard` stored in SQLite with `yard_prediction.confidence=0.8756`.
- **AIY-only fallback:** Verified — no crash when model files missing.
- **Production service:** Running in watch mode with dual-model active.

## Success Criteria

1. ~~Yard model loads alongside AIY at startup with no errors~~ DONE
2. ~~Both models run on every crop, pick-winner selects correctly~~ DONE
3. ~~`model_source` stored in every classification result~~ DONE
4. ~~Missing yard model = AIY-only mode, no crash~~ DONE
5. ~~Yard model inference error = graceful fallback to AIY for that crop~~ DONE
6. ~~No regression: existing intelligence layers (yard prior, visit voter, range filter) unaffected~~ DONE
