"""test_video_pipeline — replay video files through the full bird detection pipeline.

Decodes video frames from MP4/MOV files using PyAV, runs YOLO bird detection,
classifies with both models (AIY SpeciesClassifier + YardClassifier), tracks
birds with BirdTracker, and produces a per-frame timeline and species summary.

Usage:
    python test_video_pipeline.py video1.mp4 video2.mp4
    python test_video_pipeline.py --video-dir ~/Desktop/test-videos/
    python test_video_pipeline.py video.mp4 --output report.json --skip-frames 3
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ── Paths ────────────────────────────────────────────────────────────────────

MODELS_DIR = Path("/Users/vives/bird-classifier/models")
YOLO_MODEL = MODELS_DIR / "yolov8n_bird.onnx"
SPECIES_MODEL = MODELS_DIR / "aiy_birds_v1.onnx"
TPU_MODEL = MODELS_DIR / "aiy_birds_v1_edgetpu.tflite"
LABELS = MODELS_DIR / "inat_bird_labels.txt"
REGIONAL_SPECIES = MODELS_DIR / "chilmark_feeder_species.txt"
YARD_MODEL = MODELS_DIR / "yard_model.tflite"
YARD_LABELS = MODELS_DIR / "yard_model_labels.txt"

# Confidence threshold for yard model to win over AIY
YARD_CONFIDENCE_THRESHOLD = 0.45

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DetectionResult:
    """One detected bird in one frame."""
    species: str
    confidence: float
    model_source: str          # "yard" or "aiy"
    box: list                  # [x1, y1, x2, y2]


@dataclass
class FrameResult:
    """All detections and active tracks for one processed frame."""
    frame_number: int
    timestamp_ms: float
    detections: list           # list[DetectionResult]
    tracks: list               # list of track state dicts from BirdTracker


@dataclass
class VideoReport:
    """Full report for one video file."""
    video_path: str
    frames: list = field(default_factory=list)   # list[FrameResult]
    total_frames: int = 0
    fps: float = 0.0
    duration_s: float = 0.0
    processing_time_s: float = 0.0

    def species_summary(self) -> dict:
        """Return per-species detection counts, avg confidence, model sources.

        Returns
        -------
        dict[str, dict]
            {species_name: {count, avg_confidence, model_sources}}
        """
        summary = {}
        for frame in self.frames:
            for det in frame.detections:
                name = det.species
                if name not in summary:
                    summary[name] = {
                        "count": 0,
                        "confidence_sum": 0.0,
                        "model_sources": set(),
                    }
                summary[name]["count"] += 1
                summary[name]["confidence_sum"] += det.confidence
                summary[name]["model_sources"].add(det.model_source)

        # Finalise: compute avg_confidence, convert set to list
        result = {}
        for name, data in summary.items():
            result[name] = {
                "count": data["count"],
                "avg_confidence": round(data["confidence_sum"] / data["count"], 4),
                "model_sources": sorted(data["model_sources"]),
            }
        return result

    def to_dict(self) -> dict:
        """Serialise report to a JSON-compatible dict."""
        return {
            "video_path": self.video_path,
            "total_frames": self.total_frames,
            "fps": self.fps,
            "duration_s": self.duration_s,
            "processing_time_s": self.processing_time_s,
            "frames_processed": len(self.frames),
            "species_summary": self.species_summary(),
            "frames": [
                {
                    "frame_number": f.frame_number,
                    "timestamp_ms": f.timestamp_ms,
                    "detections": [asdict(d) for d in f.detections],
                    "tracks": f.tracks,
                }
                for f in self.frames
            ],
        }


# ── Model loading ────────────────────────────────────────────────────────────

def load_regional_species() -> Optional[set]:
    """Load regional species set from disk. Returns None if file missing."""
    if not REGIONAL_SPECIES.exists():
        log.warning("Regional species file not found: %s", REGIONAL_SPECIES)
        return None
    lines = REGIONAL_SPECIES.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip()}


def load_models():
    """Load YOLO, AIY, and (optionally) yard models. Return (detector, classifier, yard).

    yard may be None if pycoral is unavailable or yard model files are missing.
    """
    from bird_inference import YOLODetector, SpeciesClassifier

    log.info("Loading YOLO detector from %s", YOLO_MODEL)
    detector = YOLODetector(str(YOLO_MODEL), confidence=0.3)

    regional = load_regional_species()
    log.info("Loading AIY species classifier from %s", SPECIES_MODEL)
    classifier = SpeciesClassifier(
        str(SPECIES_MODEL),
        str(LABELS),
        regional_species=regional,
        tpu_model_path=str(TPU_MODEL),
    )

    yard = None
    if YARD_MODEL.exists() and YARD_LABELS.exists():
        try:
            from yard_classifier import YardClassifier
            log.info("Loading yard model from %s", YARD_MODEL)
            yard = YardClassifier(str(YARD_MODEL), str(YARD_LABELS))
            log.info("Yard model loaded successfully")
        except Exception as exc:
            log.warning("Yard model unavailable (%s) — AIY only", exc)
    else:
        log.info("Yard model files not found — AIY only")

    return detector, classifier, yard


# ── Classification helpers ────────────────────────────────────────────────────

def classify_crop(crop, classifier, yard) -> DetectionResult:
    """Classify a single bird crop using both models; return winning result.

    Dual-model logic:
    - Always run AIY classifier.
    - If YardClassifier is available and top confidence >= YARD_CONFIDENCE_THRESHOLD,
      yard model wins.
    - Otherwise AIY result is used.
    """
    # AIY classification
    aiy_species = "unidentified bird"
    aiy_confidence = 0.0
    try:
        filtered, _raw = classifier.classify(crop)
        if filtered:
            top = filtered[0]
            aiy_species = top["common_name"]
            # raw_score is uint8 0-255; normalise to 0–1 for reporting
            raw = top.get("raw_score", 0)
            aiy_confidence = round(min(1.0, raw / 255.0), 4)
    except Exception as exc:
        log.warning("AIY classify error: %s", exc)

    # Yard model (optional)
    if yard is not None:
        try:
            yard_preds = yard.classify(crop)
            if yard_preds:
                top_yard = yard_preds[0]
                yard_conf = top_yard["confidence"]
                if yard_conf >= YARD_CONFIDENCE_THRESHOLD:
                    return DetectionResult(
                        species=top_yard["common_name"],
                        confidence=round(yard_conf, 4),
                        model_source="yard",
                        box=[],  # filled by caller
                    )
        except Exception as exc:
            log.warning("Yard classify error: %s", exc)

    return DetectionResult(
        species=aiy_species,
        confidence=aiy_confidence,
        model_source="aiy",
        box=[],  # filled by caller
    )


# ── Core video processing ─────────────────────────────────────────────────────

def process_video(
    video_path: Path,
    detector,
    classifier,
    yard,
    skip_frames: int = 1,
) -> VideoReport:
    """Process a single video file end-to-end.

    Parameters
    ----------
    video_path : Path
        Path to the MP4/MOV file.
    detector : YOLODetector
    classifier : SpeciesClassifier
    yard : YardClassifier or None
    skip_frames : int
        Process every Nth frame (1 = every frame, 3 = every 3rd frame, etc.)

    Returns
    -------
    VideoReport
    """
    import av
    from bird_inference import crop_bird
    from bird_tracker import BirdTracker

    tracker = BirdTracker(iou_threshold=0.3, expire_seconds=5.0)
    frame_results: list[FrameResult] = []

    t_start = time.monotonic()
    total_frames = 0
    fps = 0.0
    duration_s = 0.0

    log.info("Opening video: %s", video_path)
    try:
        container = av.open(str(video_path))
    except Exception as exc:
        log.error("Failed to open video %s: %s", video_path, exc)
        return VideoReport(video_path=str(video_path))

    try:
        stream = container.streams.video[0]
        if stream.frames:
            total_frames = stream.frames
        if stream.average_rate:
            fps = float(stream.average_rate)
        if stream.duration and stream.time_base:
            duration_s = float(stream.duration * stream.time_base)

        log.info(
            "Video: %d frames, %.1f fps, %.1f s",
            total_frames, fps, duration_s,
        )

        frame_idx = 0
        for av_frame in container.decode(video=0):
            frame_idx += 1

            # Skip frames for speed
            if (frame_idx - 1) % skip_frames != 0:
                continue

            # Decode to PIL RGB
            pil_image = av_frame.to_image().convert("RGB")
            timestamp_ms = (
                float(av_frame.pts * stream.time_base * 1000)
                if av_frame.pts is not None
                else 0.0
            )

            # YOLO detection
            try:
                detections = detector.detect(pil_image)
            except Exception as exc:
                log.warning("YOLO error on frame %d: %s", frame_idx, exc)
                pil_image.close()
                continue

            if not detections:
                pil_image.close()
                continue

            # Classify each detected bird
            det_results: list[DetectionResult] = []
            species_list = []

            for det in detections:
                box = det["box"]
                crop = crop_bird(pil_image, box)

                if crop.size[0] < 5 or crop.size[1] < 5:
                    det_result = DetectionResult(
                        species="unidentified bird",
                        confidence=det["confidence"],
                        model_source="none",
                        box=box,
                    )
                else:
                    det_result = classify_crop(crop, classifier, yard)
                    det_result.box = box

                det_results.append(det_result)
                species_list.append(det_result.species)

            # Update tracker
            tracks = tracker.update(detections, species_list)

            frame_result = FrameResult(
                frame_number=frame_idx,
                timestamp_ms=round(timestamp_ms, 1),
                detections=det_results,
                tracks=tracks,
            )
            frame_results.append(frame_result)

            # Log detections
            for dr in det_results:
                log.info(
                    "  frame=%d t=%.0fms  %s  conf=%.3f  [%s]",
                    frame_idx, timestamp_ms, dr.species, dr.confidence, dr.model_source,
                )

            pil_image.close()

    finally:
        container.close()

    processing_time_s = time.monotonic() - t_start
    log.info(
        "Done: %d frames processed in %.1fs (%.1f fps)",
        len(frame_results), processing_time_s,
        len(frame_results) / processing_time_s if processing_time_s > 0 else 0,
    )

    return VideoReport(
        video_path=str(video_path),
        frames=frame_results,
        total_frames=total_frames,
        fps=fps,
        duration_s=duration_s,
        processing_time_s=round(processing_time_s, 2),
    )


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(report: VideoReport):
    """Print a human-readable summary to stdout."""
    print(f"\n{'='*60}")
    print(f"Video: {report.video_path}")
    print(f"  Total frames: {report.total_frames}  |  FPS: {report.fps:.1f}  |  Duration: {report.duration_s:.1f}s")
    print(f"  Frames processed: {len(report.frames)}  |  Processing time: {report.processing_time_s:.1f}s")

    summary = report.species_summary()
    if not summary:
        print("  No birds detected.")
        return

    print(f"\n  Species summary ({len(summary)} species):")
    for species, data in sorted(summary.items(), key=lambda x: -x[1]["count"]):
        sources = ", ".join(data["model_sources"])
        print(
            f"    {species:<40} "
            f"count={data['count']:>4}  "
            f"avg_conf={data['avg_confidence']:.3f}  "
            f"[{sources}]"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Replay video files through the bird detection pipeline."
    )
    parser.add_argument(
        "videos",
        nargs="*",
        metavar="VIDEO",
        help="One or more video files (MP4/MOV).",
    )
    parser.add_argument(
        "--video-dir",
        metavar="DIR",
        help="Directory of video files to process (MP4/MOV).",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write JSON report to this file.",
    )
    parser.add_argument(
        "--skip-frames",
        type=int,
        default=1,
        metavar="N",
        help="Process every Nth frame (default: 1 = every frame).",
    )
    return parser.parse_args()


def collect_videos(args) -> list[Path]:
    """Collect video file paths from CLI arguments."""
    videos = []
    for v in args.videos:
        p = Path(v).expanduser()
        if not p.exists():
            log.warning("Video file not found: %s", p)
        else:
            videos.append(p)

    if args.video_dir:
        d = Path(args.video_dir).expanduser()
        if d.is_dir():
            for ext in ("*.mp4", "*.MP4", "*.mov", "*.MOV"):
                videos.extend(sorted(d.glob(ext)))
        else:
            log.warning("--video-dir not found: %s", d)

    return videos


def main():
    args = parse_args()
    videos = collect_videos(args)

    if not videos:
        print("No video files specified. Use positional args or --video-dir.")
        sys.exit(1)

    # Load models once
    try:
        detector, classifier, yard = load_models()
    except Exception as exc:
        log.error("Failed to load models: %s", exc)
        sys.exit(1)

    all_reports = []
    for video_path in videos:
        report = process_video(
            video_path,
            detector,
            classifier,
            yard,
            skip_frames=args.skip_frames,
        )
        print_report(report)
        all_reports.append(report)

    # JSON output
    if args.output:
        out_path = Path(args.output)
        payload = [r.to_dict() for r in all_reports]
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("Report written to %s", out_path)


if __name__ == "__main__":
    main()
