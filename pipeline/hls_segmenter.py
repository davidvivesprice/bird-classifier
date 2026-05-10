"""HLS segmenter: PyAV passthrough mux + manifest/sidecar writer.

See spec at docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


# PDT epoch: encode our PTS as 1970-01-01 + pts_seconds. See spec §C2/S3.
_PDT_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def pts_to_pdt(pts_s: float) -> str:
    """Encode camera PTS (seconds) as an ISO 8601 PDT string with ms precision."""
    dt = _PDT_EPOCH + timedelta(seconds=pts_s)
    # Format: 1970-01-01T00:14:33.002Z (3-digit ms, Z suffix not +00:00)
    # datetime.isoformat() with timespec='milliseconds' gives the .fff part.
    iso = dt.isoformat(timespec="milliseconds")
    return iso.replace("+00:00", "Z")


def pdt_to_pts(pdt: str) -> float:
    """Decode a PDT string back to PTS seconds (inverse of pts_to_pdt)."""
    # Strip trailing Z, parse as +00:00
    if pdt.endswith("Z"):
        pdt = pdt[:-1] + "+00:00"
    dt = datetime.fromisoformat(pdt)
    return (dt - _PDT_EPOCH).total_seconds()


@dataclass
class Segment:
    """One HLS segment's metadata for the sidecar."""
    name: str
    pts_start: float
    pts_end: float

    @property
    def duration(self) -> float:
        return self.pts_end - self.pts_start


@dataclass
class Discontinuity:
    after: str                # filename after which the discontinuity occurs
    old_pts_end: float
    new_pts_start: float


def serialize_sidecar(
    stream: str,
    segments: list[Segment],
    discontinuities: list[Discontinuity],
) -> str:
    """Render the segments.json sidecar contents as a JSON string."""
    return json.dumps({
        "stream": stream,
        "time_base_seconds": 1.0,
        "segments": [
            {
                "name": s.name,
                "pts_start": s.pts_start,
                "pts_end": s.pts_end,
                "duration": s.duration,
            }
            for s in segments
        ],
        "discontinuities": [
            {
                "after": d.after,
                "old_pts_end": d.old_pts_end,
                "new_pts_start": d.new_pts_start,
            }
            for d in discontinuities
        ],
    }, indent=2)


import math


def serialize_manifest(
    segments: list[Segment],
    media_sequence: int,
    discontinuity_sequence: int,
    target_duration: Optional[int],
    discontinuity_boundaries: set[str],
) -> str:
    """Render a live HLS manifest (.m3u8) for the given sliding window.

    Args:
        segments: ordered list of segments to include (oldest first).
        media_sequence: EXT-X-MEDIA-SEQUENCE — sequence number of segments[0].
        discontinuity_sequence: EXT-X-DISCONTINUITY-SEQUENCE — incremented
            across each discontinuity in the FULL history (not just this window).
        target_duration: EXT-X-TARGETDURATION (integer seconds). If None,
            auto-computed as ceil(max segment duration).
        discontinuity_boundaries: set of segment.name values where a
            DISCONTINUITY tag should be inserted IMMEDIATELY BEFORE.
    """
    if target_duration is None:
        target_duration = max(1, math.ceil(max(s.duration for s in segments)))

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]

    last_pdt_emitted_pts: Optional[float] = None

    for i, seg in enumerate(segments):
        is_disc_boundary = seg.name in discontinuity_boundaries
        # Emit DISCONTINUITY tag before this segment if needed.
        if is_disc_boundary:
            lines.append("#EXT-X-DISCONTINUITY")
            last_pdt_emitted_pts = None  # force re-anchor after disc

        # Emit PDT at first segment AND after each discontinuity.
        if i == 0 or last_pdt_emitted_pts is None:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pts_to_pdt(seg.pts_start)}")
            last_pdt_emitted_pts = seg.pts_start

        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(seg.name)

    return "\n".join(lines) + "\n"


import os
from pathlib import Path


