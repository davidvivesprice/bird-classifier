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

import json
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
HLS_ROOT = Path.home() / "bird-snapshots" / "hls"


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
                 hls_root: Path | str = HLS_ROOT,
                 hls_wait_timeout_s: float = 7.0,
                 # Legacy params kept for call-site compat. hires_ring is
                 # still not wired in this path; the Pi recovers high-res
                 # frames from the PTS-aware HLS segmenter when inline
                 # bgr_full is only detector-sized.
                 hires_ring=None, shadow_mode: bool = False):
        """
        Args:
            maxsize: max queued snapshots before oldest is dropped
            classifier: PiClassifier / SmartClassifier — if provided, the
                snapshot writer re-runs the classifier on the (now hi-res)
                crop at write time and records it as the "authoritative"
                second-opinion alongside the lock-time vote.
            go2rtc_url: legacy parameter, no longer used by the primary path.
            hls_root: root containing per-camera HLS segmenter output. Used
                when Frame.bgr_full is only the low-res detector frame.
            hls_wait_timeout_s: max time to wait for the lock-time PTS to
                appear in segments.json. The matching segment may still be
                open when the track locks.
            hires_ring: legacy, must be None.
            shadow_mode: legacy, must be False.
        """
        import queue as _q
        self._q: "_q.Queue" = _q.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.classifier = classifier
        self.go2rtc_url = go2rtc_url.rstrip("/")
        self.hls_root = Path(hls_root)
        self.hls_wait_timeout_s = float(hls_wait_timeout_s)
        self.stats = {
            "submitted": 0,
            "written": 0,
            "dropped_full": 0,
            "errors": 0,
            "hires_ok": 0,           # true high-res frame used
            "hires_fail": 0,         # fell back to detector-resolution frame
            "hires_inline_ok": 0,    # frame.bgr_full was genuinely high-res
            "hires_hls_ok": 0,       # frame extracted from HLS segment by PTS
            "hires_hls_miss": 0,     # no usable HLS frame for the lock PTS
            "hires_lowres_fallback": 0,
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

    def submit(self, camera: str, frame_bgr: np.ndarray, wall_time_ms: float, track,
               frame_bgr_full: Optional[np.ndarray] = None,
               pts: float = 0.0):
        """Non-blocking submit. Drops oldest on backpressure.

        The caller passes the detection-sized frame (`frame_bgr`, e.g.
        640×360) and may pass an inline decoded input frame as
        `frame_bgr_full`. On the Pi's substream detect path that inline frame
        is also 640×360; the writer then uses `pts` to extract the matching
        1920×1080 frame from the main-stream HLS segmenter.

        `pts` is the canonical clock — recorded so downstream consumers
        (review UI, debugging, test harness) can correlate events across
        the system without going through wall-clock.
        """
        payload = {
            "camera": camera,
            # No .copy() (Track B audit 2026-05-11): FrameCapture allocates
            # fresh ndarrays per frame via PyAV.to_ndarray — the producer
            # never mutates a frame after put_nowait, so holding the
            # reference is enough to keep this buffer alive until the
            # worker processes it. Two ~6 MB copies per locked track were
            # pure defensive overhead.
            "frame": frame_bgr,
            "hires_frame": frame_bgr_full,
            "wall_time_ms": wall_time_ms,
            "pts": float(pts),
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

    def _locate_hls_segment(self, camera: str, pts: float,
                            tolerance_s: float = 0.25) -> Optional[tuple[Path, float]]:
        """Return (segment_path, relative_offset_s) covering the target PTS."""
        if pts is None or pts <= 0:
            return None
        sidecar = self.hls_root / camera / "segments.json"
        try:
            data = json.loads(sidecar.read_text())
        except Exception as e:
            log.debug("HLS sidecar read failed for %s: %s", camera, e)
            return None

        segments = data.get("segments") or []
        best = None
        best_distance = None
        for seg in segments:
            try:
                start = float(seg["pts_start"])
                end = float(seg["pts_end"])
                name = str(seg["name"])
            except Exception:
                continue
            if start <= pts <= end:
                offset = max(0.0, min(pts - start, max(0.0, end - start)))
                return self.hls_root / camera / name, offset
            distance = min(abs(pts - start), abs(pts - end))
            if best_distance is None or distance < best_distance:
                offset = max(0.0, min(pts - start, max(0.0, end - start)))
                best = (self.hls_root / camera / name, offset)
                best_distance = distance

        if best is not None and best_distance is not None and best_distance <= tolerance_s:
            return best
        return None

    def _extract_hls_frame(self, segment_path: Path, offset_s: float,
                           timeout_s: float = 10.0) -> Optional[np.ndarray]:
        """Decode one frame from a finalized HLS segment.

        `-ss` is intentionally placed after `-i`: the Pi's MPEG-TS segments
        preserve absolute PTS, and input-side seeking against these short
        segments can return zero frames. Output-side seeking decodes at most
        one ~5s segment, which is acceptable at snapshot event rate.
        """
        if not segment_path.exists():
            return None
        try:
            proc = subprocess.run(
                [shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg",
                 "-hide_banner", "-loglevel", "error",
                 "-i", str(segment_path),
                 "-ss", f"{max(0.0, float(offset_s)):.3f}",
                 "-frames:v", "1",
                 "-vcodec", "mjpeg",
                 "-f", "image2", "pipe:1"],
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except Exception as e:
            log.debug("HLS frame extract failed for %s: %s", segment_path, e)
            return None
        if proc.returncode != 0 or not proc.stdout:
            err_preview = (proc.stderr or b"")[:200].decode("utf-8", errors="replace")
            log.debug("HLS frame extract returned no frame for %s: rc=%d err=%s",
                      segment_path, proc.returncode, err_preview)
            return None
        arr = np.frombuffer(proc.stdout, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None or bgr.size == 0:
            return None
        return bgr

    def _fetch_hls_frame_for_pts(self, camera: str, pts: float,
                                 timeout_s: Optional[float] = None) -> Optional[np.ndarray]:
        """Wait for the HLS segment covering `pts`, then extract one frame."""
        if pts is None or pts <= 0:
            return None
        if not (self.hls_root / camera / "segments.json").exists():
            return None
        if timeout_s is None:
            timeout_s = self.hls_wait_timeout_s
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            hit = self._locate_hls_segment(camera, pts)
            if hit is not None:
                segment_path, offset_s = hit
                return self._extract_hls_frame(segment_path, offset_s)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

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

    def _write_one(self, p: dict):
        # Prefer a true high-resolution frame from the same timeline. In the
        # intended single-stream mode `bgr_full` is already larger than the
        # detector frame. On the Pi's current substream architecture, bgr_full
        # is also 640x360; in that case extract one main-stream frame from the
        # PTS-aware HLS segmenter before falling back to detector resolution.
        low_w = p["frame"].shape[1] or 1
        low_h = p["frame"].shape[0] or 1
        inline_hires_frame = p.get("hires_frame")
        hires_frame = None
        if (inline_hires_frame is not None
                and inline_hires_frame.shape[1] > low_w
                and inline_hires_frame.shape[0] > low_h):
            hires_frame = inline_hires_frame
            self.stats["hires_inline_ok"] += 1
        else:
            fetched = self._fetch_hls_frame_for_pts(
                p["camera"], float(p.get("pts", 0.0)),
                timeout_s=self.hls_wait_timeout_s,
            )
            if (fetched is not None
                    and fetched.shape[1] > low_w
                    and fetched.shape[0] > low_h):
                hires_frame = fetched
                self.stats["hires_hls_ok"] += 1
            else:
                self.stats["hires_hls_miss"] += 1

        if hires_frame is not None:
            sx = hires_frame.shape[1] / low_w
            sy = hires_frame.shape[0] / low_h
            p["bbox"] = [p["bbox"][0] * sx, p["bbox"][1] * sy,
                         p["bbox"][2] * sx, p["bbox"][3] * sy]
            p["frame"] = hires_frame
            self.stats["hires_ok"] += 1
        else:
            # Detect frame stays as-is — used for tests / dry-run / any
            # capture path that doesn't carry bgr_full. The authoritative
            # classifier pass below still runs on whatever frame we have.
            self.stats["hires_fail"] += 1
            if inline_hires_frame is not None:
                self.stats["hires_lowres_fallback"] += 1

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
            "source_mode": "demo" if os.environ.get("PIPELINE_TEST_RTSP_URL") else "live",
            "source_stream": os.environ.get("PIPELINE_TEST_RTSP_URL") or "live",
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
            # PTS: canonical clock for cross-component sync. Recorded on
            # every classification row so reviewers / debuggers / the test
            # harness can correlate this snapshot to the SSE event and the
            # exact video frame, without going through wall-clock.
            "pts": float(p.get("pts", 0.0)),
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
