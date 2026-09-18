"""Tracker-agnostic pieces shared by the v3 (Norfair) and v4 trackers:
the `Track` / `TrackerOutput` contract every downstream consumer reads,
box geometry helpers, the detection-dedup knobs, and the factory that picks
the tracker implementation (PIPELINE_TRACKER=v4|v3).

Kept norfair-free so v4 (and its tests) never import norfair.
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# DEDUP_IOU: greedy IoU suppression of the detection list BEFORE tracking, so
# a double-box (two detections on one bird) can't spawn a second track_id.
# DEDUP_CONT: containment companion — YOLO's classic bird dup is a head-box
# INSIDE the body-box: IoU 0.1-0.4 (slips the IoU gate) but
# intersection/min-area ~1.0. 0 disables either.
_DEDUP_IOU = float(os.environ.get("PIPELINE_TRACK_DEDUP_IOU", "0.55"))
_DEDUP_CONT = float(os.environ.get("PIPELINE_TRACK_DEDUP_CONT", "0.65"))


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _containment(a, b) -> float:
    """Intersection over the SMALLER box's area — 1.0 when one box sits fully
    inside the other, regardless of the size ratio (which is what keeps IoU
    low for head-in-body dups)."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    min_area = min((a[2] - a[0]) * (a[3] - a[1]),
                   (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min_area if min_area > 0 else 0.0


@dataclass
class Track:
    track_id: int
    created_at_ms: float
    last_updated_ms: float
    bbox: list = field(default_factory=lambda: [0, 0, 0, 0])
    confidence: float = 0.0
    species: Optional[str] = None
    species_confidence: Optional[float] = None
    model_source: Optional[str] = None
    trust_level: str = "normal"
    needs_classification: bool = True
    classification_attempts: int = 0
    frame_count: int = 0
    motion_history: deque = field(default_factory=lambda: deque(maxlen=10))
    vote_history: list = field(default_factory=list)
    is_locked: bool = False
    snapshot_saved: bool = False  # set True once we've written JPG + DB row for this track
    seed_count: int = 0  # consecutive frames this track has been kept alive by seeding
    # True on frames where the track survives only by coasting (no detection
    # matched this frame). The reported bbox is then FROZEN at the last
    # detection, so consumers should render coasting tracks as stale/held
    # rather than as a live fix on the bird.
    coasting: bool = False
    # Classification pacing + post-lock verification state (2026-07-04):
    # frame_count value at the last classify call (cadence + cooldown anchor);
    # consecutive classify calls that produced no vote (sub-floor crops);
    # consecutive verified votes disagreeing with a locked species.
    last_classify_fc: int = -1_000_000
    no_vote_streak: int = 0
    lock_disagreements: int = 0
    # Display-path label contract (2026-09-18, SSE schema 2). tentative: a lock
    # demoted as unverifiable but the species deliberately kept — rendered like
    # locked. label_epoch: +1 on every transition of the DISPLAYED state (lock,
    # unlock, relabel while locked/tentative), never on candidate churn — the
    # client rewrites its buffered frames when it changes. lock_pts: pts of the
    # frame that established the current lock (kept through tentative).
    # seg_pts: pts where the current contiguous visible segment began (spawn,
    # revival, merge-adopted young) — the split rekey boundary.
    tentative: bool = False
    label_epoch: int = 0
    lock_pts: Optional[float] = None
    seg_pts: Optional[float] = None

    @property
    def is_stationary(self) -> bool:
        if len(self.motion_history) < 10:
            return False
        xs = [p[0] for p in self.motion_history]
        ys = [p[1] for p in self.motion_history]
        return (max(xs) - min(xs)) < 10 and (max(ys) - min(ys)) < 10


@dataclass
class TrackerOutput:
    active: list
    new: list
    expired: list
    frame_time_ms: float


def make_tracker():
    """Build the configured tracker. PIPELINE_TRACKER=v4 (default) selects
    BirdTrackerV4; v3 keeps the Norfair tracker for A/B against the demo lab.
    The v3 params stay env-tunable exactly as before."""
    kind = os.environ.get("PIPELINE_TRACKER", "v4").strip().lower()
    kwargs = dict(
        distance_threshold=float(os.environ.get("PIPELINE_TRACK_DIST", "2.5")),
        hit_counter_max=int(os.environ.get("PIPELINE_TRACK_HIT_MAX", "150")),
        initialization_delay=int(os.environ.get("PIPELINE_TRACK_INIT_DELAY", "2")),
    )
    if kind == "v3":
        from pipeline.tracker import BirdTracker
        return BirdTracker(**kwargs)
    from pipeline.tracker_v4 import BirdTrackerV4
    return BirdTrackerV4(**kwargs)
