"""HailoDetector — YOLOv8 on Hailo 8L.

Pre-compiled HEFs from the Hailo model zoo (e.g. yolov8s_h8l.hef) include
the NMS postprocess stage. Output shape is (num_classes, 5, max_per_class)
where the 5 dims are [y_min, x_min, y_max, x_max, confidence] per box.

Drop-in for BirdDetector: same `detect(frame, motion_regions, forced_full)`
contract, returns a list of Detection(box=[x1,y1,x2,y2], confidence=float).
"""
from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np

log = logging.getLogger(__name__)


# YOLOv8-COCO class 14 = bird (indexing from 0, matches the class list).
# Some of our targets of interest could also be class 15 (cat) or 16 (dog),
# but for a bird observatory we restrict to bird.
BIRD_CLASS_ID = 14
# Include cats+dogs too so we can catch squirrels-mis-detected-as-dog patterns
# and surface them to the review flow. Adjust as needed.
ACCEPT_CLASSES = {BIRD_CLASS_ID}  # {14: 'bird'}


class HailoDetector:
    """YOLOv8 detector running on Hailo.

    Replaces BirdDetector on the Pi. YOLOv8n(s) with built-in NMS.
    """

    def __init__(self, hef_path: str, confidence: float = 0.3,
                 accept_classes: Optional[set] = None):
        self.hef_path = hef_path
        self.confidence = confidence
        self.accept_classes = accept_classes if accept_classes is not None else ACCEPT_CLASSES

        from pipeline.hailo_engine import HailoEngine
        self._model = HailoEngine.get().acquire_model(hef_path)
        self._input_name = self._model.input_names[0]
        self._output_name = self._model.output_names[0]
        self._input_shape = self._model.input_shape()    # (h, w, c)
        self._output_shape = self._model.output_shape()
        self.stats = {
            "detect_calls": 0,
            "total_detections": 0,
            "bird_detections": 0,
            "last_ms": 0.0,
        }
        log.info("HailoDetector ready: in=%s out=%s", self._input_shape, self._output_shape)

    # ── Main API ─────────────────────────────────────────────────────

    def detect(self, frame, motion_regions=None, forced_full=False):
        """Detect objects in a frame. Returns list of Detection objects.

        Matches the BirdDetector interface so process_thread doesn't care.
        motion_regions / forced_full are accepted for compatibility but not
        used — Hailo is fast enough that we always run full-frame.
        """
        from pipeline.detector import Detection
        import time

        # Handle Frame wrapper
        if hasattr(frame, 'bgr'):
            bgr = frame.bgr
        else:
            bgr = frame

        t0 = time.monotonic()
        # Preprocess: BGR -> RGB, resize to model size, uint8
        h_in, w_in = self._input_shape[0], self._input_shape[1]
        resized = _letterbox(bgr, (w_in, h_in))
        rgb = resized[..., ::-1]  # BGR -> RGB
        x = rgb.astype(np.uint8)[np.newaxis, ...]  # (1, H, W, 3)

        output = self._model.infer({self._input_name: x})

        # Hailo's NMS postprocess returns a Python list-of-ndarrays: one array
        # per class, each shape (num_detections, 5) with [y1, x1, y2, x2, conf]
        # in normalized coords. Different classes have different lengths so
        # np.asarray() bombs with "inhomogeneous shape". Parse as a list.
        raw_out = output[self._output_name]
        detections = _parse_yolo_list_output(
            raw_out,
            input_hw=(h_in, w_in),
            frame_hw=(bgr.shape[0], bgr.shape[1]),
            confidence_threshold=self.confidence,
            accept_classes=self.accept_classes,
        )

        self.stats["detect_calls"] += 1
        self.stats["total_detections"] += len(detections)
        self.stats["bird_detections"] += sum(1 for _ in detections)  # all already filtered
        self.stats["last_ms"] = (time.monotonic() - t0) * 1000.0

        return [Detection(box=d["box"], confidence=d["confidence"]) for d in detections]

    def close(self):
        # Engine owns the VDevice; per-model cleanup happens via
        # HailoEngine.shutdown() at process exit.
        pass


# ── Helpers ───────────────────────────────────────────────────────────


def _letterbox(img, new_wh):
    """Resize img (HxW BGR) to (new_wh[0] x new_wh[1]) preserving aspect ratio
    with gray padding. Return a copy ready for the model."""
    new_w, new_h = new_wh
    h, w = img.shape[:2]
    scale = min(new_w / w, new_h / h)
    sw = int(w * scale)
    sh = int(h * scale)
    import cv2
    resized = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((new_h, new_w, 3), 114, dtype=np.uint8)
    off_x = (new_w - sw) // 2
    off_y = (new_h - sh) // 2
    canvas[off_y:off_y + sh, off_x:off_x + sw] = resized
    return canvas


