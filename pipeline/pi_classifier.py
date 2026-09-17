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
from numbers import Integral
from typing import Optional

from pipeline.calibration import calibrate

log = logging.getLogger(__name__)


def _normalize_raw_score(raw_score) -> float:
    """Return confidence in [0, 1] from registry raw_score values.

    The registry contract uses AIY/Hailo-style integer scores on a 0-255
    scale. A prior `raw > 1 else raw` shortcut treated integer raw_score=1 as
    perfect confidence, which made weak AIY ties eligible for live vote-locks.
    """
    try:
        value = float(raw_score or 0)
    except (TypeError, ValueError):
        return 0.0
    if isinstance(raw_score, Integral) or value > 1.0:
        value = value / 255.0
    return max(0.0, min(1.0, value))


@dataclass
class ClassificationResult:
    species: Optional[str]
    confidence: float
    model_source: Optional[str]
    should_retry: bool
    # True when the crop looks like NO BIRD (AIY's explicit 'background' class
    # or off-list mass with nothing regional): an empty/partial box, not a
    # bird the regional filter disagrees with. 74% of live no-vote crops
    # (measured 2026-09-17). Consumers weight it heavier than a plain None.
    no_bird: bool = False


# Off-list/background test thresholds (raw AIY scores, 0-255).
NO_BIRD_REGIONAL_MAX = 20     # regional top this weak…
NO_BIRD_OFFLIST_MIN = 120     # …while an off-list species this strong → empty box
TTA_INSET = 0.04              # crop-jitter inset fraction for the extra views


class PiClassifier:
    """Wraps a ModelRegistry to provide the SmartClassifier interface."""

    def __init__(self, registry, confident_threshold: float = 0.25):
        self.registry = registry
        self.confident_threshold = confident_threshold
        # stats shaped like SmartClassifier.stats for dashboard compat
        self.stats = {}
        self._lock = threading.Lock()

    # ── SmartClassifier-compatible methods ────────────────────────────

    def _predict(self, crop_pil):
        """(regional_preds, unfiltered_preds) for one crop; unfiltered may be
        the same list when the active model has no regional filter."""
        inst = getattr(self.registry, "_current_instance", None)
        if inst is not None and hasattr(inst, "classify_full"):
            try:
                out = inst.classify_full(crop_pil)
                # Trust only a real (regional, unfiltered) pair of lists —
                # mocks and odd adapters fall through to the plain path.
                if (isinstance(out, (tuple, list)) and len(out) == 2
                        and all(isinstance(x, list) for x in out)):
                    return out[0], out[1]
            except Exception:
                pass
        preds = self.registry.classify(crop_pil)
        preds = preds if isinstance(preds, list) else []
        return preds, preds

    @staticmethod
    def _views(crop_pil, views: int):
        """Test-time augmentation views: original, horizontal flip, and two
        slightly inset crops. Measured +0.4-0.8 pt recall on verified crops;
        padding/square crops were measured to HURT, so none here."""
        if views <= 1:
            return [crop_pil]
        from PIL import ImageOps
        w, h = crop_pil.size
        dx, dy = max(1, int(w * TTA_INSET)), max(1, int(h * TTA_INSET))
        out = [crop_pil, ImageOps.mirror(crop_pil)]
        if views >= 3 and w > 4 * dx and h > 4 * dy:
            out.append(crop_pil.crop((dx, dy, w - dx, h - dy)))
        if views >= 4 and w > 4 * dx and h > 4 * dy:
            out.append(crop_pil.crop((2 * dx, 0, w, h - 2 * dy)))
        return out

    @staticmethod
    def _merge(pred_lists):
        """Average raw scores per species across views (missing = 0)."""
        if len(pred_lists) == 1:
            return pred_lists[0]
        acc: dict = {}
        meta: dict = {}
        for preds in pred_lists:
            for p in preds:
                name = p.get("common_name")
                if not name:
                    continue
                acc[name] = acc.get(name, 0.0) + float(p.get("raw_score", 0))
                meta.setdefault(name, p)
        n = len(pred_lists)
        merged = [dict(meta[k], raw_score=v / n) for k, v in acc.items()]
        merged.sort(key=lambda p: -p["raw_score"])
        return merged

    def classify(self, crop_pil, frame_time_ms: float, camera: str,
                 views: int = 1) -> ClassificationResult:
        cam_stats = self.stats.setdefault(camera, {
            "model_current": 0, "model_fallback": 0, "unlabeled_call": 0,
            "no_bird": 0,
        })

        reg_lists, raw_lists = [], []
        for v in self._views(crop_pil, views):
            reg, raw_preds = self._predict(v)
            reg_lists.append(reg or [])
            raw_lists.append(raw_preds or [])
        preds = self._merge(reg_lists)
        unfiltered = self._merge(raw_lists)

        # Empty-box signal, independent of the regional floor below.
        no_bird = False
        if unfiltered:
            u_top = unfiltered[0]
            u_name = (u_top.get("common_name") or "").lower()
            reg_raw = float(preds[0].get("raw_score", 0)) if preds else 0.0
            if u_name == "background" or (
                    reg_raw < NO_BIRD_REGIONAL_MAX
                    and float(u_top.get("raw_score", 0)) > NO_BIRD_OFFLIST_MIN):
                no_bird = True

        if not preds:
            cam_stats["unlabeled_call"] += 1
            if no_bird:
                cam_stats["no_bird"] = cam_stats.get("no_bird", 0) + 1
            return ClassificationResult(None, 0.0, None, False, no_bird=no_bird)

        top = preds[0]
        raw = top.get("raw_score", 0)
        # Eligibility floor stays on the raw normalized score (F2). The RETURNED
        # confidence is the post-hoc CALIBRATED P(correct) so display + vote-lock
        # see an honest probability instead of the wildly-underconfident raw/255.
        if _normalize_raw_score(raw) < self.confident_threshold:
            cam_stats["unlabeled_call"] += 1
            if no_bird:
                cam_stats["no_bird"] = cam_stats.get("no_bird", 0) + 1
            return ClassificationResult(None, 0.0, None, False, no_bird=no_bird)

        cam_stats["model_current"] += 1
        return ClassificationResult(
            species=top.get("common_name"),
            confidence=calibrate(raw),
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
        if not top.get("common_name"):
            return None
        return ClassificationResult(
            species=top.get("common_name"),
            confidence=calibrate(top.get("raw_score", 0)),
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
