"""Norfair-based bird tracker with Frigate-inspired distance function."""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import norfair
import numpy as np

from pipeline.detector import Detection

# Tracker hardening (2026-07-01, from the tracker deep-dive vs the demo replay):
# - MAX_JUMP_MULT: an absolute pixel-motion ceiling inside _frigate_distance.
#   The size-normalized distance has NO absolute cap, so a shrinking box can
#   match a detection on the far side of the frame — the "phantom/hog" track
#   that teleported cx 33->472 and drove most of the label reversions. Reject a
#   match when the detection jumped more than this many box-widths/heights in
#   RAW PIXELS. 0 disables.
# - DEDUP_IOU: greedy IoU suppression of the detection list BEFORE tracking, so
#   a double-box (two detections on one bird) can't spawn a second track_id
#   (Norfair matches exactly one detection per object). 0 disables.
_MAX_JUMP_MULT = float(os.environ.get("PIPELINE_TRACK_MAX_JUMP_MULT", "1.5"))
_DEDUP_IOU = float(os.environ.get("PIPELINE_TRACK_DEDUP_IOU", "0.55"))
# Stationary-box re-injection (Frigate's trick): when the detector misses a
# STILL bird, seed its last box as a synthetic detection so its track_id — and
# thus its accumulated votes/lock/label — survives the gap instead of dying and
# re-acquiring a fresh ID. Strictly stronger than Kalman coast (no counter decay,
# no drift). Capped at _MAX_SEED_FRAMES consecutive frames so a bird that truly
# flew off still expires. 0 disables.
# DEFAULT OFF (2026-07-01): on the demo replay seeding halves fragmentation
# (25->12 IDs) and lifts lock-hold, but it REGRESSES dup-box 0%->22.9% (the
# seeded ghost lingers and overlaps a real re-detection). It needs its companion
# fix first — overlay-suppression of the seed-coasting box + seed/real
# reconciliation — before it's safe to enable. Env-enable to keep iterating.
_MAX_SEED_FRAMES = int(os.environ.get("PIPELINE_TRACK_SEED_FRAMES", "0"))  # ~1.5s @30fps when on


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


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
    # True on frames where the track survives only via Kalman coast (no detection
    # matched this frame). The reported bbox is then FROZEN at the last detection
    # (see update(): bbox comes from last_detection), so consumers should render
    # coasting tracks as stale/held rather than as a live fix on the bird.
    coasting: bool = False

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

    # Absolute-motion ceiling (raw pixels). Without this, a shrinking box lets a
    # track claim a detection on the far side of the frame — the phantom/hog.
    if _MAX_JUMP_MULT > 0:
        max_w = max(det_w, trk_w, 1)
        max_h = max(det_h, trk_h, 1)
        if (abs(det_cx - trk_cx) > _MAX_JUMP_MULT * max_w or
                abs(det_by - trk_by) > _MAX_JUMP_MULT * max_h):
            return 1e6  # reject this match

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
        self._distance_threshold = distance_threshold
        self.tracks: dict = {}
        self.id_switches: int = 0

    def update(self, detections: list, frame_time_ms: float) -> TrackerOutput:
        # Dedup the detection list before tracking. A double-box (two detections
        # on one bird) would otherwise spawn a second track_id, since Norfair
        # matches exactly one detection per object. Greedy: keep the highest-
        # confidence box, drop any box overlapping a kept one above _DEDUP_IOU.
        if _DEDUP_IOU > 0 and len(detections) > 1:
            kept: list = []
            for d in sorted(detections, key=lambda x: -x.confidence):
                if all(_iou(d.box, k.box) <= _DEDUP_IOU for k in kept):
                    kept.append(d)
            detections = kept

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

        # Stationary-box re-injection: seed a synthetic detection at the last box
        # of any STILL track the detector missed this frame, so its ID/votes
        # survive the gap. Tracks that got a real box reset their seed counter.
        if _MAX_SEED_FRAMES > 0 and self.tracks:
            real_boxes = [nd.data["box"] for nd in norfair_dets]
            for trk in self.tracks.values():
                covered = any(_iou(trk.bbox, rb) > 0.3 for rb in real_boxes)
                if covered:
                    trk.seed_count = 0
                    continue
                if not trk.is_stationary or trk.seed_count >= _MAX_SEED_FRAMES:
                    continue
                x1, y1, x2, y2 = trk.bbox
                if x2 <= x1 or y2 <= y1:
                    continue
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                norfair_dets.append(norfair.Detection(
                    points=np.array([[cx, cy]]),
                    scores=np.array([0.6]),
                    data={"box": list(trk.bbox), "w": x2 - x1, "h": y2 - y1},
                ))
                trk.seed_count += 1

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

            # A track "got a hit" this frame when its hit_counter increased
            # (matched a detection) vs. decreased (coasting on Kalman prediction).
            got_hit = tobj.hit_counter > prev_hit_counters.get(tid, -1)
            if got_hit:
                track.frame_count += 1
            track.coasting = not got_hit

            # Update bbox from last detection
            if tobj.last_detection is not None:
                track.bbox = list(tobj.last_detection.data["box"])
                track.confidence = float(tobj.last_detection.scores[0])

            # Update motion history
            cx = (track.bbox[0] + track.bbox[2]) / 2
            cy = (track.bbox[1] + track.bbox[3]) / 2
            track.motion_history.append((cx, cy))

            active_tracks.append(track)

        # Expire tracks in our dict that norfair no longer tracks
        expired_ids = set(self.tracks.keys()) - seen_ids
        expired = [self.tracks.pop(tid) for tid in expired_ids]

        # ID-switch detection: a new track_id appears adjacent to an existing
        # track that just missed a detection this frame. This fires at the frame
        # where the switch occurs (not at the later expiry time), so hit_counter
        # state is the right signal. A track that "missed" has curr_hc < prev_hc.
        for new_t in new_tracks:
            if new_t.bbox == [0, 0, 0, 0]:
                continue
            ncx = (new_t.bbox[0] + new_t.bbox[2]) / 2
            ncy = (new_t.bbox[1] + new_t.bbox[3]) / 2
            nw = max(new_t.bbox[2] - new_t.bbox[0], 1)
            nh = max(new_t.bbox[3] - new_t.bbox[1], 1)
            for obj in tracked_objs:
                if obj.id == new_t.track_id:
                    continue
                if obj.id not in prev_hit_counters:
                    continue  # also new this frame
                if obj.hit_counter >= prev_hit_counters[obj.id]:
                    continue  # got a match; genuine parallel track
                if obj.last_detection is None:
                    continue
                det = obj.last_detection
                ecx = float(det.points[0][0])
                ecy = float(det.points[0][1])
                ew = max(float(det.data["w"]), 1)
                eh = max(float(det.data["h"]), 1)
                dx = abs(ncx - ecx) / ((nw + ew) / 2)
                dy = abs((ncy + nh / 2) - (ecy + eh / 2)) / ((nh + eh) / 2)
                if dx + dy < self._distance_threshold * 1.5:
                    self.id_switches += 1
                    break

        return TrackerOutput(
            active=active_tracks,
            new=new_tracks,
            expired=expired,
            frame_time_ms=frame_time_ms,
        )

    def stationary_regions(self) -> list:
        """Return bboxes of tracks that have been stationary for 10+ frames."""
        return [tuple(t.bbox) for t in self.tracks.values() if t.is_stationary]
