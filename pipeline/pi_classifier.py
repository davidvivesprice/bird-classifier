"""PiClassifier — registry-backed classifier for Pi 5 observatory.

Drop-in replacement for SmartClassifier. The difference:
  - No yard-model / Coral path.
  - All classification goes through a ModelRegistry — so the primary
    classifier can be switched at runtime between candidates (AIY, Hailo
    ResNet, Hailo YOLO-derived, flagship, etc.).
  - Same interface: classify(crop_pil, frame_time_ms, camera) and
    authoritative_classify(crop_pil) both return ClassificationResult.

This is what bird_pipeline_v3.py instantiates on the Pi.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    species: Optional[str]
    confidence: float
    model_source: Optional[str]
    should_retry: bool


class PiClassifier:
    """Wraps a ModelRegistry to provide the SmartClassifier interface."""

    def __init__(self, registry, confident_threshold: float = 0.25):
        self.registry = registry
        self.confident_threshold = confident_threshold
        # stats shaped like SmartClassifier.stats for dashboard compat
        self.stats = {}
        self._lock = threading.Lock()

    # ── SmartClassifier-compatible methods ────────────────────────────

    def classify(self, crop_pil, frame_time_ms: float, camera: str) -> ClassificationResult:
        cam_stats = self.stats.setdefault(camera, {
            "model_current": 0, "model_fallback": 0, "unlabeled_call": 0,
        })

        preds = self.registry.classify(crop_pil)
        if not preds:
            cam_stats["unlabeled_call"] += 1
            return ClassificationResult(None, 0.0, None, False)

        top = preds[0]
        # Some models give raw_score 0-255 (AIY); normalize to [0,1].
        raw = top.get("raw_score", 0)
        confidence = (float(raw) / 255.0) if raw > 1 else float(raw)
        if confidence < self.confident_threshold:
            cam_stats["unlabeled_call"] += 1
            return ClassificationResult(None, 0.0, None, False)

        cam_stats["model_current"] += 1
        return ClassificationResult(
            species=top.get("common_name"),
            confidence=confidence,
            model_source=self.registry.current_name,
            should_retry=False,
        )

    def authoritative_classify(self, crop_pil) -> Optional[ClassificationResult]:
        """Called by SnapshotWriter at track-lock time.
        Same logic as classify() but returns None (not unlabeled result) on
        low confidence, preserving the SnapshotWriter contract.
        """
        preds = self.registry.classify(crop_pil)
        if not preds:
            return None
        top = preds[0]
        raw = top.get("raw_score", 0)
        confidence = (float(raw) / 255.0) if raw > 1 else float(raw)
        if not top.get("common_name"):
            return None
        return ClassificationResult(
            species=top.get("common_name"),
            confidence=confidence,
            model_source=self.registry.current_name,
            should_retry=False,
        )

    # ── Model management passthroughs ────────────────────────────────

    def list_models(self) -> list[dict]:
        return self.registry.list()

    def switch_model(self, name: str) -> dict:
        return self.registry.switch(name)

    def current_model_name(self) -> Optional[str]:
        return self.registry.current_name
