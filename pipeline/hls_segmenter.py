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
