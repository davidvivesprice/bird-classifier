#!/usr/bin/env python3
"""Download gallery images (4+ per species) from All About Birds.

Scrapes allaboutbirds.org/guide/{species}/id pages to find photo asset IDs
(skipping videos), downloads from the Macaulay Library CDN, and extracts
captions (e.g., "Adult male", "Juvenile") for each image.

Preserves existing hand-selected images. Only downloads additional photos
to reach the minimum gallery count.

Usage:
    python3 dashboard/download_gallery.py                    # fill to 4 per species
    python3 dashboard/download_gallery.py --min 6            # fill to 6 per species
    python3 dashboard/download_gallery.py --species "Blue Jay"  # single species
"""

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

SPECIES_INFO = Path(__file__).parent / "species_info.json"
GALLERY_JSON = Path(__file__).parent / "species_gallery.json"
IMAGE_DIR = Path(__file__).parent / "species_images"

ssl_ctx = ssl.create_default_context()

DELAY = 1.2  # seconds between page fetches
IMG_DELAY = 0.3  # seconds between image downloads
MIN_IMAGES = 4

CDN_URL = "https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{}/1200"
UA = "VivesBirdObservatory/1.0 (personal bird dashboard)"

AAB_NAME_MAP = {
    "American Barn Swallow": "Barn_Swallow",
    "American Green-winged Teal": "Green-winged_Teal",
    "American Herring Gull": "Herring_Gull",
    "Slate-colored Junco": "Dark-eyed_Junco",
    "Myrtle Warbler": "Yellow-rumped_Warbler",
    "Feral Pigeon": "Rock_Pigeon",
    "Yellow-shafted Flicker": "Northern_Flicker",
    "Bonaparte's Gull": "Bonapartes_Gull",
    "Cooper's Hawk": "Coopers_Hawk",
    "Forster's Tern": "Forsters_Tern",
    "Lincoln's Sparrow": "Lincolns_Sparrow",
    "Nelson's Sparrow": "Nelsons_Sparrow",
    "Northern Harrier (American)": "Northern_Harrier",
    "Swainson's Thrush": "Swainsons_Thrush",
    "Wilson's Snipe": "Wilsons_Snipe",
    "Wilson's Warbler": "Wilsons_Warbler",
}


def sanitize_filename(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name.replace("'", "")).strip('_')


def caption_to_suffix(caption: str, index: int) -> str:
    """Convert a caption like 'Adult male' to a filename suffix like '_adult_male'.
    Falls back to '_2', '_3', etc. for generic/duplicate captions."""
    if not caption or caption.lower() in ('adult', ''):
        return f"_{index + 1}" if index > 0 else ""
    # Clean up caption for filename
    c = caption.lower().strip()
    c = re.sub(r'[^a-z0-9 ]', '', c)
    c = '_'.join(c.split())
    if len(c) > 40:
        c = c[:40]
    return f"_{c}" if c else f"_{index + 1}"


def get_existing_images(safe_base: str) -> list[str]:
    """Find all existing images for a species base name."""
    found = []
    for f in IMAGE_DIR.glob(f"{safe_base}*.jpg"):
        found.append(f.name)
    for f in IMAGE_DIR.glob(f"{safe_base}*.png"):
        found.append(f.name)
    return sorted(found)


def scrape_photos(species_name: str) -> list[dict]:
    """Scrape All About Birds and return list of {asset_id, caption} for photos only."""
    slug = AAB_NAME_MAP.get(species_name, species_name.replace(' ', '_'))
    url = f"https://www.allaboutbirds.org/guide/{urllib.request.quote(slug)}/id"

    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        if e.code == 404 and species_name.startswith("American "):
            alt = species_name.replace("American ", "").replace(' ', '_')
            url2 = f"https://www.allaboutbirds.org/guide/{urllib.request.quote(alt)}/id"
            req2 = urllib.request.Request(url2, headers={'User-Agent': UA})
            try:
                with urllib.request.urlopen(req2, context=ssl_ctx, timeout=15) as resp2:
                    html = resp2.read().decode('utf-8', errors='replace')
            except Exception:
                return []
        else:
            return []
    except Exception:
        return []

    # Collect video asset IDs to exclude
    video_ids = set(re.findall(r'macaulaylibrary\.org/video/(\d+)', html))

    # Find photo-gallery entries with their captions
    # Pattern: caption text near the photo-gallery link
    # The HTML structure typically has captions as headings or labels near the photo links
    photos = []
    seen_ids = set()

    # Extract caption + asset ID pairs
    # Look for patterns like: <h4>Adult male</h4>...photo-gallery/ASSET_ID
    # or figcaption/alt text near the asset ID
    blocks = re.split(r'(?=photo-gallery/\d+)', html)

    for block in blocks:
        m = re.search(r'photo-gallery/(\d+)', block)
        if not m:
            continue
        asset_id = m.group(1)
        if asset_id in video_ids or asset_id in seen_ids:
            continue
        seen_ids.add(asset_id)

        # Try to find caption - look backwards in the preceding text
        # Common patterns: "Adult male", "Female", "Juvenile", etc.
        caption = ""
        # Check for heading tags
        cap_match = re.search(r'<h[234][^>]*>([^<]+)</h[234]>', block)
        if not cap_match:
            # Look in the text before this block in the original HTML
            idx = html.find(f'photo-gallery/{asset_id}')
            if idx > 0:
                preceding = html[max(0, idx-500):idx]
                # Find last heading before this link
                caps = re.findall(r'<h[234][^>]*>([^<]+)</h[234]>', preceding)
                if caps:
                    caption = caps[-1].strip()
        else:
            caption = cap_match.group(1).strip()

        # Clean caption
        caption = re.sub(r'<[^>]+>', '', caption).strip()

        photos.append({"asset_id": asset_id, "caption": caption})

    return photos


