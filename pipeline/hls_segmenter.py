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
