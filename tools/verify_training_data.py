#!/usr/bin/env python3
"""
Verify training data before Phase 2 Tier 2 flagship training.

HARD GATE (per feedback_verify_data_first.md): Visually sample ≥5 crops
per species before ANY model training. The yard-0/14 disaster was caused by
bad training data — this gate prevents recurrence.

Usage:
    python3 tools/verify_training_data.py [--open]      # list + sample paths
    python3 tools/verify_training_data.py --open        # open samples in Preview (macOS)
    python3 tools/verify_training_data.py --check       # validate data integrity
"""

import argparse
import json
import random
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

TRAINING_DATA_ROOT = Path.home() / "bird-classifier" / "data" / "bird_crops_train_labeled"


def discover_species(root: Path) -> dict:
    """Return {species: [paths]} for all species directories."""
    species_data = defaultdict(list)
    if not root.exists():
        print(f"ERROR: Training data root not found: {root}")
        return {}

    for species_dir in sorted(root.iterdir()):
        if not species_dir.is_dir():
            continue
        species_name = species_dir.name
        crops = list(species_dir.glob("*.jpg"))
        if crops:
            species_data[species_name] = crops

    return species_data


def sample_species(species_data: dict, samples_per_species: int = 5) -> dict:
    """Return {species: [sampled_paths]} with up to samples_per_species from each."""
    samples = {}
    for species, crops in sorted(species_data.items()):
        n = min(len(crops), samples_per_species)
        sampled = random.sample(crops, n)
        samples[species] = sampled
    return samples


def print_summary(species_data: dict, samples: dict):
    """Print summary of training data."""
    print("\n" + "="*70)
    print("TRAINING DATA VERIFICATION — Tier 2 Flagship Phase 1 Hard Gate")
    print("="*70)

    total_species = len(species_data)
    total_crops = sum(len(crops) for crops in species_data.values())

    print(f"\n✓ Species found: {total_species}")
    print(f"✓ Total crops: {total_crops}")

    print(f"\nSample Plan: {min(5, max(len(crops) for crops in species_data.values()))} crops per species")
    print("\nSpecies Coverage:")

    for species in sorted(species_data.keys()):
        crops = species_data[species]
        sampled_count = len(samples.get(species, []))
        pct = (sampled_count / len(crops) * 100) if crops else 0
        print(f"  {species:30s} : {len(crops):4d} crops → {sampled_count} samples ({pct:.0f}%)")

    print("\n" + "="*70)
    print("VERIFICATION CHECKLIST:")
    print("="*70)
    print("Before proceeding to Phase 2, manually verify:")
    print("  ☐ Each species directory contains only VALID bird crops")
    print("  ☐ No squirrels, humans, non-bird objects")
    print("  ☐ No duplicate images across species")
    print("  ☐ No corrupted JPGs (open them, check they're readable)")
    print("  ☐ Species names match field guide (e.g., no typos)")
    print("  ☐ Image quality is reasonable (not blurry, not over-exposed)")
    print(f"\nMinimum required: {min(5, 'all')} crops per species\n")


def open_samples_macos(samples: dict):
    """Open sample images in Preview (macOS only)."""
    all_samples = []
    for species, paths in sorted(samples.items()):
        for path in paths:
            all_samples.append(str(path))

    if not all_samples:
        print("No samples to open")
        return

    print(f"\nOpening {len(all_samples)} samples in Preview...")
    try:
        subprocess.run(["open", "-a", "Preview"] + all_samples, check=True)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to open Preview: {e}")
        return

    print("✓ Preview window should open. Check each image for:")
    print("  - Valid bird crop (not too zoomed, not too far)")
    print("  - No squirrels, humans, or non-bird objects")
    print("  - No corrupted/blurry images")
    print("  - Reasonable lighting and exposure")


def validate_integrity(species_data: dict) -> bool:
    """Check for common data integrity issues."""
    print("\nValidating training data integrity...")

    issues = []

    # Check 1: Species with <3 crops (too small for meaningful training)
    for species, crops in species_data.items():
        if len(crops) < 3:
            issues.append(f"  ⚠ {species}: only {len(crops)} crops (< 3)")

    # Check 2: Look for duplicates (same filename in multiple species)
    filenames = defaultdict(list)
    for species, crops in species_data.items():
        for crop in crops:
            filenames[crop.name].append(species)

    duplicates = {fn: species_list for fn, species_list in filenames.items() if len(species_list) > 1}
    if duplicates:
        sample = list(duplicates.items())[:3]
        for fn, species_list in sample:
            issues.append(f"  ⚠ Duplicate filename: {fn} in {', '.join(species_list)}")

    # Check 3: File readability (corrupt JPGs)
    try:
        from PIL import Image
        unreadable = []
        for species, crops in species_data.items():
            for crop in random.sample(crops, min(3, len(crops))):  # Sample check
                try:
                    img = Image.open(crop)
                    img.verify()
                except Exception as e:
                    unreadable.append(f"  ⚠ Unreadable: {crop.name} ({e})")

        issues.extend(unreadable[:5])  # Report first 5
    except ImportError:
        print("  (Pillow not installed; skipping corruption check)")

    if issues:
        print("\nIntegrity Issues Found:")
        for issue in issues[:10]:  # Report first 10
            print(issue)
        return False
    else:
        print("  ✓ No obvious integrity issues found")
        return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--open", action="store_true",
                    help="Open sample crops in Preview (macOS)")
    ap.add_argument("--check", action="store_true",
                    help="Validate data integrity")
    ap.add_argument("--samples", type=int, default=5,
                    help="Number of samples per species (default: 5)")
    args = ap.parse_args()

    # Discover training data
    species_data = discover_species(TRAINING_DATA_ROOT)
    if not species_data:
        print("ERROR: No training data found")
        return 1

    # Generate sample list
    samples = sample_species(species_data, args.samples)

    # Print summary
    print_summary(species_data, samples)

    # Validate integrity if requested
    if args.check:
        validate_integrity(species_data)

    # Open samples if requested
    if args.open:
        open_samples_macos(samples)
    else:
        print("\nSample Paths (run with --open to view in Preview):")
        for species in sorted(samples.keys()):
            print(f"\n{species}:")
            for path in samples[species]:
                print(f"  {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
