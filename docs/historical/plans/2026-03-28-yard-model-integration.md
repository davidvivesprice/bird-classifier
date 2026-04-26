> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Yard Model Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the trained yard model alongside AIY Birds V1 on every bird crop, pick the winner, and store which model decided.

**Architecture:** New `yard_classifier.py` wraps the yard model TFLite on Coral Edge TPU. `classify.py` loads both models at startup, runs both on each crop, applies pick-winner logic with calibrated confidence thresholds. Graceful fallback to AIY-only if yard model is missing or errors.

**Tech Stack:** pycoral 2.0, tflite-runtime 2.5, numpy, PIL. Python 3.9 (venv-coral).

**Spec:** `docs/superpowers/specs/2026-03-28-yard-model-integration-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `yard_classifier.py` | Create | Load yard model on Coral, classify crops, handle labels/aliases/dedup |
| `classify.py` | Modify | Dual-model pipeline: run both classifiers, pick winner, store model_source |
| `probe_yard_model.py` | Create | One-time verification script: score ranges, Coral sharing, label collisions |
| `tests/test_yard_classifier.py` | Create | Unit tests for YardClassifier |
| `tests/conftest.py` | Modify | Add yard model fixtures |

---

### Task 1: Probe Script — Verify All Assumptions

Before writing any production code, verify the 5 unknowns on the live system.

**Files:**
- Create: `probe_yard_model.py`

- [ ] **Step 1: Write the probe script**

```python
#!/usr/bin/env python3
"""Probe script: verify yard model assumptions before integration.

Run this ONCE on the live system to confirm:
1. Score ranges for both models
2. Label collisions after normalize_species()
3. 'not a bird' class behavior
4. Two Coral interpreters on one USB device
5. Score distributions on real images
"""

import sys
import numpy as np
from pathlib import Path
from PIL import Image

# Must run in venv-coral
from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
from pycoral.adapters import common as coral_common

from bird_inference import (
    SpeciesClassifier, normalize_species, parse_label, crop_bird,
    YOLODetector,
)

MODELS_DIR = Path("/Users/vives/bird-classifier/models")
CLASSIFIED_DIR = Path("/Users/vives/bird-snapshots/classified")

YARD_MODEL = MODELS_DIR / "yard_model.tflite"
YARD_LABELS = MODELS_DIR / "yard_model_labels.txt"
AIY_ONNX = MODELS_DIR / "aiy_birds_v1.onnx"
AIY_TPU = MODELS_DIR / "aiy_birds_v1_edgetpu.tflite"
AIY_LABELS = MODELS_DIR / "inat_bird_labels.txt"
YOLO_MODEL = MODELS_DIR / "yolov8n_bird.onnx"
REGIONAL_SPECIES = MODELS_DIR / "chilmark_feeder_species.txt"


def probe_1_label_collisions():
    """Check which yard labels collide after normalization."""
    print("\n" + "=" * 60)
    print("PROBE 1: Label Collisions")
    print("=" * 60)

    labels = YARD_LABELS.read_text().strip().splitlines()
    canonical = {}
    for i, label in enumerate(labels):
        norm = normalize_species(label)
        canonical.setdefault(norm, []).append((i, label))

    collisions = {k: v for k, v in canonical.items() if len(v) > 1}
    if collisions:
        print(f"Found {len(collisions)} collision(s):")
        for canon, pairs in collisions.items():
            print(f"  '{canon}' <- {pairs}")
    else:
        print("No collisions found.")

    # Check 'not a bird' class
    for i, label in enumerate(labels):
        if "not a bird" in label.lower():
            print(f"\n'not a bird' class: index={i}, label='{label}'")

    print(f"\nTotal labels: {len(labels)}")
    return labels


