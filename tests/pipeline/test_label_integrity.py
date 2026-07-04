"""Label-integrity fixes (2026-07-04, the 'Blue Jay bug' package).

Live-demo evidence (178s capture, David watching): a locked 'Blue Jay' track
teleported bird-to-bird (224px jumps — the relative jump gate is toothless for
big boxes), head-in-body dup boxes (containment 1.0, IoU 0.25) slipped the
IoU-only dedup and spawned phantom tracks that stole locked tracks' detections,
and the 12-attempt lifetime classification cap (tuned for the 5fps era) burned
out in ~0.4s at 30fps leaving tracks 'identifying…' forever. Locks could never
be corrected because classification stopped permanently at lock.
"""
import queue
from unittest.mock import MagicMock
from types import SimpleNamespace

import numpy as np

from pipeline.tracker import BirdTracker
from pipeline.classifier import ClassificationResult
from pipeline.frame import Frame


def _det(box, conf=0.9):
    return SimpleNamespace(box=list(box), confidence=conf)


# ── F1: containment dedup ──────────────────────────────────────────────

def test_dedup_kills_head_in_body_box():
    """A small high-containment box (YOLO head-dup) must not spawn a track,
    even when IoU is far below the IoU dedup threshold."""
    t = BirdTracker(distance_threshold=2.5, hit_counter_max=10,
                    initialization_delay=1)
    body = (100, 100, 300, 260)     # 200x160
    head = (110, 110, 165, 160)     # 55x50 fully inside body: IoU ~0.09, cont 1.0
    out = None
    for i in range(6):
        out = t.update([_det(body, 0.9), _det(head, 0.6)], float(i * 33))
    assert len(out.active) == 1, (
        f"expected 1 track (head box deduped), got {len(out.active)}")


# ── F2: absolute jump cap ──────────────────────────────────────────────

def test_jump_cap_blocks_teleport_for_large_boxes():
    """A large box's track must NOT claim a detection 250px away between
    consecutive frames (the Blue-Jay-rides mechanism: relative gate = 1.5x a
    520px box = the whole frame)."""
    t = BirdTracker(distance_threshold=2.5, hit_counter_max=60,
                    initialization_delay=1)
    big_left = (40, 100, 340, 260)      # 300 wide, center x=190
    for i in range(6):
        out = t.update([_det(big_left)], float(i * 33))
    orig_id = out.active[0].track_id
    # same-size detection 250px to the right, next frame
    big_right = (290, 100, 590, 260)    # center x=440
    out = t.update([_det(big_right)], 6 * 33.0)
    claimed = [tr for tr in out.active
               if tr.bbox == list(big_right) and tr.track_id == orig_id]
    assert not claimed, "large-box track teleported 250px — jump cap failed"


# ── shared harness for classification-path tests ───────────────────────

def _mk_thread(classify_results):
    """CameraProcessThread with a scripted classifier; returns (thread, calls)."""
    from pipeline.process_thread import CameraProcessThread
    calls = []

    def classify(crop, wall_ms, camera):
        calls.append(1)
        r = classify_results[min(len(calls) - 1, len(classify_results) - 1)]
        return r

    classifier = MagicMock()
    classifier.classify = classify
    thread = CameraProcessThread(
        name="feeder", frame_queue=queue.Queue(),
        motion_gate=MagicMock(), detector=MagicMock(),
        tracker=MagicMock(), classifier=classifier,
        event_store=MagicMock(), health=MagicMock(),
    )
    return thread, calls


def _frame():
    return Frame(bgr=np.ones((360, 640, 3), dtype=np.uint8) * 128,
                 wall_time_ms=0, camera="feeder", width=640, height=360)


def _track(**kw):
    from pipeline.tracker import Track
    tr = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
               bbox=[100, 100, 200, 200], confidence=0.9)
    for k, v in kw.items():
        setattr(tr, k, v)
    return tr


def _run_frames(thread, track, n):
    """Advance the track by n hit-frames, classifying per the real cadence."""
    f = _frame()
    for _ in range(n):
        track.frame_count += 1
        thread._classify_tracks(f, [track])


