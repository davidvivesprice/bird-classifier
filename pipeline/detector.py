"""BirdDetector — region-based YOLO with full-frame coordinate output."""
import logging
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image

from pipeline.frame import Frame

log = logging.getLogger(__name__)


@dataclass
class Detection:
    """A detected bird with its bounding box and confidence.

    box: [x1, y1, x2, y2] in FULL-FRAME coordinates (after region offset)
    """
    box: list
    confidence: float


def _iou(a, b) -> float:
    """IoU between two boxes [x1,y1,x2,y2]."""
    x1 = max(a[0], b[0]); y1 = max(a[1], b[1])
    x2 = min(a[2], b[2]); y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    if union == 0:
        return 0.0
    return inter / union


class BirdDetector:
    """Region-based YOLO detector with stationary-track skipping.

    Runs YOLO on motion crops (faster than full-frame 1080p detection)
    and offsets detection boxes back to full-frame coordinates.
    Skips motion regions that contain only stationary tracks.
    """

    def __init__(self, yolo_model_path: str,
                 stationary_track_regions_fn: Callable[[], list],
                 confidence: float = 0.3):
        from bird_inference import YOLODetector
        self.yolo = YOLODetector(yolo_model_path, confidence=confidence)
        self.get_stationary = stationary_track_regions_fn

    def detect(self, frame: Frame, motion_regions: list,
               forced_full: bool = False) -> list:
        """Run detection.

        ALWAYS uses full-frame YOLO. Region detection sounds smart but
        ONNX Runtime resizes everything to 640x640 anyway, so multiple
        small regions = multiple full-cost YOLO calls = much slower than
        one full-frame call. Motion regions are still used as a gate
        (skip detection entirely if no motion), but when there IS motion
        we run a single full-frame inference.
        """
        # Skip detection entirely if no motion (unless forced)
        if not motion_regions and not forced_full:
            return []
        # Stationary suppression: if all motion regions are explained by
        # tracks that haven't moved, skip YOLO. A perched bird that triggers
        # motion (slight wind sway) but is already tracked doesn't need
        # re-detection. This saves ~150 YOLO calls per 30-second perch.
        if motion_regions and not forced_full:
            stationary = self.get_stationary()
            if stationary and all(self._is_stationary_only(r, stationary) for r in motion_regions):
                return []
        return self._detect_full(frame)

    def _detect_full(self, frame: Frame) -> list:
        """Run YOLO on the full frame."""
        # YOLODetector.detect takes a PIL image (RGB)
        pil = Image.fromarray(frame.bgr[:, :, ::-1])  # BGR → RGB
        try:
            raw = self.yolo.detect(pil)
        except Exception as e:
            log.warning("YOLO full-frame error: %s", e)
            return []
        return [
            Detection(box=list(r["box"]), confidence=float(r["confidence"]))
            for r in raw
        ]

    def _detect_region(self, bgr: np.ndarray, region: tuple) -> list:
        """Run YOLO on a cropped region and offset boxes to full-frame coords."""
        x1, y1, x2, y2 = region
        crop_bgr = bgr[y1:y2, x1:x2]
        if crop_bgr.size == 0:
            return []
        # Convert to PIL RGB for YOLODetector
        pil = Image.fromarray(crop_bgr[:, :, ::-1])
        try:
            raw = self.yolo.detect(pil)
        except Exception as e:
            log.warning("YOLO region error: %s", e)
            return []
        out = []
        for r in raw:
            b = r["box"]
            out.append(Detection(
                box=[b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1],
                confidence=float(r["confidence"]),
            ))
        return out

    def _is_stationary_only(self, region: tuple, stationary: list) -> bool:
        """True if the motion region is entirely explained by a stationary track."""
        if not stationary:
            return False
        for st in stationary:
            if _iou(region, st) > 0.8:
                return True
        return False
