"""Write classified-track snapshots to disk + classifications.db.

Restores the pre-v3 data flow: when a track locks, save a JPG of the current
frame to `~/bird-snapshots/classified/<species>/` and insert a row in
`classifications.db` so the dashboard's Classify / Activity views have fresh
data to review.

v3's `event_store` (pipeline.db) still receives per-frame and per-track rows;
this is an additional, one-row-per-locked-track path feeding the legacy
`classifications` schema the dashboard already knows how to query.

Classifier wrongness model differs by host: iMac runs yard (Coral) → AIY
fallback, so a track can lock as "yard" then get an AIY second opinion at
write time. Pi runs AIY-only via PiClassifier, so the lock-time and
write-time labels are both AIY (re-run on a sharper crop). In either case
the plumbing must keep flowing — reviewers correct labels downstream.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


log = logging.getLogger(__name__)

SNAPSHOT_ROOT = Path.home() / "bird-snapshots" / "classified"
ANNOTATED_ROOT = Path.home() / "bird-snapshots" / "annotated"
PENDING_ROOT = Path.home() / "bird-snapshots" / "pending"  # pre-classification holding, for parity


def _draw_annotated_brackets(frame, bbox, label: str, inflate: float = 0.10):
    """Draw /live-style corner L-brackets + pill label onto a BGR frame in place.

    Matches the visual language of the live overlay (corner brackets inflated
    10% outside the bbox, white strokes with dark shadow). Used for the
    'annotated' tab in the dashboard's Classify view.
    """
    x1, y1, x2, y2 = [int(v) for v in bbox]
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return
    inf_x = int(bw * inflate)
    inf_y = int(bh * inflate)
    fh_px, fw_px = frame.shape[:2]
    x1i = max(0, x1 - inf_x)
    y1i = max(0, y1 - inf_y)
    x2i = min(fw_px - 1, x2 + inf_x)
    y2i = min(fh_px - 1, y2 + inf_y)

    L = int(max(10, min(28, min(x2i - x1i, y2i - y1i) * 0.18)))

    # Draw shadow pass first (thick black), then crisp white on top
    for (color, thick) in [((0, 0, 0), 4), ((255, 255, 255), 2)]:
        # top-left
        cv2.line(frame, (x1i, y1i + L), (x1i, y1i), color, thick, cv2.LINE_AA)
        cv2.line(frame, (x1i, y1i), (x1i + L, y1i), color, thick, cv2.LINE_AA)
        # top-right
        cv2.line(frame, (x2i - L, y1i), (x2i, y1i), color, thick, cv2.LINE_AA)
        cv2.line(frame, (x2i, y1i), (x2i, y1i + L), color, thick, cv2.LINE_AA)
        # bottom-left
        cv2.line(frame, (x1i, y2i - L), (x1i, y2i), color, thick, cv2.LINE_AA)
        cv2.line(frame, (x1i, y2i), (x1i + L, y2i), color, thick, cv2.LINE_AA)
        # bottom-right
        cv2.line(frame, (x2i - L, y2i), (x2i, y2i), color, thick, cv2.LINE_AA)
        cv2.line(frame, (x2i, y2i), (x2i, y2i - L), color, thick, cv2.LINE_AA)

    # Label above top edge
    if label:
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.5
        thickness = 1
        (tw, th), baseline = cv2.getTextSize(label, font, scale, thickness)
        cx = (x1i + x2i) // 2
        pad = 6
        tx = max(pad, min(fw_px - tw - pad, cx - tw // 2))
        ty = max(th + pad + 2, y1i - 8)
        # Dark pill background
        cv2.rectangle(
            frame,
            (tx - pad, ty - th - pad // 2),
            (tx + tw + pad, ty + pad // 2 + 2),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            frame, label, (tx, ty),
            font, scale, (255, 255, 255), thickness, cv2.LINE_AA,
        )


def _safe_species_dir(species: Optional[str]) -> str:
    """Filesystem-safe species directory name."""
    if not species:
        return "_unclassified"
    # Keep letters, digits, spaces, hyphens, apostrophes; collapse others to _
    kept = []
    for ch in species:
        if ch.isalnum() or ch in " -'":
            kept.append(ch)
        else:
            kept.append("_")
    return "".join(kept).strip() or "_unclassified"


class SnapshotWriter:
    """One instance per pipeline. Owns a background queue so snapshot I/O
    never blocks the process thread.

    Usage:
        writer = SnapshotWriter()
        writer.start()
        # per locked track:
        writer.submit(camera, frame_bgr, wall_time_ms, track)
    """

    def __init__(self, maxsize: int = 32, classifier=None,
                 go2rtc_url: str = "http://localhost:1984",
                 hires_ring=None,
                 shadow_mode: bool = True):
        """
        Args:
            maxsize: max queued snapshots before oldest is dropped
            classifier: SmartClassifier instance — if provided, the snapshot
                writer re-runs AIY on the hi-res crop at write time and uses
                AIY's species (not the track's live yard label) for the
                classifications.db row. This is how the review queue gets
                AIY's 965-species authority while yard still drives the fast
                live overlay.
            go2rtc_url: base URL for go2rtc. Used to fetch 1080p frames via
                `/api/frame.mp4?src=<camera>-main` so the saved JPG is
                full-res rather than the 640x360 detector frame.
            hires_ring: optional HiResRingBuffer. When provided, _write_one
                prefers the nearest-timestamp frame from the ring over the
                go2rtc /api/frame.mp4 fetch (which has a 2-5s keyframe wait
                that causes the stale-bbox hallucination). None = current
                behavior preserved.
            shadow_mode: when True AND hires_ring is provided, the ring's
                picked frame is written as a sidecar JSON only; the actual
                JPG is still produced via the old go2rtc path. Lets David
                eyeball ring-vs-old for 3-4 days before the ring becomes
                authoritative. Flip to False after soak.
        """
        import queue as _q
        self._q: "_q.Queue" = _q.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.classifier = classifier
        self.go2rtc_url = go2rtc_url.rstrip("/")
        self.hires_ring = hires_ring
        self.shadow_mode = shadow_mode
        self.stats = {
            "submitted": 0,
            "written": 0,
            "dropped_full": 0,
            "errors": 0,
            "hires_ok": 0,
            "hires_fail": 0,
            "hires_skipped": 0,      # cheap-restore path (default since 2026-04-23)
            "aiy_relabel": 0,
            "aiy_none": 0,
            "ring_pick_ok": 0,
            "ring_pick_empty": 0,
            "shadow_sidecar_written": 0,
        }

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name="snapshot-writer", daemon=True,
        )
        self._thread.start()
        SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)

    def stop(self):
        self._stop.set()

    def submit(self, camera: str, frame_bgr: np.ndarray, wall_time_ms: float, track):
        """Non-blocking submit. Drops oldest on backpressure."""
        # Copy the frame (np arrays are shared; caller reuses the buffer).
        payload = {
            "camera": camera,
            "frame": frame_bgr.copy(),
            "wall_time_ms": wall_time_ms,
            "track_id": track.track_id,
            "species": track.species,
            "species_confidence": track.species_confidence,
            "model_source": track.model_source,
            "confidence": track.confidence,  # YOLO detection confidence
            "bbox": list(track.bbox),
            "frame_count": track.frame_count,
            "vote_history": list(track.vote_history),
        }
        self.stats["submitted"] += 1
        try:
            self._q.put_nowait(payload)
        except Exception:
            # Full — drop silently but count.
            self.stats["dropped_full"] += 1

    def _loop(self):
        while not self._stop.is_set():
            try:
                payload = self._q.get(timeout=0.5)
            except Exception:
                continue
            try:
                self._write_one(payload)
                self.stats["written"] += 1
            except Exception as e:
                self.stats["errors"] += 1
                log.exception("snapshot write failed: %s", e)

    def _fetch_hires_frame(self, camera: str, timeout_s: float = 10.0) -> Optional[np.ndarray]:
        """Fetch a single 1080p BGR frame from go2rtc for `{camera}-main`.

        Pipeline detection runs on the 640x360 substream for CPU reasons, so
        `p["frame"]` in the snapshot queue is always low-res. For the review
        queue we want the full resolution. go2rtc exposes a single-frame MP4
        at `/api/frame.mp4?src=<stream>` — we fetch it over HTTP, pipe the
        bytes through ffmpeg to produce a JPEG, then decode back to BGR.

        Timeout is 10s by default — go2rtc has to wait for the next H.264
        keyframe in the main stream before it can emit the MP4, which is
        measured at 2–5s in practice. Tighter timeouts fail on cold streams.

        Runs only in the background snapshot worker thread, so the ~3–5 s
        wait doesn't block detection. Returns None on any failure (bad HTTP
        status, ffmpeg error, short timeout), letting the caller fall back
        to the low-res detector frame with a counter bump.
        """
        url = f"{self.go2rtc_url}/api/frame.mp4?src={camera}-main"
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                if resp.status != 200:
                    return None
                mp4_bytes = resp.read()
        except Exception as e:
            log.debug("hires fetch failed for %s: %s", camera, e)
            return None
        if not mp4_bytes:
            return None
        try:
            proc = subprocess.run(
                [shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg",
                 "-hide_banner", "-loglevel", "error",
                 "-f", "mp4", "-i", "pipe:0",
                 "-frames:v", "1",
                 "-vcodec", "mjpeg",
                 "-f", "image2", "pipe:1"],
                input=mp4_bytes,
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except Exception as e:
            log.debug("hires ffmpeg spawn failed for %s: %s", camera, e)
            return None
        if proc.returncode != 0 or not proc.stdout:
            err_preview = (proc.stderr or b"")[:200].decode("utf-8", errors="replace")
            log.debug("hires ffmpeg decode failed for %s: rc=%d err=%s",
                      camera, proc.returncode, err_preview)
            return None
        try:
            arr = np.frombuffer(proc.stdout, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None or bgr.size == 0:
                return None
            return bgr
        except Exception as e:
            log.debug("hires jpeg decode failed for %s: %s", camera, e)
            return None

    def _authoritative_species(self, frame_bgr: np.ndarray, bbox) -> Optional[dict]:
        """Run AIY on the given frame+bbox crop, return {species, confidence,
        model_source} or None. Used by `_write_one` to record an authoritative
        second opinion alongside the lock-time label (see RC3 in
        docs/superpowers/plans/2026-04-25-rc3-preserve-lock-time-vote.md). The
        lock-time vote remains canonical; this result is metadata for review/
        filtering.
        """
        if self.classifier is None:
            return None
        x1, y1, x2, y2 = [int(v) for v in bbox]
        x1 = max(0, x1); y1 = max(0, y1)
        x2 = min(frame_bgr.shape[1], x2); y2 = min(frame_bgr.shape[0], y2)
        if x2 - x1 < 5 or y2 - y1 < 5:
            return None
        crop_bgr = frame_bgr[y1:y2, x1:x2]
        if crop_bgr.size == 0:
            return None
        try:
            from PIL import Image as _PILImage
            crop_pil = _PILImage.fromarray(crop_bgr[:, :, ::-1])  # BGR→RGB
            result = self.classifier.authoritative_classify(crop_pil)
        except Exception as e:
            log.debug("authoritative_classify threw: %s", e)
            return None
        if result is None or not result.species:
            return None
        return {
            "species": result.species,
            "confidence": float(result.confidence or 0.0),
            "model_source": result.model_source or "aiy",
        }

    def _pick_from_ring(self, camera, wall_time_ms, bbox_scaled, detector_conf):
        """Query the ring for candidates, score, return best (frame, meta)."""
        if self.hires_ring is None:
            return None, {"reason": "no_ring"}
        try:
            from pipeline.hires_ring import score_frame
            cands = self.hires_ring.find_candidates(wall_time_ms, k=5)
        except Exception as e:
            log.debug("ring query failed: %s", e)
            return None, {"reason": "query_error"}
        if not cands:
            return None, {"reason": "ring_empty"}
        scored = []
        for c in cands:
            s = score_frame(c.frame, bbox_scaled, detector_conf)
            scored.append((s, c))
        scored.sort(key=lambda sc: sc[0], reverse=True)
        best_score, best = scored[0]
        meta = {
            "picked_wall_ms": best.wall_ms,
            "picked_score": best_score,
            "delta_ms": best.wall_ms - wall_time_ms,
            "requested_wall_ms": wall_time_ms,
            "candidates": [{"wall_ms": c.wall_ms, "score": s}
                           for s, c in scored],
        }
        if best_score <= 0:
            return None, {"reason": "all_zero_score", **meta}
        return best.frame, meta

    def _write_one(self, p: dict):
        # Hi-res frame acquisition — three paths:
        # (1) If self.hires_ring is configured AND shadow_mode is False, use
        #     the ring's best-quality nearest-timestamp frame. Real fix for
        #     the stale-bbox hallucination. (Needs Pi 5 CPU headroom.)
        # (2) CHEAP RESTORE (default since 2026-04-23, David-approved):
        #     skip the go2rtc /api/frame.mp4 fetch entirely. The detection-
        #     time 640x360 frame is used as-is. This is what AIY had
        #     pre-Sonoma when it scored 69.3% top-1 / 75.6% macro-F1 — the
        #     SAME frame the bird was detected on, no stale-bbox window.
        #     Crop is smaller but the bird is IN IT.
        # (3) Old broken behavior (opt-in, env PIPELINE_HIRES_RECROP=1):
        #     fetch a 1080p frame via /api/frame.mp4. Had a 2-5 second
        #     keyframe wait that caused the hallucination. Retained for
        #     comparison / A-B only.
        _do_hires_recrop = os.environ.get("PIPELINE_HIRES_RECROP", "0") == "1"

        low_w = p["frame"].shape[1] or 1
        low_h = p["frame"].shape[0] or 1
        HIRES_W, HIRES_H = 1920, 1080
        sx_hires = HIRES_W / low_w
        sy_hires = HIRES_H / low_h
        bbox_hires = [p["bbox"][0] * sx_hires, p["bbox"][1] * sy_hires,
                      p["bbox"][2] * sx_hires, p["bbox"][3] * sy_hires]

        ring_frame, ring_meta = self._pick_from_ring(
            p["camera"], p["wall_time_ms"], bbox_hires,
            p.get("confidence", 0.0),
        )

        if ring_frame is not None and not self.shadow_mode:
            # Path 1: ring authoritative.
            p["frame"] = ring_frame
            p["bbox"] = bbox_hires
            self.stats["hires_ok"] += 1
            self.stats["ring_pick_ok"] += 1
            p["_ring_sidecar"] = ring_meta
        elif _do_hires_recrop:
            # Path 3: the old (broken) hi-res fetch, opt-in only.
            if ring_frame is None:
                self.stats["ring_pick_empty"] += 1
            hires = self._fetch_hires_frame(p["camera"])
            if hires is not None:
                sx = hires.shape[1] / low_w
                sy = hires.shape[0] / low_h
                p["bbox"] = [p["bbox"][0] * sx, p["bbox"][1] * sy,
                             p["bbox"][2] * sx, p["bbox"][3] * sy]
                p["frame"] = hires
                self.stats["hires_ok"] += 1
            else:
                self.stats["hires_fail"] += 1
            if ring_meta.get("picked_wall_ms") is not None:
                p["_ring_sidecar"] = ring_meta
        else:
            # Path 2 (default): cheap restore. Keep the detection-time 640x360
            # frame + its bbox as-is. AIY classifies the crop the bird was
            # actually in.
            self.stats["hires_skipped"] = self.stats.get("hires_skipped", 0) + 1
            if ring_frame is None:
                self.stats["ring_pick_empty"] += 1
            if ring_meta.get("picked_wall_ms") is not None:
                p["_ring_sidecar"] = ring_meta

        # Capture lock-time classification values BEFORE auth call. RC3:
        # the live pipeline's vote-lock decision (yard / AIY / both_agree at
        # lock moment) is the canonical "what the system thought" record.
        # The authoritative AIY second opinion below is metadata, not a
        # replacement. See docs/superpowers/plans/2026-04-25-rc3-*.md
        lock_time_species = p["species"]
        lock_time_confidence = p["species_confidence"]
        lock_time_source = p["model_source"]

        # Re-classify with AIY on the (now hi-res, ideally) crop. Result is
        # stored as METADATA (not as a replacement for lock-time). This lets
        # us see in review whether AIY at write time agrees with what the
        # live pipeline decided. Disagreement + low auth confidence = noise
        # marker for retrospective filtering.
        auth = self._authoritative_species(p["frame"], p["bbox"])
        if auth is not None:
            self.stats["aiy_relabel"] += 1
        else:
            self.stats["aiy_none"] += 1

        wall_time_ms = p["wall_time_ms"]
        camera = p["camera"]
        species = p["species"]
        track_id = p["track_id"]

        source_dt = datetime.fromtimestamp(wall_time_ms / 1000.0)
        source_ts = source_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
        fname_stamp = source_dt.strftime("%Y-%m-%d_%H-%M-%S")
        fname = f"{camera}_{fname_stamp}_{int(track_id)}.jpg"

        species_dir = SNAPSHOT_ROOT / _safe_species_dir(species)
        species_dir.mkdir(parents=True, exist_ok=True)
        out_path = species_dir / fname

        # Encode + write raw JPG (the "photo" tab)
        ok, buf = cv2.imencode(".jpg", p["frame"], [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError("cv2.imencode failed")
        out_path.write_bytes(buf.tobytes())

        # Annotated version with corner brackets (the "annotated" tab).
        # Reviewer needs a visible marker on the bird so they know which
        # detection in the frame was the classified one — critical for
        # multi-bird frames.
        try:
            ANNOTATED_ROOT.mkdir(parents=True, exist_ok=True)
            annotated = p["frame"].copy()
            conf_pct = int((p["species_confidence"] or 0) * 100)
            label_text = f"{species} {conf_pct}%" if species else "bird"
            _draw_annotated_brackets(annotated, p["bbox"], label_text)
            ok2, buf2 = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok2:
                (ANNOTATED_ROOT / fname).write_bytes(buf2.tobytes())
        except Exception as e:
            log.warning("annotated write failed for %s: %s", fname, e)

        # Build classifications row in the same shape insert_classification expects.
        top_prediction = {
            "common_name": species or "",
            "scientific_name": None,
            "raw_score": int((p["species_confidence"] or 0) * 100),
        }
        best_detection = {
            "box": p["bbox"],
            "confidence": float(p["confidence"] or 0),
        }

        # top3: we only have one species in vote_history; synthesize [species, ., .].
        # The review UI tolerates sparse top3.
        top3 = [top_prediction]

        entry = {
            "file": fname,
            "camera": camera,
            "timestamp": datetime.now().isoformat(),
            "source_timestamp": source_ts,
            "action": "classified",
            "detections": 1,
            "best_detection": best_detection,
            "top_prediction": top_prediction,
            "top3": top3,
            "birds": [{
                "box": p["bbox"],
                "common_name": species or "",
                "confidence": best_detection["confidence"],
                "raw_score": top_prediction["raw_score"],
            }],
            "extra_json": None,
            # Provenance tag so this pipeline's rows are identifiable later.
            "pipeline_source": "bird_pipeline_v3",
            "track_id": int(track_id),
            "vote_history_len": len(p.get("vote_history") or []),
            "model_source": str(p.get("model_source") or ""),
            # RC3: lock-time + authoritative + disagreement, all stored in
            # extra_json (classifications_db packs unknown fields automatically
            # — see classifications_db.py:149).
            "lock_time": {
                "species": lock_time_species,
                "confidence": lock_time_confidence,
                "source": lock_time_source,
            },
            "authoritative": {
                "species": auth["species"] if auth else None,
                "confidence": auth["confidence"] if auth else None,
                "source": auth["model_source"] if auth else None,
            } if auth else None,
            "disagreement": bool(auth and auth["species"] != lock_time_species),
        }

        try:
            import classifications_db as cdb
            cdb.insert_classification(entry)
        except Exception as e:
            # Don't leave an orphan JPG around if DB write fails.
            try:
                out_path.unlink()
            except Exception:
                pass
            raise

        # Ring-buffer sidecar: when the ring is active, record the ring's
        # pick next to the JPG so David can eyeball ring-vs-written for
        # trust-building before shadow_mode flips off.
        sidecar_meta = p.get("_ring_sidecar")
        if sidecar_meta:
            import json as _json
            sidecar_path = out_path.with_suffix(".ring.json")
            try:
                sidecar_path.write_text(_json.dumps(sidecar_meta, indent=2))
                self.stats["shadow_sidecar_written"] += 1
            except Exception as e:
                log.debug("ring sidecar write failed: %s", e)
