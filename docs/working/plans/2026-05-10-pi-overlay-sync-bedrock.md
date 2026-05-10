# Pi Overlay Sync Bedrock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Pi-side frame-accurate label overlay synced to live video, per the bedrock spec.

**Architecture:** A new PyAV-based HLS segmenter taps the existing camera RTSP and writes passthrough-mux `.ts` segments + sidecar PTS index + HLS manifest with PDT encoded as `1970-epoch + pts_seconds`. The browser uses vanilla `<video>` with hls.js (or iOS native HLS) and a canvas overlay redrawn per video frame via `requestVideoFrameCallback`, with PTS computed from `frag.programDateTime` (hls.js path) or sidecar polling (native iOS path). Adaptive Lock smoothing ported from iMac with a symmetric Gaussian kernel (uses future events from HLS buffer). Test harness replays `may10_demo_video.mp4` via mediamtx on iMac LAN, asserts 5/5 of David's annotated visits pass on three Playwright browsers, both LAN and tunnel paths.

**Tech Stack:** Python 3.13 + PyAV 17 on the Pi (segmenter); FastAPI (already exists, no new routes); vanilla HTML/CSS/JS + hls.js ≥1.5.7 (browser); Playwright + Python pytest (harness).

**Spec:** `/Users/vives/bird-classifier-pi/docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md` (commit `b49bd27`)

**Prototypes already verified:**
- `tools/prototype_hls_passthrough.py` (commit `ac77abc`) — PTS preservation, 30 packets, 0.000 ms drift
- `tools/prototype_hls_passthrough_v2.py` (commit `6c873cc`) — multi-segment + decode-back, 4 segments, byte-exact

---

## File Structure

### New files

