#!/usr/bin/env python3
"""Fetch featured species photos from All About Birds / Macaulay Library CDN.

For each species in the dashboard grid, scrapes the allaboutbirds.org photo
gallery page to find the first (featured) photo asset ID, then downloads
the 1200px version from the Macaulay Library CDN.

Usage:
    python3 fetch_allaboutbirds.py              # all grid species
    python3 fetch_allaboutbirds.py --dry-run    # just show what would be downloaded
    python3 fetch_allaboutbirds.py --force      # re-download even if image exists
"""

import json, re, sys, time, os, urllib.request, urllib.error
from pathlib import Path

IMAGES_DIR = Path(__file__).parent / "species_images"
API_URL = "http://localhost:8099/api/species?date=all"
CDN_URL = "https://cdn.download.ams.birds.cornell.edu/api/v1/asset/{asset_id}/1200"
GALLERY_URL = "https://www.allaboutbirds.org/guide/{slug}/photo-gallery"

# Some species have non-standard allaboutbirds slugs
SLUG_OVERRIDES = {
    "Slate-colored Junco": "Dark-eyed_Junco",
    "Myrtle Warbler": "Yellow-rumped_Warbler",
    "Feral Pigeon": "Rock_Pigeon",
    "American Green-winged Teal": "Green-winged_Teal",
    "American Herring Gull": "Herring_Gull",
    "American Barn Swallow": "Barn_Swallow",
}

# Skip these — no allaboutbirds page or not a real species
SKIP = {"unidentified"}

USER_AGENT = "VivesBirdObservatory/1.0 (personal bird dashboard)"


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name.replace("'", "")).strip("_")


def species_slug(name: str) -> str:
    if name in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[name]
    return name.replace(" ", "_")


def fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def find_asset_id(html: str) -> str | None:
    """Extract the first Macaulay Library asset ID from the page HTML."""
    # Look for asset IDs in photo gallery links or data attributes
    patterns = [
        r'photo-gallery/(\d{6,})',           # /photo-gallery/12345678
        r'asset/(\d{6,})',                    # /asset/12345678
        r'macaulaylibrary\.org/asset/(\d{6,})',  # full ML URL
        r'"assetId"\s*:\s*(\d{6,})',          # JSON data
        r'data-asset-id="(\d{6,})"',          # data attribute
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def download_image(asset_id: str, dest: Path) -> bool:
    url = CDN_URL.format(asset_id=asset_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        if len(data) < 5000:
            print(f"    WARNING: Download too small ({len(data)} bytes), skipping")
            return False
        dest.write_bytes(data)
        print(f"    Saved: {dest.name} ({len(data)//1024}KB)")
        return True
    except Exception as e:
        print(f"    ERROR downloading: {e}")
        return False


def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    # Get species list from API
    try:
        with urllib.request.urlopen(API_URL, timeout=10) as resp:
            species_data = json.loads(resp.read())
    except Exception as e:
        print(f"Failed to fetch species list from API: {e}")
        sys.exit(1)

    names = [sp["common_name"] for sp in species_data
             if sp.get("common_name") and sp["common_name"] not in SKIP]

    # Load pinned covers from gallery JSON — never overwrite these
    pinned = set()
    gallery_path = IMAGES_DIR.parent / "species_gallery.json"
    if gallery_path.exists():
        try:
            gallery = json.load(open(gallery_path))
            pinned = {name for name in gallery if "cover" in gallery[name]}
        except Exception:
            pass

    print(f"Processing {len(names)} species ({len(pinned)} pinned)...\n")

    success, skipped, failed = 0, 0, 0

    for i, name in enumerate(names):
        safe = sanitize_filename(name)
        dest = IMAGES_DIR / f"{safe}.jpg"

        # Never overwrite pinned/curated covers
        if not force and name in pinned:
            print(f"[{i+1}/{len(names)}] {name} — pinned cover, skipping")
            skipped += 1
            continue

        # Check for existing PNG too
        dest_png = IMAGES_DIR / f"{safe}.png"

        if not force and (dest.exists() and dest.stat().st_size > 10000):
            if dest.stat().st_size > 50000:
                print(f"[{i+1}/{len(names)}] {name} — already have good image, skipping")
                skipped += 1
                continue

        slug = species_slug(name)
        url = GALLERY_URL.format(slug=slug)
        print(f"[{i+1}/{len(names)}] {name}")
        print(f"    Fetching: {url}")

        if dry_run:
            skipped += 1
            continue

        try:
            html = fetch_page(url)
            asset_id = find_asset_id(html)
            if not asset_id:
                print(f"    WARNING: No asset ID found on page")
                failed += 1
                time.sleep(1)
                continue

            print(f"    Asset ID: {asset_id}")
            if download_image(asset_id, dest):
                success += 1
            else:
                failed += 1

        except urllib.error.HTTPError as e:
            print(f"    HTTP {e.code}: {e.reason}")
            failed += 1
        except Exception as e:
            print(f"    ERROR: {e}")
            failed += 1

        # Be polite — 1 second between requests
        time.sleep(1)

    print(f"\nDone! {success} downloaded, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
