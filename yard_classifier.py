"""yard_classifier — Coral Edge TPU classifier using the yard-trained model.

Provides a YardClassifier class that runs inference on a custom-trained
TFLite model for species commonly seen at the feeder.  Also exposes
pure-function helpers for label normalisation, alias merging, and softmax
so they can be unit-tested without hardware.
"""

import numpy as np

from bird_inference import normalize_species

# ── Thresholds ────────────────────────────────────────────────────────────

YARD_THRESHOLD = 0.45   # softmax probability — yard model (used by classify.py pick-winner, not internally)
AIY_THRESHOLD = 0.50    # softmax probability — AIY model (used by classify.py pick-winner, not internally)

# ── Label helpers ─────────────────────────────────────────────────────────


def _normalize_labels(labels):
    """Normalise raw label strings into canonical names.

    Returns
    -------
    canonical : list[str]
        Canonical name for each class index (same length as *labels*).
    alias_groups : dict[str, list[int]]
        Canonical names that map to more than one class index.
    not_a_bird_ids : set[int]
        Class indices whose label is "not a bird" (case-insensitive).
    """
    canonical = []
    # Build mapping: canonical_name -> [class_indices]
    name_to_ids: dict[str, list[int]] = {}
    not_a_bird_ids: set[int] = set()

    for idx, raw in enumerate(labels):
        name = normalize_species(raw.strip())
        canonical.append(name)

        if name.lower() == "not a bird":
            not_a_bird_ids.add(idx)
            continue

        name_to_ids.setdefault(name, []).append(idx)

    # Only keep groups with more than one index
    alias_groups = {name: ids for name, ids in name_to_ids.items() if len(ids) > 1}

    return canonical, alias_groups, not_a_bird_ids


def _merge_alias_scores(scores, canonical, alias_groups, not_a_bird_ids):
    """Merge aliased class scores and filter out ``not a bird``.

    Parameters
    ----------
    scores : array-like
        Raw scores array (one per class).
    canonical : list[str]
        Canonical name per class index (from ``_normalize_labels``).
    alias_groups : dict[str, list[int]]
        Aliased canonical names (from ``_normalize_labels``).
    not_a_bird_ids : set[int]
        Indices to exclude (from ``_normalize_labels``).

    Returns
    -------
    dict[str, float]
        {canonical_name: summed_score} with ``not a bird`` removed.
    """
    merged: dict[str, float] = {}

    # Collect indices that are handled by alias groups
    alias_indices: set[int] = set()
    for ids in alias_groups.values():
        alias_indices.update(ids)

    for idx, name in enumerate(canonical):
        if idx in not_a_bird_ids:
            continue
        if idx in alias_indices:
            continue  # handled below
        merged[name] = float(scores[idx])

    # Sum alias groups
    for name, ids in alias_groups.items():
        merged[name] = sum(float(scores[i]) for i in ids)

    return merged


def softmax_top3(raw_scores):
    """Apply softmax to the top-3 raw scores.

    Parameters
    ----------
    raw_scores : dict[str, float] or array-like
        If a dict, the top-3 values are selected.  If array-like, the top-3
        elements are used.

    Returns
    -------
    list[float]
        Three probabilities summing to ~1.0, in descending order.
    """
    if isinstance(raw_scores, dict):
        sorted_items = sorted(raw_scores.values(), reverse=True)
    else:
        sorted_items = sorted(raw_scores, reverse=True)

    top3 = np.array(sorted_items[:3], dtype=np.float64)

    # Numerical stability: subtract max before exp
    top3 -= top3.max()
    exps = np.exp(top3)
    probs = exps / exps.sum()

    return probs.tolist()


# ── YardClassifier ────────────────────────────────────────────────────────


class YardClassifier:
    """Coral Edge TPU classifier using the yard-trained model.

    Parameters
    ----------
    model_path : str or Path
        Path to the yard model ``.tflite`` file (Edge TPU compiled).
    labels_path : str or Path
        Path to the yard model labels text file (one label per line).
    """

    def __init__(self, model_path, labels_path):
        from pathlib import Path as _Path

        # Load labels
        with open(labels_path) as f:
            raw_labels = [line.strip() for line in f if line.strip()]

        self._canonical, self._alias_groups, self._not_a_bird_ids = (
            _normalize_labels(raw_labels)
        )

        # Load model on Coral
        self._enabled = True
        try:
            from pycoral.utils.edgetpu import make_interpreter
            from pycoral.adapters import common as coral_common

            self._interpreter = make_interpreter(str(model_path))
            self._interpreter.allocate_tensors()
            self._coral_common = coral_common
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load yard model on Coral: {exc}"
            ) from exc

    @property
    def enabled(self):
        """Whether the yard classifier is active."""
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)

    def classify(self, crop):
        """Classify a bird crop image using the yard model.

        Parameters
        ----------
        crop : PIL.Image.Image or numpy.ndarray
            A cropped bird image (any size — resized to 224x224).

        Returns
        -------
        list[dict]
            Up to 3 predictions, each with keys ``common_name``,
            ``scientific_name``, and ``confidence`` (softmax probability).
            Returns ``[]`` if disabled or confidence is very low.
        """
        if not self._enabled:
            return []

        from PIL import Image as PILImage

        # Accept numpy arrays
        if isinstance(crop, np.ndarray):
            crop = PILImage.fromarray(crop)

        # Resize to model input size and convert to uint8 numpy
        resized = crop.resize((224, 224))
        arr = np.array(resized, dtype=np.uint8)  # (224, 224, 3)

        # Run inference
        self._coral_common.set_input(self._interpreter, arr)
        self._interpreter.invoke()
        scores = np.array(
            self._coral_common.output_tensor(self._interpreter, 0),
            dtype=np.float32,
        )

        # Flatten (output may be (1, N))
        if scores.ndim == 2:
            scores = scores[0]
        scores = scores.flatten()

        # Merge aliases and filter not_a_bird
        merged = _merge_alias_scores(
            scores, self._canonical, self._alias_groups, self._not_a_bird_ids
        )

        if not merged:
            return []

        # Sort by score descending, take top 3
        sorted_names = sorted(merged, key=merged.get, reverse=True)
        top3_names = sorted_names[:3]
        top3_raw = {name: merged[name] for name in top3_names}

        # Softmax on top-3 scores
        probs = softmax_top3(top3_raw)

        # Noise floor — distinct from YARD_THRESHOLD which is applied by
        # classify.py's pick-winner logic. This just filters pure garbage.
        if probs[0] < 0.10:
            return []

        results = []
        for name, prob in zip(top3_names, probs):
            results.append({
                "common_name": name,
                "scientific_name": "",  # yard model has no scientific names
                "confidence": round(prob, 4),
            })

        return results