def download_asset(asset_id: str, dest: Path) -> bool:
    url = CDN_URL.format(asset_id)
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=20) as resp:
            data = resp.read()
            if len(data) < 2000:
                return False
            dest.write_bytes(data)
            return True
    except Exception:
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--min', type=int, default=MIN_IMAGES,
                        help=f'Minimum images per species (default: {MIN_IMAGES})')
    parser.add_argument('--species', type=str, help='Process single species')
    args = parser.parse_args()

    if not SPECIES_INFO.exists():
        print(f"ERROR: {SPECIES_INFO} not found")
        sys.exit(1)

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    with open(SPECIES_INFO) as f:
        all_species = json.load(f)

    # Load existing gallery JSON
    gallery = {}
    if GALLERY_JSON.exists():
        with open(GALLERY_JSON) as f:
            gallery = json.load(f)

    if args.species:
        name = args.species
        if name not in all_species:
            for k in all_species:
                if k.lower() == name.lower():
                    name = k
                    break
            else:
                print(f"Species '{args.species}' not found")
                sys.exit(1)
        species_list = [name]
    else:
        species_list = sorted(all_species.keys())

    total_downloaded = 0
    total_failed = 0
    total_skipped = 0

    for si, species_name in enumerate(species_list, 1):
        safe = sanitize_filename(species_name)
        existing = get_existing_images(safe)

        if len(existing) >= args.min:
            total_skipped += 1
            continue

        need = args.min - len(existing)
        print(f"[{si}/{len(species_list)}] {species_name}: have {len(existing)}, need {need} more")

        # Scrape All About Birds for photo assets
        photos = scrape_photos(species_name)
        if not photos:
            print(f"  ✗ no photos found on All About Birds")
            total_failed += 1
            time.sleep(DELAY)
            continue

        print(f"  found {len(photos)} photos on AAB")

        # Figure out which assets we already have (by checking existing filenames)
        downloaded_this = 0
        used_suffixes = set()

        # Build gallery entry
        gallery_images = []
        # Add existing images first
        for ef in existing:
            gallery_images.append({
                "file": ef,
                "caption": ""  # keep existing captions if in gallery JSON
            })

        for pi, photo in enumerate(photos):
            if downloaded_this >= need:
                break

            asset_id = photo["asset_id"]
            caption = photo["caption"]

            # Generate filename suffix from caption
            suffix = caption_to_suffix(caption, len(existing) + downloaded_this)

            # Avoid filename collisions
            fname = f"{safe}{suffix}.jpg"
            attempt = 0
            while fname in [e for e in existing] or fname in used_suffixes:
                attempt += 1
                fname = f"{safe}{suffix}_{attempt}.jpg"

            # Skip if this would overwrite the cover image (index 0, no suffix)
            if fname == f"{safe}.jpg" and f"{safe}.jpg" in existing:
                continue

            used_suffixes.add(fname)
            dest = IMAGE_DIR / fname

            if download_asset(asset_id, dest):
                size_kb = dest.stat().st_size / 1024
                print(f"  ✓ {fname} — {caption or '(no caption)'} ({size_kb:.0f} KB)")
                downloaded_this += 1
                total_downloaded += 1
                gallery_images.append({
                    "file": fname,
                    "caption": caption
                })
                time.sleep(IMG_DELAY)
            else:
                print(f"  ✗ failed asset {asset_id}")

        # Update gallery JSON for this species
        if gallery_images:
            cover = existing[0] if existing else f"{safe}.jpg"
            gallery[species_name] = {
                "cover": cover,
                "images": gallery_images
            }

        time.sleep(DELAY)

    # Save updated gallery JSON
    with open(GALLERY_JSON, 'w') as f:
        json.dump(gallery, f, indent=2)

    print(f"\n{'='*50}")
    print(f"Done:")
    print(f"  {total_skipped} species already had {args.min}+ images")
    print(f"  {total_downloaded} images downloaded")
    print(f"  {total_failed} species failed (not found on AAB)")
    print(f"  Gallery JSON updated: {GALLERY_JSON}")
    print(f"  Total images: {len(list(IMAGE_DIR.glob('*.jpg'))) + len(list(IMAGE_DIR.glob('*.png')))}")


if __name__ == "__main__":
    main()