def probe_2_two_interpreters():
    """Load both AIY and yard model interpreters on the same Coral."""
    print("\n" + "=" * 60)
    print("PROBE 2: Two Interpreters on One Coral")
    print("=" * 60)

    tpus = list_edge_tpus()
    print(f"Edge TPUs found: {len(tpus)}")
    if not tpus:
        print("FAIL: No Edge TPU detected")
        return False

    print("Loading AIY interpreter...")
    aiy_interp = make_interpreter(str(AIY_TPU))
    aiy_interp.allocate_tensors()
    print(f"  AIY input: {aiy_interp.get_input_details()[0]['shape']}, dtype={aiy_interp.get_input_details()[0]['dtype']}")
    print(f"  AIY output: {aiy_interp.get_output_details()[0]['shape']}, dtype={aiy_interp.get_output_details()[0]['dtype']}")

    print("Loading yard interpreter...")
    yard_interp = make_interpreter(str(YARD_MODEL))
    yard_interp.allocate_tensors()
    print(f"  Yard input: {yard_interp.get_input_details()[0]['shape']}, dtype={yard_interp.get_input_details()[0]['dtype']}")
    print(f"  Yard output: {yard_interp.get_output_details()[0]['shape']}, dtype={yard_interp.get_output_details()[0]['dtype']}")

    # Run a dummy inference on each
    dummy = np.zeros((224, 224, 3), dtype=np.uint8)

    coral_common.set_input(aiy_interp, dummy)
    aiy_interp.invoke()
    aiy_scores = np.array(coral_common.output_tensor(aiy_interp, 0), dtype=np.float32)
    print(f"\n  AIY dummy inference OK. Output shape: {aiy_scores.shape}, range: [{aiy_scores.min():.4f}, {aiy_scores.max():.4f}]")

    coral_common.set_input(yard_interp, dummy)
    yard_interp.invoke()
    yard_scores = np.array(coral_common.output_tensor(yard_interp, 0), dtype=np.float32)
    print(f"  Yard dummy inference OK. Output shape: {yard_scores.shape}, range: [{yard_scores.min():.4f}, {yard_scores.max():.4f}]")

    print("\nSUCCESS: Both interpreters loaded and ran on same Coral USB.")
    return True


def probe_3_score_ranges(labels):
    """Run both models on real bird images and compare score ranges."""
    print("\n" + "=" * 60)
    print("PROBE 3: Score Ranges on Real Images")
    print("=" * 60)

    # Find 10 real classified images
    images = []
    for species_dir in sorted(CLASSIFIED_DIR.iterdir()):
        if not species_dir.is_dir():
            continue
        for img_path in sorted(species_dir.glob("*.jpg"))[:2]:
            images.append((species_dir.name, img_path))
        if len(images) >= 10:
            break

    if not images:
        print("FAIL: No classified images found")
        return

    # Load models
    detector = YOLODetector(str(YOLO_MODEL), confidence=0.3)

    regional = set()
    if REGIONAL_SPECIES.exists():
        regional = {l.strip() for l in REGIONAL_SPECIES.read_text().splitlines() if l.strip()}

    aiy = SpeciesClassifier(
        str(AIY_ONNX), str(AIY_LABELS),
        regional_species=regional,
        tpu_model_path=str(AIY_TPU),
    )

    yard_interp = make_interpreter(str(YARD_MODEL))
    yard_interp.allocate_tensors()

    print(f"\nRunning on {len(images)} images...\n")
    print(f"{'Image':<40} {'AIY Species':<25} {'AIY Raw':>7} {'AIY Softmax':>11} {'Yard Species':<25} {'Yard Score':>10}")
    print("-" * 125)

    for species_dir_name, img_path in images:
        img = Image.open(img_path).convert("RGB")
        dets = detector.detect(img)
        if not dets:
            continue

        crop = crop_bird(img, dets[0]["box"]).resize((224, 224))
        crop_arr = np.array(crop, dtype=np.uint8)

        # AIY
        preds, raw = aiy.classify(crop)
        aiy_top = preds[0] if preds else {"common_name": "?", "raw_score": 0}
        aiy_raw_scores = [p["raw_score"] for p in raw[:3]]
        # Softmax on top 3
        exp_scores = np.exp(np.array(aiy_raw_scores, dtype=np.float32))
        softmax_scores = exp_scores / exp_scores.sum()

        # Yard
        coral_common.set_input(yard_interp, crop_arr)
        yard_interp.invoke()
        yard_scores = np.array(coral_common.output_tensor(yard_interp, 0), dtype=np.float32)
        if yard_scores.ndim == 2:
            yard_scores = yard_scores[0]

        yard_top_idx = int(np.argmax(yard_scores))
        yard_top_label = labels[yard_top_idx] if yard_top_idx < len(labels) else "?"
        yard_top_score = float(yard_scores[yard_top_idx])

        print(f"{img_path.name:<40} {aiy_top['common_name']:<25} {aiy_top['raw_score']:>7} {softmax_scores[0]:>10.4f} {normalize_species(yard_top_label):<25} {yard_top_score:>10.4f}")

        img.close()
        crop.close()

    print("\n--- AIY score type: integers (0-255 quantized logits)")
    print("--- Yard score type: see output above (expected: 0.0-1.0 L2-norm)")
    print("--- If yard scores are NOT in 0.0-1.0, the spec's calibration approach needs revision.")


