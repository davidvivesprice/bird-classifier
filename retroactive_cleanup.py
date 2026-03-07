#!/usr/bin/env python3
"""
Retroactive data cleanup for bird classifier filter rebuild.

Performs:
1. Species conversions (Carolina Chickadee → Black-capped, etc.)
2. Restores range-filtered "unidentified" entries where original_species is on new feeder list
3. Moves non-feeder misclassified images back to incoming/ for reprocessing
4. Updates JSONL records for all changes

Run with --dry-run first to preview changes.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime

# --- Config ---
BASE_DIR = Path("/Users/vives/bird-snapshots")
CLASSIFIED_DIR = BASE_DIR / "classified"
ANNOTATED_DIR = BASE_DIR / "annotated"
INCOMING_DIR = BASE_DIR / "incoming"
JSONL_PATH = BASE_DIR / "logs" / "classifications.jsonl"
FEEDER_LIST_PATH = Path("/Users/vives/bird-classifier/models/chilmark_feeder_species.txt")

# Species conversions: old_name → new_name
CONVERSIONS = {
    "Carolina Chickadee": "Black-capped Chickadee",
    "Boat-tailed Grackle": "Common Grackle",
    # Slate-colored Junco stays on feeder list as alias — model may output it
}

# Non-feeder species to reprocess (move images back to incoming/)
# Will be computed dynamically from directories vs feeder list


def sanitize_dirname(name):
    """Match classify.py's sanitize_dirname."""
    return name.replace(" ", "_").replace("'", "").replace("/", "-")


def unsanitize_dirname(dirname):
    """Best-effort reverse of sanitize_dirname (can't restore apostrophes)."""
    return dirname.replace("_", " ")


def load_feeder_species():
    """Load the new feeder species list."""
    with open(FEEDER_LIST_PATH) as f:
        return {line.strip() for line in f if line.strip()}


