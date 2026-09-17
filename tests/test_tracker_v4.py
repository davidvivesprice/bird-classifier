"""BirdTrackerV4 — identity persistence under the failure modes that
fragmented v3 tracks: detector gaps in flight, low-confidence frames,
leave-and-return, crossing birds, and YOLO double-boxes. Synthetic
trajectories; no norfair, no Hailo."""
import os

import numpy as np
import pytest

os.environ.setdefault("PIPELINE_TRACK_COAST", "45")
os.environ.setdefault("PIPELINE_TRACK_LOST", "300")

from pipeline.detector import Detection  # noqa: E402
from pipeline.tracker_v4 import BirdTrackerV4, _hs_hist  # noqa: E402


def det(cx, cy, w=40, h=30, conf=0.8):
    return Detection(box=[cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], confidence=conf)


def frame_with(color_boxes, size=(360, 640)):
    """Flat-gray frame with solid-colour rectangles for appearance tests."""
    f = np.full((size[0], size[1], 3), 120, dtype=np.uint8)
    for (x1, y1, x2, y2), bgr in color_boxes:
        f[int(y1):int(y2), int(x1):int(x2)] = bgr
    return f


def run(tracker, frames, fps_ms=33.3, coast_log=None):
    """frames: list of (detections, frame_bgr|None). Returns list of outputs.
    Track objects are shared across frames (mutated in place), so per-frame
    state like `coasting` must be sampled here, not read back later."""
    outs = []
    t = 0.0
    for dets, fb in frames:
        out = tracker.update(dets, t, frame_bgr=fb)
        outs.append(out)
        if coast_log is not None:
            coast_log.append([bool(trk.coasting) for trk in out.active])
        t += fps_ms
    return outs


def ids_seen(outs):
    return {trk.track_id for o in outs for trk in o.active}


def test_straight_flight_survives_detector_gaps():
    """A bird crossing at 12 px/frame with 5-frame detector gaps keeps ONE id.
    (v3 compared against the last detection and rejected these re-acquisitions.)"""
    tr = BirdTrackerV4()
    frames = []
    for i in range(60):
        x = 50 + 12 * i
        seen = not (10 <= i < 15 or 30 <= i < 35 or 45 <= i < 50)
        frames.append(([det(x, 180)] if seen else [], None))
    coast_log = []
    outs = run(tr, frames, coast_log=coast_log)
    assert ids_seen(outs) == {1}
    assert tr.id_switches == 0
    # while the detector is blind (frames 10-14) the track is flagged coasting
    assert all(coast_log[i] == [True] for i in range(11, 15))
    assert coast_log[20] == [False]


def test_low_conf_rescue_keeps_track_but_never_spawns():
    tr = BirdTrackerV4()
    frames = [([det(100, 100)], None), ([det(104, 100)], None)]
    # 20 frames where the detector only sees the bird at 0.2 conf
    frames += [([det(100 + 4 * i, 100, conf=0.2)], None) for i in range(3, 23)]
    frames += [([det(100 + 4 * i, 100)], None) for i in range(23, 30)]
    outs = run(tr, frames)
    assert ids_seen(outs) == {1}
    # low-conf detections were consumed as hits (not coasting)
    assert not any(trk.coasting for trk in outs[10].active)
    # a lone low-conf detection elsewhere must not become a track
    tr2 = BirdTrackerV4()
    outs2 = run(tr2, [([det(300, 200, conf=0.2)], None)] * 30)
    assert ids_seen(outs2) == set()


def test_leave_and_return_reidentifies_by_appearance():
    """Bird leaves for 4 s (beyond the coast window) and returns near where it
    left with the same colours: same track_id, votes intact."""
    tr = BirdTrackerV4()
    red = (30, 30, 200)
    box = lambda cx, cy: (cx - 20, cy - 15, cx + 20, cy + 15)
    frames = [([det(200, 150)], frame_with([(box(200, 150), red)])) for _ in range(20)]
    frames += [([], None) for _ in range(120)]           # gone 4s @30fps
    frames += [([det(230, 160)], frame_with([(box(230, 160), red)])) for _ in range(10)]
    outs = run(tr, frames)
    # tag the track with a lock while visible, check it survives the gap
    first = outs[5].active[0]
    first.species, first.is_locked = "Northern Cardinal", True
    assert ids_seen(outs) == {1}
    back = outs[-1].active[0]
    assert back.track_id == 1 and back.species == "Northern Cardinal" and back.is_locked


