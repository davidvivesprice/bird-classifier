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
