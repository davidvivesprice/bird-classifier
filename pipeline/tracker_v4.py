"""BirdTrackerV4 — identity persistence first.

Why a new tracker (2026-09-17): the Norfair-based v3 tracker matched each new
detection against a track's LAST DETECTION position (see `_frigate_distance`
in tracker.py), not against where the bird should be now. A bird missed for a
few frames in flight was therefore compared to where it *was*; the 120px jump
cap then rejected the legitimate re-acquisition and a fresh track_id was born —
the "loss of tracking" that undermines everything downstream (votes, locks,
labels, visit counts). v3 also had no low-confidence rescue and no re-
identification: once a track coasted out, a returning bird was always new.

v4 is the small, well-understood industry recipe (SORT/ByteTrack/BoT-SORT
lineage), sized for a Pi 5 parent process:

  1. Kalman constant-velocity filter on the BOX [cx, cy, w, h] + velocities.
     Association gates against the PREDICTED box, so fast birds and short
     detector gaps stay on one identity.
  2. Two-stage association (ByteTrack): high-confidence detections first
     (Hungarian on 1-IoU with a size-aware center gate), then LOW-confidence
     detections are used ONLY to keep existing tracks alive — never to spawn.
  3. Coast → lost → expire: a confirmed track that misses detections is
     rendered "coasting" (bbox held at the last detection) for a SHORT window,
     then becomes silently LOST for a long window in which a re-appearing
     bird can be RE-IDENTIFIED (spatial plausibility + HS-histogram
     appearance) and continue the SAME track_id — votes, lock and label
     intact. Only after the lost window does the track expire.
  4. Same `Track` / `TrackerOutput` objects as v3, so process_thread,
     snapshot_writer and event_store need no changes.

Cost: a few 8x8 matrix ops per track per frame and one tiny histogram per
matched detection — negligible next to decode/detect.

All knobs are env-tunable (PIPELINE_TRACK_*); defaults are for 640x360 @ ~30fps.
"""
from __future__ import annotations

import os
from collections import deque
from typing import Optional

import numpy as np
from scipy.optimize import linear_sum_assignment

from pipeline.tracker_common import (  # shared contract + dedup knobs
    Track, TrackerOutput, _iou, _containment, _DEDUP_IOU, _DEDUP_CONT,
)