def load_jsonl():
    """Load all JSONL entries."""
    entries = []
    with open(JSONL_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def save_jsonl(entries):
    """Write all JSONL entries back."""
    backup = str(JSONL_PATH) + ".bak"
    shutil.copy2(str(JSONL_PATH), backup)
    print(f"  Backed up JSONL to {backup}")
    with open(JSONL_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def move_species_files(old_species, new_species, dry_run=False):
    """Move files from one classified species dir to another."""
    old_dir = CLASSIFIED_DIR / sanitize_dirname(old_species)
    new_dir = CLASSIFIED_DIR / sanitize_dirname(new_species)

    if not old_dir.exists():
        print(f"  [SKIP] Directory not found: {old_dir}")
        return 0

    files = list(old_dir.glob("*.jpg"))
    if not files:
        print(f"  [SKIP] No files in {old_dir}")
        return 0

    if not dry_run:
        new_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for f in files:
        dest = new_dir / f.name
        if dry_run:
            print(f"  [DRY] Would move {f.name}: {old_species} → {new_species}")
        else:
            shutil.move(str(f), str(dest))
        moved += 1

    # Remove empty old directory
    if not dry_run and old_dir.exists() and not list(old_dir.iterdir()):
        old_dir.rmdir()
        print(f"  Removed empty directory: {old_dir.name}")

    return moved


def move_to_incoming(species_dir, dry_run=False):
    """Move files from a classified species dir back to incoming/."""
    if not species_dir.exists():
        return 0

    files = list(species_dir.glob("*.jpg"))
    if not files:
        return 0

    moved = 0
    for f in files:
        dest = INCOMING_DIR / f.name
        if dry_run:
            print(f"  [DRY] Would reprocess {f.name} (was: {species_dir.name})")
        else:
            shutil.move(str(f), str(dest))
            # Also remove annotated version if it exists
            ann = ANNOTATED_DIR / f.name
            if ann.exists():
                ann.unlink()
        moved += 1

    # Remove empty directory
    if not dry_run and species_dir.exists() and not list(species_dir.iterdir()):
        species_dir.rmdir()

    return moved


def restore_from_unidentified(filename, new_species, dry_run=False):
    """Move a single file from unidentified/ to the correct species dir."""
    src = CLASSIFIED_DIR / "unidentified" / filename
    if not src.exists():
        return False

    new_dir = CLASSIFIED_DIR / sanitize_dirname(new_species)
    if not dry_run:
        new_dir.mkdir(parents=True, exist_ok=True)
        dest = new_dir / filename
        shutil.move(str(src), str(dest))
    return True


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=" * 60)
        print("DRY RUN — no files will be modified")
        print("=" * 60)
    else:
        print("=" * 60)
        print("RETROACTIVE CLEANUP — modifying files")
        print("=" * 60)

    feeder_species = load_feeder_species()
    # Build a lookup that also handles apostrophe-stripped names
    feeder_lookup = {}
    for sp in feeder_species:
        feeder_lookup[sp] = sp
        # Also index without apostrophes for directory matching
        stripped = sp.replace("'", "")
        if stripped != sp:
            feeder_lookup[stripped] = sp

    entries = load_jsonl()
    print(f"\nLoaded {len(entries)} JSONL entries")
    print(f"Feeder species list: {len(feeder_species)} species")

    changes = {
        "conversions_jsonl": 0,
        "conversions_files": 0,
        "restored_jsonl": 0,
        "restored_files": 0,
        "reprocess_jsonl": 0,
        "reprocess_files": 0,
    }

    # ─── PHASE 1: Species Conversions ───
    print("\n" + "─" * 60)
    print("PHASE 1: Species Conversions")
    print("─" * 60)

    for old_name, new_name in CONVERSIONS.items():
        print(f"\n  Converting: {old_name} → {new_name}")

        # Move files
        moved = move_species_files(old_name, new_name, dry_run)
        changes["conversions_files"] += moved
        print(f"  Files moved: {moved}")

        # Update JSONL entries
        updated = 0
        for entry in entries:
            if entry.get("action") != "classified":
                continue
            tp = entry.get("top_prediction", {})
            if tp.get("common_name") == old_name:
                if not dry_run:
                    tp["common_name"] = new_name
                    entry["conversion_applied"] = True
                    entry["original_classification"] = old_name
                    entry["conversion_reason"] = f"Range-impossible: {old_name} → {new_name}"
                updated += 1
            # Also check/update birds array
            for bird in entry.get("birds", []):
                if bird.get("species") == old_name:
                    if not dry_run:
                        bird["species"] = new_name
            # Also update top3 if present
            for pred in entry.get("top3", []):
                if pred.get("common_name") == old_name:
                    if not dry_run:
                        pred["common_name"] = new_name

        changes["conversions_jsonl"] += updated
        print(f"  JSONL entries updated: {updated}")

    # ─── PHASE 2: Restore Range-Filtered Entries ───
    print("\n" + "─" * 60)
    print("PHASE 2: Restore Range-Filtered 'unidentified' Entries")
    print("─" * 60)

    restored_by_species = {}
    for entry in entries:
        if not entry.get("range_filter_applied"):
            continue

        original = entry.get("original_species", "")
        if not original:
            continue

        # Check if original species is on the new feeder list
        # Also apply conversions (e.g., range-filtered Carolina Chickadee → Black-capped)
        target_species = original
        if original in CONVERSIONS:
            target_species = CONVERSIONS[original]

        # Check feeder list (with apostrophe handling)
        if target_species not in feeder_lookup and target_species.replace("'", "") not in feeder_lookup:
            if dry_run:
                print(f"  [SKIP] {original} not on feeder list — stays as unidentified")
            continue

        # Restore this entry
        fname = entry.get("file", "")
        file_restored = False
        if fname:
            file_restored = restore_from_unidentified(fname, target_species, dry_run)
            if file_restored:
                changes["restored_files"] += 1

        if not dry_run:
            entry["top_prediction"]["common_name"] = target_species
            entry["top_prediction"]["scientific_name"] = entry.get("top_prediction", {}).get("scientific_name", "unknown")
            entry["action"] = "classified"
            entry["range_filter_restored"] = True
            entry["range_filter_restore_reason"] = f"Original species {original} is on Chilmark feeder list"
            if original != target_species:
                entry["conversion_applied"] = True
                entry["original_classification"] = original
                entry["conversion_reason"] = f"Range-impossible: {original} → {target_species}"

        changes["restored_jsonl"] += 1
        restored_by_species[target_species] = restored_by_species.get(target_species, 0) + 1

    print(f"\n  Total restored: {changes['restored_jsonl']} JSONL entries, {changes['restored_files']} files")
    if restored_by_species:
        print("  By species:")
        for sp, ct in sorted(restored_by_species.items(), key=lambda x: -x[1]):
            print(f"    {ct:>4}  {sp}")

    # ─── PHASE 3: Reprocess Non-Feeder Misclassifications ───
    print("\n" + "─" * 60)
    print("PHASE 3: Reprocess Non-Feeder Misclassifications")
    print("─" * 60)

    # Find classified dirs that are not on feeder list and not handled above
    skip_dirs = {"unidentified", "background"}
    for old in CONVERSIONS:
        skip_dirs.add(sanitize_dirname(old))

    reprocess_species = []
    for d in sorted(os.listdir(CLASSIFIED_DIR)):
        full = CLASSIFIED_DIR / d
        if not full.is_dir():
            continue
        if d in skip_dirs:
            continue

        species_name = unsanitize_dirname(d)

        # Check feeder list (with apostrophe handling)
        on_list = species_name in feeder_lookup or species_name.replace("'", "") in feeder_lookup
        if not on_list:
            # Also check with apostrophes restored (for dir names like "Lincolns_Sparrow")
            # Try common apostrophe positions
            for sp in feeder_species:
                if sanitize_dirname(sp) == d:
                    on_list = True
                    break

        if not on_list:
            file_count = len(list(full.glob("*.jpg")))
            if file_count > 0:
                reprocess_species.append((d, species_name, file_count))

    print(f"\n  Non-feeder species to reprocess: {len(reprocess_species)}")
    for dirname, sp_name, count in reprocess_species:
        print(f"    {count:>4}  {sp_name}")

        moved = move_to_incoming(CLASSIFIED_DIR / dirname, dry_run)
        changes["reprocess_files"] += moved

        # Mark JSONL entries for reprocessing
        for entry in entries:
            if entry.get("action") != "classified":
                continue
            tp = entry.get("top_prediction", {})
            if tp.get("common_name") == sp_name:
                if not dry_run:
                    entry["action"] = "reprocess_pending"
                    entry["reprocess_reason"] = f"Species {sp_name} removed from feeder list"
                changes["reprocess_jsonl"] += 1

    # ─── PHASE 4: Handle remaining unidentified ───
    print("\n" + "─" * 60)
    print("PHASE 4: Remaining Unidentified (keeping as-is)")
    print("─" * 60)

    unid_dir = CLASSIFIED_DIR / "unidentified"
    if unid_dir.exists():
        remaining = len(list(unid_dir.glob("*.jpg")))
        print(f"  {remaining} unidentified files remaining (non-restorable)")

    # ─── Save JSONL ───
    if not dry_run:
        print("\n" + "─" * 60)
        print("Saving updated JSONL...")
        save_jsonl(entries)

    # ─── Summary ───
    print("\n" + "=" * 60)
    print("CLEANUP SUMMARY")
    print("=" * 60)
    print(f"  Conversions:  {changes['conversions_jsonl']} JSONL entries, {changes['conversions_files']} files moved")
    print(f"  Restored:     {changes['restored_jsonl']} JSONL entries, {changes['restored_files']} files moved")
    print(f"  Reprocess:    {changes['reprocess_jsonl']} JSONL entries, {changes['reprocess_files']} files → incoming/")
    total_changes = sum(changes.values())
    print(f"  Total changes: {total_changes}")

    if dry_run:
        print("\n  *** DRY RUN — no files were modified ***")
        print("  Run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