def main():
    print("Yard Model Integration — Probe Script")
    print("=" * 60)

    for path, name in [
        (YARD_MODEL, "Yard model"),
        (YARD_LABELS, "Yard labels"),
        (AIY_TPU, "AIY TPU model"),
        (AIY_ONNX, "AIY ONNX model"),
        (AIY_LABELS, "AIY labels"),
        (YOLO_MODEL, "YOLO model"),
    ]:
        status = "OK" if path.exists() else "MISSING"
        print(f"  {name}: {status} ({path})")

    labels = probe_1_label_collisions()
    ok = probe_2_two_interpreters()
    if ok:
        probe_3_score_ranges(labels)

    print("\n" + "=" * 60)
    print("PROBES COMPLETE — review output above before proceeding.")
    print("=" * 60)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the probe script on the live system**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python probe_yard_model.py`

Expected output:
- Probe 1: Two collisions (Feral Pigeon/Rock Pigeon, Slate-colored Junco/Dark-eyed Junco) + `not a bird` at index 43
- Probe 2: Both interpreters load and run on same Coral USB
- Probe 3: AIY raw scores as integers 0-255, yard scores in some range (verify 0.0-1.0)

**CRITICAL:** Read the output carefully. If yard scores are NOT in 0.0-1.0 range, the softmax/threshold approach in the spec needs revision. Update the spec before continuing.

- [ ] **Step 3: Record probe results**

Add a comment at the top of `probe_yard_model.py` with the actual results:

```python
# PROBE RESULTS (run date: YYYY-MM-DD):
# - Label collisions: [list actual collisions]
# - Two interpreters: [OK/FAIL]
# - AIY score range: [actual range]
# - Yard score range: [actual range]
# - Threshold recommendation: yard >= X, aiy_softmax >= Y
```

- [ ] **Step 4: Commit**

```bash
git add probe_yard_model.py
git commit -m "feat: add probe script to verify yard model assumptions before integration"
```

---

### Task 2: YardClassifier — Core Class with Tests

**Files:**
- Create: `yard_classifier.py`
- Create: `tests/test_yard_classifier.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add yard model fixtures to conftest.py**

Add to `tests/conftest.py` after the existing `regional_species` fixture:

```python
@pytest.fixture(scope="session")
def yard_model_path(models_dir: Path) -> Path:
    """Path to the yard model TFLite file."""
    path = models_dir / "yard_model.tflite"
    if not path.exists():
        pytest.skip(f"Yard model not found: {path}")
    return path


@pytest.fixture(scope="session")
def yard_labels_path(models_dir: Path) -> Path:
    """Path to the yard model labels file."""
    path = models_dir / "yard_model_labels.txt"
    if not path.exists():
        pytest.skip(f"Yard labels not found: {path}")
    return path
```

- [ ] **Step 2: Write failing tests for YardClassifier**

Create `tests/test_yard_classifier.py`:

```python
"""Tests for yard_classifier — yard-specific bird classifier wrapper."""

import numpy as np
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bird_inference import normalize_species


# ── Label handling (no Coral needed) ──────────────────────────────────────

class TestLabelLoading:
    """Test label parsing, normalization, and deduplication logic."""

    def test_normalize_labels(self):
        """All yard labels should normalize without error."""
        from yard_classifier import _normalize_labels

        labels = [
            "American Robin",
            "Feral Pigeon",
            "Rock Pigeon",
            "Slate-colored Junco",
            "Dark-eyed Junco",
            "not a bird",
        ]
        canonical, alias_groups, not_a_bird_ids = _normalize_labels(labels)

        # Feral Pigeon and Rock Pigeon collapse
        assert canonical[1] == "Rock Pigeon"
        assert canonical[2] == "Rock Pigeon"
        # Slate-colored and Dark-eyed collapse
        assert canonical[3] == "Dark-eyed Junco"
        assert canonical[4] == "Dark-eyed Junco"
        # American Robin stays
        assert canonical[0] == "American Robin"

    def test_alias_groups_detected(self):
        """Alias groups should map canonical name to list of class IDs."""
        from yard_classifier import _normalize_labels

        labels = ["Feral Pigeon", "Rock Pigeon", "Song Sparrow"]
        canonical, alias_groups, not_a_bird_ids = _normalize_labels(labels)

        assert "Rock Pigeon" in alias_groups
        assert set(alias_groups["Rock Pigeon"]) == {0, 1}
        assert "Song Sparrow" not in alias_groups  # only 1 class, no alias

    def test_not_a_bird_detected(self):
        """'not a bird' class should be identified."""
        from yard_classifier import _normalize_labels

        labels = ["Song Sparrow", "not a bird"]
        canonical, alias_groups, not_a_bird_ids = _normalize_labels(labels)

        assert 1 in not_a_bird_ids

    def test_not_a_bird_case_insensitive(self):
        """'Not A Bird' and 'not a bird' should both be caught."""
        from yard_classifier import _normalize_labels

        labels = ["Not A Bird"]
        canonical, alias_groups, not_a_bird_ids = _normalize_labels(labels)

        assert 0 in not_a_bird_ids


class TestScoreDeduplication:
    """Test score merging for alias classes."""

    def test_merge_alias_scores(self):
        """Scores for alias classes should be summed."""
        from yard_classifier import _merge_alias_scores

        scores = np.array([0.3, 0.4, 0.2, 0.1], dtype=np.float32)
        canonical = ["Rock Pigeon", "Rock Pigeon", "Song Sparrow", "not a bird"]
        alias_groups = {"Rock Pigeon": [0, 1]}
        not_a_bird_ids = {3}

        merged = _merge_alias_scores(scores, canonical, alias_groups, not_a_bird_ids)

        # Rock Pigeon: 0.3 + 0.4 = 0.7
        assert merged["Rock Pigeon"] == pytest.approx(0.7, abs=1e-5)
        # Song Sparrow: 0.2 unchanged
        assert merged["Song Sparrow"] == pytest.approx(0.2, abs=1e-5)
        # not a bird: filtered out
        assert "not a bird" not in merged

    def test_empty_scores(self):
        """Empty scores should return empty dict."""
        from yard_classifier import _merge_alias_scores

        scores = np.array([], dtype=np.float32)
        merged = _merge_alias_scores(scores, [], {}, set())
        assert merged == {}


class TestSoftmax:
    """Test AIY score normalization."""

    def test_softmax_basic(self):
        """Softmax should produce probabilities summing to 1."""
        from yard_classifier import softmax_top3

        raw_scores = [200, 150, 100]
        probs = softmax_top3(raw_scores)

        assert len(probs) == 3
        assert sum(probs) == pytest.approx(1.0, abs=1e-5)
        assert probs[0] > probs[1] > probs[2]

    def test_softmax_preserves_order(self):
        """Highest raw score should have highest probability."""
        from yard_classifier import softmax_top3

        probs = softmax_top3([255, 100, 50])
        assert probs[0] > probs[1] > probs[2]

    def test_softmax_identical_scores(self):
        """Equal scores should produce equal probabilities."""
        from yard_classifier import softmax_top3

        probs = softmax_top3([100, 100, 100])
        assert probs[0] == pytest.approx(probs[1], abs=1e-5)
        assert probs[1] == pytest.approx(probs[2], abs=1e-5)


# ── Integration tests (require Coral + models) ───────────────────────────

class TestYardClassifierIntegration:
    """Integration tests that require the Coral USB and model files."""

    def test_loads_without_error(self, yard_model_path, yard_labels_path):
        """YardClassifier should load model and labels."""
        from yard_classifier import YardClassifier

        yc = YardClassifier(str(yard_model_path), str(yard_labels_path))
        assert len(yc.labels) == 44
        assert yc.enabled is True

    def test_classify_returns_top3(self, yard_model_path, yard_labels_path, test_bird_image_pil):
        """classify() should return up to 3 predictions sorted by confidence."""
        from yard_classifier import YardClassifier

        yc = YardClassifier(str(yard_model_path), str(yard_labels_path))
        results = yc.classify(test_bird_image_pil)

        # Could be empty if model says "not a bird", otherwise up to 3
        assert isinstance(results, list)
        if results:
            assert len(results) <= 3
            assert "common_name" in results[0]
            assert "scientific_name" in results[0]
            assert "confidence" in results[0]
            # Sorted descending
            for i in range(len(results) - 1):
                assert results[i]["confidence"] >= results[i + 1]["confidence"]

    def test_classify_no_crash_on_small_image(self, yard_model_path, yard_labels_path):
        """Should handle a tiny image without crashing."""
        from yard_classifier import YardClassifier
        from PIL import Image

        yc = YardClassifier(str(yard_model_path), str(yard_labels_path))
        tiny = Image.new("RGB", (10, 10), (128, 128, 128))
        results = yc.classify(tiny)
        tiny.close()
        assert isinstance(results, list)

    def test_enabled_flag_skips_inference(self, yard_model_path, yard_labels_path, test_bird_image_pil):
        """When enabled=False, classify() returns empty list."""
        from yard_classifier import YardClassifier

        yc = YardClassifier(str(yard_model_path), str(yard_labels_path))
        yc.enabled = False
        results = yc.classify(test_bird_image_pil)
        assert results == []

    def test_not_a_bird_filtered(self, yard_model_path, yard_labels_path):
        """'not a bird' should never appear in results."""
        from yard_classifier import YardClassifier
        from PIL import Image

        yc = YardClassifier(str(yard_model_path), str(yard_labels_path))
        # Pure noise image — likely to trigger 'not a bird' or low scores
        noise = Image.fromarray(np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8))
        results = yc.classify(noise)
        noise.close()
        for r in results:
            assert "not a bird" not in r["common_name"].lower()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_yard_classifier.py -v`