def test_return_with_different_colours_is_a_new_bird():
    tr = BirdTrackerV4()
    red, yellow = (30, 30, 200), (40, 220, 230)
    box = lambda cx, cy: (cx - 20, cy - 15, cx + 20, cy + 15)
    frames = [([det(200, 150)], frame_with([(box(200, 150), red)])) for _ in range(20)]
    frames += [([], None) for _ in range(120)]
    frames += [([det(230, 160)], frame_with([(box(230, 160), yellow)])) for _ in range(10)]
    outs = run(tr, frames)
    assert ids_seen(outs) == {1, 2}


def test_lost_track_expires_once_after_lost_window():
    tr = BirdTrackerV4()
    frames = [([det(100, 100)], None) for _ in range(5)] + [([], None) for _ in range(400)]
    outs = run(tr, frames)
    expired = [trk.track_id for o in outs for trk in o.expired]
    assert expired == [1]
    assert tr.tracks == {} and tr._lost == {}


def test_two_birds_crossing_do_not_swap():
    """Two birds moving toward each other, passing through each other's
    x-range at different heights. Prediction-based gating keeps identities."""
    tr = BirdTrackerV4()
    frames = []
    for i in range(50):
        a = det(50 + 8 * i, 120)     # left→right, y=120
        b = det(450 - 8 * i, 220)    # right→left, y=220
        frames.append(([a, b], None))
    outs = run(tr, frames)
    assert ids_seen(outs) == {1, 2}
    # the track that started at y=120 ends at y=120
    end = {trk.track_id: trk.bbox for trk in outs[-1].active}
    start = {trk.track_id: trk.bbox for trk in outs[2].active}
    for tid in (1, 2):
        assert abs(((start[tid][1] + start[tid][3]) / 2) - ((end[tid][1] + end[tid][3]) / 2)) < 5


def test_head_in_body_double_box_is_one_track():
    tr = BirdTrackerV4()
    body = det(200, 150, w=60, h=50, conf=0.7)
    head = det(215, 138, w=16, h=14, conf=0.5)   # inside the body box
    outs = run(tr, [([body, head], None)] * 10)
    assert ids_seen(outs) == {1}


def test_single_frame_flicker_never_becomes_a_track():
    tr = BirdTrackerV4()
    outs = run(tr, [([det(300, 300)], None), ([], None), ([], None), ([det(100, 100)], None), ([], None)])
    assert ids_seen(outs) == set()


def test_frame_count_counts_only_real_hits():
    tr = BirdTrackerV4()
    frames = [([det(100, 100)], None)] * 10 + [([], None)] * 10 + [([det(100, 100)], None)] * 5
    outs = run(tr, frames)
    trk = outs[-1].active[0]
    assert trk.frame_count == 15


def test_hist_is_none_for_tiny_crops_and_stable_for_solid_colour():
    f = frame_with([((100, 100, 140, 130), (20, 200, 20))])
    assert _hs_hist(f, [100, 100, 105, 105]) is None
    h1 = _hs_hist(f, [100, 100, 140, 130])
    h2 = _hs_hist(f, [102, 101, 138, 129])
    assert h1 is not None and float(np.sum(np.sqrt(h1 * h2))) > 0.95


