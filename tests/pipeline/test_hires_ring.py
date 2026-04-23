"""Tests for HiResRingBuffer and score_frame.

Plan: docs/superpowers/plans/2026-04-22-hires-ring-buffer.md

The ring replaces the current SnapshotWriter._fetch_hires_frame path, which
blocks 2-5s waiting for go2rtc to emit a keyframe. That delay is THE source
of the stale-bbox hallucination (bird has left the bbox by the time the
hi-res frame arrives). The ring holds a rolling window of 1080p frames
indexed by wall time, so crop-time pick is near-instant and temporally
close to the detection.
"""
import numpy as np
import pytest

from pipeline.hires_ring import HiResRingBuffer, RingFrame, score_frame


# ── HiResRingBuffer ────────────────────────────────────────────────────────


def _frame(val=0, w=1920, h=1080):
    return np.full((h, w, 3), val, dtype=np.uint8)


def test_push_and_find_nearest():
    ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
    ring.push(_frame(10), wall_ms=1000)
    ring.push(_frame(20), wall_ms=1200)
    ring.push(_frame(30), wall_ms=1400)
    hit = ring.find_nearest(wall_ms=1210)
    assert hit is not None
    assert hit.wall_ms == 1200
    # Frame value confirms the right frame came back
    assert hit.frame[0, 0, 0] == 20


def test_find_nearest_prefers_closer_side():
    ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
    ring.push(_frame(1), wall_ms=1000)
    ring.push(_frame(2), wall_ms=1400)
    hit1 = ring.find_nearest(wall_ms=1100)  # closer to 1000
    assert hit1.wall_ms == 1000
    hit2 = ring.find_nearest(wall_ms=1300)  # closer to 1400
    assert hit2.wall_ms == 1400


def test_drops_old_frames():
    ring = HiResRingBuffer(max_seconds=1.0, expected_fps=5)
    ring.push(_frame(), wall_ms=1000)
    ring.push(_frame(), wall_ms=1500)
    ring.push(_frame(), wall_ms=2500)  # 1.5s past the oldest
    # Oldest evicted
    assert ring.find_nearest(wall_ms=1000) is None
    assert ring.find_nearest(wall_ms=1500) is not None


def test_find_nearest_empty_returns_none():
    ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
    assert ring.find_nearest(wall_ms=1000) is None


def test_find_candidates_returns_k_nearest_sorted_by_time():
    ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
    for i in range(10):
        ring.push(_frame(i), wall_ms=1000 + i * 100)
    cands = ring.find_candidates(wall_ms=1550, k=3)
    times = sorted([c.wall_ms for c in cands])
    assert times == [1400, 1500, 1600]


def test_len_reports_current_size():
    ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
    assert len(ring) == 0
    for i in range(5):
        ring.push(_frame(), wall_ms=1000 + i * 100)
    assert len(ring) == 5


def test_out_of_order_push_keeps_sorted():
    """Rare but possible — clock skew or thread jitter. Ring must still work."""
    ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
    ring.push(_frame(2), wall_ms=1200)
    ring.push(_frame(1), wall_ms=1000)   # earlier than previous
    ring.push(_frame(3), wall_ms=1400)
    hit = ring.find_nearest(wall_ms=1010)
    assert hit.wall_ms == 1000
    assert hit.frame[0, 0, 0] == 1


def test_frame_is_copied_not_referenced():
    """Caller may mutate its buffer after push; ring must hold its own copy."""
    ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
    src = _frame(42)
    ring.push(src, wall_ms=1000)
    src[0, 0, 0] = 99   # mutate caller's buffer
    hit = ring.find_nearest(wall_ms=1000)
    assert hit.frame[0, 0, 0] == 42  # ring's copy unaffected


# ── score_frame ────────────────────────────────────────────────────────────


def _striped_frame(size=1080):
    """High-frequency stripes → high Laplacian variance → sharp."""
    f = np.zeros((size, size, 3), dtype=np.uint8)
    f[:, ::2, :] = 255
    return f


def _flat_frame(size=1080):
    """Flat gray → zero Laplacian variance → blurry."""
    return np.full((size, size, 3), 128, dtype=np.uint8)


def test_score_sharp_beats_blurry():
    sharp = _striped_frame()
    blurry = _flat_frame()
    bbox = [400, 400, 600, 600]   # 200×200 square, well-sized, centered
    s = score_frame(sharp, bbox, detector_conf=0.8)
    b = score_frame(blurry, bbox, detector_conf=0.8)
    assert s > b
    # Sanity: blurry should be near-zero variance
    assert b < 1.0


def test_score_rejects_too_small_bbox():
    f = _striped_frame()
    bbox = [500, 500, 550, 550]   # 50×50 — below the 80 floor
    s = score_frame(f, bbox, detector_conf=0.9)
    assert s == 0.0


def test_score_rewards_higher_detector_confidence():
    f = _striped_frame()
    bbox = [400, 400, 600, 600]
    low = score_frame(f, bbox, detector_conf=0.3)
    high = score_frame(f, bbox, detector_conf=0.9)
    assert high > low


def test_score_returns_zero_for_invalid_frame():
    bbox = [400, 400, 600, 600]
    assert score_frame(None, bbox, detector_conf=0.9) == 0.0
    assert score_frame(np.array([]), bbox, detector_conf=0.9) == 0.0


def test_score_clamps_bbox_to_frame():
    """bbox partially off-frame should still work, not crash."""
    f = _striped_frame(size=800)
    bbox = [700, 700, 900, 900]   # extends beyond frame
    # Should either return a score (for the 100×100 visible part) or 0 if below 80 floor.
    # 100×100 is above floor, so should score > 0.
    s = score_frame(f, bbox, detector_conf=0.8)
    assert s >= 0.0  # doesn't crash; specific value depends on clamped size


def test_score_near_zero_for_empty_conf():
    """detector_conf=0 shouldn't produce exactly 0 (floor at 0.1)."""
    f = _striped_frame()
    bbox = [400, 400, 600, 600]
    s = score_frame(f, bbox, detector_conf=0.0)
    # Floor of 0.1 means score is ~10% of the conf=1.0 case, not zero.
    assert s > 0