# ── Knobs ────────────────────────────────────────────────────────────────
# Detection confidence bands. HIGH = eligible to match AND to spawn a track;
# LOW..HIGH = rescue-only (keep an existing track alive, never spawn). For the
# LOW band to exist at all the decode child must forward detections down to
# LOW (PIPELINE_DET_CONF) — with the child at 0.30 the second stage is a no-op.
_T_HIGH = float(os.environ.get("PIPELINE_TRACK_HIGH", "0.30"))
_T_LOW = float(os.environ.get("PIPELINE_TRACK_LOW", "0.15"))
# Consecutive hits before a new track is confirmed (emitted). Mirrors v3's
# initialization_delay=2: a single-frame flicker never becomes a track.
_INIT_HITS = int(os.environ.get("PIPELINE_TRACK_INIT_HITS", "2"))
# Misses a tentative (unconfirmed) track survives while waiting for its
# confirming hit — the detector is sparse on small birds.
_TENT_COAST = int(os.environ.get("PIPELINE_TRACK_TENT_COAST", "10"))
# Visible coast (frames): bbox held at the last detection, rendered dashed.
# Short on purpose — a 5s held box on an empty perch is a UX wart; the lost
# buffer below does the long-memory job silently.
_COAST_MAX = int(os.environ.get("PIPELINE_TRACK_COAST", "45"))
# Lost buffer (frames) after the coast: the track is hidden but re-identifiable.
_LOST_MAX = int(os.environ.get("PIPELINE_TRACK_LOST", "300"))
# Stage-1 center gate around the PREDICTED box: max(size_mult*size, gate_min)
# widening gently while coasting, hard-capped at gate_max (the jump cap's
# successor — nothing legitimately teleports across the frame in one step).
_GATE_SIZE_MULT = float(os.environ.get("PIPELINE_TRACK_GATE_MULT", "0.9"))
_GATE_MIN = float(os.environ.get("PIPELINE_TRACK_GATE_MIN", "48"))
_GATE_MAX = float(os.environ.get("PIPELINE_TRACK_GATE_MAX", "180"))
# Stage-2 (low-conf rescue) needs real overlap with the predicted box AND a
# FRESH miss: ByteTrack's second stage exists for the blurred/occluded frames
# of a bird we just saw, not to feed a coasting ghost with the detector's
# low-score false positives on the feeder itself (measured: unrestricted
# rescue kept dead tracks alive and forced returning birds onto new ids).
_LOW_IOU_MIN = float(os.environ.get("PIPELINE_TRACK_LOW_IOU", "0.20"))
_LOW_MAX_MISSES = int(os.environ.get("PIPELINE_TRACK_LOW_MAX_MISSES", "5"))
# Spatial-only ReID (no appearance on one side) is a perch-sharing trap —
# a titmouse landing where the finch left got the finch's id in replay. Without
# appearance, only bridge SHORT gaps at essentially the same spot.
_REID_BLIND_MAX_FRAMES = int(os.environ.get("PIPELINE_TRACK_REID_BLIND_FRAMES", "45"))
# ReID against lost tracks: appearance similarity (Bhattacharyya coefficient of
# HS histograms, 1.0 = identical) and a spatial radius (px) that grows with
# time lost, capped.
_REID_SIM = float(os.environ.get("PIPELINE_TRACK_REID_SIM", "0.75"))
_REID_RADIUS_MAX = float(os.environ.get("PIPELINE_TRACK_REID_RADIUS", "260"))
_REID_PX_PER_FRAME = float(os.environ.get("PIPELINE_TRACK_REID_SPEED", "6"))
# A jay is 4-6x a titmouse in box area; colour alone can't tell those two
# apart (both grey-blue, BC≈0.77) but size can. Areas must be within this ratio.
_REID_SIZE_RATIO = float(os.environ.get("PIPELINE_TRACK_REID_SIZE_RATIO", "2.5"))
# Appearance histogram: hue×saturation of the INNER 60% of the box (the
# outer ring is feeder/seed background and made every bird look alike —
# measured on the may10 reel: whole-box finch↔titmouse BC 0.67, inner-box
# 0.47; same bird stays ≥0.88). EMA across hits, refreshed every N hits.
_HIST_ALPHA = 0.3
_HIST_MIN_SIDE = 12   # crops smaller than this carry no usable appearance
_HIST_INNER = 0.6     # fraction of the box used for the histogram
_HIST_EVERY = 4       # recompute every Nth hit (appearance drifts slowly)
# Vote-based (deferred) ReID: the classifier's species votes are the strongest
# "same bird?" signal we already pay for. A YOUNG track that starts voting
# like a recently-LOST track (cosine of species-weight signatures) and sits
# where that bird could plausibly be is merged back INTO the lost track —
# keeping its id, votes and lock. Colour ReID is the fast path at first
# sight; this is the species-aware second chance a few frames later.
_VOTE_SIM = float(os.environ.get("PIPELINE_TRACK_VOTE_SIM", "0.60"))
_VOTE_YOUNG_FRAMES = int(os.environ.get("PIPELINE_TRACK_VOTE_YOUNG", "90"))
_VOTE_MIN_WEIGHT = float(os.environ.get("PIPELINE_TRACK_VOTE_MIN", "1.0"))
# The mirror rule: a track REVIVED by colour/space ReID whose first votes
# CONTRADICT the bird it was assumed to be (cosine below this) is a different
# bird — split it off onto a fresh id and put the original back in the lost
# buffer with its identity restored. (Replay case: a titmouse's id handed to a
# partially-visible jay at the same perch 3 s later.)
_VOTE_SPLIT_SIM = float(os.environ.get("PIPELINE_TRACK_VOTE_SPLIT_SIM", "0.20"))
# Merge/split ops kept for the display path. process_thread drains them by op
# id on every emitted event, so it is never more than one frame behind; the
# bound only stops a long-running pipeline from growing the log without limit.
_IDENTITY_LOG_MAX = 1000


