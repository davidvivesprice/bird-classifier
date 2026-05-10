"""Frame dataclass carried through the pipeline.

The CANONICAL CLOCK in this system is `pts` — the stream-relative timestamp
in seconds, extracted from the H.264 bitstream by PyAV. It comes from the
camera's encoder and is independent of NTP, wall-clock, or any local clock.
Every component that needs to sync (snapshot writer, SSE events, browser
overlay) keys off `pts`.

`wall_time_ms` is kept for log timestamps and filename generation only —
"this snapshot was taken at 8:46am" is wall-clock; "this label belongs to
this video frame" is PTS. Never use wall_time_ms for sync decisions.
"""
from dataclasses import dataclass
from typing import Optional
import numpy as np


@dataclass
class Frame:
    """A decoded video frame.

    bgr: detection-sized BGR (e.g. 640×360). Used by motion gate, YOLO,
        classifier — everything that operates on the small frame for speed.
    bgr_full: optional full-resolution BGR (e.g. 1920×1080). Used by
        SnapshotWriter for the saved photo. Same physical camera moment as
        `bgr`; in single-stream mode they're literally the same buffer
        downscaled, so they cannot disagree.
    pts: stream PTS in seconds. THE clock. Compare/sort/match by this.
    wall_time_ms: unix milliseconds at decode. For log lines and filenames
        only. Do not use for sync.
    camera: camera name (e.g. "feeder").
    width, height: dimensions of `bgr` (detection size).
    full_width, full_height: dimensions of `bgr_full` if present.
    """
    bgr: np.ndarray
    wall_time_ms: float
    camera: str
    width: int
    height: int
    pts: float = 0.0
    bgr_full: Optional[np.ndarray] = None
    full_width: Optional[int] = None
    full_height: Optional[int] = None
