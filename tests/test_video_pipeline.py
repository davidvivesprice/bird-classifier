"""Tests for test_video_pipeline data structures.

These tests cover DetectionResult, FrameResult, and VideoReport without
requiring model files or a Coral USB device. Full pipeline integration
tests (process_video) are covered in Task 4.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so imports work from the tests/ directory
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── DetectionResult ──────────────────────────────────────────────────────────

class TestDetectionResult:
    def test_basic_construction(self):
        from test_video_pipeline import DetectionResult
        det = DetectionResult(
            species="Black-capped Chickadee",
            confidence=0.92,
            model_source="yard",
            box=[100, 100, 200, 200],
        )
        assert det.species == "Black-capped Chickadee"
        assert det.confidence == 0.92
        assert det.model_source == "yard"
        assert det.box == [100, 100, 200, 200]

    def test_aiy_source(self):
        from test_video_pipeline import DetectionResult
        det = DetectionResult(
            species="House Finch",
            confidence=0.75,
            model_source="aiy",
            box=[0, 0, 50, 50],
        )
        assert det.model_source == "aiy"

    def test_empty_box(self):
        from test_video_pipeline import DetectionResult
        det = DetectionResult(species="unknown", confidence=0.1, model_source="none", box=[])
        assert det.box == []


# ── FrameResult ───────────────────────────────────────────────────────────────

class TestFrameResult:
    def test_frame_results_structure(self):
        """FrameResult should have expected fields."""
        from test_video_pipeline import FrameResult
        result = FrameResult(frame_number=1, timestamp_ms=33.3, detections=[], tracks=[])
        assert result.frame_number == 1

    def test_timestamp_and_detections(self):
        from test_video_pipeline import FrameResult, DetectionResult
        det = DetectionResult("Song Sparrow", 0.88, "aiy", [10, 10, 60, 60])
        frame = FrameResult(frame_number=5, timestamp_ms=166.7, detections=[det], tracks=[])
        assert frame.timestamp_ms == 166.7
        assert len(frame.detections) == 1
        assert frame.detections[0].species == "Song Sparrow"

    def test_empty_frame(self):
        from test_video_pipeline import FrameResult
        frame = FrameResult(frame_number=0, timestamp_ms=0.0, detections=[], tracks=[])
        assert frame.detections == []
        assert frame.tracks == []

    def test_multiple_detections(self):
        from test_video_pipeline import FrameResult, DetectionResult
        dets = [
            DetectionResult("Tufted Titmouse", 0.91, "yard", [10, 10, 80, 80]),
            DetectionResult("Dark-eyed Junco", 0.65, "aiy", [200, 50, 280, 130]),
        ]
        frame = FrameResult(frame_number=10, timestamp_ms=333.0, detections=dets, tracks=[])
        assert len(frame.detections) == 2


# ── VideoReport ───────────────────────────────────────────────────────────────

def _make_report_with_chickadees():
    """Helper: build a VideoReport with two Chickadee detections."""
    from test_video_pipeline import VideoReport, FrameResult, DetectionResult
    frames = [
        FrameResult(
            frame_number=1,
            timestamp_ms=0,
            detections=[
                DetectionResult(
                    species="Black-capped Chickadee",
                    confidence=0.92,
                    model_source="yard",
                    box=[100, 100, 200, 200],
                ),
            ],
            tracks=[],
        ),
        FrameResult(
            frame_number=2,
            timestamp_ms=33,
            detections=[
                DetectionResult(
                    species="Black-capped Chickadee",
                    confidence=0.89,
                    model_source="yard",
                    box=[105, 105, 205, 205],
                ),
            ],
            tracks=[],
        ),
    ]
    return VideoReport(video_path="test.mp4", frames=frames)


class TestVideoReport:
    def test_video_report_summarizes_species(self):
        """Report should count detections per species."""
        report = _make_report_with_chickadees()
        summary = report.species_summary()
        assert "Black-capped Chickadee" in summary
        assert summary["Black-capped Chickadee"]["count"] == 2

    def test_avg_confidence(self):
        report = _make_report_with_chickadees()
        summary = report.species_summary()
        avg = summary["Black-capped Chickadee"]["avg_confidence"]
        assert abs(avg - (0.92 + 0.89) / 2) < 0.001

    def test_model_source_attribution(self):
        report = _make_report_with_chickadees()
        summary = report.species_summary()
        assert summary["Black-capped Chickadee"]["model_sources"] == ["yard"]

    def test_mixed_model_sources(self):
        from test_video_pipeline import VideoReport, FrameResult, DetectionResult
        frames = [
            FrameResult(1, 0, [DetectionResult("House Sparrow", 0.8, "aiy", [])], []),
            FrameResult(2, 33, [DetectionResult("House Sparrow", 0.7, "yard", [])], []),
        ]
        report = VideoReport(video_path="x.mp4", frames=frames)
        summary = report.species_summary()
        assert sorted(summary["House Sparrow"]["model_sources"]) == ["aiy", "yard"]

    def test_multiple_species(self):
        from test_video_pipeline import VideoReport, FrameResult, DetectionResult
        frames = [
            FrameResult(1, 0, [
                DetectionResult("Tufted Titmouse", 0.91, "yard", []),
                DetectionResult("Dark-eyed Junco", 0.65, "aiy", []),
            ], []),
            FrameResult(2, 33, [
                DetectionResult("Tufted Titmouse", 0.87, "yard", []),
            ], []),
        ]
        report = VideoReport(video_path="x.mp4", frames=frames)
        summary = report.species_summary()
        assert summary["Tufted Titmouse"]["count"] == 2
        assert summary["Dark-eyed Junco"]["count"] == 1

    def test_empty_report(self):
        from test_video_pipeline import VideoReport
        report = VideoReport(video_path="empty.mp4", frames=[])
        assert report.species_summary() == {}

    def test_to_dict_structure(self):
        report = _make_report_with_chickadees()
        d = report.to_dict()
        assert "video_path" in d
        assert "species_summary" in d
        assert "frames" in d
        assert d["video_path"] == "test.mp4"
        assert d["frames_processed"] == 2

    def test_to_dict_frames_serializable(self):
        """to_dict() output should be JSON serializable."""
        import json
        report = _make_report_with_chickadees()
        d = report.to_dict()
        # Should not raise
        serialized = json.dumps(d)
        assert "Black-capped Chickadee" in serialized

    def test_to_dict_species_summary_inline(self):
        report = _make_report_with_chickadees()
        d = report.to_dict()
        chickadee = d["species_summary"]["Black-capped Chickadee"]
        assert chickadee["count"] == 2
        assert chickadee["model_sources"] == ["yard"]

    def test_default_fields(self):
        from test_video_pipeline import VideoReport
        report = VideoReport(video_path="vid.mp4")
        assert report.total_frames == 0
        assert report.fps == 0.0
        assert report.duration_s == 0.0
        assert report.processing_time_s == 0.0
        assert report.frames == []
