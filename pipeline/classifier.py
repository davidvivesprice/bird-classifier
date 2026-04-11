"""SmartClassifier — Smart B decision tree with Coral lock and retry semantics."""
from __future__ import annotations
import logging
import threading
from dataclasses import dataclass
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)

CONFIDENT = 0.60
UNCERTAIN_LOW = 0.30
CORAL_ACQUIRE_TIMEOUT = 5.0  # seconds to wait FOR the lock (not inference itself)
MAX_CLASSIFICATION_ATTEMPTS = 3


@dataclass
class ClassificationResult:
    species: Optional[str]
    confidence: float
    model_source: Optional[str]
    should_retry: bool  # True if Coral was busy — retry on next frame


class SmartClassifier:
    def __init__(self, yard_model_path: str, yard_labels_path: str,
                 aiy_model_path: str, aiy_labels_path: str,
                 regional_species):
        from yard_classifier import YardClassifier
        from bird_inference import SpeciesClassifier

        self.yard = YardClassifier(yard_model_path, yard_labels_path)
        self.aiy = SpeciesClassifier(
            aiy_model_path, aiy_labels_path,
            regional_species=regional_species,
        )
        self._coral_lock = threading.Lock()
        self.stats = {
            "yard": 0, "aiy": 0, "both_agree": 0,
            "unlabeled": 0, "lock_timeouts": 0, "retries": 0,
        }

    def classify(self, crop_pil: Image.Image, frame_time_ms: float,
                 camera: str) -> ClassificationResult:
        got = self._coral_lock.acquire(timeout=CORAL_ACQUIRE_TIMEOUT)
        if not got:
            self.stats["lock_timeouts"] += 1
            return ClassificationResult(None, 0.0, None, should_retry=True)

        try:
            # Path 1: yard confident
            yard_res = self._run_yard(crop_pil)
            if yard_res and yard_res.confidence >= CONFIDENT:
                self.stats["yard"] += 1
                return ClassificationResult(
                    yard_res.species, yard_res.confidence, "yard", False
                )

            # Path 2: yard useless → AIY only
            if not yard_res or yard_res.confidence < UNCERTAIN_LOW:
                aiy_res = self._run_aiy(crop_pil)
                if aiy_res and aiy_res.confidence >= CONFIDENT:
                    self.stats["aiy"] += 1
                    return ClassificationResult(
                        aiy_res.species, aiy_res.confidence, "aiy", False
                    )
                self.stats["unlabeled"] += 1
                return ClassificationResult(None, 0.0, None, False)

            # Path 3: yard uncertain, compare with AIY
            aiy_res = self._run_aiy(crop_pil)
            if not aiy_res:
                self.stats["unlabeled"] += 1
                return ClassificationResult(None, 0.0, None, False)

            if aiy_res.species == yard_res.species:
                self.stats["both_agree"] += 1
                return ClassificationResult(
                    yard_res.species,
                    max(yard_res.confidence, aiy_res.confidence),
                    "both_agree", False
                )

            # Path 4 (audio cross-check) removed in v3 — see docs/superpowers/specs/2026-04-11-live-detection-v3-design.md § 10 forget-me-nots

            self.stats["unlabeled"] += 1
            return ClassificationResult(None, 0.0, None, False)
        finally:
            self._coral_lock.release()

    def _run_yard(self, crop_pil):
        """Run yard classifier. Returns object with .species and .confidence, or None.

        YardClassifier.classify returns a LIST of up to 3 dicts:
            [{"common_name": ..., "scientific_name": ..., "confidence": ...}, ...]
        We take the top result.
        """
        try:
            results = self.yard.classify(crop_pil)
            if not results:
                return None
            top = results[0]
            return type("YardResult", (), {
                "species": top.get("common_name"),
                "confidence": float(top.get("confidence", 0.0)),
            })()
        except Exception as e:
            log.warning("Yard classify error: %s", e)
            return None

    def _run_aiy(self, crop_pil):
        """Run AIY classifier. Returns object with .species and .confidence, or None."""
        try:
            filtered, _raw = self.aiy.classify(crop_pil)
            if not filtered:
                return None
            top = filtered[0]
            return type("AiyResult", (), {
                "species": top.get("common_name"),
                "confidence": float(top.get("raw_score", 0)) / 100.0,
            })()
        except Exception as e:
            log.debug("AIY classify error: %s", e)
            return None