def _parse_yolo_list_output(raw_out,
                            input_hw: tuple,
                            frame_hw: tuple,
                            confidence_threshold: float,
                            accept_classes: set) -> List[dict]:
    """Parse Hailo's NMS list-of-arrays output.
    raw_out: list of arrays (one per class) OR a (1, num_classes) object array.
    Each inner array has rows [y_min, x_min, y_max, x_max, confidence] normalized.
    """
    in_h, in_w = input_hw
    f_h, f_w = frame_hw
    scale = min(in_w / f_w, in_h / f_h)
    pad_x = (in_w - f_w * scale) / 2
    pad_y = (in_h - f_h * scale) / 2

    # Normalize to per-class list
    if isinstance(raw_out, np.ndarray) and raw_out.dtype == object:
        # (1, num_classes) object array — flatten to list
        per_class = list(raw_out.flatten())
    elif isinstance(raw_out, (list, tuple)):
        if len(raw_out) == 1 and isinstance(raw_out[0], (list, np.ndarray)):
            per_class = list(raw_out[0])
        else:
            per_class = list(raw_out)
    else:
        return []

    results = []
    for cls_idx, cls_dets in enumerate(per_class):
        if cls_idx not in accept_classes:
            continue
        if cls_dets is None:
            continue
        cls_dets = np.asarray(cls_dets)
        if cls_dets.ndim == 1:
            if cls_dets.size == 0:
                continue
            cls_dets = cls_dets.reshape(1, -1)
        if cls_dets.shape[0] == 0:
            continue
        for det in cls_dets:
            if len(det) < 5:
                continue
            conf = float(det[4])
            if conf < confidence_threshold:
                continue
            y1n, x1n, y2n, x2n = float(det[0]), float(det[1]), float(det[2]), float(det[3])
            if max(abs(x1n), abs(y1n), abs(x2n), abs(y2n)) > 1.5:
                x1m, y1m, x2m, y2m = x1n, y1n, x2n, y2n
            else:
                x1m = x1n * in_w
                y1m = y1n * in_h
                x2m = x2n * in_w
                y2m = y2n * in_h
            x1 = (x1m - pad_x) / scale
            y1 = (y1m - pad_y) / scale
            x2 = (x2m - pad_x) / scale
            y2 = (y2m - pad_y) / scale
            x1 = max(0, min(f_w, x1))
            x2 = max(0, min(f_w, x2))
            y1 = max(0, min(f_h, y1))
            y2 = max(0, min(f_h, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            results.append({
                "box": [float(x1), float(y1), float(x2), float(y2)],
                "confidence": conf,
                "class": cls_idx,
            })
    return results


def _parse_yolo_nms_output(arr: np.ndarray,
                           input_hw: tuple,
                           frame_hw: tuple,
                           confidence_threshold: float,
                           accept_classes: set) -> List[dict]:
    """Parse the NMS-baked-in output of a Hailo YOLO HEF.

    The shape is typically (num_classes, max_boxes, 5) where each box is
    [y_min, x_min, y_max, x_max, confidence] in normalized [0, 1] coords
    relative to the model's input size. We rescale to original frame coords
    and return [{'box': [x1,y1,x2,y2], 'confidence': float}].
    """
    in_h, in_w = input_hw
    f_h, f_w = frame_hw

    # Normalize axis order: we want (classes, boxes, 5)
    if arr.ndim == 3:
        if arr.shape[2] == 5:
            pass  # (classes, boxes, 5)
        elif arr.shape[1] == 5:
            arr = np.transpose(arr, (0, 2, 1))  # (classes, boxes, 5)
        elif arr.shape[0] == 5:
            arr = np.transpose(arr, (1, 2, 0))  # (classes, boxes, 5)

    # Scale factor (we letterboxed, so undo letterbox)
    scale = min(in_w / f_w, in_h / f_h)
    pad_x = (in_w - f_w * scale) / 2
    pad_y = (in_h - f_h * scale) / 2

    results = []
    for cls in range(arr.shape[0]):
        if cls not in accept_classes:
            continue
        for det in arr[cls]:
            conf = float(det[4])
            if conf < confidence_threshold:
                continue
            # Hailo NMS output: [y_min, x_min, y_max, x_max, conf] in normalized coords
            y1n, x1n, y2n, x2n = float(det[0]), float(det[1]), float(det[2]), float(det[3])
            # Some HEFs output in pixel coords vs normalized — detect by magnitude
            if max(x1n, y1n, x2n, y2n) > 1.5:
                # Pixel coords in model-input space
                x1m, y1m, x2m, y2m = x1n, y1n, x2n, y2n
            else:
                x1m = x1n * in_w
                y1m = y1n * in_h
                x2m = x2n * in_w
                y2m = y2n * in_h
            # Undo letterbox: (x - pad) / scale → frame coords
            x1 = (x1m - pad_x) / scale
            y1 = (y1m - pad_y) / scale
            x2 = (x2m - pad_x) / scale
            y2 = (y2m - pad_y) / scale
            # Clamp
            x1 = max(0, min(f_w, x1))
            x2 = max(0, min(f_w, x2))
            y1 = max(0, min(f_h, y1))
            y2 = max(0, min(f_h, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            results.append({
                "box": [float(x1), float(y1), float(x2), float(y2)],
                "confidence": conf,
                "class": cls,
            })

    return results
