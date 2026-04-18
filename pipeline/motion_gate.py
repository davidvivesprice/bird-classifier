"""MotionGate — OpenCV background subtraction → motion regions.

Optionally applies an Area-of-Interest (AOI) polygon mask: motion pixels
outside the polygon are zeroed before contour detection, so regions that lie
entirely outside the AOI never propagate downstream. Main consumer is the v3
pipeline's CameraProcessThread, which skips YOLO when `regions` is empty —
so an AOI directly reduces YOLO calls.

Polygon is expressed in frame-native pixel coordinates (typically the 640x360
substream for detection). Format: list of (x, y) tuples, any polygon shape.
"""
import cv2
import numpy as np


class MotionGate:
    """Background subtraction motion gate that emits regions.

    Usage:
        gate = MotionGate()
        regions = gate.regions(bgr_frame)  # list of (x1,y1,x2,y2)

        # With AOI:
        gate = MotionGate(aoi_polygon=[(96, 306), (128, 198), (512, 198), (544, 306)],
                          frame_width=640, frame_height=360)
    """

    def __init__(self, history: int = 500, var_threshold: int = 16,
                 min_region_area: int = 400, pad: int = 20,
                 aoi_polygon: list = None,
                 frame_width: int = 640, frame_height: int = 360):
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=False,
        )
        self.min_region_area = min_region_area
        self.pad = pad
        self._kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

        # AOI mask: single-channel uint8, 255 inside polygon, 0 outside.
        # Applied via bitwise_and to the BG-subtracted motion mask before
        # morphology + contour detection. None = no AOI, process whole frame.
        self._aoi_mask = None
        self._aoi_polygon = None
        if aoi_polygon:
            self._aoi_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
            pts = np.array(aoi_polygon, dtype=np.int32)
            cv2.fillPoly(self._aoi_mask, [pts], 255)
            self._aoi_polygon = list(aoi_polygon)

    def regions(self, bgr_frame: np.ndarray) -> list:
        """Return list of motion bounding boxes (x1,y1,x2,y2) in frame coordinates."""
        mask = self.bg.apply(bgr_frame)

        # Apply AOI mask before morphology so noise outside the zone can't
        # leak back in via dilation.
        if self._aoi_mask is not None:
            # Resize mask if frame doesn't match (defensive; normally exact).
            if self._aoi_mask.shape != mask.shape:
                self._aoi_mask = cv2.resize(
                    self._aoi_mask, (mask.shape[1], mask.shape[0]),
                    interpolation=cv2.INTER_NEAREST,
                )
            mask = cv2.bitwise_and(mask, self._aoi_mask)

        # Clean up noise
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel)

        # Find contours
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        h, w = bgr_frame.shape[:2]
        regions = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_region_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            x1 = max(0, x - self.pad)
            y1 = max(0, y - self.pad)
            x2 = min(w, x + bw + self.pad)
            y2 = min(h, y + bh + self.pad)
            regions.append((x1, y1, x2, y2))
        return regions
