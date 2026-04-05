"""bird_tracker — IoU-based multi-bird tracker for real-time detection.

Tracks birds across video frames by matching bounding box overlap (IoU).
Each track has a species label, bounding box, confidence, and keeper frame.
Tracks expire when no matching detection appears for expire_seconds.

Used by bird_pipeline.py for the live detection overlay.
"""

import time
import uuid


def _iou(box_a, box_b):
    """Compute Intersection over Union between two boxes [x1,y1,x2,y2]."""
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - inter
    if union == 0:
        return 0.0
    return inter / union


class Track:
    __slots__ = ("track_id", "species", "bbox", "confidence",
                 "created", "updated", "keeper_data", "keeper_confidence",
                 "trust_level")

    def __init__(self, track_id, species, bbox, confidence, frame_data=None, trust_level="normal"):
        self.track_id = track_id
        self.species = species
        self.bbox = bbox
        self.confidence = confidence
        self.created = time.monotonic()
        self.updated = time.monotonic()
        self.keeper_data = frame_data
        self.keeper_confidence = confidence
        self.trust_level = trust_level


class BirdTracker:
    """Track birds across frames by bounding box IoU overlap.

    Args:
        iou_threshold: Minimum IoU to match a detection to existing track (default 0.3)
        expire_seconds: Seconds with no match before track expires (default 3.0)
        max_tracks: Maximum concurrent tracks per camera (default 20)
        max_lifetime: Hard max track lifetime in seconds (default 600 = 10 min)
    """

    def __init__(self, iou_threshold=0.3, expire_seconds=3.0,
                 max_tracks=20, max_lifetime=600):
        self.iou_threshold = iou_threshold
        self.expire_seconds = expire_seconds
        self.max_tracks = max_tracks
        self.max_lifetime = max_lifetime
        self.tracks: dict[int, Track] = {}
        self._next_id = 0
        self.session_id = uuid.uuid4().hex[:8]

    def _new_id(self):
        tid = self._next_id
        self._next_id += 1
        return tid

    def update(self, detections, species_list, frame_data=None, trust_levels=None):
        """Match detections to existing tracks. Create new tracks for unmatched.

        Args:
            detections: list of {"box": [x1,y1,x2,y2], "confidence": float}
            species_list: list of species names, parallel to detections
            frame_data: optional bytes/object to store as keeper frame
            trust_levels: optional list of trust levels, parallel to detections

        Returns:
            list of track state dicts for SSE broadcast
        """
        if trust_levels is None:
            trust_levels = ["normal"] * len(detections)

        now = time.monotonic()
        matched_track_ids = set()
        new_track_ids = set()

        for det, species, trust in zip(detections, species_list, trust_levels):
            box = det["box"]
            conf = det["confidence"]

            # Find best IoU match among existing tracks
            best_iou = 0
            best_track = None
            for track in self.tracks.values():
                iou = _iou(box, track.bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_track = track

            if best_track and best_iou >= self.iou_threshold:
                # Update existing track
                best_track.bbox = box
                best_track.confidence = conf
                best_track.updated = now
                matched_track_ids.add(best_track.track_id)
                # Update keeper if this frame is better
                if frame_data and conf > best_track.keeper_confidence:
                    best_track.keeper_data = frame_data
                    best_track.keeper_confidence = conf
            else:
                # New track
                tid = self._new_id()
                track = Track(tid, species, box, conf, frame_data, trust_level=trust)
                self.tracks[tid] = track
                matched_track_ids.add(tid)
                new_track_ids.add(tid)

        # Evict if over max
        while len(self.tracks) > self.max_tracks:
            oldest_id = min(self.tracks, key=lambda k: self.tracks[k].created)
            del self.tracks[oldest_id]

        return self._build_states(now, new_track_ids)

    def get_expired_tracks(self):
        """Remove and return tracks that have expired."""
        now = time.monotonic()
        expired = []
        to_remove = []
        for tid, track in self.tracks.items():
            age = now - track.updated
            lifetime = now - track.created
            if age > self.expire_seconds or lifetime > self.max_lifetime:
                expired.append({
                    "track_id": track.track_id,
                    "species": track.species,
                    "bbox": track.bbox,
                    "keeper_data": track.keeper_data,
                    "keeper_confidence": track.keeper_confidence,
                    "duration": now - track.created,
                })
                to_remove.append(tid)
        for tid in to_remove:
            del self.tracks[tid]
        return expired

    def get_active_tracks(self):
        """Return current active track states."""
        return self._build_states(time.monotonic())

    def _build_states(self, now, new_track_ids=None):
        if new_track_ids is None:
            new_track_ids = set()
        return [
            {
                "track_id": t.track_id,
                "species": t.species,
                "bbox": t.bbox,
                "confidence": t.confidence,
                "is_new": t.track_id in new_track_ids,
                "age_seconds": round(now - t.created, 1),
                "keeper_data": t.keeper_data,
                "trust_level": t.trust_level,
            }
            for t in self.tracks.values()
        ]