Expected: FAIL — `yard_classifier` module doesn't exist yet.

- [ ] **Step 4: Implement `yard_classifier.py`**

Create `yard_classifier.py`:

```python
"""Yard-specific bird classifier — runs alongside AIY Birds V1.

Loads the yard model (trained via Coral weight imprinting) on the Edge TPU
and classifies bird crops. Handles label normalization, alias deduplication,
and 'not a bird' filtering.

Used by classify.py as a second opinion: yard model wins for species it
knows well, AIY catches everything else.
"""

import logging
from pathlib import Path

import numpy as np
from PIL import Image

from bird_inference import normalize_species

log = logging.getLogger(__name__)

# Confidence thresholds for pick-winner logic.
# Tuned from probe_yard_model.py results. Update after re-probing.
YARD_THRESHOLD = 0.70
AIY_THRESHOLD = 0.60


def _normalize_labels(labels):
    """Normalize yard model labels and detect aliases + 'not a bird' classes.

    Returns:
        canonical: list[str] — normalized name per class ID
        alias_groups: dict[str, list[int]] — canonical name → class IDs (only for aliases)
        not_a_bird_ids: set[int] — class IDs for 'not a bird'
    """
    canonical = []
    name_to_ids = {}
    not_a_bird_ids = set()

    for i, label in enumerate(labels):
        if label.strip().lower() == "not a bird":
            not_a_bird_ids.add(i)
            canonical.append(label.strip())
            continue

        norm = normalize_species(label.strip())
        canonical.append(norm)
        name_to_ids.setdefault(norm, []).append(i)

    # Only create alias groups where multiple class IDs share a canonical name
    alias_groups = {name: ids for name, ids in name_to_ids.items() if len(ids) > 1}

    return canonical, alias_groups, not_a_bird_ids


def _merge_alias_scores(scores, canonical, alias_groups, not_a_bird_ids):
    """Merge scores for alias classes and filter 'not a bird'.

    Returns dict: {canonical_name: summed_score}
    """
    if len(scores) == 0:
        return {}

    merged = {}
    seen_aliases = set()

    for i, score in enumerate(scores):
        if i in not_a_bird_ids:
            continue
        name = canonical[i]

        # If this class is part of an alias group, sum with its partners
        if name in alias_groups and name not in seen_aliases:
            total = sum(float(scores[j]) for j in alias_groups[name])
            merged[name] = total
            seen_aliases.add(name)
        elif name not in alias_groups:
            merged[name] = float(score)
        # else: already handled via alias group

    return merged


def softmax_top3(raw_scores):
    """Apply softmax to top-3 AIY raw scores (integers 0-255).

    Returns list of 3 probabilities summing to 1.0.
    Used to normalize AIY scores into probability space for comparison
    with yard model L2-norm scores.
    """
    arr = np.array(raw_scores[:3], dtype=np.float64)
    # Shift for numerical stability
    arr = arr - arr.max()
    exp = np.exp(arr)
    probs = exp / exp.sum()
    return probs.tolist()


class YardClassifier:
    """Yard-specific bird classifier running on Coral Edge TPU.

    Wraps the yard model trained via weight imprinting. Handles:
    - Label normalization via normalize_species()
    - Alias deduplication (Feral Pigeon + Rock Pigeon → Rock Pigeon)
    - 'not a bird' class filtering
    - Graceful disable via enabled flag
    """

    def __init__(self, model_path, labels_path):
        """Load yard model on Coral Edge TPU.

        Args:
            model_path: Path to yard_model.tflite
            labels_path: Path to yard_model_labels.txt

        Raises:
            RuntimeError: If Coral USB not available
            FileNotFoundError: If model or labels missing
        """
        from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
        from pycoral.adapters import common as coral_common

        if not list_edge_tpus():
            raise RuntimeError("No Coral Edge TPU detected")

        self.labels = Path(labels_path).read_text().strip().splitlines()
        self._canonical, self._alias_groups, self._not_a_bird_ids = _normalize_labels(self.labels)
        self._interp = make_interpreter(str(model_path))
        self._interp.allocate_tensors()
        self._coral_common = coral_common
        self._enabled = True

        log.info("YardClassifier loaded: %d labels, %d aliases, %d 'not a bird' classes",
                 len(self.labels), len(self._alias_groups), len(self._not_a_bird_ids))

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)

    def classify(self, crop):
        """Classify a bird crop image.

        Args:
            crop: PIL Image or numpy array (any size, resized to 224x224)

        Returns:
            List of top-3 predictions sorted by confidence descending:
            [{"common_name": str, "scientific_name": str, "confidence": float}, ...]
            Empty list if disabled or model abstains ('not a bird' is top).
        """
        if not self._enabled:
            return []

        # Resize to 224x224
        if isinstance(crop, np.ndarray):
            crop = Image.fromarray(crop)
        resized = crop.resize((224, 224))
        arr = np.array(resized, dtype=np.uint8)

        # Run inference on Coral
        self._coral_common.set_input(self._interp, arr)
        self._interp.invoke()
        scores = np.array(
            self._coral_common.output_tensor(self._interp, 0), dtype=np.float32
        )
        if scores.ndim == 2:
            scores = scores[0]

        # Merge alias scores and filter 'not a bird'
        merged = _merge_alias_scores(scores, self._canonical, self._alias_groups, self._not_a_bird_ids)

        if not merged:
            return []

        # Sort by score descending, take top 3
        sorted_species = sorted(merged.items(), key=lambda x: x[1], reverse=True)[:3]

        results = []
        for name, score in sorted_species:
            results.append({
                "common_name": name,
                "scientific_name": "",  # yard model doesn't know scientific names
                "confidence": score,
            })

        return results
```

