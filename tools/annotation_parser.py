"""Tolerant parser for may10_demo_video.annotations.md.

David fills this in by hand from Final Cut while scrubbing the video.
The format is mostly the template we generated, but with predictable
human variation:
  - truncated timecodes: "25:25" (mm:ss) instead of "00:25:25:00"
  - inline comments: "1:44:09 (head only)"
  - multiple "first_identifiable:" lines per visit (the writer was
    refining their answer)
  - blank fields, blank species, blank counts
  - species with parenthetical sex like "american goldfinch (male)"

The parser is deliberately permissive: it logs warnings rather than
raising. The harness uses Visit objects and treats any field=None as
"don't assert on this field for this visit."
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Visit:
    id: str
    first_in_frame_s: Optional[float] = None
    first_identifiable_s: Optional[float] = None
    last_identifiable_s: Optional[float] = None
    last_in_frame_s: Optional[float] = None
    species: Optional[str] = None
    count: int = 1
    motion_pattern: Optional[str] = None
    notes: str = ""
    parser_warnings: list[str] = field(default_factory=list)


# Strip inline parens like "1:44:09 (head only)" → "1:44:09"
_INLINE_PAREN = re.compile(r"\s*\([^)]*\)\s*")


def parse_timecode(s: str, fps: int = 30) -> Optional[float]:
    """Parse a timecode string to seconds. None if blank or unparseable.

    Accepted formats (in order of preference):
        HH:MM:SS:FF   (Final Cut native)
        MM:SS:FF      (no hours)
        MM:SS         (David's truncated form; no frames either)
        H:MM:SS       (David's variant)
        SS            (rare)

    Inline parens are stripped: "1:44:09 (head only)" → "1:44:09".
    """
    if s is None:
        return None
    s = _INLINE_PAREN.sub("", s).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 4:  # HH:MM:SS:FF
        h, m, sec, f = nums
    elif len(nums) == 3:  # MM:SS:FF (assume MM:SS:FF since David uses this for >1hr OR <1hr)
        # Disambiguate: if the third number is < fps it's likely frames; otherwise it's seconds (HH:MM:SS).
        # David's notes show "25:25:11" = 25min 25sec 11 frames (sec value > fps would be invalid frames).
        # Pragmatic rule: if parts[2] < fps, treat as MM:SS:FF; else HH:MM:SS.
        if nums[2] < fps:
            h, m, sec, f = 0, nums[0], nums[1], nums[2]
        else:
            h, m, sec, f = nums[0], nums[1], nums[2], 0
    elif len(nums) == 2:  # MM:SS
        h, m, sec, f = 0, nums[0], nums[1], 0
    elif len(nums) == 1:
        h, m, sec, f = 0, 0, nums[0], 0
    else:
        return None
    return h * 3600 + m * 60 + sec + f / fps


def parse_annotations(text: str, fps: int = 30) -> list[Visit]:
    """Parse the annotations markdown into a list of Visit objects."""
    visits: list[Visit] = []
    current: Optional[Visit] = None
    saw_first_id_already = False

    for line in text.splitlines():
        line = line.rstrip()
        # Visit header
        m = re.match(r"###\s+Visit\s+(\S+)", line)
        if m:
            if current is not None:
                _maybe_keep(visits, current)
            current = Visit(id=m.group(1))
            saw_first_id_already = False
            continue
        if current is None:
            continue
        # Field line: "- name: value"
        m = re.match(r"-\s+(\w+)\s*:\s*(.*)", line)
        if not m:
            continue
        name = m.group(1).lower()
        value = m.group(2).strip()
        if name == "first_in_frame":
            current.first_in_frame_s = parse_timecode(value, fps)
        elif name == "first_identifiable":
            t = parse_timecode(value, fps)
            if saw_first_id_already:
                current.parser_warnings.append(
                    f"duplicate first_identifiable line ignored: {value!r}"
                )
            else:
                current.first_identifiable_s = t
                saw_first_id_already = True
        elif name == "last_identifiable":
            current.last_identifiable_s = parse_timecode(value, fps)
        elif name == "last_in_frame":
            current.last_in_frame_s = parse_timecode(value, fps)
        elif name == "species":
            current.species = value.lower() if value else None
        elif name == "count":
            try:
                current.count = int(value.strip().split()[0]) if value else 1
            except (ValueError, IndexError):
                current.count = 1
                current.parser_warnings.append(f"unparseable count: {value!r}")
        elif name == "motion_pattern":
            current.motion_pattern = value.lower() if value else None
        elif name == "notes":
            current.notes = value

    if current is not None:
        _maybe_keep(visits, current)
    return visits


def _maybe_keep(visits: list[Visit], v: Visit):
    """Skip visits where ALL four windows are None and species is blank."""
    has_any_time = any([
        v.first_in_frame_s is not None,
        v.first_identifiable_s is not None,
        v.last_identifiable_s is not None,
        v.last_in_frame_s is not None,
    ])
    if has_any_time:
        visits.append(v)


def load_annotations_file(path: Path | str, fps: int = 30) -> list[Visit]:
    return parse_annotations(Path(path).read_text(encoding="utf-8"), fps=fps)
