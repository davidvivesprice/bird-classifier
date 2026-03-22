"""
Motion gate — skip frames where nothing moved since the last frame.

Drop-in module for classify.py. Compares each incoming image against the
previous frame from the same camera using cv2.absdiff(). If the change
is below a threshold, the frame is skipped without running YOLO or AIY.

Usage in classify.py:
    from motion_gate import MotionGate
    gate = MotionGate(threshold_pct=1.5, resize_width=320)

    # In the processing loop, before process_file():
    if not gate.has_motion(image_path, camera="feeder"):
        logging.debug("No motion, skipping: %s", fname)
        continue
"""

import cv2
import logging
import numpy as np
import os


class MotionGate:
    """Compare consecutive frames per camera; skip if change is below threshold.

    Args:
        threshold_pct: Minimum percentage of pixels that must change to
                       count as motion.  Default 1.5% works well for
                       static feeder cameras.  Lower = more sensitive.
        resize_width:  Downscale frames before comparison.  320px is plenty
                       for motion detection and keeps it fast (~1ms per check).
        blur_kernel:   Gaussian blur kernel size to suppress noise/compression
                       artifacts.  Must be odd.
        pixel_threshold: Per-pixel intensity difference to count as "changed".
                         Default 25 (out of 255) filters sensor noise.
    """

    def __init__(self, threshold_pct=1.5, resize_width=320,
                 blur_kernel=21, pixel_threshold=25):
        self.threshold_pct = threshold_pct
        self.resize_width = resize_width
        self.blur_kernel = blur_kernel
        self.pixel_threshold = pixel_threshold
        self._prev_frames = {}  # camera → grayscale frame
        self._stats = {"checked": 0, "skipped": 0}

    def has_motion(self, image_or_path, camera="feeder"):
        """Return True if the frame has meaningful motion vs the previous frame.

        Args:
            image_or_path: File path (str/Path) or numpy BGR/RGB array.
                           When a numpy array is passed, cv2.imread is skipped.
            camera: Camera identifier for per-camera tracking.

        Always returns True for the first frame per camera (no reference).
        Returns True on any error (fail-open — never block the pipeline).
        """
        self._stats["checked"] += 1
        try:
            if isinstance(image_or_path, np.ndarray):
                img = image_or_path
            else:
                img = cv2.imread(str(image_or_path))
            if img is None:
                return True  # Can't read → let process_file handle it

            # Downscale + grayscale + blur
            h, w = img.shape[:2]
            scale = self.resize_width / w
            small = cv2.resize(img, (self.resize_width, int(h * scale)),
                               interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (self.blur_kernel, self.blur_kernel), 0)

            prev = self._prev_frames.get(camera)
            self._prev_frames[camera] = gray

            if prev is None:
                return True  # First frame for this camera

            # Pixel-wise diff
            delta = cv2.absdiff(gray, prev)
            changed = np.count_nonzero(delta > self.pixel_threshold)
            total = delta.size
            pct = (changed / total) * 100

            if pct < self.threshold_pct:
                self._stats["skipped"] += 1
                label = camera if isinstance(image_or_path, np.ndarray) else os.path.basename(str(image_or_path))
                logging.debug("Motion gate: %.2f%% change (<%s%% threshold), skipping %s",
                              pct, self.threshold_pct, label)
                return False

            return True

        except Exception as exc:
            # Fail-open: if anything goes wrong, let the frame through
            logging.warning("Motion gate error, passing through: %s", exc)
            return True

    @property
    def skip_rate(self):
        """Percentage of frames skipped by the motion gate."""
        if self._stats["checked"] == 0:
            return 0.0
        return (self._stats["skipped"] / self._stats["checked"]) * 100

    @property
    def stats(self):
        return dict(self._stats)