- [ ] **Step 5: Run tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_yard_classifier.py -v`
Expected: All tests PASS (unit tests pass immediately; integration tests pass if Coral USB is connected)

- [ ] **Step 6: Run full test suite to check nothing broke**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -v`
Expected: 150+ tests pass (existing tests unaffected)

- [ ] **Step 7: Commit**

```bash
git add yard_classifier.py tests/test_yard_classifier.py tests/conftest.py
git commit -m "feat: add YardClassifier wrapper for yard model inference on Coral"
```

---

### Task 3: Integrate Dual-Model into classify.py

**Files:**
- Modify: `classify.py:39-46` (imports)
- Modify: `classify.py:64-68` (model paths)
- Modify: `classify.py:93-95` (module globals)
- Modify: `classify.py:499-513` (classification loop)
- Modify: `classify.py:586-627` (result dict)
- Modify: `classify.py:919-935` (model loading in main)

- [ ] **Step 1: Add imports and constants**

In `classify.py`, add after the existing `from visit_voter import check_visit_consensus` line (line 44):

```python
from yard_classifier import YardClassifier, softmax_top3, YARD_THRESHOLD, AIY_THRESHOLD
```

Add model paths after `REGIONAL_SPECIES_PATH` (around line 68):

```python
YARD_MODEL_PATH = MODEL_DIR / "yard_model.tflite"
YARD_LABELS_PATH = MODEL_DIR / "yard_model_labels.txt"
```

Add module-level global after `_classifier = None` (around line 95):

```python
_yard_classifier = None    # type: YardClassifier | None
```

- [ ] **Step 2: Add pick-winner function**

Add after the `sanitize_dirname` function (around line 416), before `process_file`:

```python
def _pick_winner(aiy_preds, aiy_raw_preds, yard_preds):
    """Pick the winning species prediction from AIY and yard model results.

    Args:
        aiy_preds: Filtered AIY predictions (list of dicts with common_name, raw_score)
        aiy_raw_preds: Raw (unfiltered) AIY predictions
        yard_preds: Yard model predictions (list of dicts with common_name, confidence)
                    or None/empty if yard model unavailable

    Returns:
        (winner_pred, model_source, yard_top, aiy_confidence) tuple where:
        - winner_pred: the winning prediction dict (from aiy_preds format)
        - model_source: "yard" | "aiy" | "both_agree" | "aiy_uncertain" | "aiy_only"
        - yard_top: top yard prediction dict or None
        - aiy_confidence: softmax-normalized AIY confidence (float)
    """
    aiy_top = aiy_preds[0] if aiy_preds else None
    if not aiy_top:
        return None, "aiy_only", None, 0.0

    # Compute AIY softmax confidence
    aiy_raw_scores = [p["raw_score"] for p in aiy_raw_preds[:3]]
    aiy_softmax = softmax_top3(aiy_raw_scores)
    aiy_confidence = aiy_softmax[0] if aiy_softmax else 0.0

    # No yard model → AIY only
    if not yard_preds:
        return aiy_top, "aiy_only", None, aiy_confidence

    yard_top = yard_preds[0]

    # Both agree on species?
    both_agree = (yard_top["common_name"] == aiy_top["common_name"])

    if both_agree:
        return aiy_top, "both_agree", yard_top, aiy_confidence

    # Yard confident → yard wins
    if yard_top["confidence"] >= YARD_THRESHOLD:
        # Build a prediction dict in AIY format so downstream code works
        winner = {
            "common_name": yard_top["common_name"],
            "scientific_name": yard_top["scientific_name"],
            "raw_score": aiy_top["raw_score"],  # keep AIY raw score for backward compat
        }
        return winner, "yard", yard_top, aiy_confidence

    # AIY confident → AIY wins
    if aiy_confidence >= AIY_THRESHOLD:
        return aiy_top, "aiy", yard_top, aiy_confidence

    # Both uncertain
    return aiy_top, "aiy_uncertain", yard_top, aiy_confidence
```

