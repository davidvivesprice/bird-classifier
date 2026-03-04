#!/usr/bin/env python3
"""
Fetch species info from Wikipedia and Xeno-canto for the bird dashboard.

Reads the regional species list and builds a local JSON cache with:
  - Wikipedia summary + thumbnail
  - Xeno-canto audio recordings (song + call)
  - Conservation status (parsed from Wikipedia)

Usage:
    python dashboard/fetch_species_info.py              # Fetch all species
    python dashboard/fetch_species_info.py --species "Song Sparrow"  # Fetch one
    python dashboard/fetch_species_info.py --refresh    # Re-fetch all (overwrite)
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# --- Paths ---
SPECIES_LIST = Path("/Users/vives/bird-classifier/models/cape_cod_species.txt")
OUTPUT_PATH = Path("/Users/vives/bird-classifier/dashboard/species_info.json")

# --- API Config ---
WIKI_UA = "VivesBirdObservatory/1.0 (personal bird monitoring dashboard)"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
XENO_CANTO_URL = "https://xeno-canto.org/api/3/recordings"
XENO_CANTO_KEY = ""  # Set your Xeno-canto API key here, or pass --xc-key
XENO_DELAY = 1.5  # seconds between Xeno-canto requests (stay well under 1000/hr)

# Conservation status keywords to look for in Wikipedia extracts
CONSERVATION_KEYWORDS = {
    "critically endangered": "Critically Endangered",
    "endangered species": "Endangered",
    "endangered": "Endangered",
    "vulnerable species": "Vulnerable",
    "vulnerable": "Vulnerable",
    "near threatened": "Near Threatened",
    "near-threatened": "Near Threatened",
    "least concern": "Least Concern",
    "least-concern": "Least Concern",
    "conservation dependent": "Conservation Dependent",
}


def load_species_list():
    """Load species from the regional filter file."""
    if not SPECIES_LIST.exists():
        print(f"ERROR: Species list not found at {SPECIES_LIST}")
        sys.exit(1)
    with open(SPECIES_LIST) as f:
        species = [line.strip() for line in f if line.strip() and line.strip() != "background"]
    print(f"Loaded {len(species)} species from {SPECIES_LIST}")
    return species


def load_existing_cache():
    """Load existing cache if present."""
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    """Save cache to disk."""
    with open(OUTPUT_PATH, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(cache)} species to {OUTPUT_PATH}")


def parse_conservation_status(text):
    """Try to extract conservation status from Wikipedia text."""
    if not text:
        return ""
    lower = text.lower()
    for keyword, status in CONSERVATION_KEYWORDS.items():
        if keyword in lower:
            return status
    return ""


def _wiki_search_title(species_name):
    """Use MediaWiki search API to find the best article title for a species."""
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": species_name + " bird",
                "srlimit": 3,
                "format": "json",
            },
            headers={"User-Agent": WIKI_UA},
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json().get("query", {}).get("search", [])
            if results:
                return results[0]["title"].replace(" ", "_")
    except Exception:
        pass
    return None


def fetch_wikipedia(species_name):
    """Fetch species summary and thumbnail from Wikipedia REST API."""
    # Clean up name: strip parenthetical qualifiers like "(American)"
    clean_name = re.sub(r"\s*\([^)]+\)\s*", " ", species_name).strip()

    # Build list of title variants to try
    titles_to_try = []
    title = clean_name.replace(" ", "_")
    titles_to_try.append(title)
    if clean_name != species_name:
        titles_to_try.append(species_name.replace(" ", "_"))
    titles_to_try.append(title + "_(bird)")

    try:
        resp = None
        for t in titles_to_try:
            url = WIKI_SUMMARY_URL.format(title=quote(t, safe=""))
            resp = requests.get(url, headers={"User-Agent": WIKI_UA}, timeout=10)
            if resp.status_code == 200:
                break

        # Fallback: use MediaWiki search API to find the right article
        if resp is None or resp.status_code != 200:
            search_title = _wiki_search_title(clean_name)
            if search_title:
                url = WIKI_SUMMARY_URL.format(title=quote(search_title, safe=""))
                resp = requests.get(url, headers={"User-Agent": WIKI_UA}, timeout=10)

        if resp is None or resp.status_code != 200:
            print(f"  Wikipedia: {resp.status_code if resp else 'no response'} for {species_name}")
            return None

        data = resp.json()

        # Get thumbnail (prefer original size if available)
        image_url = ""
        if "thumbnail" in data:
            image_url = data["thumbnail"].get("source", "")
            # Upgrade to higher resolution (replace /NNNpx- with /800px-)
            image_url = re.sub(r"/\d+px-", "/800px-", image_url)
        elif "originalimage" in data:
            image_url = data["originalimage"].get("source", "")

        summary = data.get("extract", "")
        description = data.get("description", "")
        wiki_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")

        # Try to get scientific name from description
        scientific_name = ""
        if description:
            # Wikipedia descriptions often start with "Species of bird" or contain the scientific name
            sci_match = re.search(r"\(([A-Z][a-z]+ [a-z]+)\)", summary)
            if sci_match:
                scientific_name = sci_match.group(1)

        conservation = parse_conservation_status(summary)

        return {
            "summary": summary,
            "description": description,
            "scientific_name": scientific_name,
            "image_url": image_url,
            "wiki_url": wiki_url,
            "conservation": conservation,
        }

    except Exception as e:
        print(f"  Wikipedia error for {species_name}: {e}")
        return None


def fetch_xeno_canto(species_name, api_key=""):
    """Fetch bird call/song recordings from Xeno-canto API v3."""
    if not api_key:
        return []  # API key required for v3

    # Clean name: strip parenthetical qualifiers
    clean_name = re.sub(r"\s*\([^)]+\)\s*", " ", species_name).strip()

    try:
        # API v3 uses tag-based queries: en:"English Name" q_gt:C
        query = f'en:"{clean_name}" q_gt:C'
        resp = requests.get(
            XENO_CANTO_URL,
            params={"query": query, "key": api_key},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  Xeno-canto: {resp.status_code} for {species_name}")
            return []

        data = resp.json()
        recordings = data.get("recordings", [])

        if not recordings:
            # Try without quality filter
            resp = requests.get(
                XENO_CANTO_URL,
                params={"query": f'en:"{clean_name}"', "key": api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                recordings = resp.json().get("recordings", [])

        if not recordings:
            print(f"  Xeno-canto: no recordings for {species_name}")
            return []

        # Sort by quality (A first, then B, then C)
        quality_order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
        recordings.sort(key=lambda r: quality_order.get(r.get("q", "E"), 5))

        # Pick best song and best call
        audio = []
        found_types = set()

        for rec in recordings:
            rec_type = rec.get("type", "").lower()
            # Categorize: "song" or "call"
            if "song" in rec_type and "song" not in found_types:
                category = "song"
            elif ("call" in rec_type or "alarm" in rec_type) and "call" not in found_types:
                category = "call"
            elif not found_types:
                # Take the first good recording regardless of type
                category = "song" if "song" not in found_types else "call"
            else:
                continue

            if category in found_types:
                continue

            # Build the audio file URL
            # Xeno-canto file URLs: https://xeno-canto.org/sounds/uploaded/{dir}/{filename}
            file_url = rec.get("file", "")
            if not file_url and rec.get("sono", {}).get("small"):
                # Fallback: derive from sonogram URL
                pass

            if file_url:
                # Ensure HTTPS
                if file_url.startswith("//"):
                    file_url = "https:" + file_url

                audio.append({
                    "url": file_url,
                    "type": category,
                    "recordist": rec.get("rec", "Unknown"),
                    "quality": rec.get("q", "?"),
                    "length": rec.get("length", ""),
                    "xc_id": rec.get("id", ""),
                    "license": rec.get("lic", ""),
                })
                found_types.add(category)

            if len(found_types) >= 2:
                break

        return audio

    except Exception as e:
        print(f"  Xeno-canto error for {species_name}: {e}")
        return []


def fetch_species(species_name, existing=None, xc_key=""):
    """Fetch all info for a single species."""
    print(f"Fetching: {species_name}")

    # Wikipedia
    wiki = fetch_wikipedia(species_name)

    if not wiki:
        wiki = {
            "summary": "",
            "description": "",
            "scientific_name": "",
            "image_url": "",
            "wiki_url": "",
            "conservation": "",
        }

    # Xeno-canto (with delay to respect rate limits)
    if xc_key:
        time.sleep(XENO_DELAY)
    audio = fetch_xeno_canto(species_name, api_key=xc_key)

    # If we have existing data and Wikipedia returned a scientific name, prefer it.
    # Otherwise try to get it from existing classifier data.
    scientific_name = wiki.get("scientific_name", "")
    if not scientific_name and existing:
        scientific_name = existing.get("scientific_name", "")

    entry = {
        "common_name": species_name,
        "scientific_name": scientific_name,
        "summary": wiki["summary"],
        "image_url": wiki["image_url"],
        "image_credit": "Wikimedia Commons (CC BY-SA)" if wiki["image_url"] else "",
        "conservation": wiki["conservation"],
        "audio": audio,
        "wiki_url": wiki["wiki_url"],
        "fetched_at": datetime.now().isoformat(),
    }

    status = []
    if wiki["summary"]:
        status.append("wiki")
    if wiki["image_url"]:
        status.append("photo")
    if audio:
        status.append(f"{len(audio)} audio")
    if wiki["conservation"]:
        status.append(wiki["conservation"])
    print(f"  → {', '.join(status) or 'no data'}")

    return entry


def main():
    parser = argparse.ArgumentParser(description="Fetch species info for bird dashboard")
    parser.add_argument("--species", type=str, help="Fetch a single species by name")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch all species (overwrite cache)")
    parser.add_argument("--xc-key", type=str, default="", help="Xeno-canto API key (free, from xeno-canto.org account)")
    args = parser.parse_args()

    xc_key = args.xc_key or XENO_CANTO_KEY
    if not xc_key:
        print("NOTE: No Xeno-canto API key provided. Audio will be skipped.")
        print("  Get a free key at: https://xeno-canto.org (create account → verify email → account page)")
        print("  Then run with: --xc-key YOUR_KEY")
        print()

    cache = load_existing_cache()

    if args.species:
        # Fetch single species
        entry = fetch_species(args.species, cache.get(args.species), xc_key=xc_key)
        cache[args.species] = entry
        save_cache(cache)
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return

    # Fetch all species
    species_list = load_species_list()
    fetched = 0
    skipped = 0
    errors = 0

    for sp in species_list:
        if not args.refresh and sp in cache and cache[sp].get("summary"):
            skipped += 1
            continue

        try:
            entry = fetch_species(sp, cache.get(sp), xc_key=xc_key)
            cache[sp] = entry
            fetched += 1

            # Save periodically (every 10 species)
            if fetched % 10 == 0:
                save_cache(cache)
                print(f"  [checkpoint: {fetched} fetched, {skipped} skipped]")

        except Exception as e:
            print(f"  ERROR: {sp}: {e}")
            errors += 1

    save_cache(cache)
    print(f"\nDone: {fetched} fetched, {skipped} skipped (already cached), {errors} errors")
    print(f"Total species in cache: {len(cache)}")

    # Stats
    with_summary = sum(1 for v in cache.values() if v.get("summary"))
    with_image = sum(1 for v in cache.values() if v.get("image_url"))
    with_audio = sum(1 for v in cache.values() if v.get("audio"))
    with_conservation = sum(1 for v in cache.values() if v.get("conservation"))
    print(f"Coverage: {with_summary} summaries, {with_image} photos, {with_audio} with audio, {with_conservation} conservation status")


if __name__ == "__main__":
    main()
