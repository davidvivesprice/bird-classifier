"""MotionGate — OpenCV background subtraction → motion regions."""
import cv2
import numpy as np


class MotionGate:
    """Background subtraction motion gate that emits regions.

    Usage:
        gate = MotionGate()
        regions = gate.regions(bgr_frame)  # list of (x1,y1,x2,y2)
    """

    def __init__(self, history: int = 500, var_threshold: int = 16,
                 min_region_area: int = 400, pad: int = 20):
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=False,
        )
        self.min_region_area = min_region_area
        self.pad = pad
        self._kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

    def regions(self, bgr_frame: np.ndarray) -> list:
        """Return list of motion bounding boxes (x1,y1,x2,y2) in frame coordinates."""
        mask = self.bg.apply(bgr_frame)
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
