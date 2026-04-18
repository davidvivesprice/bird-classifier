"""Write classified-track snapshots to disk + classifications.db.

Restores the pre-v3 data flow: when a track locks, save a JPG of the current
frame to `~/bird-snapshots/classified/<species>/` and insert a row in
`classifications.db` so the dashboard's Classify / Activity views have fresh
data to review.

v3's `event_store` (pipeline.db) still receives per-frame and per-track rows;
this is an additional, one-row-per-locked-track path feeding the legacy
`classifications` schema the dashboard already knows how to query.

See the data-integrity forget-me-not for why we accept yard-model wrongness
here: the plumbing must keep flowing so reviewers can correct labels.
"""
from __future__ import annotations

import logging
import threading
import time
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

    def __init__(self, maxsize: int = 32):
        import queue as _q
        self._q: "_q.Queue" = _q.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stats = {
            "submitted": 0,
            "written": 0,
            "dropped_full": 0,
            "errors": 0,
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

    def _write_one(self, p: dict):
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