def atomic_write_text(path: Path | str, content: str) -> None:
    """Write `content` to `path` atomically: write .part, fsync, rename.

    POSIX rename is atomic, so a concurrent reader of `path` either
    sees the old contents or the new contents — never a partial write.
    """
    path = Path(path)
    part = path.with_suffix(path.suffix + ".part")
    with open(part, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


def atomic_write_bytes(path: Path | str, content: bytes) -> None:
    """Same as atomic_write_text but bytes."""
    path = Path(path)
    part = path.with_suffix(path.suffix + ".part")
    with open(part, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


import av
import logging
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class HlsSegmenter:
    """Reads RTSP packets via PyAV, writes HLS segments + manifest + sidecar.

    Architecture (see spec §1):
      - Demux packets from `input_url` (e.g. rtsp://localhost:8554/feeder-main)
      - At every keyframe, close the current segment file and open a new one.
        Use atomic .part + os.replace() so partial files are never served.
      - After each segment closes, append to in-memory state, then rewrite
        live.m3u8 and segments.json (also atomic).
      - Prune segments older than retention_s from disk.

    PTS in/out is byte-exact (proven by tools/prototype_hls_passthrough_v2.py).
    """

    def __init__(
        self,
        camera: str,
        input_url: str,
        out_dir: Path | str,
        *,
        window_segments: int = 30,    # ~60s at 2s segments
        retention_s: float = 60.0,    # delete segments older than this
        seg_prefix: str = "seg_",
        seg_suffix: str = ".ts",
    ):
        self.camera = camera
        self.input_url = input_url
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.window_segments = window_segments
        self.retention_s = retention_s
        self.seg_prefix = seg_prefix
        self.seg_suffix = seg_suffix

        # Persistent state (would be loaded from state.json in production)
        self._seq: int = 0
        self._discontinuity_seq: int = 0

        # In-memory recent segments (sliding window).
        self._segments: list[Segment] = []
        self._discontinuity_boundaries: set[str] = set()
        self._discontinuities: list[Discontinuity] = []

        self._stop = threading.Event()

        self.stats = {
            "segments_written": 0,
            "packets_muxed": 0,
            "packets_dropped": 0,
            "discontinuities": 0,
            "manifest_updates": 0,
        }

        # Restore persisted state from prior run if any.
        self._load_state()

    def stop(self):
        self._stop.set()

    def _seg_name(self, seq: int) -> str:
        return f"{self.seg_prefix}{seq:010d}{self.seg_suffix}"

    def _open_input(self) -> av.container.InputContainer:
        options = {}
        if self.input_url.startswith("rtsp://"):
            options = {
                "rtsp_transport": "tcp",
                "fflags": "nobuffer",
                "flags": "low_delay",
                "rtsp_flags": "prefer_tcp",
                "max_delay": "200000",
            }
        return av.open(self.input_url, options=options)

    def run_until_eof(self, max_segments: Optional[int] = None) -> None:
        """Run synchronously until input EOF or stop()/max_segments reached.

        For production use, wrap with a thread (see run_forever).
        """
        in_container = self._open_input()
        in_stream = in_container.streams.video[0]
        log.info(
            "[%s] segmenter input open: %sx%s codec=%s time_base=%s",
            self.camera, in_stream.width, in_stream.height,
            in_stream.codec_context.name, in_stream.time_base,
        )

        out_container: Optional[av.container.OutputContainer] = None
        out_stream = None
        current_seg_name: Optional[str] = None
        current_seg_part: Optional[Path] = None
        current_seg_first_pts: Optional[int] = None
        current_seg_last_pts: Optional[int] = None
        prev_seg_last_pts: Optional[int] = None

        try:
            for packet in in_container.demux(in_stream):
                if self._stop.is_set():
                    break
                if packet.pts is None:
                    continue

                if packet.is_keyframe:
                    # Close prior segment if any
                    if out_container is not None:
                        out_container.close()
                        os.replace(current_seg_part, self.out_dir / current_seg_name)
                        prev_seg_last_pts = current_seg_last_pts
                        self._on_segment_closed(
                            name=current_seg_name,
                            pts_start=current_seg_first_pts * in_stream.time_base,
                            pts_end=current_seg_last_pts * in_stream.time_base,
                        )
                        if max_segments is not None and self.stats["segments_written"] >= max_segments:
                            break

                    # Detect discontinuity: keyframe's PTS is less than prior segment's last PTS
                    if (prev_seg_last_pts is not None
                            and packet.pts < prev_seg_last_pts):
                        self._discontinuity_seq += 1
                        self._discontinuities.append(Discontinuity(
                            after=current_seg_name,
                            old_pts_end=float(prev_seg_last_pts * in_stream.time_base),
                            new_pts_start=float(packet.pts * in_stream.time_base),
                        ))
                        # Mark the NEW segment as a discontinuity boundary
                        # (handled below when we set current_seg_name)
                        self.stats["discontinuities"] += 1
                        _is_disc = True
                    else:
                        _is_disc = False

                    # Open new segment
                    self._seq += 1
                    current_seg_name = self._seg_name(self._seq)
                    current_seg_part = self.out_dir / (current_seg_name + ".part")
                    out_container = av.open(str(current_seg_part), "w", format="mpegts")
                    out_stream = out_container.add_stream_from_template(in_stream)
                    current_seg_first_pts = packet.pts
                    if _is_disc:
                        self._discontinuity_boundaries.add(current_seg_name)

                if out_container is None:
                    # No keyframe seen yet — skip
                    continue

                packet.stream = out_stream
                out_container.mux(packet)
                current_seg_last_pts = packet.pts
                self.stats["packets_muxed"] += 1

        finally:
            # Close the final open segment
            if out_container is not None and current_seg_part is not None:
                try:
                    out_container.close()
                    if current_seg_part.exists():
                        os.replace(current_seg_part, self.out_dir / current_seg_name)
                        self._on_segment_closed(
                            name=current_seg_name,
                            pts_start=current_seg_first_pts * in_stream.time_base,
                            pts_end=current_seg_last_pts * in_stream.time_base,
                        )
                except Exception:
                    log.exception("[%s] close final segment failed", self.camera)
            in_container.close()

    def _on_segment_closed(self, name: str, pts_start: float, pts_end: float):
        seg = Segment(name=name, pts_start=float(pts_start), pts_end=float(pts_end))
        self._segments.append(seg)
        # Prune to window_segments
        while len(self._segments) > self.window_segments:
            dropped = self._segments.pop(0)
            # Also forget any discontinuity boundary on the dropped segment
            self._discontinuity_boundaries.discard(dropped.name)
            # Delete file from disk if past retention
            self._prune_disk_files()
        self.stats["segments_written"] += 1
        self._write_manifest_and_sidecar()
        self._save_state()

    def _save_state(self) -> None:
        state = {"seq": self._seq, "discontinuity_seq": self._discontinuity_seq}
        atomic_write_text(self.out_dir / "state.json", json.dumps(state))

    def _load_state(self) -> None:
        """Restore _seq and _discontinuity_seq. Fall back to disk scan
        if state.json is missing or corrupt (per spec N-I7)."""
        state_path = self.out_dir / "state.json"
        loaded = False
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                self._seq = int(state.get("seq", 0))
                self._discontinuity_seq = int(state.get("discontinuity_seq", 0))
                loaded = True
            except (json.JSONDecodeError, ValueError):
                log.warning("[%s] state.json corrupt; falling back to disk scan",
                            self.camera)
        if not loaded:
            # Disk scan: find max seq from segment filenames
            max_seq = 0
            import re
            pat = re.compile(rf"^{re.escape(self.seg_prefix)}(\d+){re.escape(self.seg_suffix)}$")
            for p in self.out_dir.glob(f"{self.seg_prefix}*{self.seg_suffix}"):
                m = pat.match(p.name)
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
            self._seq = max_seq
            log.info("[%s] resumed seq=%d from disk scan", self.camera, max_seq)

    def _prune_disk_files(self):
        # Delete any seg_*.ts on disk that is NOT in the current window AND
        # older than retention_s (per spec N3: decouples manifest window
        # from disk retention so future long-term-keep feature is a flag flip).
        keep_names = {s.name for s in self._segments}
        now = time.time()
        for p in self.out_dir.glob(f"{self.seg_prefix}*{self.seg_suffix}"):
            if p.name in keep_names:
                continue
            try:
                age = now - p.stat().st_mtime
                if age > self.retention_s:
                    p.unlink()
            except FileNotFoundError:
                pass

    def _write_manifest_and_sidecar(self):
        # Sidecar
        sidecar_str = serialize_sidecar(
            stream=self.camera,
            segments=self._segments,
            discontinuities=self._discontinuities[-50:],   # tail; bounded
        )
        atomic_write_text(self.out_dir / "segments.json", sidecar_str)

        # Manifest
        manifest_str = serialize_manifest(
            segments=self._segments,
            media_sequence=max(0, self._seq - len(self._segments) + 1),
            discontinuity_sequence=self._discontinuity_seq,
            target_duration=None,
            discontinuity_boundaries=self._discontinuity_boundaries,
        )
        atomic_write_text(self.out_dir / "live.m3u8", manifest_str)
        self.stats["manifest_updates"] += 1

    def run_forever(self) -> None:
        """Production entry point: run until stop() is called, recovering
        from RTSP disconnects by re-opening the container. Use in a daemon
        thread.
        """
        while not self._stop.is_set():
            try:
                self.run_until_eof()
            except Exception as e:
                log.warning("[%s] segmenter session ended: %s — reopening in 2s",
                            self.camera, e)
            if self._stop.is_set():
                break
            time.sleep(2.0)

    def start(self) -> None:
        """Start the segmenter in a daemon thread."""
        self._thread = threading.Thread(
            target=self.run_forever,
            name=f"hls-segmenter-{self.camera}",
            daemon=True,
        )
        self._thread.start()
