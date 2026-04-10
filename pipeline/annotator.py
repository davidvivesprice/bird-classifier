"""FrameAnnotator — draws labels on frames and encodes as JPEG."""
from __future__ import annotations
import logging
import queue
import threading
from typing import Optional

import cv2
import numpy as np

from pipeline.frame import Frame
from pipeline.tracker import Track

log = logging.getLogger(__name__)

LABEL_COLOR_NORMAL = (74, 222, 128)   # BGR green
LABEL_COLOR_MUTED = (128, 128, 128)   # BGR gray for unlabeled
LABEL_BG = (0, 0, 0)
JPEG_QUALITY = 75


class FrameAnnotator:
    def __init__(self, camera_name: str, debug_stream,
                 out_width: int = 960, out_height: int = 540):
        self.camera_name = camera_name
        self.debug_stream = debug_stream
        self.out_width = out_width
        self.out_height = out_height
        self.queue: queue.Queue = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name=f"annot-{self.camera_name}", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.queue.put_nowait(None)  # wake thread to exit
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def submit(self, frame: Frame, tracks: list):
        """Non-blocking: drop oldest if queue full."""
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.queue.put_nowait((frame, list(tracks)))
        except queue.Full:
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            frame, tracks = item
            try:
                jpeg_bytes = self._annotate(frame.bgr, tracks)
                self.debug_stream.push(self.camera_name, jpeg_bytes, frame.wall_time_ms)
            except Exception as e:
                # Never let annotator errors take down the thread
                log.warning("[%s] annotator error: %s", self.camera_name, e)

    def _annotate(self, bgr: np.ndarray, tracks: list) -> bytes:
        h_src, w_src = bgr.shape[:2]
        out = cv2.resize(
            bgr, (self.out_width, self.out_height),
            interpolation=cv2.INTER_LINEAR,
        )
        scale_x = self.out_width / w_src
        scale_y = self.out_height / h_src

        for track in tracks:
            x1 = int(track.bbox[0] * scale_x)
            y1 = int(track.bbox[1] * scale_y)
            x2 = int(track.bbox[2] * scale_x)
            y2 = int(track.bbox[3] * scale_y)
            cx = (x1 + x2) // 2
            label_y = max(22, y1 - 8)  # above the bird, clamped to visible

            if track.species:
                label = track.species
                color = LABEL_COLOR_NORMAL
            else:
                label = "·"
                color = LABEL_COLOR_MUTED

            self._draw_label_pill(out, label, cx, label_y, color)
            if track.model_source == "both_agree":
                self._draw_checkmark(out, label, cx, label_y)

        ok, jpeg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            return b""
        return jpeg.tobytes()

    def _draw_label_pill(self, img, text, cx, cy, color):
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.5
        thickness = 1
        (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
        pad_x = 7
        pad_y = 4
        x1 = cx - (tw // 2) - pad_x
        x2 = cx + (tw // 2) + pad_x
        y1 = cy - th - pad_y
        y2 = cy + pad_y
        # Semi-transparent background
        overlay = img.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), LABEL_BG, -1)
        cv2.addWeighted(overlay, 0.75, img, 0.25, 0, dst=img)
        # Text
        cv2.putText(img, text, (cx - tw // 2, cy - 2),
                    font, scale, color, thickness, cv2.LINE_AA)

    def _draw_checkmark(self, img, text, cx, cy):
        """Small double-check badge to the right of the label."""
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.4
        (tw, _), _ = cv2.getTextSize(text, font, 0.5, 1)
        x = cx + tw // 2 + 10
        y = cy - 2
        cv2.putText(img, "vv", (x, y), font, scale,
                    (255, 255, 255), 1, cv2.LINE_AA)
