#!/usr/bin/env python3
"""Download species cover images from All About Birds (Cornell Lab).

Scrapes allaboutbirds.org/guide/{species}/id pages to find the first
photo asset ID (skipping videos), then downloads from the Macaulay Library CDN.

Preserves existing hand-selected images — only downloads for species that
don't already have an image.

Usage:
    python3 dashboard/download_allaboutbirds.py              # download missing only
    python3 dashboard/download_allaboutbirds.py --replace    # replace all with AAB
    python3 dashboard/download_allaboutbirds.py --species "American Robin"  # single species
"""

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SPECIES_INFO = Path(__file__).parent / "species_info.json"
IMAGE_DIR = Path(__file__).parent / "species_images"

ssl_ctx = ssl.create_default_context()

# Delay between requests to be respectful
DELAY = 1.5

# CDN URL pattern for Macaulay Library assets
CDN_URL = "https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{asset_id}/1200"

# User agent
UA = "VivesBirdObservatory/1.0 (personal bird dashboard; contact david@vives.dev)"


def sanitize_filename(name: str) -> str:
    """Convert species name to safe filename."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name.replace("'", "")).strip('_')


def has_cached_image(safe: str) -> bool:
    """Check if we already have a good image for this species."""
    for ext in (".jpg", ".png"):
        p = IMAGE_DIR / f"{safe}{ext}"
        if p.exists() and p.stat().st_size > 1000:
            return True
    return False


def species_to_aab_slug(name: str) -> str:
    """Convert species name to All About Birds URL slug.

    e.g., 'Black-capped Chickadee' -> 'Black-capped_Chickadee'
          'American Crow' -> 'American_Crow'
    """
    return name.replace(' ', '_')


# Some species have different names on All About Birds
AAB_NAME_MAP = {
    "American Barn Swallow": "Barn_Swallow",
    "American Green-winged Teal": "Green-winged_Teal",
    "American Herring Gull": "Herring_Gull",
    "Slate-colored Junco": "Dark-eyed_Junco",
    "Myrtle Warbler": "Yellow-rumped_Warbler",
    "Feral Pigeon": "Rock_Pigeon",
    "Yellow-shafted Flicker": "Northern_Flicker",
}


def get_first_photo_asset(species_name: str) -> tuple[str | None, str | None]:
    """Scrape the All About Birds ID page and return (asset_id, label) for the first photo.

    Skips video assets. Returns (None, None) if no photo found.
    """
    slug = AAB_NAME_MAP.get(species_name, species_to_aab_slug(species_name))
    url = f"https://www.allaboutbirds.org/guide/{urllib.request.quote(slug)}/id"

    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Try without "American " prefix for some species
            if species_name.startswith("American "):
                alt = species_name.replace("American ", "")
                slug2 = species_to_aab_slug(alt)
                url2 = f"https://www.allaboutbirds.org/guide/{urllib.request.quote(slug2)}/id"
                req2 = urllib.request.Request(url2, headers={'User-Agent': UA})
                try:
                    with urllib.request.urlopen(req2, context=ssl_ctx, timeout=15) as resp2:
                        html = resp2.read().decode('utf-8', errors='replace')
                except Exception:
                    return None, None
            else:
                return None, None
        else:
            return None, None
    except Exception:
        return None, None

    # Find all photo-gallery links and video links
    # Photo pattern: /guide/Species/photo-gallery/ASSET_ID with nearby macaulaylibrary.org/photo/ASSET_ID
    # Video pattern: macaulaylibrary.org/video/ASSET_ID

    # Collect all video asset IDs so we can exclude them
    video_ids = set(re.findall(r'macaulaylibrary\.org/video/(\d+)', html))

    # Find all asset IDs from photo-gallery links
    photo_gallery_ids = re.findall(r'/photo-gallery/(\d+)', html)

    # The first photo-gallery ID that's NOT a video is our cover photo
    for asset_id in photo_gallery_ids:
        if asset_id not in video_ids:
            return asset_id, "cover"

    return None, None


def download_asset(asset_id: str, dest: Path) -> bool:
    """Download an image from the Macaulay Library CDN."""
    url = CDN_URL.format(asset_id=asset_id)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=20) as resp:
            data = resp.read()
            if len(data) < 2000:
                return False
            dest.write_bytes(data)
            return True
    except Exception as e:
        print(f"  download error: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--replace', action='store_true',
                        help='Replace existing images (default: skip if exists)')
    parser.add_argument('--species', type=str,
                        help='Download for a single species only')
    args = parser.parse_args()

    if not SPECIES_INFO.exists():
        print(f"ERROR: {SPECIES_INFO} not found")
        sys.exit(1)

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    with open(SPECIES_INFO) as f:
        cache = json.load(f)

    if args.species:
        if args.species not in cache:
            # Try case-insensitive match
            for k in cache:
                if k.lower() == args.species.lower():
                    args.species = k
                    break
            else:
                print(f"Species '{args.species}' not found in species_info.json")
                sys.exit(1)
        species_list = [(args.species, cache[args.species])]
    else:
        species_list = list(cache.items())

    total = len(species_list)
    skipped = 0
    downloaded = 0
    failed = 0
    already_cached = 0

    need_download = []
    for name, info in species_list:
        safe = sanitize_filename(name)
        if not args.replace and has_cached_image(safe):
            already_cached += 1
            continue
        need_download.append((name, info))

    print(f"Total: {total} species")
    print(f"Already cached: {already_cached}")
    print(f"To download: {len(need_download)}")
    print()

    for i, (name, info) in enumerate(need_download, 1):
        safe = sanitize_filename(name)
        print(f"[{i}/{len(need_download)}] {name}:")

        # Get first photo asset ID from All About Birds
        asset_id, label = get_first_photo_asset(name)

        if not asset_id:
            print(f"  ✗ no photo found on All About Birds")
            failed += 1
            time.sleep(DELAY)
            continue

        print(f"  found asset {asset_id}, downloading...")
        dest = IMAGE_DIR / f"{safe}.jpg"

        if download_asset(asset_id, dest):
            size_kb = dest.stat().st_size / 1024
            print(f"  ✓ {dest.name} ({size_kb:.0f} KB)")
            downloaded += 1
        else:
            print(f"  ✗ download failed")
            failed += 1

        time.sleep(DELAY)

    print(f"\nDone:")
    print(f"  {already_cached} previously cached (kept)")
    print(f"  {downloaded} newly downloaded from All About Birds")
    print(f"  {failed} failed")
    print(f"  Total images: {already_cached + downloaded}")


if __name__ == "__main__":
    main()
