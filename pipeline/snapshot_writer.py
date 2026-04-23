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
                 go2rtc_url: str = "http://localhost:1984"):
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
        """
        import queue as _q
        self._q: "_q.Queue" = _q.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.classifier = classifier
        self.go2rtc_url = go2rtc_url.rstrip("/")
        self.stats = {
            "submitted": 0,
            "written": 0,
            "dropped_full": 0,
            "errors": 0,
            "hires_ok": 0,
            "hires_fail": 0,
            "aiy_relabel": 0,
            "aiy_none": 0,
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
                ["/usr/local/bin/ffmpeg",
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
        model_source} or None. Used by `_write_one` to override the track's
        live (yard-biased) label with AIY's 965-species verdict for the
        classifications.db row.
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

    def _write_one(self, p: dict):
        # Try to swap in a 1080p frame from go2rtc's feeder-main stream. The
        # detector's 640x360 substream frame is otherwise all the reviewer
        # ever sees — this lifts the review queue to proper resolution AND
        # gives the AIY re-classification a much larger crop to work with
        # (AIY's "don't know" rate on small crops is the reason yard was
        # kept live — a fresh 1080p crop fixes that systemically for the
        # DB-write path without changing the live overlay).
        hires = self._fetch_hires_frame(p["camera"])
        if hires is not None:
            low_w = p["frame"].shape[1] or 1
            low_h = p["frame"].shape[0] or 1
            sx = hires.shape[1] / low_w
            sy = hires.shape[0] / low_h
            p["bbox"] = [p["bbox"][0] * sx, p["bbox"][1] * sy,
                         p["bbox"][2] * sx, p["bbox"][3] * sy]
            p["frame"] = hires
            self.stats["hires_ok"] += 1
        else:
            self.stats["hires_fail"] += 1

        # Re-classify with AIY on the (now hi-res, ideally) crop. This is the
        # authority for classifications.db — yard's 12-species label set is
        # not durable enough for the review queue.
        auth = self._authoritative_species(p["frame"], p["bbox"])
        if auth is not None:
            p["species"] = auth["species"]
            p["species_confidence"] = auth["confidence"]
            p["model_source"] = auth["model_source"]
            self.stats["aiy_relabel"] += 1
        else:
            # Keep track's live (yard) label as a fallback. In practice
            # reviewers correct it anyway; the important thing is the JPG
            # is preserved and the row goes into the queue.
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
