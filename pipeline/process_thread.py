"""CameraProcessThread — orchestrates the per-camera pipeline stages."""
from __future__ import annotations
import logging
import os
import queue
import threading
import time
from collections import Counter, deque
from typing import TYPE_CHECKING, Optional

import numpy as np
from PIL import Image

from pipeline.frame import Frame
from pipeline.classifier import MAX_CLASSIFICATION_ATTEMPTS
from pipeline.constants import ModelSource
from pipeline.track_disagreement_detector import TrackDisagreementDetector

if TYPE_CHECKING:
    from pipeline.frame_capture import FrameCapture

log = logging.getLogger(__name__)

FORCED_FULL_YOLO_INTERVAL_S = 10.0
# Vote-lock gate, now a CALIBRATED probability: pi_classifier returns the
# post-hoc calibrated P(correct) (pipeline/calibration.py), so lock only when a
# vote is genuinely >=70% likely correct -> trustworthy locked labels. (raw>=53
# maps to ~0.77; this actually locks MORE good birds than the old raw/255 0.35
# gate while keeping accuracy, since the display was wildly under-confident.)
# >=3-vote / >=60%-agreement still guards. Env-tunable; the flagship will
# re-derive its own threshold from its calibration curve.
LOCK_CONF_THRESHOLD = float(os.environ.get("PIPELINE_LOCK_CONF", "0.70"))

# ── Classification lifecycle (2026-07-04 label-integrity package) ──────────
# The old model — classify EVERY frame until a 12-attempt LIFETIME cap, then
# never again — was tuned for the 5fps thermal-throttle era. At the restored
# 30fps it burned out in ~0.4s (usually on the bird's blurriest landing
# frames), leaving long-lived tracks "identifying…" forever; and a lock ended
# classification permanently, so a wrong/ridden lock could never be corrected
# (the Blue Jay bug, live-demo evidence 2026-07-04).
#
# New model: PACE, don't die.
# - CLASSIFY_EVERY: classify an unlocked track at most every N hit-frames.
# - MAX_CLASSIFICATION_ATTEMPTS (pipeline/classifier.py) is now a CONSECUTIVE
#   no-vote cap: that many sub-floor results in a row -> plurality fallback +
#   cooldown, NOT permanent surrender.
# - CLASSIFY_COOLDOWN_FRAMES: hit-frames to wait after a no-vote burst before
#   trying again (the bird may simply look better later).
# - LOCK_VERIFY_EVERY: locked tracks keep getting re-checked at this cadence;
#   LOCK_UNLOCK_DISAGREEMENTS consecutive confident disagreements
#   (>= LOCK_VERIFY_MIN_CONF calibrated) unlock the track and restart voting —
#   a ridden or wrong lock now self-corrects in ~2s instead of never.
# Worst-case classifier load: ~6/s per unlocked track + ~2/s per locked track
# (7.4ms/crop CPU) — bounded, unlike the old every-frame burst.
# Default 2 (2026-07-04): census-graded on the annotated demo — at 5, brief
# visits (titmice, ~1-2s) never accumulated 3 votes; at 2, all 9 annotated
# windows lock (jay 0.4s, titmouse 1.0s) and correct-species coverage rose
# 5/9 -> 7/9 with zero true phantoms. Worst-case CPU ~15 classify/s/track.
CLASSIFY_EVERY = int(os.environ.get("PIPELINE_CLASSIFY_EVERY", "2"))
CLASSIFY_COOLDOWN_FRAMES = int(os.environ.get("PIPELINE_CLASSIFY_COOLDOWN", "90"))
LOCK_VERIFY_EVERY = int(os.environ.get("PIPELINE_LOCK_VERIFY_EVERY", "15"))
LOCK_UNLOCK_DISAGREEMENTS = int(os.environ.get("PIPELINE_LOCK_UNLOCK_N", "4"))
# 0.60 (2026-07-17): the old 0.45 was a NO-OP. Every verify vote must already
# clear the raw eligibility floor 0.16 (pi_classifier), and the isotonic
# calibration map's minimum output for any eligible raw score is 0.46 — i.e.
# the weakest possible vote counted as a "confident" disagreement. The map
# jumps 0.46 -> 0.77 with nothing in between, so 0.60 sits squarely in that
# gap: only genuinely confident (>=0.77-band) disagreements now count toward
# the 4-strike contradiction unlock.
LOCK_VERIFY_MIN_CONF = float(os.environ.get("PIPELINE_LOCK_VERIFY_MIN_CONF", "0.60"))
# A lock is also released when verification can't produce ANY vote this many
# times in a row (sub-floor crops). Post-fix live capture 2026-07-04: a Blue
# Jay lock rode a mostly-feeder box across the demo loop wrap for 40s — the
# crops were unclassifiable, so disagreement-based unlock never fired. A real
# locked bird yields votes; a ridden patch yields None forever.
# 8 -> 20 (2026-07-17): with this classifier sub-floor crops are the NORM
# (~84% no-vote base rate on 640x360 crops), so ~4s of sub-floor was hit by
# real birds constantly — 8% of one day's locks unlocked, some flapping
# lock/unlock within a minute. 20 verify misses ≈ ~10s of continuous
# sub-floor while still being DETECTED, much closer to a true ridden-patch
# signature. On release the species is kept as TENTATIVE (see below).
LOCK_UNVERIFIED_N = int(os.environ.get("PIPELINE_LOCK_UNVERIFIED_N", "20"))


