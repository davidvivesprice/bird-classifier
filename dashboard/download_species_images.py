#!/usr/bin/env python3
"""Download all species images from Wikipedia REST API to local cache.

Uses Wikipedia page/summary API for thumbnails (reliable, not rate-limited
like direct Wikimedia Commons 800px URLs). Falls back to species_info.json
image_url if REST API doesn't have a thumbnail.

Usage: python3 dashboard/download_species_images.py
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

DELAY = 1.0  # seconds between requests


def sanitize_filename(name: str) -> str:
    """Convert species name to safe filename."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name.replace("'", "")).strip('_')


def get_wiki_thumbnail(species_name: str, wiki_url: str = None) -> str | None:
    """Get a thumbnail URL from Wikipedia REST API."""
    # Build Wikipedia article title from wiki_url or species name
    if wiki_url:
        title = wiki_url.rstrip('/').split('/')[-1]
    else:
        title = species_name.replace(' ', '_')

    url = f'https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.request.quote(title)}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'VivesBirdObservatory/1.0 (personal bird dashboard; contact david@vives.dev)'
    })
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            data = json.loads(resp.read())
        thumb = data.get('thumbnail', {}).get('source', '')
        if thumb:
            # Upgrade to 600px for better quality
            return re.sub(r'/\d+px-', '/600px-', thumb)
        orig = data.get('originalimage', {}).get('source', '')
        return orig or None
    except Exception:
        return None


def download_image(url: str, dest: Path) -> bool:
    """Download a single image."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'VivesBirdObservatory/1.0 (personal bird dashboard; contact david@vives.dev)'
    })
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
            data = resp.read()
            if len(data) < 1000:
                return False
            dest.write_bytes(data)
            return True
    except Exception as e:
        print(f"  download error: {e}")
        return False


def main():
    if not SPECIES_INFO.exists():
        print(f"ERROR: {SPECIES_INFO} not found")
        sys.exit(1)

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    with open(SPECIES_INFO) as f:
        cache = json.load(f)

    total = len(cache)
    cached = 0
    downloaded = 0
    skipped = 0
    failed = 0
    need_download = []

    # First pass: figure out what we need
    for name, info in cache.items():
        safe = sanitize_filename(name)
        # Check if already cached (.jpg or .png)
        found = False
        for ext in (".jpg", ".png"):
            p = IMAGE_DIR / f"{safe}{ext}"
            if p.exists() and p.stat().st_size > 1000:
                found = True
                break
        if found:
            cached += 1
            continue
        need_download.append((name, info))

    print(f"Total: {total} species, {cached} already cached, {len(need_download)} to download\n")

    for i, (name, info) in enumerate(need_download, 1):
        safe = sanitize_filename(name)

        # Try Wikipedia REST API first (most reliable)
        wiki_url = info.get('wiki_url', '')
        print(f"[{i}/{len(need_download)}] {name}: querying Wikipedia API...")
        thumb_url = get_wiki_thumbnail(name, wiki_url)

        if thumb_url:
            ext = ".png" if ".png" in thumb_url.lower() else ".jpg"
            dest = IMAGE_DIR / f"{safe}{ext}"
            if download_image(thumb_url, dest):
                print(f"  ✓ {dest.name} ({dest.stat().st_size} bytes) via REST API")
                downloaded += 1
                time.sleep(DELAY)
                continue

        # Fallback: try the image_url from species_info.json
        fallback_url = info.get('image_url', '')
        if fallback_url:
            print(f"  trying fallback URL...")
            ext = ".png" if ".png" in fallback_url.lower() else ".jpg"
            dest = IMAGE_DIR / f"{safe}{ext}"
            if download_image(fallback_url, dest):
                print(f"  ✓ {dest.name} ({dest.stat().st_size} bytes) via fallback")
                downloaded += 1
                time.sleep(DELAY)
                continue

        print(f"  ✗ failed")
        failed += 1
        time.sleep(DELAY)

    print(f"\nDone: {cached} previously cached, {downloaded} newly downloaded, {failed} failed")
    print(f"Total images: {cached + downloaded}")
    print(f"Images in {IMAGE_DIR}")


if __name__ == "__main__":
    main()
