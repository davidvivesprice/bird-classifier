"""SmartClassifier — per-camera decision tree with yard + AIY fallback."""
from __future__ import annotations
import logging
import threading
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from pipeline.camera_config import CameraClassifierConfig
from pipeline.constants import ModelSource

log = logging.getLogger(__name__)

CORAL_ACQUIRE_TIMEOUT = 5.0  # seconds to wait FOR the lock (not inference itself)
MAX_CLASSIFICATION_ATTEMPTS = 5


@dataclass
class ClassificationResult:
    species: Optional[str]
    confidence: float
    model_source: Optional[str]
    should_retry: bool  # True if Coral was busy — retry on next frame


class _YardLockTimeout:
    """Sentinel returned by _run_yard when the Coral lock could not be acquired.

    Distinguishes 'yard had no confident answer' (returns None) from
    'yard couldn't run due to lock contention, caller should retry' (returns
    this sentinel class object).
    """


class SmartClassifier:
    def __init__(
        self,
        yard_model_path: str,
        yard_labels_path: str,
        aiy_model_path: str,
        aiy_labels_path: str,
        regional_species,
        camera_configs: dict[str, CameraClassifierConfig],
    ):
        from yard_classifier import YardClassifier
        from bird_inference import SpeciesClassifier

        self.yard = YardClassifier(yard_model_path, yard_labels_path)
        self.aiy = SpeciesClassifier(
            aiy_model_path, aiy_labels_path,
            regional_species=regional_species,
        )
        self.camera_configs = camera_configs
        self._coral_lock = threading.Lock()
        self.stats = {
            camera: {
                "yard": 0, "aiy": 0, "both_agree": 0,
                "unlabeled_call": 0, "lock_timeouts": 0,
            }
            for camera in camera_configs
        }

    def classify(self, crop_pil: Image.Image, frame_time_ms: float,
                 camera: str) -> ClassificationResult:
        config = self.camera_configs.get(camera)
        if config is None:
            log.warning("No classifier config for camera %s, defaulting to AIY-only", camera)
            config = CameraClassifierConfig(use_yard=False)

        cam_stats = self.stats.setdefault(camera, {
            "yard": 0, "aiy": 0, "both_agree": 0,
            "unlabeled_call": 0, "lock_timeouts": 0,
        })

        if not config.use_yard:
            # Ground path: AIY only. Never touches the Coral lock.
            aiy_res = self._run_aiy(crop_pil)
            if aiy_res and aiy_res.confidence >= config.confident_threshold:
                cam_stats["aiy"] += 1
                return ClassificationResult(
                    aiy_res.species, aiy_res.confidence, ModelSource.AIY, False
                )
            cam_stats["unlabeled_call"] += 1
            return ClassificationResult(None, 0.0, None, False)

        # Feeder path: yard-first decision tree.
        # Coral lock is acquired inside _run_yard only during yard inference.
        yard_res = self._run_yard(crop_pil, cam_stats)
        if yard_res is _YardLockTimeout:
            return ClassificationResult(None, 0.0, None, should_retry=True)

        if yard_res and yard_res.confidence >= config.confident_threshold:
            cam_stats["yard"] += 1
            return ClassificationResult(
                yard_res.species, yard_res.confidence, ModelSource.YARD, False
            )

        if not yard_res or yard_res.confidence < config.uncertain_low:
            aiy_res = self._run_aiy(crop_pil)
            if aiy_res and aiy_res.confidence >= config.confident_threshold:
                cam_stats["aiy"] += 1
                return ClassificationResult(
                    aiy_res.species, aiy_res.confidence, ModelSource.AIY, False
                )
            cam_stats["unlabeled_call"] += 1
            return ClassificationResult(None, 0.0, None, False)

        # Yard is in the uncertain band — cross-check with AIY.
        aiy_res = self._run_aiy(crop_pil)
        if not aiy_res:
            cam_stats["unlabeled_call"] += 1
            return ClassificationResult(None, 0.0, None, False)

        if aiy_res.species == yard_res.species:
            cam_stats["both_agree"] += 1
            return ClassificationResult(
                yard_res.species,
                max(yard_res.confidence, aiy_res.confidence),
                ModelSource.BOTH_AGREE, False
            )

        # Disagreement: Path 4 (audio cross-check) removed in v3 — see
        # docs/superpowers/specs/2026-04-11-live-detection-v3-design.md § 10.
        cam_stats["unlabeled_call"] += 1
        return ClassificationResult(None, 0.0, None, False)

    def authoritative_classify(self, crop_pil):
        """AIY-only classification intended for classifications.db writes.

        Called by SnapshotWriter at track-lock time. Bypasses yard entirely
        so the review queue / DB records reflect AIY's 965-species label
        rather than yard's 12-species best-guess.

        Takes the same Coral lock yard uses, since AIY also runs on Coral —
        without the lock, yard's per-frame live call could race with this
        snapshot-time AIY call on the same USB Edge TPU.

        Returns a ClassificationResult, or None if the lock times out, AIY
        errors, or AIY returns no confident prediction.
        """
        got = self._coral_lock.acquire(timeout=CORAL_ACQUIRE_TIMEOUT)
        if not got:
            return None
        try:
            aiy_res = self._run_aiy(crop_pil)
            if not aiy_res or not aiy_res.species:
                return None
            return ClassificationResult(
                aiy_res.species, aiy_res.confidence, ModelSource.AIY, False
            )
        finally:
            self._coral_lock.release()

    def _run_yard(self, crop_pil, cam_stats):
        """Run yard classifier. Returns object with .species and .confidence, or None.

        Holds the Coral lock for the duration of yard inference because yard
        runs on the Coral Edge TPU and Coral USB is single-session.

        Returns _YardLockTimeout (the sentinel class) if the lock could not be
        acquired within CORAL_ACQUIRE_TIMEOUT — the caller should retry on the
        next frame rather than fall through to AIY.
        """
        got = self._coral_lock.acquire(timeout=CORAL_ACQUIRE_TIMEOUT)
        if not got:
            cam_stats["lock_timeouts"] += 1
            return _YardLockTimeout
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
        finally:
            self._coral_lock.release()

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
