"""Frame dataclass carried through the pipeline."""
from dataclasses import dataclass
import numpy as np


@dataclass
class Frame:
    """A decoded video frame with metadata.

    bgr: numpy array of shape (H, W, 3), uint8, BGR color order (OpenCV convention)
    wall_time_ms: unix milliseconds when the frame was captured
    camera: camera name (e.g., "feeder", "ground")
    width, height: frame dimensions in pixels
    """
    bgr: np.ndarray
    wall_time_ms: float
    camera: str
    width: int
    height: int
