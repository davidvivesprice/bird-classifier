"""CameraProcessThread — orchestrates the per-camera pipeline stages."""
from __future__ import annotations
import logging
import os
import queue
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING, Optional

import numpy as np
from PIL import Image

from pipeline.frame import Frame
from pipeline.classifier import MAX_CLASSIFICATION_ATTEMPTS

if TYPE_CHECKING:
    from pipeline.frame_capture import FrameCapture

log = logging.getLogger(__name__)

FORCED_FULL_YOLO_INTERVAL_S = 10.0


class CameraProcessThread:
    def __init__(self, name: str, frame_queue: queue.Queue,
                 motion_gate, detector, tracker, classifier,
                 event_store, health=None, sse_server=None,
                 frame_width: int = 640, frame_height: int = 360,
                 capture: "Optional[FrameCapture]" = None):
        self.name = name
        self.frame_queue = frame_queue
        self.motion_gate = motion_gate
        self.detector = detector
        self.tracker = tracker
        self.classifier = classifier
        self.event_store = event_store
        self.health = health
        self.sse_server = sse_server
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.capture = capture
        self._dry_run = os.environ.get("PIPELINE_DRY_RUN") == "1"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_forced_full = 0.0
        self._stats = {
            "frames_processed": 0,
            "detections": 0,
            "yolo_ms_samples": [],
            "yolo_runs_total": 0,
            "yolo_skipped_motion": 0,
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
        # Only record the timing when YOLO actually ran. BirdDetector.detect returns
        # an empty list instantly when motion_regions is empty and forced_full is False —
        # those near-zero timings pollute yolo_ms_avg (observed: ground cam 7ms because
        # most frames are skipped). Filter them out so the histogram reflects real
        # inference cost, and track "frames where YOLO was actually invoked" separately.
        yolo_actually_ran = bool(regions) or forced_full
        if yolo_actually_ran:
            self._stats["yolo_ms_samples"].append(det_ms)
            if len(self._stats["yolo_ms_samples"]) > 100:
                self._stats["yolo_ms_samples"] = self._stats["yolo_ms_samples"][-100:]
            self._stats["yolo_runs_total"] += 1
        else:
            self._stats["yolo_skipped_motion"] += 1
        self._stats["detections"] += len(detections)

        # 4. Track
        tracker_out = self.tracker.update(detections, frame.wall_time_ms)

        # 5. Classify tracks needing classification
        self._classify_tracks(frame, tracker_out.active)

        # 6. Write events to DB (skipped in dry-run / testing mode)
        if not self._dry_run:
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

        # 6b. Emit SSE event for live dashboard consumption
        if tracker_out.active and self.sse_server is not None:
            tracks_payload = []
            for track in tracker_out.active:
                bbox = list(track.bbox)
                tracks_payload.append({
                    "track_id": track.track_id,
                    "bbox": bbox,
                    "bbox_center_x": (bbox[0] + bbox[2]) // 2,
                    "frame_width": self.frame_width,
                    "frame_height": self.frame_height,
                    "species": track.species,
                    "species_confidence": getattr(track, "species_confidence", None),
                    "model_source": track.model_source,
                    "is_locked": track.is_locked,
                    "frame_count": getattr(track, "frame_count", 0),
                })
            self.sse_server.emit(
                camera=self.name,
                wall_time_ms=int(frame.wall_time_ms),
                tracks=tracks_payload,
            )

        # 7. Track expired → write summary (skipped in dry-run)
        if not self._dry_run:
            for track in tracker_out.expired:
                try:
                    self.event_store.write_track_summary(
                        camera=self.name, track=track,
                        num_frames=track.frame_count,
                    )
                except Exception as e:
                    log.warning("[%s] write_track_summary error: %s", self.name, e)

        # 8. Debug frame: draw YOLO boxes on a small copy for /debug/latest.jpg
        #    Only runs if health_server has the debug slot (low cost: one
        #    cv2.resize + a few cv2.rectangle per frame, no JPEG encode
        #    on frames with no tracks).
        if tracker_out.active and hasattr(self.health, 'latest_debug_jpeg'):
            try:
                import cv2
                h, w = frame.bgr.shape[:2]
                debug = frame.bgr.copy() if (w, h) == (640, 360) else cv2.resize(frame.bgr, (640, 360), interpolation=cv2.INTER_LINEAR)
                for track in tracker_out.active:
                    x1, y1, x2, y2 = [int(v) for v in track.bbox]
                    color = (128, 222, 74) if getattr(track, 'is_locked', False) else (21, 204, 250)
                    cv2.rectangle(debug, (x1, y1), (x2, y2), color, 2)
                    label = getattr(track, 'species', None) or '...'
                    conf = getattr(track, 'species_confidence', None)
                    if conf is not None:
                        label += f' {int(conf*100)}%'
                    cv2.putText(debug, label, (x1, max(y1-6, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
                ok, jpeg = cv2.imencode('.jpg', debug, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self.health.latest_debug_jpeg[self.name] = jpeg.tobytes()
            except Exception:
                pass

        # 9. Update health
        self._update_health(frame, det_ms)

    def _classify_tracks(self, frame: Frame, tracks: list):
        """Run SmartClassifier on tracks that still need classification.

        Phase 2 voting: accumulate classification votes across multiple frames.
        Lock the species only when enough votes agree. This fixes the
        "first-blurry-crop permanently mislabels the bird" problem from Phase 1.
        """
        for track in tracks:
            if not track.needs_classification:
                continue
            if track.classification_attempts >= MAX_CLASSIFICATION_ATTEMPTS:
                # Attempt cap reached without consensus. Take the plurality
                # winner if any votes exist, otherwise leave unlabeled.
                if track.vote_history and not track.is_locked:
                    species_counts = Counter(s for s, c in track.vote_history)
                    top_species, top_count = species_counts.most_common(1)[0]
                    top_conf = max(c for s, c in track.vote_history if s == top_species)
                    track.species = top_species
                    track.species_confidence = top_conf
                    track.model_source = "vote_plurality"
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

            # Got a result — add to vote history
            if result.species is not None:
                track.vote_history.append((result.species, result.confidence))
                # Propagate model_source from the latest vote
                track.model_source = result.model_source
                # Show the current top-voted species even before lock
                # (so the label shows something while votes accumulate)
                species_counts = Counter(s for s, c in track.vote_history)
                top_species, _ = species_counts.most_common(1)[0]
                track.species = top_species
                track.species_confidence = max(
                    c for s, c in track.vote_history if s == top_species
                )

                # Check lock condition
                if (len(track.vote_history) >= 3 and
                        track.species_confidence >= 0.6 and
                        species_counts[top_species] / len(track.vote_history) >= 0.6):
                    track.is_locked = True
                    track.needs_classification = False
            else:
                # Classifier returned None (unlabeled) — counts as an attempt
                # but doesn't add a vote. Track stays needs_classification=True
                # for next frame.
                pass

    def _update_health(self, frame: Frame, det_ms: float):
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
        capture_payload = {
            "last_frame_age_ms": int(age_ms),
            "frames_processed": self._stats["frames_processed"],
        }
        if getattr(self, "capture", None) is not None:
            # Merge FrameCapture's own stats so honesty-contract fields
            # (ffmpeg_restarts, dropped_oldest, ffmpeg_restarts_last_hour)
            # actually exist in the health snapshot.
            capture_payload["frames_captured"] = self.capture.stats.get("frames", 0)
            capture_payload["dropped_oldest"] = self.capture.stats.get("dropped_oldest", 0)
            capture_payload["ffmpeg_restarts"] = self.capture.stats.get("ffmpeg_restarts", 0)
            capture_payload["ffmpeg_restarts_last_hour"] = self.capture.restarts_last_hour()
        self.health.update(self.name, "capture", capture_payload)
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
            cam_classifier_stats = self.classifier.stats.get(self.name, {})
            self.health.update(self.name, "classifier", dict(cam_classifier_stats))
        except Exception:
            pass
