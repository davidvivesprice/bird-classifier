"""CameraProcessThread — orchestrates the per-camera pipeline stages."""
from __future__ import annotations
import logging
import queue
import threading
import time
from typing import Optional

from PIL import Image

from pipeline.frame import Frame
from pipeline.classifier import MAX_CLASSIFICATION_ATTEMPTS

log = logging.getLogger(__name__)

FORCED_FULL_YOLO_INTERVAL_S = 10.0


class CameraProcessThread:
    def __init__(self, name: str, frame_queue: queue.Queue,
                 motion_gate, detector, tracker, classifier,
                 event_store, annotator, health):
        self.name = name
        self.frame_queue = frame_queue
        self.motion_gate = motion_gate
        self.detector = detector
        self.tracker = tracker
        self.classifier = classifier
        self.event_store = event_store
        self.annotator = annotator
        self.health = health
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_forced_full = 0.0
        self._stats = {
            "frames_processed": 0,
            "detections": 0,
            "yolo_ms_samples": [],
        }

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name=f"proc-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self):
        while not self._stop.is_set():
            try:
                frame: Frame = self.frame_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process_frame(frame)
            except Exception as e:
                log.exception("[%s] process frame error: %s", self.name, e)

    def _process_frame(self, frame: Frame):
        self._stats["frames_processed"] += 1

        # 1. Motion gate
        regions = self.motion_gate.regions(frame.bgr)

        # 2. Decide whether to force a full-frame YOLO scan
        now = time.time()
        forced_full = (now - self._last_forced_full) > FORCED_FULL_YOLO_INTERVAL_S
        if forced_full:
            self._last_forced_full = now

        # 3. Detect
        t_det = time.monotonic()
        detections = self.detector.detect(frame, regions, forced_full=forced_full)
        det_ms = (time.monotonic() - t_det) * 1000
        self._stats["yolo_ms_samples"].append(det_ms)
        if len(self._stats["yolo_ms_samples"]) > 100:
            self._stats["yolo_ms_samples"] = self._stats["yolo_ms_samples"][-100:]
        self._stats["detections"] += len(detections)

        # 4. Track
        tracker_out = self.tracker.update(detections, frame.wall_time_ms)

        # 5. Classify tracks needing classification
        self._classify_tracks(frame, tracker_out.active)

        # 6. Write events (one per active track per frame)
        new_ids = {t.track_id for t in tracker_out.new}
        for track in tracker_out.active:
            self.event_store.write_event(
                camera=self.name,
                frame_time_ms=frame.wall_time_ms,
                track_id=track.track_id,
                species=track.species,
                confidence=track.confidence,
                model_source=track.model_source,
                bbox=track.bbox,
                is_new=(track.track_id in new_ids),
            )

        # 7. Track expired → write summary
        for track in tracker_out.expired:
            try:
                self.event_store.write_track_summary(
                    camera=self.name, track=track,
                    num_frames=self._stats["frames_processed"],
                )
            except Exception as e:
                log.warning("[%s] write_track_summary error: %s", self.name, e)

        # 8. Annotate + push
        self.annotator.submit(frame, tracker_out.active)

        # 9. Update health
        self._update_health(frame, det_ms)

    def _classify_tracks(self, frame: Frame, tracks: list):
        """Run SmartClassifier on any track that still needs classification."""
        for track in tracks:
            if not track.needs_classification:
                continue
            if track.classification_attempts >= MAX_CLASSIFICATION_ATTEMPTS:
                track.needs_classification = False
                continue

            # Crop the bird
            x1, y1, x2, y2 = [int(v) for v in track.bbox]
            x1 = max(0, x1); y1 = max(0, y1)
            x2 = min(frame.width, x2); y2 = min(frame.height, y2)
            if x2 <= x1 or y2 <= y1:
                track.needs_classification = False
                continue
            crop_bgr = frame.bgr[y1:y2, x1:x2]
            if crop_bgr.size == 0:
                track.needs_classification = False
                continue
            # OpenCV BGR → PIL RGB
            crop_pil = Image.fromarray(crop_bgr[:, :, ::-1])
            if crop_pil.size[0] < 5 or crop_pil.size[1] < 5:
                track.needs_classification = False
                continue

            track.classification_attempts += 1
            try:
                result = self.classifier.classify(
                    crop_pil, frame.wall_time_ms, self.name
                )
            except Exception as e:
                log.warning("[%s] classify error: %s", self.name, e)
                continue

            if result.should_retry:
                # Will retry on next frame (needs_classification stays True)
                continue
            # Got a final answer (species may be None = unlabeled)
            track.species = result.species
            if result.confidence:
                track.confidence = result.confidence
            track.model_source = result.model_source
            track.needs_classification = False

    def _update_health(self, frame: Frame, det_ms: float):
        import numpy as np
        samples = self._stats["yolo_ms_samples"]
        if len(samples) >= 10:
            yolo_avg = float(np.mean(samples))
            yolo_p99 = float(np.percentile(samples, 99))
        elif samples:
            yolo_avg = float(np.mean(samples))
            yolo_p99 = None  # insufficient_samples — honesty contract
        else:
            yolo_avg = 0.0
            yolo_p99 = None
        age_ms = (time.time() * 1000) - frame.wall_time_ms
        self.health.update(self.name, "capture", {
            "last_frame_age_ms": int(age_ms),
            "frames_processed": self._stats["frames_processed"],
        })
        self.health.update(self.name, "detector", {
            "yolo_ms_avg": round(yolo_avg),
            "yolo_ms_p99": round(yolo_p99) if yolo_p99 is not None else None,
            "yolo_samples_count": len(samples),
            "detections_total": self._stats["detections"],
        })
        try:
            self.health.update(self.name, "tracker", {
                "active_tracks": len(self.tracker.tracks),
                "stationary_tracks": len(self.tracker.stationary_regions()),
            })
        except Exception:
            pass
        try:
            self.health.update(self.name, "classifier", dict(self.classifier.stats))
        except Exception:
            pass