- [ ] **Step 3: Modify classification loop to run both models**

In `process_file()`, replace the classification loop (lines 501-513):

```python
    # Classify each detection
    all_predictions = []   # list of filtered predictions per detection
    all_raw = []           # list of raw predictions per detection
    for det in detections:
        bird_crop = crop_bird(img, det["box"])
        preds, raw_preds = _classifier.classify(bird_crop)
        all_predictions.append(preds)
        all_raw.append(raw_preds)

    classify_ms = (time.monotonic() - t1) * 1000
    total_ms = (time.monotonic() - t0) * 1000

    top = all_predictions[0][0]  # best detection's top prediction
```

with:

```python
    # Classify each detection with both models
    all_predictions = []   # list of filtered predictions per detection
    all_raw = []           # list of raw predictions per detection
    all_yard = []          # list of yard predictions per detection (or empty lists)
    all_model_source = []  # which model won per detection
    all_aiy_conf = []      # softmax-normalized AIY confidence per detection
    for det in detections:
        bird_crop = crop_bird(img, det["box"])
        preds, raw_preds = _classifier.classify(bird_crop)

        # Run yard model if available
        yard_preds = []
        if _yard_classifier is not None:
            try:
                yard_preds = _yard_classifier.classify(bird_crop)
            except Exception as e:
                logging.debug("Yard model error on %s: %s", fname, e)

        # Pick winner
        winner, model_source, yard_top, aiy_conf = _pick_winner(preds, raw_preds, yard_preds)
        if winner and model_source in ("yard", "both_agree"):
            # Replace AIY's top prediction with the winner
            preds = [winner] + [p for p in preds if p["common_name"] != winner["common_name"]][:2]

        all_predictions.append(preds)
        all_raw.append(raw_preds)
        all_yard.append(yard_preds)
        all_model_source.append(model_source)
        all_aiy_conf.append(aiy_conf)

    classify_ms = (time.monotonic() - t1) * 1000
    total_ms = (time.monotonic() - t0) * 1000

    top = all_predictions[0][0]  # best detection's top prediction (after pick-winner)
```

- [ ] **Step 4: Add model_source to result dict**

In the result dict construction (around line 592-627), add after the `"classifier_uncertain"` line:

```python
        # Model source tracking
        "model_source": all_model_source[0] if all_model_source else "aiy_only",
        "aiy_confidence": round(all_aiy_conf[0], 4) if all_aiy_conf else 0.0,
```

And add yard prediction info. After the `if range_filter_info:` block (around line 630):

```python
    # Yard model info (stored in extra_json via classifications_db)
    if all_yard and all_yard[0]:
        yard_top = all_yard[0][0]
        result["yard_prediction"] = {
            "species": yard_top["common_name"],
            "confidence": round(yard_top["confidence"], 4),
        }
```

- [ ] **Step 5: Load yard model in main()**

In `main()`, after the AIY classifier loading (around line 935), add:

```python
    # Load yard model (optional — graceful fallback to AIY-only)
    global _yard_classifier
    if YARD_MODEL_PATH.exists() and YARD_LABELS_PATH.exists():
        try:
            _yard_classifier = YardClassifier(str(YARD_MODEL_PATH), str(YARD_LABELS_PATH))
            logging.info("Yard model loaded: %d species (backend=coral)", len(_yard_classifier.labels))
        except Exception as e:
            _yard_classifier = None
            logging.warning("Could not load yard model: %s — AIY-only mode", e)
    else:
        logging.info("Yard model not found, AIY-only mode")
```

- [ ] **Step 6: Update log line to show model source**

Replace the final logging line in process_file (around line 728):

```python
    raw_note = ""
    if all_raw[0][0]["common_name"] != top["common_name"]:
        raw_note = f" (raw: {all_raw[0][0]['common_name']})"
    bird_count = f" +{len(birds)-1} more" if len(birds) > 1 else ""
    logging.info(
        "BIRD %s → %s%s%s (det=%.0f%%, score=%d, %dms)",
        fname,
        top["common_name"],
        raw_note,
        bird_count,
        best_det["confidence"] * 100,
        top["raw_score"],
        total_ms,
    )
```