# ── F3: classification never permanently gives up ──────────────────────

def test_classification_retries_after_cooldown():
    """No-vote (sub-floor) results must not end classification forever: after
    the no-vote cap, the track cools down and tries again."""
    import pipeline.process_thread as pt
    no_vote = ClassificationResult(species=None, confidence=0.0,
                                   model_source="aiy_onnx", should_retry=False)
    thread, calls = _mk_thread([no_vote])
    tr = _track()
    # enough hit-frames to pass the cap and a full cooldown window
    frames_needed = (pt.MAX_CLASSIFICATION_ATTEMPTS * pt.CLASSIFY_EVERY
                     + pt.CLASSIFY_COOLDOWN_FRAMES + pt.CLASSIFY_EVERY * 3)
    _run_frames(thread, tr, frames_needed)
    assert len(calls) > pt.MAX_CLASSIFICATION_ATTEMPTS, (
        f"classifier called only {len(calls)}x — gave up permanently "
        f"(cap {pt.MAX_CLASSIFICATION_ATTEMPTS})")


# ── F4: post-lock re-verification ──────────────────────────────────────

def test_locked_track_unlocks_on_persistent_disagreement():
    """A locked label must yield when the classifier keeps seeing a different
    species with real confidence (the ridden Blue Jay lock)."""
    import pipeline.process_thread as pt
    goldfinch = ClassificationResult(species="American Goldfinch",
                                     confidence=0.85,
                                     model_source="aiy_onnx", should_retry=False)
    thread, calls = _mk_thread([goldfinch])
    tr = _track(is_locked=True, species="Blue Jay", species_confidence=0.9,
                needs_classification=False,
                vote_history=[("Blue Jay", 0.9)] * 3)
    frames_needed = pt.LOCK_VERIFY_EVERY * (pt.LOCK_UNLOCK_DISAGREEMENTS + 2)
    _run_frames(thread, tr, frames_needed)
    assert len(calls) >= pt.LOCK_UNLOCK_DISAGREEMENTS, "locked track was never re-verified"
    # The ridden label must self-correct: either unlocked and re-voting, or
    # already re-locked as the species actually on the bird.
    assert tr.species == "American Goldfinch", (
        f"label did not correct: still '{tr.species}' (locked={tr.is_locked})")
    assert not (tr.is_locked and tr.species == "Blue Jay"), (
        "ridden Blue Jay lock survived persistent disagreement")


def test_locked_track_unlocks_when_unverifiable():
    """A lock whose crops can't produce ANY vote for a sustained stretch is
    stale — post-fix live capture showed a Blue Jay lock riding a mostly-feeder
    box across the demo loop wrap for 40s because sub-floor verifies counted
    'neither way'. A real locked bird yields votes; a ridden patch doesn't."""
    import pipeline.process_thread as pt
    none_result = ClassificationResult(species=None, confidence=0.0,
                                       model_source="aiy_onnx", should_retry=False)
    thread, calls = _mk_thread([none_result])
    tr = _track(is_locked=True, species="Blue Jay", species_confidence=0.9,
                needs_classification=False,
                vote_history=[("Blue Jay", 0.9)] * 3)
    _run_frames(thread, tr, pt.LOCK_VERIFY_EVERY * (pt.LOCK_UNVERIFIED_N + 2))
    assert len(calls) >= pt.LOCK_UNVERIFIED_N, "locked track was never re-verified"
    assert tr.is_locked is False, "unverifiable lock never released"


def test_locked_track_stays_locked_on_agreement():
    import pipeline.process_thread as pt
    bluejay = ClassificationResult(species="Blue Jay", confidence=0.9,
                                   model_source="aiy_onnx", should_retry=False)
    thread, calls = _mk_thread([bluejay])
    tr = _track(is_locked=True, species="Blue Jay", species_confidence=0.9,
                needs_classification=False,
                vote_history=[("Blue Jay", 0.9)] * 3)
    _run_frames(thread, tr, pt.LOCK_VERIFY_EVERY * 6)
    assert tr.is_locked is True, "agreeing verification must keep the lock"
    assert len(calls) >= 3, "verification cadence never fired"