def test_vote_reid_merges_young_track_into_lost_same_species():
    """Bird A (votes 'Tufted Titmouse') leaves; 2 s later a bird appears
    nearby with DIFFERENT colours (colour ReID declines) but votes titmouse:
    it is merged back into A — A's id, lock and votes continue."""
    tr = BirdTrackerV4()
    red, gray = (30, 30, 200), (150, 150, 150)
    box = lambda cx, cy: (cx - 20, cy - 15, cx + 20, cy + 15)
    t = 0.0
    for _ in range(40):
        tr.update([det(200, 150)], t, frame_bgr=frame_with([(box(200, 150), red)])); t += 33
    tr.note_vote(1, "Tufted Titmouse", 0.8); tr.note_vote(1, "Tufted Titmouse", 0.7)
    a = tr.tracks[1]; a.species, a.is_locked = "Tufted Titmouse", True
    for _ in range(80):
        tr.update([], t, frame_bgr=None); t += 33          # gone ~2.6 s → lost
    assert 1 in tr._lost
    out = None
    for _ in range(6):
        out = tr.update([det(230, 160)], t, frame_bgr=frame_with([(box(230, 160), gray)])); t += 33
    young = out.active[0].track_id
    assert young != 1                                   # colour ReID correctly declined
    tr.note_vote(young, "Tufted Titmouse", 0.9)
    tr.note_vote(young, "Tufted Titmouse", 0.6)
    out = tr.update([det(232, 160)], t, frame_bgr=frame_with([(box(232, 160), gray)]))
    ids = [trk.track_id for trk in out.active]
    assert ids == [1] and tr.merges == [(young, 1)]
    assert out.active[0].species == "Tufted Titmouse" and out.active[0].is_locked
    assert young not in tr.tracks and 1 not in tr._lost


def test_vote_reid_does_not_merge_different_species():
    tr = BirdTrackerV4()
    gray = (150, 150, 150)
    box = lambda cx, cy: (cx - 20, cy - 15, cx + 20, cy + 15)
    t = 0.0
    for _ in range(40):
        tr.update([det(200, 150)], t, frame_bgr=frame_with([(box(200, 150), gray)])); t += 33
    tr.note_vote(1, "Tufted Titmouse", 0.8); tr.note_vote(1, "Tufted Titmouse", 0.7)
    for _ in range(80):
        tr.update([], t, frame_bgr=None); t += 33
    out = None
    for _ in range(6):
        out = tr.update([det(300, 200)], t, frame_bgr=frame_with([(box(300, 200), (200, 120, 40))])); t += 33
    young = out.active[0].track_id
    tr.note_vote(young, "Blue Jay", 0.9); tr.note_vote(young, "Blue Jay", 0.9)
    out = tr.update([det(302, 200)], t, frame_bgr=None)
    assert [trk.track_id for trk in out.active] == [young] and tr.merges == []


def test_vote_split_undoes_a_wrong_colour_reid():
    """A locked titmouse leaves; a same-coloured, same-sized bird lands where
    it was → colour ReID revives id 1. Its first votes say 'Blue Jay': the
    tracker splits the newcomer onto a fresh id and restores the titmouse's
    frozen identity to the lost buffer."""
    tr = BirdTrackerV4()
    gray = (150, 150, 150)
    box = lambda cx, cy: (cx - 20, cy - 15, cx + 20, cy + 15)
    t = 0.0
    for _ in range(40):
        tr.update([det(200, 150)], t, frame_bgr=frame_with([(box(200, 150), gray)])); t += 33
    tr.note_vote(1, "Tufted Titmouse", 0.8); tr.note_vote(1, "Tufted Titmouse", 0.7)
    a = tr.tracks[1]; a.species, a.is_locked = "Tufted Titmouse", True
    for _ in range(60):
        tr.update([], t, frame_bgr=None); t += 33
    assert 1 in tr._lost
    out = tr.update([det(205, 152)], t, frame_bgr=frame_with([(box(205, 152), gray)])); t += 33
    assert [x.track_id for x in out.active] == [1]        # colour ReID revived it (plausible)
    tr.note_vote(1, "Blue Jay", 0.9); tr.note_vote(1, "Blue Jay", 0.8)
    out = tr.update([det(207, 152)], t, frame_bgr=frame_with([(box(207, 152), gray)]))
    ids = [x.track_id for x in out.active]
    assert ids == [2] and tr.splits == [(1, 2)]
    newcomer = out.active[0]
    assert newcomer.species is None and not newcomer.is_locked
    assert 1 in tr._lost and tr._lost[1].species == "Tufted Titmouse" and tr._lost[1].is_locked