class CameraProcessThread:
    # Class-level defaults. Tests construct via __new__ (skipping __init__) and
    # then set the attributes they need; anything not set here would raise
    # AttributeError when _process_frame accesses it. Keep this list in sync
    # with every attribute that _process_frame / _classify_tracks / _emit_sse
    # touches by name.
    snapshot_writer = None
    capture = None
    disagreement_detector = None

    def __init__(self, name: str, frame_queue: queue.Queue,
                 motion_gate, detector, tracker, classifier,
                 event_store, health=None, sse_server=None,
                 frame_width: int = 640, frame_height: int = 360,
                 capture: "Optional[FrameCapture]" = None,
                 snapshot_writer=None):
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
        self.snapshot_writer = snapshot_writer
        self.disagreement_detector = TrackDisagreementDetector()
        self._dry_run = os.environ.get("PIPELINE_DRY_RUN") == "1"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_forced_full = 0.0
        self._last_debug_encode_ms = 0
        self._last_health_update_ms = 0
        self._stats = {
            "frames_processed": 0,
            "detections": 0,
            "yolo_ms_samples": deque(maxlen=100),
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

        # 1. Motion gate — only if the detector actually consumes regions.
        #    Hailo runs full-frame and ignores them (uses_motion_regions=False),
        #    so computing MOG2 every frame across 4 TBB threads was the dominant
        #    CPU/thermal cost for output we then discarded. Default True keeps
        #    region-gated detectors (iMac BirdDetector) working unchanged.
        uses_motion = getattr(self.detector, "uses_motion_regions", True)
        regions = self.motion_gate.regions(frame.bgr) if uses_motion else None

        # 2. Decide whether to force a full-frame YOLO scan
        now = time.time()
        forced_full = (now - self._last_forced_full) > FORCED_FULL_YOLO_INTERVAL_S
        if forced_full:
            self._last_forced_full = now

        # 3. Detect
        t_det = time.monotonic()
        detections = self.detector.detect(frame, regions, forced_full=forced_full)
        det_ms = (time.monotonic() - t_det) * 1000
        # Isolated mode: detection already ran in the decode child; the local
        # timing above measured only the PrecomputedDetector shim (~0 ms). The
        # child ships its true Hailo time on the frame — use that so the
        # health endpoint's yolo_ms / detector-fps stay meaningful.
        child_det_ms = getattr(frame, "det_ms", None)
        if child_det_ms:
            det_ms = float(child_det_ms)
        # Record timing whenever YOLO actually ran. A full-frame detector
        # (not uses_motion) runs every frame; a region-gated one (BirdDetector)
        # returns instantly when regions is empty and forced_full is False —
        # those near-zero timings would pollute yolo_ms_avg, so we exclude them
        # and count "frames where YOLO was actually invoked" separately.
        yolo_actually_ran = (not uses_motion) or bool(regions) or forced_full
        if yolo_actually_ran:
            self._stats["yolo_ms_samples"].append(det_ms)
            self._stats["yolo_runs_total"] += 1
        else:
            self._stats["yolo_skipped_motion"] += 1
        self._stats["detections"] += len(detections)

        # 4. Track
        tracker_out = self.tracker.update(detections, frame.wall_time_ms)

        # 4b. Idle-scan hint (r4 thermal lever): tell the capture child
        # whether anything is active. With no active tracks it drops to
        # every-PIPELINE_IDLE_STRIDE-th frame (~15fps at the default 2) —
        # >99.9% of daylight frames are empty, so this halves decode-child
        # heat for most of the day; full rate resumes the moment a detection
        # produces a track. One shm int write, no-op on non-proc captures.
        if self.capture is not None and hasattr(self.capture, "set_idle"):
            try:
                self.capture.set_idle(not tracker_out.active)
            except Exception:
                pass

        # 5. Classify tracks needing classification
        self._classify_tracks(frame, tracker_out.active)

        # 5b. Snapshot + classifications.db write for freshly-locked tracks.
        # Runs once per track (snapshot_saved flag on Track). Non-blocking: the
        # writer has its own thread + bounded queue. This restores the pre-v3
        # data flow into classifications.db so the dashboard sees fresh rows.
        if self.snapshot_writer is not None and not self._dry_run:
            for track in tracker_out.active:
                if track.is_locked and not track.snapshot_saved:
                    try:
                        # Single-stream: pass bgr_full (1080p) from the SAME
                        # decoded camera moment as bgr (640×360). pts is the
                        # canonical clock for cross-component sync.
                        self.snapshot_writer.submit(
                            self.name, frame.bgr, frame.wall_time_ms, track,
                            frame_bgr_full=frame.bgr_full,
                            pts=frame.pts,
                        )
                        track.snapshot_saved = True
                    except Exception as e:
                        log.warning("[%s] snapshot submit error: %s", self.name, e)

        # 6. Write events to DB (skipped in dry-run / testing mode)
        if not self._dry_run:
            new_ids = {t.track_id for t in tracker_out.new}
            for track in tracker_out.active:
                # Coasting tracks carry a bbox FROZEN at the last detection —
                # at 30fps a full coast window (hit_counter_max=150) writes
                # 150 identical rows per track. Persisting every 3rd coasting
                # frame keeps the replay timeline gap-free (a row at least
                # every ~100ms) at ~1/3 the write + prune volume. Live
                # (non-coasting) frames and is_new rows always write.
                if (getattr(track, "coasting", False)
                        and track.track_id not in new_ids
                        and self._stats["frames_processed"] % 3):
                    continue
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
                    # Coasting tracks have a bbox frozen at the last detection —
                    # the overlay renders them held/dimmed instead of live.
                    "coasting": bool(getattr(track, "coasting", False)),
                })
            self.sse_server.emit(
                camera=self.name,
                wall_time_ms=int(frame.wall_time_ms),
                pts=float(frame.pts),
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
        #    Throttled to 2fps max (500ms) — the debug PiP polls at 500ms,
        #    so encoding more often wastes ~60% of encodes.
        now_ms = time.time() * 1000
        if (tracker_out.active and hasattr(self.health, 'latest_debug_jpeg')
                and (now_ms - getattr(self, '_last_debug_encode_ms', 0)) >= 500):
            self._last_debug_encode_ms = now_ms
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

        # 9. Update health — capture stats every frame (cheap: just age + frame
        #    count), numpy stats (mean/p99) throttled to every 2 seconds.
        self._update_health(frame, det_ms)

    def _crop_track(self, frame: Frame, track):
        """Crop the track's bbox from the detect frame as PIL RGB, or None if
        degenerate (off-frame / sub-5px). Failure is transient — the bird may
        re-enter frame — so no state is written here."""
        x1, y1, x2, y2 = [int(v) for v in track.bbox]
        x1 = max(0, x1); y1 = max(0, y1)
        x2 = min(frame.width, x2); y2 = min(frame.height, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        crop_bgr = frame.bgr[y1:y2, x1:x2]
        if crop_bgr.size == 0:
            return None
        crop_pil = Image.fromarray(crop_bgr[:, :, ::-1])   # BGR → RGB
        if crop_pil.size[0] < 5 or crop_pil.size[1] < 5:
            return None
        return crop_pil

    def _classify_tracks(self, frame: Frame, tracks: list):
        """Run the active classifier on tracks that still need classification.

        On iMac, the classifier is `SmartClassifier` (yard-on-Coral first,
        AIY fallback). On Pi (PI_MODE=1), it's `PiClassifier` wrapping a
        registry of candidates (default `aiy_onnx`). Both expose the same
        `classify(crop_pil, frame_time_ms, camera)` interface.

        Phase 2 voting: accumulate classification votes across multiple frames.
        Lock the species only when enough votes agree. This fixes the
        "first-blurry-crop permanently mislabels the bird" problem from Phase 1.
        """
        for track in tracks:
            since_last = track.frame_count - track.last_classify_fc

            # ── Locked tracks: low-cadence re-verification (never final) ──
            if track.is_locked:
                if LOCK_VERIFY_EVERY <= 0 or since_last < LOCK_VERIFY_EVERY:
                    continue
                crop_pil = self._crop_track(frame, track)
                if crop_pil is None:
                    continue
                track.last_classify_fc = track.frame_count
                try:
                    result = self.classifier.classify(
                        crop_pil, frame.wall_time_ms, self.name
                    )
                except Exception as e:
                    log.warning("[%s] verify error: %s", self.name, e)
                    continue
                if result.should_retry:
                    continue
                if result.species is None:
                    # Sub-floor crop. One is noise; a sustained run means the
                    # locked box no longer contains a classifiable bird.
                    track.no_vote_streak += 1
                    if track.no_vote_streak >= LOCK_UNVERIFIED_N:
                        log.info(
                            "[%s] track %s UNLOCKED: '%s' unverifiable "
                            "(%d consecutive sub-floor crops) — kept tentative",
                            self.name, track.track_id, track.species,
                            track.no_vote_streak,
                        )
                        track.is_locked = False
                        track.needs_classification = True
                        track.vote_history = []
                        # KEEP species/confidence as a TENTATIVE label: this
                        # unlock fires most often on a departing bird's last
                        # small/blurry crops. Nulling the species here erased a
                        # whole visit's confirmed ID from write_track_summary
                        # (species=NULL in pipeline_tracks) and flipped the
                        # live overlay back to "identifying…" while the bird
                        # was still visible. Demote the lock; a contradicting
                        # confident vote still replaces the label via the
                        # normal voting path.
                        track.classification_attempts = 0
                        track.no_vote_streak = 0
                        track.lock_disagreements = 0
                    continue
                track.no_vote_streak = 0
                if result.species == track.species:
                    track.lock_disagreements = 0
                elif (result.confidence or 0) >= LOCK_VERIFY_MIN_CONF:
                    track.lock_disagreements += 1
                    if track.lock_disagreements >= LOCK_UNLOCK_DISAGREEMENTS:
                        log.info(
                            "[%s] track %s UNLOCKED: '%s' contradicted %dx "
                            "(latest: %s @ %.2f) — restarting votes",
                            self.name, track.track_id, track.species,
                            track.lock_disagreements,
                            result.species, result.confidence or 0,
                        )
                        track.is_locked = False
                        track.needs_classification = True
                        track.vote_history = [(result.species, result.confidence)]
                        track.species = result.species
                        track.species_confidence = result.confidence
                        track.model_source = result.model_source
                        track.classification_attempts = 0
                        track.no_vote_streak = 0
                        track.lock_disagreements = 0
                continue

            # ── Unlocked tracks: paced voting with cooldown, never give up ──
            if track.no_vote_streak >= MAX_CLASSIFICATION_ATTEMPTS:
                # No-vote burst exhausted — cool down, then try again (the
                # bird may simply look better later; never surrender forever).
                if since_last < CLASSIFY_COOLDOWN_FRAMES:
                    continue
                track.no_vote_streak = 0
            elif since_last < CLASSIFY_EVERY:
                continue

            crop_pil = self._crop_track(frame, track)
            if crop_pil is None:
                continue

            track.last_classify_fc = track.frame_count
            track.classification_attempts += 1
            try:
                result = self.classifier.classify(
                    crop_pil, frame.wall_time_ms, self.name
                )
            except Exception as e:
                log.warning("[%s] classify error: %s", self.name, e)
                continue

            if result.should_retry:
                # Will retry at the next cadence slot
                continue

            # Got a result — add to vote history
            if result.species is not None:
                track.no_vote_streak = 0
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

                # Check lock condition.
                #
                # 2026-04-18: the 0.6 confidence gate previously worked because
                # the yard model always reported 1.0 (pre-softmax-fix). After
                # yard_classifier.py's temperature scaling (T=100), the MAX
                # yard-only confidence is ~0.54 even for very peaked predictions
                # — so a yard-only track could never lock under the old 0.6
                # gate. Lowering to 0.35 matches the post-fix distribution:
                #   peaked yard: 0.45–0.54 → pass
                #   less peaked: 0.25–0.40 → pass only when agreement is strong
                #   genuine uncertainty: ≤0.16 → fails (good)
                # AIY and BOTH_AGREE results can still go much higher (AIY's
                # 'confidence' is raw_score/100, which ranges 0–2.55), so they
                # clear this threshold easily when they do match.
                #
                # The 60% agreement gate is the real across-frame quality
                # check — flip-flopping yard predictions across frames fail it
                # even if each individual prediction is "peaked" at 0.45.
                if (len(track.vote_history) >= 3 and
                        track.species_confidence >= LOCK_CONF_THRESHOLD and
                        species_counts[top_species] / len(track.vote_history) >= 0.6):
                    track.is_locked = True
                    track.needs_classification = False

                # Within-track disagreement: always record the prediction in the
                # window. If the track is flip-flopping and hasn't locked, stop
                # early (take plurality winner or leave unlabeled) so we don't
                # waste remaining attempts on a confused track.
                if self.disagreement_detector is not None:
                    is_disagreed = self.disagreement_detector.check(
                        track.track_id, result.species, result.confidence
                    )
                    if is_disagreed and not track.is_locked:
                        log.debug(
                            "[%s] track %s disagreement (%.0f%% unique species) — early stop",
                            self.name, track.track_id,
                            self.disagreement_detector.track_windows[track.track_id].disagreement_score() * 100,
                        )
                        if len(track.vote_history) >= 3:
                            track.species = top_species
                            track.species_confidence = max(
                                c for s, c in track.vote_history if s == top_species
                            )
                            track.model_source = ModelSource.VOTE_PLURALITY
                        # Flip-flopping: stop wasting the current burst, but
                        # cool down and retry later rather than giving up for
                        # the track's whole life.
                        track.no_vote_streak = MAX_CLASSIFICATION_ATTEMPTS
            else:
                # Classifier returned None (sub-floor crop) — no vote. After a
                # full burst of these, take the plurality (if any votes exist)
                # and enter cooldown; the cadence gate retries after
                # CLASSIFY_COOLDOWN_FRAMES.
                track.no_vote_streak += 1
                if (track.no_vote_streak >= MAX_CLASSIFICATION_ATTEMPTS
                        and track.vote_history):
                    species_counts = Counter(s for s, c in track.vote_history)
                    top_species, _ = species_counts.most_common(1)[0]
                    track.species = top_species
                    track.species_confidence = max(
                        c for s, c in track.vote_history if s == top_species
                    )
                    track.model_source = ModelSource.VOTE_PLURALITY

        # Evict windows for tracks that are no longer active so memory doesn't
        # grow unboundedly across a long session.
        if self.disagreement_detector is not None:
            self.disagreement_detector.cleanup_expired_tracks(
                [t.track_id for t in tracks]
            )

    def _update_health(self, frame: Frame, det_ms: float):
        # Capture stats: cheap, update every frame so last_frame_age_ms stays
        # fresh and stall detection (based on age) is never falsely triggered.
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
            _restarts_hr = self.capture.restarts_last_hour()
            capture_payload["ffmpeg_restarts_last_hour"] = _restarts_hr  # legacy key (dashboard compat)
            capture_payload["reader_restarts_last_hour"] = _restarts_hr  # truthful name (PyAV reader)
        self.health.update(self.name, "capture", capture_payload)

        # Expensive stats: throttle numpy mean/p99 to every 2 seconds.
        # These are only consumed by the health endpoint (~1/10s polling).
        now = time.time()
        if not hasattr(self, '_last_stats_compute') or (now - self._last_stats_compute) >= 2.0:
            self._last_stats_compute = now
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
                    "id_switches": self.tracker.id_switches,
                })
            except Exception:
                pass
            try:
                cam_classifier_stats = self.classifier.stats.get(self.name, {})
                self.health.update(self.name, "classifier", dict(cam_classifier_stats))
            except Exception:
                pass
            try:
                if self.disagreement_detector is not None:
                    self.health.update(self.name, "disagreement",
                                       self.disagreement_detector.get_stats())
            except Exception:
                pass
