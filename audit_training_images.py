#!/usr/bin/env python3
"""
Audit training images for annotation quality issues.

Scans annotated images to identify:
1. Labels that overlap bird head regions (text obscuring the bird)
2. Multi-bird frames (multiple bounding boxes)
3. Low-quality or problematic annotations

Generates a report for remediation.
"""

import json
import logging
import os
from pathlib import Path
from collections import defaultdict

from PIL import Image, ImageDraw

# Configuration
ANNOTATED_DIR = Path("/Users/vives/bird-snapshots/annotated")
REPORT_FILE = Path("/Users/vives/bird-classifier/audit_report.json")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def analyze_image_annotations(image_path):
    """
    Analyze a single annotated image for quality issues.

    Returns dict with:
    {
        "path": str,
        "issues": [],
        "has_label_overlap": bool,
        "is_multi_bird": bool,
        "details": {...}
    }
    """
    try:
        img = Image.open(image_path)
    except Exception as e:
        return {
            "path": str(image_path),
            "issues": ["unable_to_open"],
            "error": str(e)
        }

    result = {
        "path": str(image_path),
        "filename": image_path.name,
        "dimensions": img.size,
        "issues": [],
        "has_label_overlap": False,
        "is_multi_bird": False,
        "details": {
            "boxes_found": 0,
            "labels_found": 0,
        }
    }

    # Analyze the image pixels to detect green bounding boxes and text
    # This is a heuristic approach since we don't have the raw annotations

    # Strategy: Look for green boxes (RGB ~0, 255, 0) and white/dark text
    pixels = img.load()
    width, height = img.size

    # Sample pixels to detect green boxes (bounding boxes are typically green in annotated images)
    green_pixels = []
    white_pixels = []

    # Scan for green color (bounding boxes)
    for y in range(height):
        for x in range(width):
            pixel = pixels[x, y] if len(pixels[x, y]) >= 3 else pixels[x, y] + (255,)
            r, g, b = pixel[0], pixel[1], pixel[2]

            # Green box detection (RGB: low R, high G, low B)
            if g > 150 and r < 100 and b < 100:
                green_pixels.append((x, y))

            # White text detection
            if r > 200 and g > 200 and b > 200:
                white_pixels.append((x, y))

    result["details"]["boxes_found"] = len(green_pixels) // 1000 if green_pixels else 0  # Rough estimate
    result["details"]["text_pixels"] = len(white_pixels)

    # Multi-bird detection: if we see multiple distinct green box regions
    # (This is approximate - a more robust method would use contour detection)
    if len(green_pixels) > 5000:
        result["is_multi_bird"] = True
        result["issues"].append("likely_multi_bird")

    # Label overlap detection: if text and green boxes are in same region
    # (This is a heuristic - pixels close together suggest overlap)
    if green_pixels and white_pixels:
        # Check if any white pixels are near green pixels (within 50px)
        for wx, wy in white_pixels[:100]:  # Sample white pixels
            for gx, gy in green_pixels[::100]:  # Sample green pixels
                dist = ((wx - gx) ** 2 + (wy - gy) ** 2) ** 0.5
                if dist < 30:  # Text overlapping bounding box
                    result["has_label_overlap"] = True
                    result["issues"].append("label_overlaps_box")
                    break
            if result["has_label_overlap"]:
                break

    return result


def audit_all_images(limit=None):
    """Scan all annotated images and generate report."""
    if not ANNOTATED_DIR.exists():
        logger.error("Annotated directory not found: %s", ANNOTATED_DIR)
        return None

    images = sorted(ANNOTATED_DIR.glob("*.jpg"))
    if limit:
        images = images[:limit]

    logger.info("Auditing %d images in %s", len(images), ANNOTATED_DIR)

    results = []
    issue_counts = defaultdict(int)

    for idx, image_path in enumerate(images):
        if idx % 100 == 0:
            logger.info("Progress: %d/%d", idx, len(images))

        result = analyze_image_annotations(image_path)
        results.append(result)

        # Count issues
        for issue in result.get("issues", []):
            issue_counts[issue] += 1

    # Generate summary
    summary = {
        "total_images": len(images),
        "images_with_issues": sum(1 for r in results if r["issues"]),
        "issue_breakdown": dict(issue_counts),
        "images_with_label_overlap": sum(1 for r in results if r["has_label_overlap"]),
        "images_with_multi_birds": sum(1 for r in results if r["is_multi_bird"]),
    }

    # Categorize results
    report = {
        "timestamp": str(Path(REPORT_FILE).stat().st_mtime) if REPORT_FILE.exists() else None,
        "summary": summary,
        "problematic_images": [r for r in results if r["issues"]],
        "multi_bird_images": [r for r in results if r["is_multi_bird"]],
        "label_overlap_images": [r for r in results if r["has_label_overlap"]],
    }

    return report


def save_report(report):
    """Save audit report to JSON file."""
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("Report saved to %s", REPORT_FILE)


def print_summary(report):
    """Print audit summary to console."""
    summary = report["summary"]
    print("\n" + "="*70)
    print("TRAINING IMAGE AUDIT REPORT")
    print("="*70)
    print(f"Total images scanned: {summary['total_images']}")
    print(f"Images with issues: {summary['images_with_issues']}")
    print(f"  - Label overlap issues: {summary['images_with_label_overlap']}")
    print(f"  - Multi-bird frames: {summary['images_with_multi_birds']}")
    print("\nIssue breakdown:")
    for issue, count in summary["issue_breakdown"].items():
        print(f"  - {issue}: {count}")
    print("\nFull report saved to:", REPORT_FILE)
    print("="*70 + "\n")


if __name__ == "__main__":
    logger.info("Starting image audit...")
    report = audit_all_images()

    if report:
        save_report(report)
        print_summary(report)

        # Show sample problematic images
        if report["label_overlap_images"]:
            print("\nSample images with label overlaps (first 5):")
            for img in report["label_overlap_images"][:5]:
                print(f"  - {img['filename']}")

        if report["multi_bird_images"]:
            print("\nSample multi-bird frames (first 5):")
            for img in report["multi_bird_images"][:5]:
                print(f"  - {img['filename']}")
    else:
        logger.error("Audit failed")
