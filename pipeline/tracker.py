"""Norfair-based bird tracker with Frigate-inspired distance function."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import norfair
import numpy as np

from pipeline.detector import Detection


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


def _frigate_distance(detection: norfair.Detection,
                      tracked: norfair.TrackedObject) -> float:
    """Frigate-inspired distance: centroid-x + bottom-y normalized by size.

    - X-distance normalized by average object width
    - Y-distance uses BOTTOM of box (stable under perspective, more
      consistent for perched birds than using centroid-y)
    - Both normalized by size so small/large birds have similar thresholds
    """
    det_data = detection.data
    trk_det = tracked.last_detection
    trk_data = trk_det.data

    det_w = det_data["w"]
    det_h = det_data["h"]
    trk_w = trk_data["w"]
    trk_h = trk_data["h"]

    det_cx = detection.points[0][0]
    det_cy = detection.points[0][1]
    trk_cx = trk_det.points[0][0]
    trk_cy = trk_det.points[0][1]

    d_x = abs(det_cx - trk_cx) / max((det_w + trk_w) / 2, 1)
    det_by = det_cy + det_h / 2  # bottom-y
    trk_by = trk_cy + trk_h / 2
    d_y = abs(det_by - trk_by) / max((det_h + trk_h) / 2, 1)

    return d_x + d_y


class BirdTracker:
    """Norfair wrapper with frigate_distance and stationary detection."""

    def __init__(self, distance_threshold: float = 2.0,
                 hit_counter_max: int = 15, initialization_delay: int = 1):
        # 2026-04-17: bumped from 1.0 → 2.0. Threshold = normalized (dx_norm +
        # dy_norm) per Frigate-style distance. At 5fps effective detection rate,
        # a bird flying 1 body-width in 200ms is normal motion; the old 1.0
        # threshold lost the track on anything faster → new track_id → label
        # change mid-flight. 2.0 tolerates 2 body-widths between frames.
        # Larger threshold can cause two-bird confusion when tracks cross; with
        # typical 1-3 simultaneous tracks on this feeder it's a safe trade.
        self.norfair = norfair.Tracker(
            distance_function=_frigate_distance,
            distance_threshold=distance_threshold,
            hit_counter_max=hit_counter_max,
            initialization_delay=initialization_delay,
        )
        self.tracks: dict = {}
        self.id_switches: int = 0  # count of track ID changes due to distance threshold
        self.prev_centroids: dict = {}  # track_id → (cx, cy) for switch detection

    def update(self, detections: list, frame_time_ms: float) -> TrackerOutput:
        # Convert Detection → norfair.Detection
        norfair_dets = []
        for d in detections:
            x1, y1, x2, y2 = d.box
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            norfair_dets.append(norfair.Detection(
                points=np.array([[cx, cy]]),
                scores=np.array([d.confidence]),
                data={"box": list(d.box), "w": x2 - x1, "h": y2 - y1},
            ))

        # Snapshot hit_counters before update to detect which tracks got a hit.
        # A track "got a hit" this frame when its hit_counter increases (matched
        # to a detection) vs. decreases (coasting on Kalman prediction).
        prev_hit_counters = {o.id: o.hit_counter for o in self.norfair.tracked_objects}

        tracked_objs = self.norfair.update(detections=norfair_dets)

        new_tracks = []
        active_tracks = []
        seen_ids = set()

        for tobj in tracked_objs:
            tid = tobj.id
            seen_ids.add(tid)
            is_new = tid not in self.tracks
            
            # Detect ID-switch: if same spatial region got a new track_id
            if tobj.last_detection is not None:
                cx = tobj.last_detection.points[0][0]
                cy = tobj.last_detection.points[0][1]
                # Check if any nearby previous centroid from a different track_id
                # would suggest this is the same physical object with a new ID
                for prev_tid, (prev_cx, prev_cy) in list(self.prev_centroids.items()):
                    if prev_tid != tid:
                        dist = ((cx - prev_cx)**2 + (cy - prev_cy)**2)**0.5
                        if dist < 50:  # < 50 pixels = probably same bird, different ID
                            self.id_switches += 1
                            break
            if is_new:
                track = Track(
                    track_id=tid,
                    created_at_ms=frame_time_ms,
                    last_updated_ms=frame_time_ms,
                )
                self.tracks[tid] = track
                new_tracks.append(track)
            else:
                track = self.tracks[tid]
                track.last_updated_ms = frame_time_ms

            # Increment frame_count only when this track received a detection hit
            # this frame (hit_counter increased), not when it's coasting via Kalman.
            if tobj.hit_counter > prev_hit_counters.get(tid, -1):
                track.frame_count += 1

            # Update bbox from last detection
            if tobj.last_detection is not None:
                track.bbox = list(tobj.last_detection.data["box"])
                track.confidence = float(tobj.last_detection.scores[0])

            # Update motion history
            cx = (track.bbox[0] + track.bbox[2]) / 2
            cy = (track.bbox[1] + track.bbox[3]) / 2
            track.motion_history.append((cx, cy))
            self.prev_centroids[tid] = (cx, cy)

            active_tracks.append(track)

        # Expire tracks in our dict that norfair no longer tracks
        expired_ids = set(self.tracks.keys()) - seen_ids
        expired = [self.tracks.pop(tid) for tid in expired_ids]
        # Clean up expired centroids
        for tid in expired_ids:
            self.prev_centroids.pop(tid, None)

        return TrackerOutput(
            active=active_tracks,
            new=new_tracks,
            expired=expired,
            frame_time_ms=frame_time_ms,
        )

    def stationary_regions(self) -> list:
        """Return bboxes of tracks that have been stationary for 10+ frames."""
        return [tuple(t.bbox) for t in self.tracks.values() if t.is_stationary]
