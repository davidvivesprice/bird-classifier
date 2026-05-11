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
import cv2

log = logging.getLogger(__name__)


# YOLOv8-COCO class 14 = bird (indexing from 0, matches the class list).
# Some of our targets of interest could also be class 15 (cat) or 16 (dog),
# but for a bird observatory we restrict to bird.
BIRD_CLASS_ID = 14
# Bird-only as of 2026-04-30. The earlier comment proposed including
# cats+dogs to catch squirrel-mis-detected-as-dog patterns, but the
# squirrel-channel review flow doesn't exist yet, and broadening the
# accept set without it just bloats false positives downstream.
# To extend, add class IDs here AND update bird_detections counter at
# line 96 (currently couples 1:1 with ACCEPT_CLASSES).
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

        # ── Preallocated input buffer (Track B audit 2026-05-11) ─────────
        # Previously _letterbox allocated `canvas = np.full((640,640,3), 114)`
        # AND `cv2.resize` allocated a new array each call — ~3-4 MB/frame
        # of malloc/free pressure at 30fps = 90-120 MB/s alloc traffic.
        # Now: allocate once, refill region each frame in place.
        h_in, w_in = self._input_shape[0], self._input_shape[1]
        # Hailo model wants (1, H, W, 3) uint8. Pre-fill padding with 114 (gray).
        self._input_buf = np.full((1, h_in, w_in, 3), 114, dtype=np.uint8)
        # Cache of the active inscribed-rect (sw, sh, off_x, off_y) so we only
        # reset the padding to 114 when the input frame resolution changes
        # (rare — once on camera reconfig).
        self._last_letterbox_dims: Optional[tuple] = None

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
        # Preprocess: BGR -> RGB, resize to model size into preallocated
        # buffer (no per-frame allocations).
        h_in, w_in = self._input_shape[0], self._input_shape[1]
        h, w = bgr.shape[:2]
        scale = min(w_in / w, h_in / h)
        sw = max(1, int(w * scale))
        sh = max(1, int(h * scale))
        off_x = (w_in - sw) // 2
        off_y = (h_in - sh) // 2

        # If the inscribed region changed (rare — camera res change), reset
        # the padding to 114. Otherwise the prior frame's pixels in the
        # target region get overwritten and the padding is already gray.
        dims = (sw, sh, off_x, off_y)
        if dims != self._last_letterbox_dims:
            self._input_buf.fill(114)
            self._last_letterbox_dims = dims

        # Resize BGR straight into the target slice of the input buffer.
        # cv2 handles non-contiguous dst views (OpenCV 3+).
        target_slice = self._input_buf[0, off_y:off_y + sh, off_x:off_x + sw, :]
        cv2.resize(bgr, (sw, sh), dst=target_slice,
                   interpolation=cv2.INTER_LINEAR)
        # BGR → RGB in place (Hailo HEF expects RGB).
        cv2.cvtColor(target_slice, cv2.COLOR_BGR2RGB, dst=target_slice)

        output = self._model.infer({self._input_name: self._input_buf})

        # InferModel's NMS-baked YOLO HEF returns a flat (N,) FLOAT32 buffer:
        # densely packed [count_c0, det0_c0_5fl, det1_c0_5fl, ..., count_c1, ...]
        # for 80 COCO classes. Parse with _parse_yolo_flat_output.
        raw_out = output[self._output_name]
        detections = _parse_yolo_flat_output(
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


def _parse_yolo_flat_output(raw_out,
                            input_hw: tuple,
                            frame_hw: tuple,
                            confidence_threshold: float,
                            accept_classes: set,
                            num_classes: int = 80) -> List[dict]:
    """Parse the InferModel flat NMS output.

    The HailoRT InferModel.run_async path delivers NMS-baked YOLO output
    as a single flat FLOAT32 ndarray, densely packed per class:

        [count_c0, y1, x1, y2, x2, conf,        # det 0 of class 0
                  y1, x1, y2, x2, conf,         # det 1 of class 0
                  ...
         count_c1, ...,
         ...
         count_c79, ...]

    Each per-class block is variable length: 1 (count) + count*5. The
    buffer is allocated with worst-case size on the Hailo side; trailing
    bytes after the last detection of class 79 are zero.

    Boxes are normalized [0, 1] in input-space coords. We rescale through
    the letterbox transform back to frame coords. (Same final coord space
    as the prior list-based parser.)
    """
    in_h, in_w = input_hw
    f_h, f_w = frame_hw
    scale = min(in_w / f_w, in_h / f_h)
    pad_x = (in_w - f_w * scale) / 2
    pad_y = (in_h - f_h * scale) / 2

    arr = np.asarray(raw_out).ravel()
    results: List[dict] = []
    pos = 0
    for cls_idx in range(num_classes):
        if pos >= arr.size:
            break
        count = int(arr[pos])
        pos += 1
        if count <= 0:
            continue
        if pos + count * 5 > arr.size:
            break
        if cls_idx not in accept_classes:
            pos += count * 5
            continue
        for _ in range(count):
            y1n, x1n, y2n, x2n, conf = (
                float(arr[pos]), float(arr[pos + 1]), float(arr[pos + 2]),
                float(arr[pos + 3]), float(arr[pos + 4]),
            )
            pos += 5
            if conf < confidence_threshold:
                continue
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