# ── Kalman (const-velocity on [cx, cy, w, h]) ────────────────────────────
class _KalmanBox:
    """8-state constant-velocity filter, dt = 1 frame. Process/measurement
    noise scale with box height (BoT-SORT convention) so small and large
    birds get comparable gating."""
    __slots__ = ("x", "P")

    _F = np.eye(8)
    _F[:4, 4:] = np.eye(4)
    _H = np.eye(4, 8)

    def __init__(self, z: np.ndarray):
        self.x = np.zeros(8)
        self.x[:4] = z
        h = max(float(z[3]), 1.0)
        self.P = np.diag([2*0.05*h, 2*0.05*h, 2*0.05*h, 2*0.05*h,
                          10*0.0625*h, 10*0.0625*h, 10*0.0625*h, 10*0.0625*h]) ** 2

    def predict(self) -> np.ndarray:
        h = max(float(self.x[3]), 1.0)
        q_pos, q_vel = 0.05 * h, 0.00625 * h
        Q = np.diag([q_pos, q_pos, q_pos, q_pos, q_vel, q_vel, q_vel, q_vel]) ** 2
        self.x = self._F @ self.x
        self.P = self._F @ self.P @ self._F.T + Q
        return self.x[:4].copy()

    def update(self, z: np.ndarray):
        h = max(float(z[3]), 1.0)
        r = 0.05 * h
        R = np.diag([r, r, r, r]) ** 2
        y = z - self._H @ self.x
        S = self._H @ self.P @ self._H.T + R
        K = self.P @ self._H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(8) - K @ self._H) @ self.P

    def reset(self, z: np.ndarray):
        """Re-seat the filter at a new observation with zero velocity —
        used when a LOST track is revived far from its stale prediction."""
        self.__init__(z)


# ── helpers ──────────────────────────────────────────────────────────────
def _box_to_z(box) -> np.ndarray:
    x1, y1, x2, y2 = box
    return np.array([(x1 + x2) / 2.0, (y1 + y2) / 2.0, max(x2 - x1, 1.0), max(y2 - y1, 1.0)])


def _z_to_box(z) -> list:
    cx, cy, w, h = float(z[0]), float(z[1]), max(float(z[2]), 1.0), max(float(z[3]), 1.0)
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


try:  # cv2 makes the histogram ~50x cheaper; numpy fallback keeps tests portable
    import cv2 as _cv2
except Exception:  # pragma: no cover
    _cv2 = None


def _hs_hist(frame_bgr, box) -> Optional[np.ndarray]:
    """16x8 hue×saturation histogram of the INNER part of the crop,
    L1-normalized. Cheap and good enough to tell a cardinal from a finch —
    same-colour swaps are a known limit (and the least harmful kind)."""
    if frame_bgr is None:
        return None
    h, w = frame_bgr.shape[:2]
    bx1, by1, bx2, by2 = box
    cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
    iw, ih = (bx2 - bx1) * _HIST_INNER, (by2 - by1) * _HIST_INNER
    x1, y1 = max(0, int(round(cx - iw / 2))), max(0, int(round(cy - ih / 2)))
    x2, y2 = min(w, int(round(cx + iw / 2))), min(h, int(round(cy + ih / 2)))
    if x2 - x1 < _HIST_MIN_SIDE or y2 - y1 < _HIST_MIN_SIDE:
        return None
    crop = frame_bgr[y1:y2, x1:x2]
    if _cv2 is not None:
        hsv = _cv2.cvtColor(np.ascontiguousarray(crop), _cv2.COLOR_BGR2HSV)
        hist = _cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).ravel()
    else:
        c = crop.astype(np.float32) / 255.0
        b, g, r = c[..., 0], c[..., 1], c[..., 2]
        mx, mn = np.max(c, axis=2), np.min(c, axis=2)
        delta = mx - mn
        sat = np.where(mx > 0, delta / np.maximum(mx, 1e-6), 0.0)
        nz = delta > 1e-6
        rc = np.where(nz, (mx - r) / np.maximum(delta, 1e-6), 0)
        gc = np.where(nz, (mx - g) / np.maximum(delta, 1e-6), 0)
        bc = np.where(nz, (mx - b) / np.maximum(delta, 1e-6), 0)
        hue = np.where(mx == r, bc - gc, np.where(mx == g, 2.0 + rc - bc, 4.0 + gc - rc))
        hue = np.where(nz, (hue / 6.0) % 1.0, 0.0)
        hist, _, _ = np.histogram2d(hue.ravel(), sat.ravel(), bins=[16, 8],
                                    range=[[0, 1], [0, 1]])
        hist = hist.ravel()
    s = float(hist.sum())
    return (hist / s).astype(np.float32) if s > 0 else None


