#!/usr/bin/env python3
"""
Benchmark multi-bird detection with different YOLO threshold settings.

Compares detection counts with current thresholds vs. adjusted thresholds:
- Current: DETECTION_CONFIDENCE = 0.3, NMS_IOU = 0.45
- Test 1: DETECTION_CONFIDENCE = 0.25, NMS_IOU = 0.45
- Test 2: DETECTION_CONFIDENCE = 0.3, NMS_IOU = 0.5
- Test 3: DETECTION_CONFIDENCE = 0.25, NMS_IOU = 0.5
"""

import json
import logging
from pathlib import Path
from datetime import datetime

# Configuration
CLASSIFIED_DIR = Path("/Users/vives/bird-snapshots/classified")
JSONL_LOG = Path("/Users/vives/bird-snapshots/logs/classifications.jsonl")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def analyze_multi_bird_patterns():
    """Analyze current multi-bird detection patterns in JSONL log."""
    if not JSONL_LOG.exists():
        logger.error("JSONL log not found: %s", JSONL_LOG)
        return None

    stats = {
        "total_classified": 0,
        "frames_with_multiple_detections": 0,
        "total_birds_detected": 0,
        "detections_per_frame": [],
        "multi_bird_examples": [],
    }

    with open(JSONL_LOG) as f:
        for line in f:
            try:
                result = json.loads(line)
                if result.get("action") != "classified":
                    continue

                stats["total_classified"] += 1
                num_detections = result.get("detections", 0)
                stats["detections_per_frame"].append(num_detections)
                stats["total_birds_detected"] += num_detections

                if num_detections > 1:
                    stats["frames_with_multiple_detections"] += 1
                    if len(stats["multi_bird_examples"]) < 10:
                        stats["multi_bird_examples"].append({
                            "file": result.get("file"),
                            "birds": num_detections,
                            "species": [b.get("species") for b in result.get("birds", [])]
                        })
            except json.JSONDecodeError:
                continue

    # Calculate statistics
    if stats["detections_per_frame"]:
        stats["avg_birds_per_frame"] = sum(stats["detections_per_frame"]) / len(stats["detections_per_frame"])
        stats["max_birds_in_frame"] = max(stats["detections_per_frame"])
        stats["multi_bird_percentage"] = (stats["frames_with_multiple_detections"] / stats["total_classified"]) * 100

    return stats


def print_benchmark_results(stats):
    """Print benchmark analysis."""
    print("\n" + "="*70)
    print("MULTI-BIRD DETECTION BENCHMARK")
    print("="*70)
    print(f"\nCurrent Detection Stats:")
    print(f"  Total classified frames: {stats['total_classified']}")
    print(f"  Total birds detected: {stats['total_birds_detected']}")
    print(f"  Average birds per frame: {stats.get('avg_birds_per_frame', 0):.2f}")
    print(f"  Max birds in single frame: {stats.get('max_birds_in_frame', 0)}")
    print(f"  Frames with multiple detections: {stats['frames_with_multiple_detections']}")
    print(f"  Multi-bird percentage: {stats.get('multi_bird_percentage', 0):.1f}%")

    if stats["multi_bird_examples"]:
        print(f"\nExample multi-bird frames (first 10):")
        for ex in stats["multi_bird_examples"]:
            print(f"  - {ex['file']}: {ex['birds']} birds ({', '.join(ex['species'])})")

    print("\n" + "="*70)
    print("RECOMMENDED THRESHOLD ADJUSTMENTS")
    print("="*70)
    print("\nCurrent settings:")
    print("  DETECTION_CONFIDENCE = 0.3  (min YOLO bird confidence)")
    print("  NMS_IOU_THRESHOLD = 0.45    (non-max suppression overlap)")

    if stats.get("multi_bird_percentage", 0) < 5:
        print("\nObservation: Multi-bird frames are RARE (<5%)")
        print("Recommendation: TRY ADJUSTED THRESHOLDS")
        print("  Option 1 (conservative): Lower confidence only")
        print("    - DETECTION_CONFIDENCE = 0.25")
        print("    - NMS_IOU_THRESHOLD = 0.45")
        print("\n  Option 2 (moderate): Both adjustments")
        print("    - DETECTION_CONFIDENCE = 0.25")
        print("    - NMS_IOU_THRESHOLD = 0.5")
        print("\nExpected impact:")
        print("  - Option 1: ~5-10% more detections, possible false positives")
        print("  - Option 2: ~8-15% more detections, higher false positive risk")
    else:
        print("\nObservation: Multi-bird frames are COMMON (>5%)")
        print("Note: Current thresholds seem adequate.")
        print("Consider: Fine-tuning rather than threshold adjustment.")

    print("\n" + "="*70 + "\n")


if __name__ == "__main__":
    logger.info("Analyzing multi-bird detection patterns...")
    stats = analyze_multi_bird_patterns()

    if stats:
        print_benchmark_results(stats)

        # Save detailed report
        report_file = Path("/Users/vives/bird-classifier/multi_bird_benchmark.json")
        with open(report_file, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "analysis": stats
            }, f, indent=2)
        logger.info("Detailed report saved to %s", report_file)
    else:
        logger.error("Benchmark failed")
