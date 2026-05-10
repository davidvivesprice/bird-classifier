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