with:

```python
    raw_note = ""
    if all_raw[0][0]["common_name"] != top["common_name"]:
        raw_note = f" (raw: {all_raw[0][0]['common_name']})"
    bird_count = f" +{len(birds)-1} more" if len(birds) > 1 else ""
    model_tag = f" [{all_model_source[0]}]" if all_model_source else ""
    logging.info(
        "BIRD %s → %s%s%s%s (det=%.0f%%, score=%d, %dms)",
        fname,
        top["common_name"],
        raw_note,
        bird_count,
        model_tag,
        best_det["confidence"] * 100,
        top["raw_score"],
        total_ms,
    )
```

- [ ] **Step 7: Run full test suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -v`
Expected: All 150+ tests pass. No regressions.

- [ ] **Step 8: Commit**

```bash
git add classify.py
git commit -m "feat: dual-model classification — yard model + AIY with pick-winner logic"
```

---

### Task 4: End-to-End Smoke Test

**Files:**
- No new files — manual verification on live system

- [ ] **Step 1: Test AIY-only mode (yard model temporarily renamed)**

```bash
cd /Users/vives/bird-classifier/models
mv yard_model.tflite yard_model.tflite.bak
mv yard_model_labels.txt yard_model_labels.txt.bak
```

Run classifier on one image:
```bash
/Users/vives/bird-classifier/venv-coral/bin/python -c "
import sys; sys.path.insert(0, '/Users/vives/bird-classifier')
from classify import *
main_args = type('A', (), {'watch': False, 'reprocess': False, 'summary': False})()
# Just test that it loads without yard model
setup_logging()
import logging
logging.info('Testing AIY-only mode...')
_det = YOLODetector(str(YOLO_MODEL_PATH), confidence=DETECTION_CONFIDENCE)
regional = load_regional_filter(REGIONAL_SPECIES_PATH)
_cls = SpeciesClassifier(str(SPECIES_MODEL_PATH), str(LABELS_PATH), regional_species=regional, tpu_model_path=str(SPECIES_TPU_PATH))
logging.info('AIY loaded: backend=%s', _cls._backend)
# Yard model should be None
from yard_classifier import YardClassifier
logging.info('Yard model files missing — AIY-only mode confirmed')
"
```

Restore model files:
```bash
mv yard_model.tflite.bak yard_model.tflite
mv yard_model_labels.txt.bak yard_model_labels.txt
```

Expected: Logs show "Yard model not found, AIY-only mode". No crash.

- [ ] **Step 2: Test dual-model mode on real images**

Copy 5 recent images back to incoming and run one-shot:
```bash
# Find 5 recent classified images
ls -t /Users/vives/bird-snapshots/classified/*/*.jpg | head -5 | while read f; do
    cp "$f" /Users/vives/bird-snapshots/incoming/
done

# Run one-shot classification
/Users/vives/bird-classifier/venv-coral/bin/python classify.py
```

Expected: Log lines show `[yard]`, `[aiy]`, `[both_agree]`, or `[aiy_only]` tags. No crashes. Species identified.

- [ ] **Step 3: Verify result data in SQLite**

```bash
/Users/vives/bird-classifier/venv-coral/bin/python -c "
import sqlite3, json
conn = sqlite3.connect('/Users/vives/bird-snapshots/logs/classifications.db')
conn.row_factory = sqlite3.Row
rows = conn.execute('''
    SELECT file, common_name, extra_json
    FROM classifications
    WHERE action = \"classified\"
    ORDER BY timestamp DESC LIMIT 5
''').fetchall()
for r in rows:
    extra = json.loads(r['extra_json']) if r['extra_json'] else {}
    print(f\"{r['file']}: {r['common_name']} — model_source={extra.get('model_source', 'N/A')}, yard={extra.get('yard_prediction', 'N/A')}\")
"
```

Expected: `model_source` field present in extra_json for new classifications.

- [ ] **Step 4: Commit any fixes from smoke testing**

If smoke tests revealed issues, fix and commit:
```bash
git add -u
git commit -m "fix: address issues found during dual-model smoke testing"
```

---

### Task 5: Update Documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-03-28-yard-model-integration-design.md`

- [ ] **Step 1: Update spec with probe results**

Add a "Verified Assumptions" section to the spec with actual probe results (score ranges, Coral sharing confirmation, label collisions found).

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-03-28-yard-model-integration-design.md
git commit -m "docs: update integration spec with verified probe results"
```