def _hist_sim(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[float]:
    if a is None or b is None:
        return None
    return float(np.sum(np.sqrt(a * b)))  # Bhattacharyya coefficient


# ── tracker ──────────────────────────────────────────────────────────────
class _State:
    """Per-track filter + bookkeeping (kept off the public Track dataclass)."""
    __slots__ = ("kf", "pred", "misses", "hits", "streak", "confirmed", "hist",
                 "last_box", "lost_frames", "last_center", "area_ema", "votes",
                 "spawn_center", "spawn_frame", "revived", "frame",
                 "revive_frame", "votes_since_revive", "frozen")

    def __init__(self, z):
        self.kf = _KalmanBox(z)
        self.pred = z.copy()
        self.misses = 0
        self.hits = 1
        self.streak = 1
        self.confirmed = False
        self.hist = None
        self.last_box = _z_to_box(z)
        self.lost_frames = 0
        self.last_center = (float(z[0]), float(z[1]))
        self.area_ema = float(z[2] * z[3])
        self.votes: dict = {}            # species -> accumulated confidence weight
        self.spawn_center = (float(z[0]), float(z[1]))
        self.spawn_frame = 0
        self.revived = False
        self.frame = 0
        self.revive_frame = 0
        self.votes_since_revive: dict = {}
        self.frozen: Optional[dict] = None   # public identity snapshot taken at loss


def _vote_sim(a: dict, b: dict) -> float:
    """Cosine similarity of two species-weight signatures."""
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    va = np.array([a.get(k, 0.0) for k in keys]); vb = np.array([b.get(k, 0.0) for k in keys])
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    return float(va @ vb / (na * nb)) if na > 0 and nb > 0 else 0.0


class BirdTrackerV4:
    """Drop-in for BirdTracker: update(detections, frame_time_ms, frame_bgr=None, pts=None)."""

    def __init__(self, distance_threshold: float = 2.5, hit_counter_max: int = 150,
                 initialization_delay: int = 2):
        # Signature kept for the factory; v4 semantics come from the knobs above.
        self.tracks: dict = {}          # confirmed, visible (active or coasting)
        self._st: dict = {}             # track_id -> _State (active + lost)
        self._tent: dict = {}           # tentative tracks (not yet emitted)
        self._lost: dict = {}           # track_id -> Track (hidden, re-identifiable)
        self._next_id = 1
        self._frame = 0
        self._pending_merge: dict = {}   # young_id -> lost_id (applied next update)
        self._pending_split: set = set()  # revived ids whose votes contradict (applied next update)
        self.merges: list = []           # (young_id, lost_id) history, for diagnostics
        self.splits: list = []           # (original_id, newcomer_id) history
        self.revivals = 0                # colour/space ReID revivals of lost tracks
        self.id_switches = 0
        # Merge/split ops for the display path ({id, op, from, into|to, at_pts}),
        # append-only with monotonic ids so a client can dedupe repeats. A
        # bounded deque read by op id (process_thread._drain_identity_ops).
        self.identity_log: deque = deque(maxlen=_IDENTITY_LOG_MAX)
        self._identity_seq = 0
        self._pts: Optional[float] = None   # stream clock of the frame in update()
        self._init_hits = max(1, initialization_delay if initialization_delay else _INIT_HITS)

    # ── public ───────────────────────────────────────────────────────────
    def update(self, detections: list, frame_time_ms: float, frame_bgr=None,
               pts: Optional[float] = None) -> TrackerOutput:
        self._frame += 1
        self._pts = pts
        self._apply_pending_merges(frame_time_ms)
        new_tracks: list = self._apply_pending_splits(frame_time_ms)
        dets = self._dedup(detections)
        high = [d for d in dets if d.confidence >= _T_HIGH]
        low = [d for d in dets if _T_LOW <= d.confidence < _T_HIGH]

        # Predict every live filter (confirmed + tentative + lost).
        for st in self._st.values():
            st.pred = st.kf.predict()

        live_ids = list(self.tracks.keys()) + list(self._tent.keys())
        matched_track: dict = {}          # tid -> Detection
        unmatched_high = list(range(len(high)))

        # Stage 1: high-conf ↔ live tracks
        if live_ids and high:
            cost = self._cost_matrix(live_ids, high, stage=1)
            rows, cols = linear_sum_assignment(cost)
            for r, c in zip(rows, cols):
                if cost[r, c] < 1e5:
                    matched_track[live_ids[r]] = high[c]
                    unmatched_high.remove(c)

        # Stage 2: low-conf rescue for tracks that missed stage 1
        rest_ids = [t for t in live_ids if t not in matched_track]
        if rest_ids and low:
            cost = self._cost_matrix(rest_ids, low, stage=2)
            rows, cols = linear_sum_assignment(cost)
            for r, c in zip(rows, cols):
                if cost[r, c] < 1e5:
                    matched_track[rest_ids[r]] = low[c]

        # Apply matches
        for tid, d in matched_track.items():
            self._hit(tid, d, frame_time_ms, frame_bgr)

        # Misses for live tracks not matched
        for tid in live_ids:
            if tid not in matched_track:
                self._miss(tid)

        # Unmatched high-conf: (1) a coasting track's frozen box overlapping the
        # detection is the same bird re-detected after a hop the motion gate
        # rejected — re-seat that track instead of spawning a twin beside it
        # (measured: the twin was the whole 2% duplicate-box rate); (2) ReID
        # vs lost; (3) spawn tentative.
        for c in unmatched_high:
            d = high[c]
            reseated = None
            for tid in self.tracks:
                if tid in matched_track:
                    continue
                st = self._st[tid]
                if st.misses > 0 and _iou(st.last_box, d.box) > 0.3:
                    reseated = tid
                    break
            if reseated is not None:
                z = _box_to_z(d.box)
                st = self._st[reseated]
                st.kf.reset(z)
                st.pred = z.copy()
                st.misses = 0
                matched_track[reseated] = d
                self._hit(reseated, d, frame_time_ms, frame_bgr)
                continue
            revived = self._try_reid(d, frame_time_ms, frame_bgr)
            if revived is not None:
                continue
            self._spawn(d, frame_time_ms, frame_bgr)

        # Promote tentatives that collected enough hits within their short life
        for tid in list(self._tent.keys()):
            st = self._st[tid]
            if st.hits >= self._init_hits:
                trk = self._tent.pop(tid)
                st.confirmed = True
                self.tracks[tid] = trk
                new_tracks.append(trk)

        # Age lost tracks; expire the stale ones
        expired: list = []
        for tid in list(self._lost.keys()):
            st = self._st[tid]
            st.lost_frames += 1
            if st.lost_frames > _LOST_MAX:
                expired.append(self._lost.pop(tid))
                self._st.pop(tid, None)

        # Active = confirmed visible tracks, ordered by id for stable output
        active = [self.tracks[t] for t in sorted(self.tracks.keys())]

        self._count_id_switches(new_tracks, matched_track)
        return TrackerOutput(active=active, new=new_tracks, expired=expired,
                             frame_time_ms=frame_time_ms)

    def stationary_regions(self) -> list:
        return [tuple(t.bbox) for t in self.tracks.values() if t.is_stationary]

    # ── internals ────────────────────────────────────────────────────────
    def _dedup(self, detections: list) -> list:
        if not ((_DEDUP_IOU > 0 or _DEDUP_CONT > 0) and len(detections) > 1):
            return list(detections)
        kept: list = []
        for d in sorted(detections, key=lambda x: -x.confidence):
            dup = False
            for k in kept:
                if _DEDUP_IOU > 0 and _iou(d.box, k.box) > _DEDUP_IOU:
                    dup = True
                    break
                if _DEDUP_CONT > 0 and _containment(d.box, k.box) > _DEDUP_CONT:
                    dup = True
                    break
            if not dup:
                kept.append(d)
        return kept

    def _cost_matrix(self, tids: list, dets: list, stage: int) -> np.ndarray:
        cost = np.full((len(tids), len(dets)), 1e6)
        for i, tid in enumerate(tids):
            st = self._st[tid]
            pbox = _z_to_box(st.pred)
            pw, ph = st.pred[2], st.pred[3]
            size = max(pw, ph, 1.0)
            gate = max(_GATE_SIZE_MULT * size, _GATE_MIN) * (1.0 + 0.25 * min(st.misses, 8))
            gate = min(gate, _GATE_MAX)
            for j, d in enumerate(dets):
                z = _box_to_z(d.box)
                dist = float(np.hypot(z[0] - st.pred[0], z[1] - st.pred[1]))
                iou = _iou(pbox, d.box)
                if stage == 2:
                    if iou >= _LOW_IOU_MIN and st.misses <= _LOW_MAX_MISSES:
                        cost[i, j] = 1.0 - iou
                    continue
                if dist > gate:
                    continue
                # Overlapping candidates (cost in [0,1)) always beat
                # non-overlapping ones (cost in [1,2)).
                cost[i, j] = (1.0 - iou) if iou > 0 else 1.0 + min(dist / gate, 0.999)
        return cost

    def _hit(self, tid: int, d, frame_time_ms: float, frame_bgr):
        st = self._st[tid]
        z = _box_to_z(d.box)
        st.kf.update(z)
        st.misses = 0
        st.hits += 1
        st.streak += 1
        st.last_box = list(d.box)
        st.last_center = (float(z[0]), float(z[1]))
        st.area_ema = 0.8 * st.area_ema + 0.2 * float(z[2] * z[3])
        trk = self.tracks.get(tid) or self._tent.get(tid)
        trk.last_updated_ms = frame_time_ms
        trk.bbox = list(d.box)
        trk.confidence = float(d.confidence)
        trk.frame_count += 1
        trk.coasting = False
        trk.motion_history.append(((d.box[0] + d.box[2]) / 2, (d.box[1] + d.box[3]) / 2))
        if d.confidence >= _T_HIGH and (st.hist is None or st.hits % _HIST_EVERY == 0):
            h = _hs_hist(frame_bgr, d.box)
            if h is not None:
                st.hist = h if st.hist is None else (1 - _HIST_ALPHA) * st.hist + _HIST_ALPHA * h

    def _miss(self, tid: int):
        st = self._st[tid]
        st.misses += 1
        st.streak = 0
        # Damp velocity while unobserved (OC-SORT spirit): the dominant loss
        # on this camera is the detector going blind for seconds on a PERCHED
        # bird — a runaway constant-velocity prediction would carry the gate
        # away from where the bird still sits. Decay ≈0.85/frame: a mover
        # keeps momentum for ~10 frames, a percher's jitter velocity vanishes.
        st.kf.x[4:6] *= 0.85
        if tid in self._tent:
            # Tentative tracks tolerate a few misses: the detector fires only
            # every 3-6 frames on a small bird, so demanding CONSECUTIVE hits
            # kept killing real birds before they could confirm (visit-08
            # coverage 0.65 in replay). A lone flicker still dies — no second
            # hit within _TENT_COAST frames.
            if st.misses > _TENT_COAST:
                self._tent.pop(tid)
                self._st.pop(tid, None)
            return
        trk = self.tracks[tid]
        trk.coasting = True   # bbox stays FROZEN at the last detection
        if st.misses > _COAST_MAX:
            self._lost[tid] = self.tracks.pop(tid)
            st.lost_frames = 0
            st.frozen = {
                "species": trk.species, "species_confidence": trk.species_confidence,
                "model_source": trk.model_source, "is_locked": trk.is_locked,
                "tentative": trk.tentative, "lock_pts": trk.lock_pts,
                "label_epoch": trk.label_epoch, "seg_pts": trk.seg_pts,
                "vote_history": list(trk.vote_history), "votes": dict(st.votes),
                "hist": st.hist, "last_box": list(st.last_box),
                "last_center": st.last_center, "area_ema": st.area_ema,
            }

    def _try_reid(self, d, frame_time_ms: float, frame_bgr) -> Optional[Track]:
        if not self._lost:
            return None
        z = _box_to_z(d.box)
        dh = _hs_hist(frame_bgr, d.box)
        best_tid, best_score = None, -1.0
        new_area = float(z[2] * z[3])
        for tid, trk in self._lost.items():
            st = self._st[tid]
            radius = min(_REID_PX_PER_FRAME * st.lost_frames + max(z[2], z[3]), _REID_RADIUS_MAX)
            dist = float(np.hypot(z[0] - st.last_center[0], z[1] - st.last_center[1]))
            if dist > radius:
                continue
            lost_area = max(st.area_ema, 1.0)   # typical size, not a last blurred box
            if max(new_area, lost_area) / min(new_area, lost_area) > _REID_SIZE_RATIO:
                continue  # a jay is not a titmouse, whatever the colours say
            sim = _hist_sim(st.hist, dh)
            if sim is None:
                # No appearance on either side: only a SHORT gap at essentially
                # the same spot counts (see _REID_BLIND_MAX_FRAMES).
                if st.lost_frames > _REID_BLIND_MAX_FRAMES or dist > 1.0 * max(z[2], z[3]):
                    continue
                score = 0.5 - dist / (radius + 1e-6)
            else:
                if sim < _REID_SIM:
                    continue
                score = sim - 0.3 * (dist / (radius + 1e-6))
            if score > best_score:
                best_tid, best_score = tid, score
        if best_tid is None:
            return None
        trk = self._lost.pop(best_tid)
        st = self._st[best_tid]
        st.kf.reset(z)
        st.pred = z.copy()
        st.misses = 0
        st.streak = 1
        st.lost_frames = 0
        st.revived = True
        st.revive_frame = self._frame
        st.votes_since_revive = {}
        self.revivals += 1
        trk.seg_pts = self._pts
        self.tracks[best_tid] = trk
        self._hit(best_tid, d, frame_time_ms, frame_bgr)
        return trk

    def _spawn(self, d, frame_time_ms: float, frame_bgr):
        tid = self._next_id
        self._next_id += 1
        z = _box_to_z(d.box)
        st = _State(z)
        st.hist = _hs_hist(frame_bgr, d.box)
        st.spawn_frame = self._frame
        self._st[tid] = st
        trk = Track(track_id=tid, created_at_ms=frame_time_ms, last_updated_ms=frame_time_ms,
                    bbox=list(d.box), confidence=float(d.confidence))
        trk.frame_count = 1
        trk.seg_pts = self._pts
        trk.motion_history.append((float(z[0]), float(z[1])))
        if self._init_hits <= 1:
            st.confirmed = True
            self.tracks[tid] = trk
        else:
            self._tent[tid] = trk

    # ── vote-based deferred ReID ─────────────────────────────────────────
    def note_vote(self, track_id: int, species, confidence: float):
        """Called by the classifier path after every vote. Accumulates the
        track's species signature and, for a YOUNG freshly-spawned track,
        checks whether it is really a recently LOST bird come back."""
        st = self._st.get(track_id)
        if st is None or not species:
            return
        st.votes[species] = st.votes.get(species, 0.0) + float(confidence or 0.0)
        if st.revived and st.frozen is not None and track_id in self.tracks:
            st.votes_since_revive[species] = st.votes_since_revive.get(species, 0.0) + float(confidence or 0.0)
            pre = st.frozen.get("votes") or {}
            if (self._frame - st.revive_frame <= _VOTE_YOUNG_FRAMES
                    and sum(pre.values()) >= _VOTE_MIN_WEIGHT
                    and sum(st.votes_since_revive.values()) >= _VOTE_MIN_WEIGHT
                    and _vote_sim(st.votes_since_revive, pre) < _VOTE_SPLIT_SIM):
                self._pending_split.add(track_id)
            return
        if (track_id not in self.tracks or st.revived or track_id in self._pending_merge
                or self._frame - st.spawn_frame > _VOTE_YOUNG_FRAMES or not self._lost):
            return
        if sum(st.votes.values()) < _VOTE_MIN_WEIGHT:
            return
        young_area = max(st.area_ema, 1.0)
        best, best_sim = None, 0.0
        for lid, ltrk in self._lost.items():
            ls = self._st[lid]
            if sum(ls.votes.values()) < _VOTE_MIN_WEIGHT:
                continue
            gap = ls.lost_frames + (self._frame - st.spawn_frame)
            radius = min(_REID_PX_PER_FRAME * gap + np.sqrt(young_area), _REID_RADIUS_MAX)
            dist = float(np.hypot(st.spawn_center[0] - ls.last_center[0],
                                  st.spawn_center[1] - ls.last_center[1]))
            if dist > radius:
                continue
            lost_area = max(ls.area_ema, 1.0)
            if max(young_area, lost_area) / min(young_area, lost_area) > _REID_SIZE_RATIO:
                continue
            sim = _vote_sim(st.votes, ls.votes)
            if sim >= _VOTE_SIM and sim > best_sim:
                best, best_sim = lid, sim
        if best is not None:
            self._pending_merge[track_id] = best

    def _apply_pending_merges(self, frame_time_ms: float):
        """Merge young tracks into the lost tracks they were identified as.
        Deferred to the top of update() so no caller holds a stale Track
        reference mid-frame. The lost track keeps its id, votes and lock; it
        adopts the young track's position/filter/appearance."""
        for young_id, lost_id in list(self._pending_merge.items()):
            self._pending_merge.pop(young_id, None)
            young = self.tracks.get(young_id)
            lost = self._lost.get(lost_id)
            if young is None or lost is None:
                continue
            ys, ls = self._st[young_id], self._st[lost_id]
            # state: young's filter/position/appearance, lost's identity/votes
            ls.kf, ls.pred, ls.misses, ls.streak = ys.kf, ys.pred, ys.misses, ys.streak
            ls.hits += ys.hits
            ls.last_box, ls.last_center = ys.last_box, ys.last_center
            ls.area_ema = ys.area_ema
            ls.hist = ys.hist if ls.hist is None else (0.5 * ls.hist + 0.5 * ys.hist) if ys.hist is not None else ls.hist
            for sp, w in ys.votes.items():
                ls.votes[sp] = ls.votes.get(sp, 0.0) + w
            ls.lost_frames = 0
            ls.revived = True
            # public Track: keep lost's species/lock/history, take young's live fields
            lost.bbox = list(young.bbox)
            lost.confidence = young.confidence
            lost.coasting = young.coasting
            lost.last_updated_ms = frame_time_ms
            lost.frame_count += young.frame_count
            lost.motion_history.extend(young.motion_history)
            lost.vote_history.extend(young.vote_history)
            lost.seg_pts = young.seg_pts
            self.tracks.pop(young_id, None)
            self._st.pop(young_id, None)
            self._lost.pop(lost_id, None)
            self.tracks[lost_id] = lost
            self.merges.append((young_id, lost_id))
            self._log_identity({"op": "merge", "from": young_id, "into": lost_id,
                                "at_pts": young.seg_pts})

    def _apply_pending_splits(self, frame_time_ms: float) -> list:
        """A revived track turned out to be a different bird: give the live
        bird a fresh id (current position/filter/appearance, its own votes)
        and return the original identity — species, lock, votes — to the lost
        buffer as it was at the moment it disappeared. Returns the newcomers
        so update() reports them as new."""
        newcomers: list = []
        for tid in list(self._pending_split):
            self._pending_split.discard(tid)
            trk = self.tracks.get(tid)
            st = self._st.get(tid)
            if trk is None or st is None or st.frozen is None:
                continue
            fr = st.frozen
            at = trk.seg_pts   # revival pts: the ride from here on was the newcomer
            # 1) the newcomer, on a fresh id
            nid = self._next_id
            self._next_id += 1
            ns = _State(st.pred.copy())
            ns.kf, ns.pred, ns.misses, ns.streak = st.kf, st.pred, st.misses, st.streak
            ns.hits = max(self._frame - st.revive_frame, 1)
            ns.confirmed = True
            ns.hist = st.hist
            ns.last_box, ns.last_center, ns.area_ema = st.last_box, st.last_center, st.area_ema
            ns.votes = dict(st.votes_since_revive)
            ns.spawn_center, ns.spawn_frame = st.last_center, st.revive_frame
            ns.revived = False
            self._st[nid] = ns
            post = trk.vote_history[len(fr["vote_history"]):]
            newcomer = Track(track_id=nid, created_at_ms=frame_time_ms, last_updated_ms=frame_time_ms,
                             bbox=list(trk.bbox), confidence=trk.confidence)
            newcomer.frame_count = ns.hits
            newcomer.coasting = trk.coasting
            newcomer.vote_history = list(post)
            newcomer.seg_pts = at
            newcomer.motion_history.extend(list(trk.motion_history)[-5:])
            self.tracks[nid] = newcomer
            newcomers.append(newcomer)
            # 2) the original, back to lost with its identity restored
            trk.species, trk.species_confidence = fr["species"], fr["species_confidence"]
            trk.model_source, trk.is_locked = fr["model_source"], fr["is_locked"]
            trk.tentative, trk.lock_pts = fr["tentative"], fr["lock_pts"]
            trk.label_epoch, trk.seg_pts = fr["label_epoch"], fr["seg_pts"]
            trk.vote_history = list(fr["vote_history"])
            trk.needs_classification = not fr["is_locked"]
            trk.coasting = True
            trk.bbox = list(fr["last_box"])
            st.votes = dict(fr["votes"])
            st.hist, st.last_box, st.last_center, st.area_ema = fr["hist"], fr["last_box"], fr["last_center"], fr["area_ema"]
            st.kf = _KalmanBox(_box_to_z(fr["last_box"]))
            st.pred = st.kf.x[:4].copy()
            st.revived = False
            st.votes_since_revive = {}
            st.lost_frames = self._frame - st.revive_frame
            self.tracks.pop(tid, None)
            self._lost[tid] = trk
            self.splits.append((tid, nid))
            self._log_identity({"op": "split", "from": tid, "to": nid, "at_pts": at})
        return newcomers

    def _log_identity(self, op: dict):
        self._identity_seq += 1
        self.identity_log.append({"id": self._identity_seq, **op})

    def _count_id_switches(self, new_tracks: list, matched_track: dict):
        """Heuristic parity with v3: a freshly confirmed track adjacent to a
        confirmed track that just MISSED this frame is a probable switch."""
        for nt in new_tracks:
            ncx, ncy = (nt.bbox[0] + nt.bbox[2]) / 2, (nt.bbox[1] + nt.bbox[3]) / 2
            nsz = max(nt.bbox[2] - nt.bbox[0], nt.bbox[3] - nt.bbox[1], 1)
            for tid, trk in self.tracks.items():
                if tid == nt.track_id or tid in matched_track:
                    continue
                ecx, ecy = (trk.bbox[0] + trk.bbox[2]) / 2, (trk.bbox[1] + trk.bbox[3]) / 2
                if np.hypot(ncx - ecx, ncy - ecy) < 1.5 * nsz:
                    self.id_switches += 1
                    break