- `pipeline/hls_segmenter.py` — HlsSegmenter class: PyAV demux + passthrough mpegts mux + manifest/sidecar writer + pruner. ~350 lines.
- `dashboard/hls.js` — vendored hls.js ≥1.5.7, served same-origin by existing static-file pattern in `api.py`.
- `tools/sync_replay_assert.py` — Playwright-driven harness that replays the demo video via mediamtx and asserts annotations match the live overlay. ~300 lines.
- `tools/annotation_parser.py` — tolerant parser for `may10_demo_video.annotations.md` (handles David's format variations: truncated timecodes like `25:25`, comments inline in field values like `1:44:09 (head only)`, multiple `first_identifiable:` lines per visit). Importable by the harness and any other tool. ~150 lines.
- `tests/pipeline/test_hls_segmenter.py` — unit tests for segmenter components (sidecar format, PDT encoding, pruner).
- `tests/test_annotation_parser.py` — unit tests for the annotation parser against David's actual file with all its format variations.

### Modified files

- `bird_pipeline_v3.py:230-310` — instantiate HlsSegmenter alongside FrameCapture; add `PIPELINE_TEST_RTSP_URL` env override so the test harness can swap go2rtc for mediamtx-on-iMac.
- `dashboard/pi_dash.html` — rewrite live view: remove `<video-stream>` custom element + transport-mode JS, add vanilla `<video>` + canvas + hls.js loader + new overlay JS. Replaces lines ~720 (HTML) and ~1180-1500 (JS). The rest of the file (header, recent classifications, model picker, etc.) stays untouched.
- `dashboard/api.py:325-330` — add a `/hls.js` route serving the vendored library.

### Unchanged but verified

- `dashboard/api.py:282` — existing `/api/hls-live/{camera}/{path:path}` wildcard route. Spec verified this already serves m3u8/ts/json with right content-types. **Do not touch.**
- `pipeline/frame_capture.py`, `pipeline/snapshot_writer.py`, `pipeline/sse_events.py` — server-side single-stream + PTS clock shipped in commit `92dd6a2`. **Do not touch.**

---

## Phase A — Server-side HLS segmenter (Tasks A1–A7)

### Task A1: Annotation parser module + tests

**Files:**
- Create: `tools/annotation_parser.py`
- Test: `tests/test_annotation_parser.py`

David's annotation file has format variations the harness must tolerate: truncated timecodes (`25:25` meaning `00:25:25:00`), inline comments in field values (`1:44:09 (head only)`), multiple `first_identifiable:` lines for one visit, blank fields, blank species. Parser converts all to canonical form: `(hours, minutes, seconds, frames)` 4-tuples → seconds; species lowercased and trimmed; multi-`first_identifiable` takes the first one and notes the rest.

- [ ] **Step 1: Write the failing test fixture**

```python
# tests/test_annotation_parser.py
import pytest
from tools.annotation_parser import parse_timecode, parse_annotations, Visit

def test_parse_timecode_full():
    assert parse_timecode("01:23:45:15", fps=30) == pytest.approx(1*3600 + 23*60 + 45 + 15/30)

def test_parse_timecode_truncated_mm_ss():
    # David's "25:25" means 25 min 25 sec
    assert parse_timecode("25:25", fps=30) == pytest.approx(25*60 + 25)

def test_parse_timecode_truncated_mm_ss_ff():
    # David's "25:11" with frames could be ambiguous - we treat as MM:SS
    assert parse_timecode("25:25:11", fps=30) == pytest.approx(25*60 + 25 + 11/30)

def test_parse_timecode_with_inline_comment():
    # David: "1:44:09 (head only)" -> strip comment
    assert parse_timecode("1:44:09 (head only)", fps=30) == pytest.approx(1*60 + 44 + 9/30)

def test_parse_timecode_blank():
    assert parse_timecode("", fps=30) is None
    assert parse_timecode("   ", fps=30) is None

def test_parse_annotations_minimal():
    md = """
### Visit 01
- first_in_frame: 00:00:01
- first_identifiable: 00:00:17
- last_identifiable: 02:07:21
- last_in_frame: 02:07:27
- species: House Finch
- count: 1
- motion_pattern: perched
- notes: long visit
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    v = visits[0]
    assert v.id == "01"
    assert v.first_in_frame_s == pytest.approx(1/30)
    assert v.species == "house finch"
    assert v.motion_pattern == "perched"

def test_parse_annotations_blank_identifiable():
    md = """
### Visit 03
- first_in_frame: 00:22:01
- first_identifiable:
- last_identifiable:
- last_in_frame: 00:25:11
- species: american goldfinch (male)
- motion_pattern: partial
- notes: never fully visible
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    assert visits[0].first_identifiable_s is None
    assert visits[0].last_identifiable_s is None
    # Species with parenthetical → keep as-is (lowercased)
    assert visits[0].species == "american goldfinch (male)"

def test_parse_annotations_multiple_first_identifiable():
    # David's Visit 06 has TWO first_identifiable lines
    md = """
### Visit 06
- first_in_frame: 1:43:16
- first_identifiable: 1:44:09 (head only)
- first_identifiable: 1:50:06 (full but still partial)
- last_identifiable: 2:04:27
- last_in_frame: 2:04:29
- species: american goldfinch (female)
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 1
    # First wins; second goes into a parser-warning list
    assert visits[0].first_identifiable_s == pytest.approx(1*60 + 44 + 9/30)
    assert "duplicate first_identifiable" in visits[0].parser_warnings[0]

def test_parse_annotations_all_empty_block_skipped():
    md = """
### Visit 99
- first_in_frame:
- first_identifiable:
- last_identifiable:
- last_in_frame:
- species:
"""
    visits = parse_annotations(md, fps=30)
    assert len(visits) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/vives/bird-classifier-pi
python3 -m pytest tests/test_annotation_parser.py -v
```

Expected: ImportError / module not found.

- [ ] **Step 3: Write the parser**

```python
# tools/annotation_parser.py
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
```

- [ ] **Step 4: Run tests, see them pass**

```bash
python3 -m pytest tests/test_annotation_parser.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Sanity check against David's actual file**

```bash
python3 -c "
from tools.annotation_parser import load_annotations_file
visits = load_annotations_file('/Users/vives/docs/bird-observatory/training videos/may10_demo_video.annotations.md')
for v in visits:
    print(f'{v.id}: {v.species} ({v.first_in_frame_s}s - {v.last_in_frame_s}s)')
    for w in v.parser_warnings:
        print(f'  WARN: {w}')
"
```

Expected: 7+ visits printed, no exceptions. Some parser warnings on Visit 06 (duplicate first_identifiable) are expected.

- [ ] **Step 6: Commit**

```bash
cd /Users/vives/bird-classifier-pi
git add tools/annotation_parser.py tests/test_annotation_parser.py
git commit -m "feat: tolerant annotation parser for test fixture"
```

---

### Task A2: PDT encoder + sidecar serializer (pure functions)

**Files:**
- Create: `pipeline/hls_segmenter.py` (start with pure helpers)
- Test: `tests/pipeline/test_hls_segmenter.py`

- [ ] **Step 1: Write tests for PDT encoder + sidecar serializer**

```python
# tests/pipeline/test_hls_segmenter.py
import json
import pytest
from pipeline.hls_segmenter import (
    pts_to_pdt, pdt_to_pts, serialize_sidecar, Segment
)


def test_pts_to_pdt_zero():
    assert pts_to_pdt(0.0) == "1970-01-01T00:00:00.000Z"

def test_pts_to_pdt_basic():
    # 873.002 s → 14 min, 33 s, 2 ms
    assert pts_to_pdt(873.002) == "1970-01-01T00:14:33.002Z"

def test_pts_to_pdt_subsecond_precision_ms():
    # Spec accepts ms precision; sub-ms gets rounded
    assert pts_to_pdt(0.0005) == "1970-01-01T00:00:00.000Z"  # rounds down to 0ms
    assert pts_to_pdt(0.0015) == "1970-01-01T00:00:00.001Z"  # rounds to 1ms

def test_pdt_to_pts_round_trip():
    for pts in [0.0, 1.5, 873.002, 95443.999]:  # under 26.5h wrap
        encoded = pts_to_pdt(pts)
        decoded = pdt_to_pts(encoded)
        # Should round-trip to within 1ms (the encoding precision)
        assert abs(decoded - pts) < 0.001

def test_serialize_sidecar_format():
    segs = [
        Segment(name="seg_0000000123.ts", pts_start=1230.0, pts_end=1232.0),
        Segment(name="seg_0000000124.ts", pts_start=1232.0, pts_end=1234.05),
    ]
    out = json.loads(serialize_sidecar("feeder", segs, discontinuities=[]))
    assert out["stream"] == "feeder"
    assert out["time_base_seconds"] == 1.0
    assert len(out["segments"]) == 2
    assert out["segments"][0]["name"] == "seg_0000000123.ts"
    assert out["segments"][0]["pts_start"] == 1230.0
    assert out["segments"][0]["pts_end"] == 1232.0
    assert out["segments"][0]["duration"] == pytest.approx(2.0)
    assert out["discontinuities"] == []
```

- [ ] **Step 2: Run test, see it fail**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v
```

Expected: ImportError.

- [ ] **Step 3: Write the helpers**

```python
# pipeline/hls_segmenter.py (initial — pure helpers only)
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
```

- [ ] **Step 4: Run tests, see all 5 pass**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/hls_segmenter.py tests/pipeline/test_hls_segmenter.py
git commit -m "feat: PDT encoder + sidecar serializer for HLS segmenter"
```

---

### Task A3: Manifest writer

**Files:**
- Modify: `pipeline/hls_segmenter.py`
- Modify: `tests/pipeline/test_hls_segmenter.py`

- [ ] **Step 1: Add tests for manifest serializer**

Append to `tests/pipeline/test_hls_segmenter.py`:

```python
from pipeline.hls_segmenter import serialize_manifest


def test_manifest_basic_live():
    segs = [
        Segment(name="seg_0000000123.ts", pts_start=1230.0, pts_end=1232.0),
        Segment(name="seg_0000000124.ts", pts_start=1232.0, pts_end=1234.0),
    ]
    out = serialize_manifest(
        segments=segs,
        media_sequence=123,
        discontinuity_sequence=0,
        target_duration=2,
        discontinuity_boundaries=set(),  # no discontinuities
    )
    lines = out.strip().splitlines()
    assert lines[0] == "#EXTM3U"
    assert "#EXT-X-VERSION:6" in lines
    assert "#EXT-X-TARGETDURATION:2" in lines
    assert "#EXT-X-MEDIA-SEQUENCE:123" in lines
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in lines
    assert "#EXT-X-INDEPENDENT-SEGMENTS" in lines
    # No ENDLIST (live)
    assert "#EXT-X-ENDLIST" not in lines
    # First segment's PDT should be 1970-epoch encoding of pts_start=1230
    assert "#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:20:30.000Z" in lines
    # EXTINF + segment URI lines
    assert "#EXTINF:2.000," in lines
    assert "seg_0000000123.ts" in lines
    assert "seg_0000000124.ts" in lines


def test_manifest_with_discontinuity():
    segs = [
        Segment(name="seg_0000000010.ts", pts_start=20.0, pts_end=22.0),
        Segment(name="seg_0000000011.ts", pts_start=0.0, pts_end=2.0),  # camera reset
    ]
    out = serialize_manifest(
        segments=segs,
        media_sequence=10,
        discontinuity_sequence=1,
        target_duration=2,
        discontinuity_boundaries={"seg_0000000011.ts"},  # boundary BEFORE this segment
    )
    lines = out.strip().splitlines()
    # DISCONTINUITY-SEQUENCE incremented
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in lines
    # DISCONTINUITY tag appears between the two segments
    idx1 = lines.index("seg_0000000010.ts")
    idx2 = lines.index("seg_0000000011.ts")
    disc_idx = lines.index("#EXT-X-DISCONTINUITY")
    assert idx1 < disc_idx < idx2
    # New PDT after DISCONTINUITY anchors at pts_start=0 → 1970-01-01T00:00:00
    pdt_for_new = [l for l in lines[disc_idx:idx2] if l.startswith("#EXT-X-PROGRAM-DATE-TIME")]
    assert pdt_for_new == ["#EXT-X-PROGRAM-DATE-TIME:1970-01-01T00:00:00.000Z"]


def test_manifest_target_duration_at_least_max():
    segs = [
        Segment(name="seg_0.ts", pts_start=0.0, pts_end=2.5),  # 2.5s
        Segment(name="seg_1.ts", pts_start=2.5, pts_end=4.0),  # 1.5s
    ]
    out = serialize_manifest(
        segments=segs, media_sequence=0, discontinuity_sequence=0,
        target_duration=None, discontinuity_boundaries=set(),
    )
    # auto-compute target_duration = ceil(max segment duration) = 3
    assert "#EXT-X-TARGETDURATION:3" in out
```

- [ ] **Step 2: Run tests, see them fail**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v
```

Expected: 3 new tests fail (NameError on serialize_manifest).

- [ ] **Step 3: Add serialize_manifest**

Append to `pipeline/hls_segmenter.py`:

```python
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
```

- [ ] **Step 4: Run tests, all pass**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v
```

Expected: 8 PASS (5 existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add pipeline/hls_segmenter.py tests/pipeline/test_hls_segmenter.py
git commit -m "feat: HLS manifest serializer with DISCONTINUITY + PDT"
```

---

### Task A4: Atomic file publication helpers + segment writer

**Files:**
- Modify: `pipeline/hls_segmenter.py`
- Modify: `tests/pipeline/test_hls_segmenter.py`

Per spec I4: writes must be atomic via `.part` + `os.replace()`. Manifest and sidecar are also written atomically.

- [ ] **Step 1: Add tests for atomic writers**

Append to `tests/pipeline/test_hls_segmenter.py`:

```python
import os
import tempfile
from pathlib import Path
from pipeline.hls_segmenter import atomic_write_text


def test_atomic_write_text_basic(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text() == "hello world"
    # No leftover .part
    assert not (tmp_path / "out.txt.part").exists()


def test_atomic_write_text_overwrites(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old content")
    atomic_write_text(target, "new content")
    assert target.read_text() == "new content"


def test_atomic_write_text_partial_never_visible(tmp_path):
    # We can't trivially test "browser would never see a partial file" in
    # a unit test, but we CAN verify the rename pattern is used: while the
    # .part exists during write, target shouldn't change.
    target = tmp_path / "out.txt"
    target.write_text("old content")
    # Manually exercise the underlying primitive
    part = target.with_suffix(target.suffix + ".part")
    part.write_text("partial...")
    assert target.read_text() == "old content"   # target unchanged
    os.replace(part, target)
    assert target.read_text() == "partial..."
    assert not part.exists()
```

- [ ] **Step 2: Run, see fail**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py::test_atomic_write_text_basic -v
```

Expected: ImportError.

- [ ] **Step 3: Add atomic_write_text + segment writer scaffolding**

Append to `pipeline/hls_segmenter.py`:

```python
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
```

- [ ] **Step 4: Tests pass**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v
```

Expected: all PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add pipeline/hls_segmenter.py tests/pipeline/test_hls_segmenter.py
git commit -m "feat: atomic file publication helpers (.part + os.replace)"
```

---

### Task A5: HlsSegmenter core class (without RTSP — uses prerecorded packets)

**Files:**
- Modify: `pipeline/hls_segmenter.py`
- Modify: `tests/pipeline/test_hls_segmenter.py`

The segmenter has two concerns: (a) muxing packets to .ts files at keyframe boundaries, (b) coordinating with manifest/sidecar/pruner. Test (a) with a fake packet stream that yields PyAV packets from a local file (so tests don't need RTSP).

- [ ] **Step 1: Write integration test using a local file**

Append to `tests/pipeline/test_hls_segmenter.py`:

```python
import av as _av
import os as _os
import shutil
import pytest
from pipeline.hls_segmenter import HlsSegmenter


@pytest.fixture
def sample_h264_file(tmp_path):
    """Generate a short test H.264 file with known keyframe pattern."""
    out_path = tmp_path / "sample.ts"
    container = _av.open(str(out_path), "w", format="mpegts")
    stream = container.add_stream("h264", rate=30)
    stream.width = 320
    stream.height = 240
    stream.pix_fmt = "yuv420p"
    # Force keyframe every 30 frames (1 second)
    stream.codec_context.gop_size = 30
    import numpy as np
    for i in range(120):  # 4 seconds → 4 keyframes (incl. first frame)
        arr = np.full((240, 320, 3), i % 256, dtype=np.uint8)
        frame = _av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return out_path


def test_segmenter_writes_keyframe_segments(tmp_path, sample_h264_file):
    out_dir = tmp_path / "hls"
    out_dir.mkdir()
    seg = HlsSegmenter(
        camera="test",
        input_url=str(sample_h264_file),
        out_dir=out_dir,
        window_segments=10,
        retention_s=60,
    )
    seg.run_until_eof(max_segments=4)   # bounded so test terminates

    # Should have at least 3 .ts files (3+ keyframe boundaries crossed)
    ts_files = sorted(out_dir.glob("seg_*.ts"))
    assert len(ts_files) >= 3
    # No .part leftovers
    assert not list(out_dir.glob("*.part"))
    # Manifest + sidecar exist
    assert (out_dir / "live.m3u8").exists()
    assert (out_dir / "segments.json").exists()

    # Sidecar content matches segments on disk
    sidecar = json.loads((out_dir / "segments.json").read_text())
    assert sidecar["stream"] == "test"
    assert len(sidecar["segments"]) >= 3
    for entry in sidecar["segments"]:
        assert (out_dir / entry["name"]).exists()
        # PTS values are monotonically increasing within a run
        assert entry["pts_end"] > entry["pts_start"]
```

- [ ] **Step 2: Run, see fail**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py::test_segmenter_writes_keyframe_segments -v
```

Expected: ImportError / class missing.

- [ ] **Step 3: Write the HlsSegmenter class**

Append to `pipeline/hls_segmenter.py`:

```python
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
```

- [ ] **Step 4: Tests pass**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v
```

Expected: 12 PASS (11 + 1 integration). The integration test may take ~3 seconds because PyAV encodes 4 seconds of synthetic video.

- [ ] **Step 5: Commit**

```bash
git add pipeline/hls_segmenter.py tests/pipeline/test_hls_segmenter.py
git commit -m "feat: HlsSegmenter class — passthrough mux + manifest/sidecar"
```

---

### Task A6: state.json persistence + disk-scan recovery

**Files:**
- Modify: `pipeline/hls_segmenter.py`
- Modify: `tests/pipeline/test_hls_segmenter.py`

Per spec N-I7: on segmenter restart, restore `_seq` and `_discontinuity_seq` from `state.json`. If state.json is missing/corrupt, fall back to scanning disk for max segment number.

- [ ] **Step 1: Add tests for state persistence**

Append to `tests/pipeline/test_hls_segmenter.py`:

```python
def test_state_save_load_roundtrip(tmp_path):
    out_dir = tmp_path / "hls"
    out_dir.mkdir()
    seg = HlsSegmenter(camera="test", input_url="x", out_dir=out_dir)
    seg._seq = 42
    seg._discontinuity_seq = 3
    seg._save_state()

    seg2 = HlsSegmenter(camera="test", input_url="x", out_dir=out_dir)
    seg2._load_state()
    assert seg2._seq == 42
    assert seg2._discontinuity_seq == 3


def test_state_recovery_via_disk_scan(tmp_path):
    out_dir = tmp_path / "hls"
    out_dir.mkdir()
    # Simulate orphaned segments from a prior run, no state.json
    (out_dir / "seg_0000000007.ts").write_bytes(b"fake")
    (out_dir / "seg_0000000010.ts").write_bytes(b"fake")
    seg = HlsSegmenter(camera="test", input_url="x", out_dir=out_dir)
    seg._load_state()  # state.json missing
    # Should resume from max seq + 1
    assert seg._seq == 10
    # Discontinuity seq stays at 0 (no info to recover)
    assert seg._discontinuity_seq == 0


def test_state_recovery_via_disk_scan_corrupt_json(tmp_path):
    out_dir = tmp_path / "hls"
    out_dir.mkdir()
    (out_dir / "state.json").write_text("not valid json{{{")
    (out_dir / "seg_0000000005.ts").write_bytes(b"fake")
    seg = HlsSegmenter(camera="test", input_url="x", out_dir=out_dir)
    seg._load_state()  # Should not raise
    assert seg._seq == 5
```

- [ ] **Step 2: Run, see fail**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v -k state
```

Expected: 3 new tests fail.

- [ ] **Step 3: Add load/save methods**

Add to `HlsSegmenter` in `pipeline/hls_segmenter.py`:

```python
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
```

Also: call `_load_state()` at end of `__init__` and `_save_state()` in `_on_segment_closed`:

```python
    def __init__(self, ...):
        # ... existing init code ...
        self._load_state()

    def _on_segment_closed(self, ...):
        # ... existing code ...
        self._write_manifest_and_sidecar()
        self._save_state()  # new
```

- [ ] **Step 4: Tests pass**

```bash
python3 -m pytest tests/pipeline/test_hls_segmenter.py -v
```

Expected: 15 PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/hls_segmenter.py tests/pipeline/test_hls_segmenter.py
git commit -m "feat: HlsSegmenter state persistence + disk-scan recovery"
```

---

### Task A7: Thread wrapper + integrate into bird_pipeline_v3.py

**Files:**
- Modify: `pipeline/hls_segmenter.py`
- Modify: `bird_pipeline_v3.py`

- [ ] **Step 1: Add run_forever() that re-opens on disconnect**

Add to `HlsSegmenter`:

```python
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
```

- [ ] **Step 2: Add PIPELINE_TEST_RTSP_URL env hook to bird_pipeline_v3.py**

Open `bird_pipeline_v3.py`. Find the CAMERAS_DETECT dict around line 30:

```python
CAMERAS_DETECT = {
    CAMERA_FEEDER: "rtsp://127.0.0.1:8554/feeder-sub",
    # ...
}
CAMERAS_MAIN = {
    CAMERA_FEEDER: "rtsp://127.0.0.1:8554/feeder-main",
}
```

Below those constants, add:

```python
# Test override: when set, the pipeline reads from this URL instead of go2rtc.
# Used by tools/sync_replay_assert.py to point at mediamtx-on-iMac (per spec §4).
_test_url = os.environ.get("PIPELINE_TEST_RTSP_URL")
if _test_url:
    log_msg = f"[PIPELINE_TEST_RTSP_URL] overriding camera URLs → {_test_url}"
    # mutate both dicts; harness expects feeder-main behaviour from the test stream
    for k in list(CAMERAS_DETECT.keys()):
        CAMERAS_DETECT[k] = _test_url
    for k in list(CAMERAS_MAIN.keys()):
        CAMERAS_MAIN[k] = _test_url
else:
    log_msg = None
# defer logging until main() so we don't double-log on import
```

In `main()`, after the existing pipeline setup, add the segmenter startup. Find where `snapshot_writer.start()` is called (around line 179) and after the per-camera stack creation (around line 311). Add a new block right after `recorder = None if PI_MODE else HlsRecorder(...)`:

```python
            # HLS segmenter — single-stream PTS-aware segmenter writing to
            # ~/bird-snapshots/hls/feeder/, served by existing
            # /api/hls-live/{camera}/{path:path} route. Spec:
            # docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md
            from pipeline.hls_segmenter import HlsSegmenter
            seg_dir = HLS_DIR / name
            hls_segmenter = HlsSegmenter(
                camera=name,
                input_url=main_url,
                out_dir=seg_dir,
                window_segments=30,
                retention_s=60.0,
            )
            hls_segmenter.start()
            log.info("[%s] HlsSegmenter started → %s", name, seg_dir)
```

Also early in main() log the test URL override:

```python
    if log_msg:
        log.info(log_msg)
```

(Just below the `log.info("Starting bird_pipeline_v3...")` line.)

- [ ] **Step 3: Deploy + restart pipeline + verify segments appear**

```bash
cd /Users/vives/bird-classifier-pi
rsync -avz pipeline/hls_segmenter.py vives@pi5.local:/home/vives/bird-classifier/pipeline/
rsync -avz bird_pipeline_v3.py vives@pi5.local:/home/vives/bird-classifier/
ssh vives@pi5.local "systemctl --user restart bird-pipeline.service && sleep 8 && ls -la ~/bird-snapshots/hls/feeder/ | head -15"
```

Expected: `live.m3u8`, `segments.json`, `seg_*.ts` files appearing within 5-10 seconds. No `.part` files visible.

- [ ] **Step 4: Verify manifest parses + segments decode**

```bash
ssh vives@pi5.local "cat ~/bird-snapshots/hls/feeder/live.m3u8 | head -20"
ssh vives@pi5.local "cat ~/bird-snapshots/hls/feeder/segments.json | python3 -m json.tool | head -30"
ssh vives@pi5.local "ffprobe -v error ~/bird-snapshots/hls/feeder/\$(ls -t ~/bird-snapshots/hls/feeder/seg_*.ts | head -1 | xargs basename) 2>&1 | head"
```

Expected:
- manifest has `#EXTM3U`, `#EXT-X-PROGRAM-DATE-TIME:1970-01-01T...`, EXTINF lines, segment URIs
- sidecar has `pts_start`/`pts_end` per segment
- ffprobe decodes the .ts file without errors

- [ ] **Step 5: Commit**

```bash
git add pipeline/hls_segmenter.py bird_pipeline_v3.py
git commit -m "feat: integrate HlsSegmenter into pipeline + PIPELINE_TEST_RTSP_URL env"
```

---

## Phase B — Browser-side rewrite (Tasks B1–B6)

### Task B1: Vendor hls.js + serve route

**Files:**
- Create: `dashboard/hls.js`
- Modify: `dashboard/api.py:325-330`

- [ ] **Step 1: Download pinned hls.js**

```bash
cd /Users/vives/bird-classifier-pi
curl -sL https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js -o dashboard/hls.js
wc -c dashboard/hls.js   # should be ~330KB
head -1 dashboard/hls.js  # should start with /*! HLS.js v1.5.7 ... */
```

- [ ] **Step 2: Add a route to serve it**

In `dashboard/api.py`, find the existing `/video-rtc.js` route (line ~327). Add immediately after:

```python
@app.get("/hls.js")
def serve_hlsjs():
    """Serve vendored hls.js (≥1.5.7) for same-origin loading.

    Pinned version per spec: avoids CDN dependency and ensures we test
    against a known build.
    """
    return FileResponse(
        str(DASHBOARD_DIR / "hls.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )
```

- [ ] **Step 3: Deploy + verify**

```bash
rsync -avz dashboard/hls.js dashboard/api.py vives@pi5.local:/home/vives/bird-classifier/dashboard/
ssh vives@pi5.local "systemctl --user restart bird-dashboard.service && sleep 2 && curl -sI http://localhost:8099/hls.js | head -5"
```

Expected: 200 OK, Content-Type: application/javascript.

- [ ] **Step 4: Commit**

```bash
git add dashboard/hls.js dashboard/api.py
git commit -m "feat: vendor hls.js 1.5.7 + serve route"
```

---

### Task B2: Replace pi_dash.html live view HTML

**Files:**
- Modify: `dashboard/pi_dash.html` (HTML structure only — JS comes in B3-B6)

The live stage currently has `<video-stream id="live-video">` plus `<canvas>`. Replace with `<video>` + `<canvas>` + diagnostic chip. Remove the BirdVideoRTC subclass from `<head>`. Remove the existing setupLiveView function from the `<script>` block (we rewrite in B3).

- [ ] **Step 1: Replace the BirdVideoRTC subclass in `<head>`**

Find the `<script type="module">` block that imports VideoRTC (added in earlier session). Replace with:

```html
<!-- Removed in May 2026: <video-stream> custom element. Pi dashboard now
     uses vanilla <video> with hls.js (or iOS native HLS). See
     docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md
     for the rationale (single transport, single PTS clock, PWA-friendly). -->
```

(Yes, the entire `<script type="module">` block with VideoRTC import is just deleted.)

- [ ] **Step 2: Replace the live stage HTML**

Find:

```html
<div class="live-stage" id="live-stage">
  ...
  <video-rtc id="live-video" class="live-video"></video-rtc>
  <canvas class="live-overlay" id="live-overlay"></canvas>
  <div class="sync-diag" id="sync-diag"></div>
</div>
```

Replace with:

```html
<div class="live-stage" id="live-stage">
  <!-- Vanilla <video> with iOS-friendly attributes. iOS Safari plays HLS
       natively via `src`; Chrome/Firefox/Edge use hls.js to MSE. -->
  <video id="live-video"
         class="live-video"
         playsinline
         webkit-playsinline
         muted
         autoplay
         preload="auto"></video>
  <canvas class="live-overlay" id="live-overlay"></canvas>
  <div class="sync-diag" id="sync-diag"></div>
  <button class="overlay-toggle" id="overlay-toggle" type="button">Labels</button>
</div>
```

- [ ] **Step 3: Add CSS for the labels-toggle button**

In the `<style>` block, near the `.live-overlay` rule, add:

```css
.overlay-toggle {
  position: absolute; top: 8px; right: 8px;
  background: rgba(0, 0, 0, 0.55);
  color: rgba(255, 255, 255, 0.85);
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 4px;
  padding: 4px 10px;
  font-family: var(--font-mono); font-size: 11px;
  cursor: pointer;
  z-index: 2;
}
.overlay-toggle.off { opacity: 0.4; text-decoration: line-through; }
.live-video { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
```

(If a `.live-video` rule already exists, REPLACE it. The previous `.live-video video { ... }` selector targets a child — no longer needed since the outer element IS the video.)

- [ ] **Step 4: Deploy + visual check**

```bash
rsync -avz dashboard/pi_dash.html vives@pi5.local:/home/vives/bird-classifier/dashboard/
ssh vives@pi5.local "systemctl --user restart bird-dashboard.service"
```

Open `http://pi5.local:8099/` in a browser. Expected: page loads without JS errors (video will be black, no source yet — that's B3's job).

- [ ] **Step 5: Commit**

```bash
git add dashboard/pi_dash.html
git commit -m "refactor: replace <video-stream> with vanilla <video> + canvas"
```

---

### Task B3: Wire up hls.js + native iOS branch in pi_dash.html JS

**Files:**
- Modify: `dashboard/pi_dash.html` (the existing setupLiveView JS block)

- [ ] **Step 1: Replace setupLiveView's player setup**

Locate the existing `function setupLiveView() { ... }` in pi_dash.html. Replace its entire body with the new implementation:

```javascript
function setupLiveView() {
  const video      = document.getElementById('live-video');
  const canvas     = document.getElementById('live-overlay');
  const ctx        = canvas.getContext('2d');
  const statusEl   = document.getElementById('live-status');
  const dotEl      = document.getElementById('live-dot');
  const diagEl     = document.getElementById('sync-diag');
  const toggleBtn  = document.getElementById('overlay-toggle');

  const HLS_URL = '/api/hls-live/feeder/live.m3u8';
  const SIDECAR_URL = '/api/hls-live/feeder/segments.json';
  const SSE_URL = '/api/pipeline/events/sse?camera=feeder';

  // ── State ────────────────────────────────────────────────────────────
  const eventBuf = [];               // {pts, tracks}, sorted by pts asc
  const EVENT_BUF_MAX = 240;
  const trackHistory = new Map();    // track_id → [{pts, cx, top}]
  let segmentsIndex = {};            // {filename: {pts_start, pts_end}}
  let hls = null;
  let currentFragPdt = null;         // Date from frag.programDateTime
  let currentFragStart = 0;          // hls.js media-timeline anchor
  let usingNativeHls = false;
  let nativeMediaTimeline = [];      // [{filename, mediaStart, duration}]
  let showLabels = (localStorage.getItem('showLabels') ?? '1') !== '0';
  let LEAD_TIME_S = 0;
  // Lead time runtime-tweakable:
  if (typeof window.__leadTimeS === 'number') LEAD_TIME_S = window.__leadTimeS;

  const diag = {
    rvfcCount: 0, sseCount: 0, drawnTracks: 0, lastTargetPts: 0,
    lastMediaT: 0, lastDrawAt: 0,
  };
  const showDiag = new URLSearchParams(window.location.search).get('syncdiag') === '1';
  if (showDiag) diagEl.classList.add('visible');

  // ── Adaptive Lock parameters (ported from iMac index.html:8231-8240) ──
  const SIGMA_WIDE_S    = 0.380;
  const SIGMA_NARROW_S  = 0.190;
  const VEL_LO_PX_S     = 20;
  const VEL_HI_PX_S     = 80;
  const VEL_LOOKBACK_S  = 0.150;
  const ALPHA_EMA_GAIN  = 0.1;
  const ANCHOR_LERP     = 0.5;
  const FADE_IN_S       = 0.30;
  const STALE_S         = 0.80;
  const FADE_OUT_S      = 0.40;
  const trackAlphaEMA   = new Map();   // track_id → alphaEMA

  // ── Player setup ─────────────────────────────────────────────────────
  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // iOS Safari, macOS Safari — native HLS. canPlayType FIRST because
    // Safari may also have MSE but native HLS is more reliable on iOS.
    usingNativeHls = true;
    video.src = HLS_URL;
  } else if (window.Hls && window.Hls.isSupported()) {
    hls = new window.Hls({
      liveSyncDuration: 8,
      liveMaxLatencyDuration: 12,
      enableWorker: true,
      lowLatencyMode: false,
    });
    hls.loadSource(HLS_URL);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.FRAG_CHANGED, (_evt, data) => {
      const frag = data.frag;
      currentFragPdt = frag.programDateTime
        ? new Date(frag.programDateTime) : null;
      currentFragStart = frag.start;
    });
  } else {
    statusEl.textContent = 'video unsupported';
    dotEl.classList.add('offline');
    return;
  }

  // (rest of setupLiveView wired in B4-B6)
}
```

- [ ] **Step 2: Load hls.js BEFORE the dashboard script runs**

In `<head>`, after the existing stylesheets but BEFORE the main `<script>` block, add:

```html
<!-- hls.js (vendored at /hls.js, served by dashboard/api.py).
     Loads as a regular script so window.Hls is available before the
     dashboard JS runs setupLiveView(). -->
<script src="/hls.js"></script>
```

- [ ] **Step 3: Deploy + check the network panel**

Open `http://pi5.local:8099/`, DevTools → Network. Expected:
- `/hls.js` returns 200 (vendored, same-origin)
- `/api/hls-live/feeder/live.m3u8` returns 200
- Several `/api/hls-live/feeder/seg_*.ts` requests, all 200
- Video plays (will be black if no overlay code yet — that's B4+ work; just verify the stream is being consumed)

- [ ] **Step 4: Commit**

```bash
git add dashboard/pi_dash.html
git commit -m "feat: hls.js + native iOS HLS branch in pi_dash setupLiveView"
```

---

### Task B4: Adaptive Lock (symmetric Gaussian) + canvas drawing

**Files:**
- Modify: `dashboard/pi_dash.html` (extend setupLiveView)

- [ ] **Step 1: Add the kernel + insertion helpers**

After the player setup block in `setupLiveView()`, add:

```javascript
  // ── Adaptive Lock — symmetric Gaussian kernel ─────────────────────────
  // Ported from iMac dashboard/index.html:8355 with the symmetric upgrade
  // (events with d > 0 are valid because HLS buffer means current frame
  // has future events available).

  function bisectInsertEvent(arr, evt) {
    // events sorted by pts asc. Linear walk from tail is fine for ~60 entries.
    let i = arr.length;
    while (i > 0 && arr[i - 1].pts > evt.pts) i--;
    arr.splice(i, 0, evt);
  }

  function gaussianAt(events, T_pts, sigma_s) {
    if (events.length === 0) return null;
    const sigma2 = sigma_s * sigma_s;
    const halfWindow = sigma_s * 3.2;
    // Binary search for insertion index of T_pts
    let lo = 0, hi = events.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (events[mid].pts < T_pts) lo = mid + 1; else hi = mid;
    }
    const center = lo;
    let sx = 0, sy = 0, sw = 0;
    // Past
    for (let i = center - 1; i >= 0; i--) {
      const d = events[i].pts - T_pts;
      if (d < -halfWindow) break;
      const w = Math.exp(-(d * d) / (2 * sigma2));
      sx += events[i].cx * w; sy += events[i].top * w; sw += w;
    }
    // Future
    for (let i = center; i < events.length; i++) {
      const d = events[i].pts - T_pts;
      if (d > halfWindow) break;
      const w = Math.exp(-(d * d) / (2 * sigma2));
      sx += events[i].cx * w; sy += events[i].top * w; sw += w;
    }
    if (sw === 0) return null;
    return { cx: sx / sw, top: sy / sw };
  }

  function adaptiveAnchorAt(trackId, events, T_pts) {
    if (!events.length) return null;
    const narrow = gaussianAt(events, T_pts, SIGMA_NARROW_S);
    if (!narrow) {
      const last = events[events.length - 1];
      return { cx: last.cx, top: last.top };
    }
    let wide = gaussianAt(events, T_pts, SIGMA_WIDE_S);
    if (!wide) wide = narrow;
    // Velocity on the narrow kernel
    const past = gaussianAt(events, T_pts - VEL_LOOKBACK_S, SIGMA_NARROW_S);
    let vel = 0;
    if (past) {
      const dx = narrow.cx - past.cx, dy = narrow.top - past.top;
      vel = Math.sqrt(dx*dx + dy*dy) / VEL_LOOKBACK_S;
    }
    let alphaRaw = (vel - VEL_LO_PX_S) / (VEL_HI_PX_S - VEL_LO_PX_S);
    alphaRaw = Math.max(0, Math.min(1, alphaRaw));
    let alphaEMA = trackAlphaEMA.get(trackId);
    if (alphaEMA == null) alphaEMA = alphaRaw;
    else alphaEMA += (alphaRaw - alphaEMA) * ALPHA_EMA_GAIN;
    trackAlphaEMA.set(trackId, alphaEMA);
    return {
      cx:  (1 - alphaEMA) * wide.cx  + alphaEMA * narrow.cx,
      top: (1 - alphaEMA) * wide.top + alphaEMA * narrow.top,
    };
  }
```

- [ ] **Step 2: Add canvas drawing helpers (port from iMac)**

After the kernel code:

```javascript
  // ── Canvas drawing ────────────────────────────────────────────────────
  function setCanvasSize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(rect.width * dpr));
    const h = Math.max(1, Math.floor(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w; canvas.height = h;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: rect.width, h: rect.height };
  }

  function videoRect(canvasW, canvasH, fw, fh) {
    const stageAR = canvasW / canvasH;
    const videoAR = fw / fh;
    let renderW, renderH, offX = 0, offY = 0;
    if (videoAR > stageAR) {
      renderW = canvasW; renderH = canvasW / videoAR;
      offY = (canvasH - renderH) / 2;
    } else {
      renderH = canvasH; renderW = canvasH * videoAR;
      offX = (canvasW - renderW) / 2;
    }
    return { sx: renderW / fw, sy: renderH / fh, offX, offY };
  }

  function drawBBox(x, y, w, h, locked, opacity) {
    ctx.save();
    ctx.globalAlpha = opacity;
    ctx.strokeStyle = locked
      ? 'rgba(74, 222, 128, 0.95)'
      : 'rgba(255, 255, 255, 0.85)';
    ctx.lineWidth = 1.6;
    ctx.shadowColor = locked
      ? 'rgba(74, 222, 128, 0.55)' : 'rgba(0, 0, 0, 0.6)';
    ctx.shadowBlur = 12;
    ctx.strokeRect(x, y, w, h);
    ctx.restore();
  }

  function drawLabel(cx, topY, text, locked, opacity) {
    ctx.save();
    ctx.globalAlpha = opacity;
    ctx.font = '600 12px ui-monospace, SFMono-Regular, Menlo, monospace';
    const padX = 8;
    const tw = ctx.measureText(text).width;
    const w = tw + 2 * padX;
    const h = 22;
    const rx = cx - w / 2;
    const ry = topY - h - 4;
    const r = 4;

    ctx.shadowColor = 'rgba(0, 0, 0, 0.55)';
    ctx.shadowBlur = 8;
    ctx.shadowOffsetY = 2;
    ctx.fillStyle = locked ? '#4ade80' : 'rgba(255, 255, 255, 0.18)';
    ctx.beginPath();
    ctx.moveTo(rx + r, ry);
    ctx.lineTo(rx + w - r, ry);
    ctx.quadraticCurveTo(rx + w, ry, rx + w, ry + r);
    ctx.lineTo(rx + w, ry + h - r);
    ctx.quadraticCurveTo(rx + w, ry + h, rx + w - r, ry + h);
    ctx.lineTo(rx + r, ry + h);
    ctx.quadraticCurveTo(rx, ry + h, rx, ry + h - r);
    ctx.lineTo(rx, ry + r);
    ctx.quadraticCurveTo(rx, ry, rx + r, ry);
    ctx.closePath();
    ctx.fill();

    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;
    ctx.fillStyle = locked ? '#0a0a0a' : '#fff';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(text, cx, ry + h / 2);
    ctx.restore();
  }
```

- [ ] **Step 3: Commit (visual rendering wired in B5)**

```bash
git add dashboard/pi_dash.html
git commit -m "feat: Adaptive Lock symmetric kernel + canvas drawing helpers"
```

---

### Task B5: Frame PTS computation (both paths) + render loop

**Files:**
- Modify: `dashboard/pi_dash.html`

- [ ] **Step 1: Add sidecar polling for native iOS path**

After the canvas helpers:

```javascript
  // ── Sidecar polling (native iOS path) ─────────────────────────────────
  let sidecarFetchInFlight = false;
  async function refreshSidecar() {
    if (sidecarFetchInFlight) return;
    sidecarFetchInFlight = true;
    try {
      const r = await fetch(SIDECAR_URL, { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      const next = {};
      for (const s of (data.segments || [])) {
        next[s.name] = s;
      }
      segmentsIndex = next;
      if (usingNativeHls) rebuildNativeMediaTimeline();
    } catch (e) {
      // tolerate transient errors
    } finally {
      sidecarFetchInFlight = false;
    }
  }
  setInterval(refreshSidecar, 2000);
  refreshSidecar();

  function rebuildNativeMediaTimeline() {
    // Native iOS plays the manifest's fragments in order; cumulative
    // durations form the media timeline. We don't have hls.js's
    // FRAG_CHANGED event so we infer via cumulative durations.
    // segments.json is ordered oldest-first.
    const ordered = Object.values(segmentsIndex).sort(
      (a, b) => a.pts_start - b.pts_start
    );
    let mediaStart = 0;
    nativeMediaTimeline = ordered.map(s => {
      const entry = { filename: s.name, mediaStart, duration: s.duration,
                      pts_start: s.pts_start };
      mediaStart += s.duration;
      return entry;
    });
  }

  // ── Frame PTS computation (universal) ────────────────────────────────
  function computeFramePts(meta) {
    if (hls && currentFragPdt) {
      // hls.js path
      const pts_at_frag_start_s = currentFragPdt.getTime() / 1000;
      const t = (typeof meta?.mediaTime === 'number')
        ? meta.mediaTime : video.currentTime;
      const offset_in_frag = t - currentFragStart;
      return pts_at_frag_start_s + offset_in_frag;
    }
    if (usingNativeHls && nativeMediaTimeline.length) {
      // Native iOS path
      const t = (typeof meta?.mediaTime === 'number')
        ? meta.mediaTime : video.currentTime;
      for (const frag of nativeMediaTimeline) {
        if (t >= frag.mediaStart && t < frag.mediaStart + frag.duration) {
          return frag.pts_start + (t - frag.mediaStart);
        }
      }
    }
    return null;
  }
```

- [ ] **Step 2: Add the render loop using rVFC**

```javascript
  // ── Per-frame render ──────────────────────────────────────────────────
  function pruneTrackHistory(framePts) {
    // Drop events older than 3s relative to current frame
    const cutoff = framePts - 3.0;
    for (const [tid, events] of trackHistory.entries()) {
      while (events.length && events[0].pts < cutoff) events.shift();
      if (events.length === 0) {
        trackHistory.delete(tid);
        trackAlphaEMA.delete(tid);
      }
    }
  }

  function renderAt(framePts) {
    const dim = setCanvasSize();
    ctx.clearRect(0, 0, dim.w, dim.h);
    if (!showLabels) return;

    pruneTrackHistory(framePts);

    let drawnCount = 0;
    for (const [tid, events] of trackHistory.entries()) {
      if (events.length === 0) continue;
      const firstPts = events[0].pts;
      const lastPts = events[events.length - 1].pts;
      // Pre-arrival fade-in (FADE_IN_S before first event)
      let opacity;
      if (framePts < firstPts - FADE_IN_S) continue;
      if (framePts < firstPts) {
        opacity = (framePts - (firstPts - FADE_IN_S)) / FADE_IN_S;
      } else if (framePts > lastPts + STALE_S + FADE_OUT_S) {
        continue;
      } else if (framePts > lastPts + STALE_S) {
        opacity = 1 - (framePts - lastPts - STALE_S) / FADE_OUT_S;
      } else {
        opacity = 1;
      }
      if (opacity <= 0) continue;

      const anchor = adaptiveAnchorAt(tid, events, framePts);
      if (!anchor) continue;

      // Get the latest event for species/label info
      const latest = events[events.length - 1];
      const fw = latest.frame_width || 640;
      const fh = latest.frame_height || 360;
      const { sx, sy, offX, offY } = videoRect(dim.w, dim.h, fw, fh);

      // For pre-arrival, fall back to first event's bbox; otherwise use anchor.cx/top
      const useAnchor = framePts >= firstPts;
      let cx, top, bboxW, bboxH;
      if (useAnchor) {
        // anchor.cx is bbox-center-x in detector coords; we need to reconstruct bbox dims.
        // For now, use the latest event's bbox dimensions (the bird's size doesn't
        // change dramatically between frames).
        cx = anchor.cx;
        top = anchor.top;
        bboxW = latest.bbox_w; bboxH = latest.bbox_h;
      } else {
        cx = events[0].cx; top = events[0].top;
        bboxW = events[0].bbox_w; bboxH = events[0].bbox_h;
      }

      const x = offX + (cx - bboxW / 2) * sx;
      const y = offY + top * sy;
      const w = bboxW * sx;
      const h = bboxH * sy;

      drawBBox(x, y, w, h, !!latest.is_locked, opacity);
      const labelText = latest.species
        ? `${latest.species} · ${Math.round((latest.species_confidence || 0) * 100)}%`
        : 'identifying…';
      drawLabel(offX + cx * sx, y, labelText, !!latest.is_locked, opacity);
      drawnCount++;
    }

    diag.drawnTracks = drawnCount;
    diag.lastDrawAt = performance.now();
    diag.lastTargetPts = framePts;
  }

  function videoFrameTick(now, meta) {
    diag.rvfcCount++;
    diag.lastMediaT = meta?.mediaTime ?? video.currentTime;
    const framePts = computeFramePts(meta);
    if (framePts !== null) renderAt(framePts + LEAD_TIME_S);
    if (typeof video.requestVideoFrameCallback === 'function') {
      video.requestVideoFrameCallback(videoFrameTick);
    }
  }

  function startFrameSync() {
    if (typeof video.requestVideoFrameCallback === 'function') {
      video.requestVideoFrameCallback(videoFrameTick);
    } else {
      // rVFC unavailable — fall back to ~30Hz interval render
      setInterval(() => {
        const framePts = computeFramePts(null);
        if (framePts !== null) renderAt(framePts + LEAD_TIME_S);
      }, 33);
    }
  }
```

- [ ] **Step 3: Commit (SSE wiring in B6)**

```bash
git add dashboard/pi_dash.html
git commit -m "feat: per-frame PTS computation + render loop (both transport paths)"
```

---

### Task B6: SSE subscriber + diagnostics + toggle + boot

**Files:**
- Modify: `dashboard/pi_dash.html`

- [ ] **Step 1: Add SSE handler that feeds trackHistory + eventBuf**

Append to `setupLiveView()`:

```javascript
  // ── SSE subscriber ───────────────────────────────────────────────────
  function ingestEvent(evt) {
    if (typeof evt.pts !== 'number') return;
    // Update global event buffer (for any future cross-track logic)
    eventBuf.push({ pts: evt.pts, tracks: evt.tracks || [] });
    while (eventBuf.length > EVENT_BUF_MAX) eventBuf.shift();
    diag.sseCount++;

    // Update per-track history
    for (const t of (evt.tracks || [])) {
      const tid = t.track_id;
      if (!trackHistory.has(tid)) trackHistory.set(tid, []);
      const events = trackHistory.get(tid);
      const cx = (t.bbox[0] + t.bbox[2]) / 2;
      const top = t.bbox[1];
      bisectInsertEvent(events, {
        pts: evt.pts,
        cx, top,
        bbox_w: t.bbox[2] - t.bbox[0],
        bbox_h: t.bbox[3] - t.bbox[1],
        frame_width: t.frame_width,
        frame_height: t.frame_height,
        species: t.species,
        species_confidence: t.species_confidence,
        is_locked: t.is_locked,
      });
    }
    // Update status indicator
    const n = (evt.tracks || []).length;
    statusEl.textContent = n === 1 ? '1 track' : `${n} tracks`;
  }

  let sse;
  function connectSSE() {
    sse = new EventSource(SSE_URL);
    sse.onopen = () => {
      statusEl.textContent = 'live';
      dotEl.classList.remove('offline');
    };
    sse.onmessage = (e) => {
      try { ingestEvent(JSON.parse(e.data)); }
      catch (err) { /* ignore */ }
    };
    sse.onerror = () => {
      statusEl.textContent = 'reconnecting…';
      dotEl.classList.add('offline');
    };
  }

  // ── Labels toggle ────────────────────────────────────────────────────
  function refreshToggleUi() {
    toggleBtn.classList.toggle('off', !showLabels);
    toggleBtn.textContent = showLabels ? 'Labels' : 'Labels off';
  }
  refreshToggleUi();
  toggleBtn.addEventListener('click', () => {
    showLabels = !showLabels;
    localStorage.setItem('showLabels', showLabels ? '1' : '0');
    refreshToggleUi();
    // Force a redraw immediately
    setCanvasSize();
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  });

  // ── Diagnostic chip ──────────────────────────────────────────────────
  if (showDiag) {
    let lastRvfc = 0, lastSse = 0, lastDiagAt = performance.now();
    setInterval(() => {
      const nowT = performance.now();
      const dt = (nowT - lastDiagAt) / 1000;
      const rvfcFps = (diag.rvfcCount - lastRvfc) / dt;
      const sseHz = (diag.sseCount - lastSse) / dt;
      lastRvfc = diag.rvfcCount; lastSse = diag.sseCount;
      lastDiagAt = nowT;
      const drawAge = ((nowT - diag.lastDrawAt) / 1000).toFixed(1);
      diagEl.textContent =
        `rVFC: ${rvfcFps.toFixed(1)} fps  (n=${diag.rvfcCount})\n` +
        `SSE:  ${sseHz.toFixed(1)} Hz   (n=${diag.sseCount})\n` +
        `buf:  ${eventBuf.length} ev    tracks:${trackHistory.size}\n` +
        `mediaT: ${diag.lastMediaT.toFixed(2)}s\n` +
        `targetPTS: ${diag.lastTargetPts.toFixed(2)}s\n` +
        `drawn: ${diag.drawnTracks} (${drawAge}s ago)`;
    }, 500);
  }

  // ── Boot ─────────────────────────────────────────────────────────────
  connectSSE();
  startFrameSync();
  window.addEventListener('resize', () => setCanvasSize());
}
```

- [ ] **Step 2: Deploy and verify end-to-end**

```bash
rsync -avz dashboard/pi_dash.html vives@pi5.local:/home/vives/bird-classifier/dashboard/
ssh vives@pi5.local "systemctl --user restart bird-dashboard.service"
```

Open `http://pi5.local:8099/?syncdiag=1` in Chrome. Expected:
- Video plays
- "rVFC: ~30 fps" in the diag chip
- "SSE: ~5 Hz" when birds are active
- `targetPTS` advances continuously
- When a bird detection fires, a bbox + label appear on the canvas, tracking the bird

- [ ] **Step 3: Commit**

```bash
git add dashboard/pi_dash.html
git commit -m "feat: SSE subscriber + labels toggle + diag chip wires everything"
```

---

## Phase C — Test harness (Tasks C1–C5)

### Task C1: SSE event recorder

**Files:**
- Create: `tools/sync_replay_record_sse.py`

The harness needs to capture SSE events during replay so it can match against annotations. This is a separate small tool.

- [ ] **Step 1: Write the recorder**

```python
# tools/sync_replay_record_sse.py
"""Connect to the Pi's SSE endpoint, write every event as a JSONL line.

Run while the pipeline is processing the replay video; the resulting
file is consumed by sync_replay_assert.py.

Usage:
    python3 sync_replay_record_sse.py \
        --url http://pi5.local:8099/api/pipeline/events/sse?camera=feeder \
        --duration 1800 \
        --out replay_events.jsonl
"""
import argparse
import json
import sys
import time
from urllib.request import urlopen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--duration", type=float, default=1800.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    t0 = time.time()
    with urlopen(args.url) as resp, open(args.out, "w") as outf:
        for line in resp:
            line = line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            outf.write(payload + "\n")
            outf.flush()
            if time.time() - t0 > args.duration:
                break
    print(f"wrote events to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add tools/sync_replay_record_sse.py
git commit -m "feat: SSE event recorder for replay harness"
```

---

### Task C2: Greedy 1:1 matcher + tests

**Files:**
- Create: `tools/sync_matcher.py`
- Create: `tests/test_sync_matcher.py`

- [ ] **Step 1: Write matcher tests**

```python
# tests/test_sync_matcher.py
import pytest
from tools.annotation_parser import Visit
from tools.sync_matcher import match_annotations_to_events, MatchResult, Event


def make_visit(id, first_in, last_in, first_id=None, last_id=None, species=None):
    return Visit(
        id=id,
        first_in_frame_s=first_in,
        first_identifiable_s=first_id,
        last_identifiable_s=last_id,
        last_in_frame_s=last_in,
        species=species,
    )


def make_event(pts, species=None):
    return Event(pts=pts, species=species, tracks=[{"species": species}])


def test_matcher_detection_only_pass():
    visits = [make_visit("01", 1.0, 5.0)]
    events = [make_event(3.0, "house finch")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].detection_matched
    assert res.results[0].species_matched is None  # species not asserted (annotation blank)


def test_matcher_species_required_pass():
    visits = [make_visit("01", 1.0, 5.0, first_id=2.0, last_id=4.0, species="house finch")]
    events = [make_event(2.5, "house finch")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].detection_matched
    assert res.results[0].species_matched is True


def test_matcher_species_required_fail_wrong_species():
    visits = [make_visit("01", 1.0, 5.0, first_id=2.0, last_id=4.0, species="house finch")]
    events = [make_event(2.5, "northern cardinal")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].detection_matched
    assert res.results[0].species_matched is False


def test_matcher_no_event_in_window_fail():
    visits = [make_visit("01", 1.0, 5.0, first_id=2.0, last_id=4.0, species="house finch")]
    events = [make_event(10.0, "house finch")]  # way out of window
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert not res.results[0].detection_matched
    assert res.results[0].species_matched is False


def test_matcher_1to1_no_double_claim():
    # Two annotations could share an event; matcher must give it to only one
    v1 = make_visit("01", 1.0, 2.0, first_id=1.5, last_id=1.8, species="house finch")
    v2 = make_visit("02", 1.7, 3.0, first_id=2.0, last_id=2.5, species="house finch")
    events = [
        make_event(1.6, "house finch"),  # closer to v1.id_midpoint=1.65
        make_event(2.2, "house finch"),  # closer to v2.id_midpoint=2.25
    ]
    res = match_annotations_to_events([v1, v2], events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].species_matched is True
    assert res.results[1].species_matched is True
    # Each event claimed exactly once
    assert len(res.unclaimed_events) == 0


def test_matcher_false_positive_detection():
    visits = [make_visit("01", 1.0, 2.0)]
    events = [make_event(1.5, "house finch"), make_event(10.0, "house finch")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    # Event at 10.0 is outside all in-frame windows → false positive
    assert any(e.pts == 10.0 for e in res.false_positives)
```

- [ ] **Step 2: Write the matcher**

```python
# tools/sync_matcher.py
"""Greedy nearest 1:1 annotation/event matcher.

Per spec §6 I3: matching is greedy-nearest, 1:1. Annotations sorted by
identifiable midpoint; for each annotation in order, find the closest
unclaimed event with matching species (or any species if annotation has
no identifiable window). Annotation has 1:1 claim on the event.

This is NOT optimal (Hungarian would be); flagged in spec §6 N-I2.
Sufficient for v1 because feeder visits rarely overlap within ±500ms.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from tools.annotation_parser import Visit


@dataclass
class Event:
    pts: float
    species: Optional[str] = None       # canonical lowercased
    tracks: list = field(default_factory=list)
    raw: Optional[dict] = None


@dataclass
class AnnotationResult:
    visit_id: str
    detection_matched: bool
    species_matched: Optional[bool]      # None if no species required
    matched_event: Optional[Event] = None
    lag_ms: Optional[float] = None       # event.pts - identifiable_midpoint
    fail_reason: Optional[str] = None


@dataclass
class MatchSummary:
    results: list[AnnotationResult]
    false_positives: list[Event]
    unclaimed_events: list[Event]


def _id_midpoint(v: Visit) -> Optional[float]:
    if v.first_identifiable_s is not None and v.last_identifiable_s is not None:
        return (v.first_identifiable_s + v.last_identifiable_s) / 2
    return None


def match_annotations_to_events(
    visits: list[Visit],
    events: list[Event],
    *,
    detection_window_ms: int = 500,
    species_window_ms: int = 1000,
) -> MatchSummary:
    # Sort annotations by identifiable midpoint when available, else by
    # in-frame midpoint.
    def sort_key(v: Visit) -> float:
        if v.first_identifiable_s is not None and v.last_identifiable_s is not None:
            return _id_midpoint(v) or 0
        return ((v.first_in_frame_s or 0) + (v.last_in_frame_s or 0)) / 2

    visits_sorted = sorted(visits, key=sort_key)
    claimed = set()
    results = []

    for v in visits_sorted:
        result = AnnotationResult(
            visit_id=v.id,
            detection_matched=False,
            species_matched=None,
        )
        id_mid = _id_midpoint(v)
        # 1) Detection assertion: any event inside [first_in_frame, last_in_frame]
        #    +/- detection_window
        in_frame_lo = (v.first_in_frame_s or 0) - detection_window_ms / 1000
        in_frame_hi = (v.last_in_frame_s or 0) + detection_window_ms / 1000
        # Find nearest unclaimed event in this range
        best_idx = None
        best_dist = float("inf")
        for i, e in enumerate(events):
            if i in claimed: continue
            if e.pts < in_frame_lo or e.pts > in_frame_hi: continue
            d = abs(e.pts - ((v.first_in_frame_s or 0) + (v.last_in_frame_s or 0)) / 2)
            if d < best_dist:
                best_dist = d; best_idx = i

        if best_idx is not None:
            result.detection_matched = True

        # 2) Species assertion (only if annotation has species + id window)
        if v.species and id_mid is not None:
            best_sp_idx = None
            best_sp_dist = float("inf")
            for i, e in enumerate(events):
                if i in claimed: continue
                if abs(e.pts - id_mid) > species_window_ms / 1000: continue
                if (e.species or "").lower() != v.species: continue
                d = abs(e.pts - id_mid)
                if d < best_sp_dist:
                    best_sp_dist = d; best_sp_idx = i
            if best_sp_idx is not None:
                result.species_matched = True
                claimed.add(best_sp_idx)
                result.matched_event = events[best_sp_idx]
                result.lag_ms = (events[best_sp_idx].pts - id_mid) * 1000
            else:
                result.species_matched = False
                result.fail_reason = "no event with matching species in window"
        elif v.species and id_mid is None:
            # Species given but no identifiable window → can't assert species
            result.species_matched = None
        # else: no species required

        results.append(result)

    # False positives: events outside ALL in-frame windows
    fps = []
    for i, e in enumerate(events):
        in_any = False
        for v in visits:
            lo = (v.first_in_frame_s or 0) - detection_window_ms / 1000
            hi = (v.last_in_frame_s or 0) + detection_window_ms / 1000
            if lo <= e.pts <= hi:
                in_any = True; break
        if not in_any:
            fps.append(e)

    unclaimed = [e for i, e in enumerate(events) if i not in claimed]
    return MatchSummary(results=results, false_positives=fps, unclaimed_events=unclaimed)
```

- [ ] **Step 3: Tests pass**

```bash
python3 -m pytest tests/test_sync_matcher.py -v
```

Expected: 6 PASS.

- [ ] **Step 4: Commit**

```bash
git add tools/sync_matcher.py tests/test_sync_matcher.py
git commit -m "feat: greedy 1:1 annotation/event matcher"
```

---

### Task C3: Playwright harness scaffold

**Files:**
- Create: `tools/sync_replay_assert.py`

- [ ] **Step 1: Install Playwright deps**

```bash
ssh vives@pi5.local "/home/vives/bird-classifier/venv/bin/pip install playwright pytest-playwright && /home/vives/bird-classifier/venv/bin/python3 -m playwright install chromium firefox webkit"
```

Expected: Playwright and three browser engines installed. May take ~2 minutes.

- [ ] **Step 2: Write the harness shell**

```python
# tools/sync_replay_assert.py
"""End-to-end replay harness.

Replays a recorded video through the pipeline (via mediamtx-on-iMac → go2rtc →
pipeline → HLS), drives a Playwright browser against the dashboard, captures
the canvas overlay state, and asserts against David's annotations.

Usage:
    python3 sync_replay_assert.py \\
        --annotations '/Users/vives/docs/bird-observatory/training videos/may10_demo_video.annotations.md' \\
        --events replay_events.jsonl \\
        --dashboard http://pi5.local:8099 \\
        --browser chromium

For tunnel testing (Layer 2b):
    python3 sync_replay_assert.py \\
        ... \\
        --dashboard https://pi5.vivessato.com \\
        --cf-client-id "$CF_ACCESS_CLIENT_ID" \\
        --cf-client-secret "$CF_ACCESS_CLIENT_SECRET"
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from tools.annotation_parser import load_annotations_file
from tools.sync_matcher import Event, match_annotations_to_events


def load_events(path: str) -> list[Event]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            data = json.loads(line)
            pts = data.get("pts")
            if pts is None: continue
            # Use the LOCKED track's species if available
            species = None
            for t in data.get("tracks", []):
                if t.get("is_locked") and t.get("species"):
                    species = t["species"].lower()
                    break
            events.append(Event(pts=float(pts), species=species, tracks=data.get("tracks", []), raw=data))
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotations", required=True)
    ap.add_argument("--events", required=True, help="JSONL from sync_replay_record_sse.py")
    ap.add_argument("--gate-count", type=int, default=5)
    ap.add_argument("--detection-window-ms", type=int, default=500)
    ap.add_argument("--species-window-ms", type=int, default=1000)
    ap.add_argument("--median-lag-ms", type=int, default=50)
    ap.add_argument("--max-lag-ms", type=int, default=1000)
    args = ap.parse_args()

    visits = load_annotations_file(args.annotations)
    events = load_events(args.events)
    print(f"loaded {len(visits)} visits, {len(events)} events")

    summary = match_annotations_to_events(
        visits, events,
        detection_window_ms=args.detection_window_ms,
        species_window_ms=args.species_window_ms,
    )

    # Filter to the configured gate (first N visits with both windows + species filled)
    gate_results = []
    for r in summary.results:
        v = next((v for v in visits if v.id == r.visit_id), None)
        if not v: continue
        if v.first_identifiable_s is None or v.last_identifiable_s is None: continue
        if not v.species: continue
        gate_results.append(r)
        if len(gate_results) >= args.gate_count: break

    if len(gate_results) < args.gate_count:
        print(f"FAIL: only {len(gate_results)}/{args.gate_count} gate-eligible annotations")
        sys.exit(1)

    # Per spec §Acceptance: 5/5 strict
    all_pass = True
    lags = []
    for r in gate_results:
        if not r.detection_matched:
            print(f"FAIL [{r.visit_id}]: detection not matched")
            all_pass = False
        if r.species_matched is False:
            print(f"FAIL [{r.visit_id}]: species mismatch ({r.fail_reason})")
            all_pass = False
        elif r.species_matched is True:
            lags.append(r.lag_ms)
            print(f"PASS [{r.visit_id}]: lag {r.lag_ms:+.0f}ms")

    # False positives
    for e in summary.false_positives:
        print(f"WARN false positive: pts={e.pts:.3f} species={e.species}")

    # Lag distribution
    if lags:
        lags_sorted = sorted(lags)
        median_lag = lags_sorted[len(lags_sorted) // 2]
        max_lag = max(abs(l) for l in lags)
        print(f"\nlag median: {median_lag:+.0f}ms, max: {max_lag:.0f}ms")
        if abs(median_lag) > args.median_lag_ms:
            print(f"FAIL: median lag {median_lag:+.0f}ms exceeds ±{args.median_lag_ms}ms gate")
            all_pass = False
        if max_lag > args.max_lag_ms:
            print(f"WARN: max single lag {max_lag:.0f}ms exceeds {args.max_lag_ms}ms")

    if all_pass:
        print(f"\nPASS: {len(gate_results)}/{args.gate_count} gate annotations matched.")
        sys.exit(0)
    else:
        print(f"\nFAIL: see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

This version is **SSE-only** — no Playwright yet. It validates the matching layer against captured SSE. Visual canvas verification comes in a follow-up (Task C5).

- [ ] **Step 3: Commit**

```bash
git add tools/sync_replay_assert.py
git commit -m "feat: SSE-vs-annotations assertion harness (matching layer)"
```

---

### Task C4: Loop the demo video on iMac, exercise full path

**Files:**
- (Run-only; no file changes)

- [ ] **Step 1: Install mediamtx on iMac**

```bash
brew install mediamtx
```

- [ ] **Step 2: Start mediamtx-as-RTSP-loop on iMac**

```bash
cd /Users/vives/bird-classifier-pi
./test_clips/serve_test_feed.sh "/Users/vives/docs/bird-observatory/training videos/may10_demo_video.mp4" 8554 feeder-main
```

Expected: mediamtx starts; ffmpeg loops the file; RTSP URL `rtsp://localhost:8554/feeder-main` available from the iMac.

In another terminal, find the iMac's LAN IP:

```bash
ifconfig | grep 'inet 192.168' | awk '{print $2}'
```

- [ ] **Step 3: Point the Pi pipeline at it**

In a separate terminal:

```bash
IMAC_IP=192.168.4.X   # (from step 2)
ssh vives@pi5.local "PIPELINE_TEST_RTSP_URL=rtsp://$IMAC_IP:8554/feeder-main systemctl --user set-environment PIPELINE_TEST_RTSP_URL=rtsp://$IMAC_IP:8554/feeder-main && systemctl --user restart bird-pipeline.service"
# wait ~10s
ssh vives@pi5.local "journalctl --user -u bird-pipeline.service --since '20 seconds ago' --no-pager | grep -E 'PIPELINE_TEST_RTSP_URL|stream open'"
```

Expected: pipeline restarts reading from the iMac's looped video; logs show "PIPELINE_TEST_RTSP_URL] overriding camera URLs" and PyAV opens the stream.

- [ ] **Step 4: Record SSE events for one loop (~30 minutes)**

```bash
python3 tools/sync_replay_record_sse.py \
    --url http://pi5.local:8099/api/pipeline/events/sse?camera=feeder \
    --duration 1800 \
    --out /tmp/replay_events.jsonl
```

Expected: file accumulates events over 30 minutes; ~5–20 events/sec when birds are present.

- [ ] **Step 5: Restore the Pi to live mode**

```bash
ssh vives@pi5.local "systemctl --user unset-environment PIPELINE_TEST_RTSP_URL && systemctl --user restart bird-pipeline.service"
```

- [ ] **Step 6: No commit (runtime test)**

---

### Task C5: Run the assertion against recorded events

**Files:**
- (Run-only)

- [ ] **Step 1: Run the harness**

```bash
cd /Users/vives/bird-classifier-pi
python3 tools/sync_replay_assert.py \
    --annotations '/Users/vives/docs/bird-observatory/training videos/may10_demo_video.annotations.md' \
    --events /tmp/replay_events.jsonl \
    --gate-count 5
```

Expected (success path): 5 PASS lines, median lag within ±50 ms, exit 0.

If it fails, inspect: which visit didn't match? Was it (a) a real pipeline miss (the model didn't classify), (b) a wrong-species classification, or (c) the matcher's window too tight? Adjust the gate-count or window if (c); fix the pipeline if (a)/(b).

- [ ] **Step 2: Commit a runbook entry**

Create `docs/working/progress/2026-05-10-bedrock-overlay-sync-runbook.md`:

```markdown
# Replay harness runbook

## Layer 2a (LAN replay)

1. iMac: `./test_clips/serve_test_feed.sh "/Users/vives/docs/bird-observatory/training videos/may10_demo_video.mp4" 8554 feeder-main`
2. Find iMac LAN IP: `ifconfig | grep 'inet 192.168' | awk '{print $2}'`
3. Pi: `systemctl --user set-environment PIPELINE_TEST_RTSP_URL=rtsp://$IMAC_IP:8554/feeder-main && systemctl --user restart bird-pipeline.service`
4. Capture SSE: `python3 tools/sync_replay_record_sse.py --url http://pi5.local:8099/api/pipeline/events/sse?camera=feeder --duration 1800 --out /tmp/replay_events.jsonl`
5. Assert: `python3 tools/sync_replay_assert.py --annotations '...' --events /tmp/replay_events.jsonl --gate-count 5`
6. Restore: `systemctl --user unset-environment PIPELINE_TEST_RTSP_URL && systemctl --user restart bird-pipeline.service`

## Layer 2b (tunnel)

Same as above, but step 4 hits `https://pi5.vivessato.com` with CF Access headers:
`curl -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" ...`
(SSE recorder needs a flag to add these headers — see follow-up task.)
```

```bash
git add docs/working/progress/2026-05-10-bedrock-overlay-sync-runbook.md
git commit -m "docs: replay harness runbook"
```

---

## Phase D — Production sentinel (Task D1)

### Task D1: Sentinel invariants in dashboard health

**Files:**
- Modify: `dashboard/api.py`

- [ ] **Step 1: Add a background sentinel task**

In `dashboard/api.py`, find the FastAPI app instance. Add a startup hook:

```python
@app.on_event("startup")
async def start_overlay_sync_sentinel():
    import threading
    sentinel = OverlaySyncSentinel()
    sentinel_thread = threading.Thread(
        target=sentinel.run_forever, daemon=True, name="overlay-sync-sentinel"
    )
    sentinel_thread.start()
    app.state.overlay_sync_sentinel = sentinel
```

Add the sentinel class:

```python
class OverlaySyncSentinel:
    """Background invariant checker for the overlay-sync subsystem.

    Per spec §6 N-I6: detects pipeline stalls, disk-low, and the cases
    the offline harness can't catch in live operation.
    """
    def __init__(self):
        self.counters = {
            "checks_total": 0,
            "manifest_stale_alerts": 0,
            "disk_low_alerts": 0,
            "sse_silent_alerts": 0,
        }

    def run_forever(self):
        import time
        from pathlib import Path
        while True:
            try:
                self.counters["checks_total"] += 1
                hls_dir = Path.home() / "bird-snapshots" / "hls" / "feeder"
                # Manifest staleness: live.m3u8 mtime > 30s old → stalled
                manifest = hls_dir / "live.m3u8"
                if manifest.exists():
                    age = time.time() - manifest.stat().st_mtime
                    if age > 30:
                        self.counters["manifest_stale_alerts"] += 1
                        logging.warning("[overlay-sync] manifest stale %.0fs", age)
                # Disk low: <1 GB free under HLS dir
                import shutil
                if hls_dir.exists():
                    free_b = shutil.disk_usage(hls_dir).free
                    if free_b < (1 << 30):
                        self.counters["disk_low_alerts"] += 1
                        logging.warning("[overlay-sync] disk low: %d MB free",
                                        free_b // (1 << 20))
            except Exception:
                logging.exception("[overlay-sync] sentinel error")
            time.sleep(30)


@app.get("/api/overlay-sync-health")
def overlay_sync_health():
    s = app.state.overlay_sync_sentinel
    return s.counters
```

- [ ] **Step 2: Deploy + curl the endpoint**

```bash
rsync -avz dashboard/api.py vives@pi5.local:/home/vives/bird-classifier/dashboard/
ssh vives@pi5.local "systemctl --user restart bird-dashboard.service && sleep 35 && curl -s http://localhost:8099/api/overlay-sync-health | python3 -m json.tool"
```

Expected: `{"checks_total": 1, "manifest_stale_alerts": 0, ...}`. After 7 days of healthy operation, the alert counters should still be 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/api.py
git commit -m "feat: overlay-sync sentinel — manifest staleness + disk-low alerts"
```

---

## Phase E — Manual acceptance (Tasks E1–E4)

These are not code tasks; they are verification steps. Each becomes a checklist
item in the runbook (`docs/working/progress/2026-05-10-bedrock-overlay-sync-runbook.md`).

### Task E1: Layer 2a (LAN) on three browsers

- [ ] Run replay harness (Tasks C4 + C5) end-to-end with `--browser chromium`
- [ ] Same with `--browser firefox`
- [ ] Same with `--browser webkit`
- [ ] All three exit 0 with 5/5 PASS

### Task E2: Layer 2b (tunnel) on three browsers

Layer 2b needs a CF Access service token created in the Cloudflare Zero Trust dashboard:

- [ ] Create service token scoped only to the `pi5.vivessato.com` Access application
- [ ] Save to `~/.bird-observatory-env` (locally, not committed): `CF_ACCESS_CLIENT_ID=...` / `CF_ACCESS_CLIENT_SECRET=...`
- [ ] Extend `sync_replay_record_sse.py` to add `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers when env vars set
- [ ] Run the full replay → assert flow against `https://pi5.vivessato.com` on all three browsers
- [ ] All three exit 0 with 5/5 PASS

### Task E3: Manual iPad smoke

- [ ] Open `https://pi5.vivessato.com/` on real iPad Safari
- [ ] Add to home screen
- [ ] Launch the PWA
- [ ] Observe: video autoplays inline (no tap-to-start)
- [ ] Observe: overlay labels track birds without 240ms tail
- [ ] Open `https://pi5.vivessato.com/?syncdiag=1` and confirm: rVFC ≥ 25 fps, sub-second lag
- [ ] Capture screenshots; save to `~/docs/bird-observatory-pi/05-dashboard.md`

### Task E4: Manual macOS Safari smoke + DISCONTINUITY recovery

- [ ] Open `https://pi5.vivessato.com/` on macOS Safari
- [ ] Confirm video plays + labels track
- [ ] Trigger a DISCONTINUITY: `ssh vives@pi5.local "systemctl --user restart bird-pipeline.service"`
- [ ] Confirm video resumes within ~5 seconds without manual reload, labels resume tracking

---

## Acceptance signoff

Per spec §Acceptance:

1. [ ] Layer 2a (LAN) replay 5/5 PASS on Chromium + Firefox + WebKit (Tasks E1)
2. [ ] Layer 2b (tunnel) replay 5/5 PASS on Chromium + Firefox + WebKit (Tasks E2)
3. [ ] Manual iPad PWA smoke captures screenshots (Task E3)
4. [ ] Manual macOS Safari DISCONTINUITY recovery (Task E4)
5. [ ] 7 consecutive days in production with no sentinel alert counter increments

Once 1–4 are checked, mark this plan complete and start the 7-day watch.
After day 7, declare the design **locked**.
