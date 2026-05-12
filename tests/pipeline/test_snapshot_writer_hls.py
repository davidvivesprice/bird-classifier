from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from pipeline.snapshot_writer import SnapshotWriter


def _payload():
    return {
        "camera": "feeder",
        "frame": np.zeros((360, 640, 3), dtype=np.uint8),
        "hires_frame": np.zeros((360, 640, 3), dtype=np.uint8),
        "wall_time_ms": 1000000.0,
        "pts": 12.5,
        "track_id": 42,
        "species": "Northern Cardinal",
        "species_confidence": 0.5,
        "model_source": "yard",
        "confidence": 0.85,
        "bbox": [100, 100, 300, 300],
        "frame_count": 5,
        "vote_history": [("Northern Cardinal", 0.5)] * 3,
    }


def _write_sidecar(root: Path, camera: str = "feeder"):
    camera_root = root / camera
    camera_root.mkdir(parents=True)
    for name in ("seg_0001.ts", "seg_0002.ts"):
        (camera_root / name).write_bytes(b"not-real-video")
    (camera_root / "segments.json").write_text(json.dumps({
        "stream": camera,
        "time_base_seconds": 1.0,
        "segments": [
            {"name": "seg_0001.ts", "pts_start": 8.0, "pts_end": 12.0, "duration": 4.0},
            {"name": "seg_0002.ts", "pts_start": 12.0, "pts_end": 16.0, "duration": 4.0},
        ],
        "discontinuities": [],
    }))


def test_locates_hls_segment_by_pts(tmp_path):
    _write_sidecar(tmp_path)
    writer = SnapshotWriter(hls_root=tmp_path)

    hit = writer._locate_hls_segment("feeder", pts=12.5)

    assert hit is not None
    path, offset_s = hit
    assert path == tmp_path / "feeder" / "seg_0002.ts"
    assert offset_s == 0.5


def test_write_one_uses_hls_frame_when_inline_full_frame_is_lowres(monkeypatch, tmp_path):
    writer = SnapshotWriter(hls_root=tmp_path)
    full_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    monkeypatch.setattr(writer, "_fetch_hls_frame_for_pts",
                        lambda camera, pts, timeout_s=None: full_frame)

    encoded_shapes = []

    def fake_imencode(_ext, frame, _params=None):
        encoded_shapes.append(frame.shape[:2])
        return True, np.zeros(10, dtype=np.uint8)

    monkeypatch.setattr("cv2.imencode", fake_imencode)
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    writer._write_one(_payload())

    assert encoded_shapes[0] == (1080, 1920)
    assert captured_entry["best_detection"]["box"] == [300, 300, 900, 900]
    assert writer.stats["hires_hls_ok"] == 1
    assert writer.stats["hires_ok"] == 1
    assert writer.stats["hires_lowres_fallback"] == 0


def test_fetch_hls_frame_returns_immediately_without_valid_pts(monkeypatch, tmp_path):
    writer = SnapshotWriter(hls_root=tmp_path)
    monkeypatch.setattr(writer, "_locate_hls_segment",
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("should not wait without PTS")))

    assert writer._fetch_hls_frame_for_pts("feeder", pts=0.0, timeout_s=10.0) is None
