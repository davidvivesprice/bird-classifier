"""
Bird Dashboard API — serves classifier data for the bird observatory dashboard.

Reads classification results from the JSONL log and serves annotated images.
Also provides an annotation/review endpoint for building training data.

Run: uvicorn dashboard.api:app --host 0.0.0.0 --port 8099
"""

import json
import os
import shutil
import time as _time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# --- Paths ---
BASE_DIR = Path("/Users/vives/bird-snapshots")
JSONL_PATH = BASE_DIR / "logs" / "classifications.jsonl"
CLASSIFIED_DIR = BASE_DIR / "classified"
ANNOTATED_DIR = BASE_DIR / "annotated"
SKIPPED_DIR = BASE_DIR / "skipped"
TRASH_DIR = BASE_DIR / "trash"
BACKGROUND_DIR = BASE_DIR / "classified" / "background"
REVIEWS_PATH = Path("/Users/vives/bird-classifier/dashboard/reviews.jsonl")
REGIONAL_SPECIES_PATH = Path("/Users/vives/bird-classifier/models/chilmark_feeder_species.txt")
SPECIES_INFO_PATH = Path("/Users/vives/bird-classifier/dashboard/species_info.json")
SPECIES_IMAGES_DIR = Path("/Users/vives/bird-classifier/dashboard/species_images")
SPECIES_GALLERY_PATH = Path("/Users/vives/bird-classifier/dashboard/species_gallery.json")

# Subspecies / regional forms → canonical parent species
SPECIES_ALIASES = {
    "Slate-colored Junco": "Dark-eyed Junco",
    "Myrtle Warbler": "Yellow-rumped Warbler",
    "Feral Pigeon": "Rock Pigeon",
}

def normalize_species(name: str) -> str:
    return SPECIES_ALIASES.get(name, name)

app = FastAPI(title="Bird Dashboard API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def load_classifications():
    """Load all classification entries from JSONL."""
    if not JSONL_PATH.exists():
        return []
    entries = []
    with open(JSONL_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                e = json.loads(line)
                # Normalize subspecies/forms to canonical parent species
                if "top_prediction" in e and "common_name" in e["top_prediction"]:
                    e["top_prediction"]["common_name"] = normalize_species(e["top_prediction"]["common_name"])
                for b in e.get("birds", []):
                    if "common_name" in b:
                        b["common_name"] = normalize_species(b["common_name"])
                    for t in b.get("top3", []):
                        if "common_name" in t:
                            t["common_name"] = normalize_species(t["common_name"])
                entries.append(e)
    return entries


def load_reviews():
    """Load review verdicts."""
    if not REVIEWS_PATH.exists():
        return {}
    reviews = {}
    with open(REVIEWS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                reviews[r["file"]] = r
    return reviews


def load_regional_species():
    """Load the regional species list."""
    if not REGIONAL_SPECIES_PATH.exists():
        return []
    with open(REGIONAL_SPECIES_PATH) as f:
        return [line.strip() for line in f if line.strip() and line.strip() != "background"]


def filter_by_date(entries, date_str):
    """Filter entries to a specific date (YYYY-MM-DD) using source_timestamp."""
    if not date_str or date_str == "all":
        return entries
    return [e for e in entries if (e.get("source_timestamp") or "")[:10] == date_str]


def filter_by_camera(entries, camera_str):
    """Filter entries by camera name. 'all' or None returns everything."""
    if not camera_str or camera_str == "all":
        return entries
    return [e for e in entries if e.get("camera", "feeder") == camera_str]


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/cameras")
def cameras_list():
    """List cameras with detection counts and last seen times."""
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]

    cam_data = defaultdict(lambda: {"count": 0, "last_seen": ""})
    for e in classified:
        cam = e.get("camera", "feeder")
        cam_data[cam]["count"] += 1
        ts = e.get("source_timestamp") or e.get("timestamp", "")
        if ts > cam_data[cam]["last_seen"]:
            cam_data[cam]["last_seen"] = ts

    return [
        {"name": name, "count": d["count"], "last_seen": d["last_seen"]}
        for name, d in sorted(cam_data.items())
    ]


@app.get("/api/stats")
def stats(date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD, or 'all'"),
          camera: Optional[str] = Query(None, description="Filter by camera: feeder, ground, or all")):
    """Overall classification statistics, optionally filtered by date and camera."""
    entries = load_classifications()
    filtered = filter_by_date(entries, date)
    filtered = filter_by_camera(filtered, camera)
    classified = [e for e in filtered if e["action"] == "classified"]
    skipped = [e for e in filtered if e["action"].startswith("skipped")]
    species = set()
    for e in classified:
        if "top_prediction" in e:
            species.add(e["top_prediction"]["common_name"])

    # Server timezone offset in minutes west of UTC (matches JS getTimezoneOffset convention)
    # e.g. EST=300, EDT=240
    _lt = _time.localtime()
    tz_offset_min = (_time.altzone if _time.daylight and _lt.tm_isdst else _time.timezone) // 60

    return {
        "total": len(filtered),
        "classified": len(classified),
        "skipped": len(skipped),
        "species_count": len(species),
        "last_updated": entries[-1]["timestamp"] if entries else None,
        "server_tz_offset": tz_offset_min,
    }


@app.get("/api/species")
def species_list(date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD, or 'all'"),
                 camera: Optional[str] = Query(None, description="Filter by camera: feeder, ground, or all")):
    """List all detected species with counts and metadata, optionally filtered by date and camera."""
    entries = load_classifications()
    filtered = filter_by_date(entries, date)
    filtered = filter_by_camera(filtered, camera)
    classified = [e for e in filtered if e["action"] == "classified"]

    species_data = defaultdict(lambda: {
        "count": 0,
        "scientific_name": "",
        "last_seen": "",
        "total_confidence": 0,
        "total_score": 0,
        "files": [],
    })

    for e in classified:
        if "top_prediction" not in e:
            continue
        name = e["top_prediction"]["common_name"]
        d = species_data[name]
        d["count"] += 1
        d["scientific_name"] = e["top_prediction"]["scientific_name"]
        d["total_score"] += e["top_prediction"]["raw_score"]
        if e.get("best_detection"):
            d["total_confidence"] += e["best_detection"].get("confidence", 0)
        ts = e.get("source_timestamp") or e["timestamp"]
        if ts > d["last_seen"]:
            d["last_seen"] = ts
        d["files"].append(e["file"])

    result = []
    for name, d in sorted(species_data.items(), key=lambda x: -x[1]["count"]):
        result.append({
            "common_name": name,
            "scientific_name": d["scientific_name"],
            "count": d["count"],
            "last_seen": d["last_seen"],
            "avg_confidence": round(d["total_confidence"] / d["count"], 3) if d["count"] else 0,
            "avg_score": round(d["total_score"] / d["count"], 1) if d["count"] else 0,
        })

    return result


@app.get("/api/species/{name}")
def species_detail(name: str):
    """Detailed data for a single species."""
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]

    detections = []
    for e in classified:
        if "top_prediction" not in e:
            continue
        if e["top_prediction"]["common_name"] == name:
            detections.append({
                "file": e["file"],
                "timestamp": e.get("source_timestamp") or e["timestamp"],
                "confidence": e.get("best_detection", {}).get("confidence", 0),
                "raw_score": e["top_prediction"]["raw_score"],
                "top3": e.get("top3", []),
                "birds": e.get("birds", []),
            })

    if not detections:
        raise HTTPException(status_code=404, detail=f"Species '{name}' not found")

    return {
        "common_name": name,
        "scientific_name": detections[0].get("top3", [{}])[0].get("scientific_name", ""),
        "count": len(detections),
        "detections": detections,
    }


@app.get("/api/recent")
def recent(limit: int = 50, camera: Optional[str] = Query(None, description="Filter by camera: feeder, ground, or all")):
    """Most recent classified detections, optionally filtered by camera."""
    entries = load_classifications()
    entries = filter_by_camera(entries, camera)
    classified = [e for e in entries if e["action"] == "classified"]
    # Most recent first
    classified.sort(key=lambda e: e.get("source_timestamp") or e["timestamp"], reverse=True)
    return classified[:limit]


@app.get("/api/image/{filename}")
def get_image(filename: str):
    """Serve an annotated image (with bounding boxes)."""
    safe_name = os.path.basename(filename)
    path = ANNOTATED_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/api/image-raw/{filename}")
def get_image_raw(filename: str):
    """Serve the original image (no bounding boxes) from classified/ subdirectories."""
    safe_name = os.path.basename(filename)
    # Search through all species subdirectories
    for species_dir in CLASSIFIED_DIR.iterdir():
        if species_dir.is_dir():
            path = species_dir / safe_name
            if path.exists():
                return FileResponse(str(path), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Raw image not found")


@app.get("/api/review/pending")
def review_pending(species: str = ""):
    """Get unreviewed classifications for the annotation GUI."""
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]
    reviews = load_reviews()

    pending = []
    species_set = set()
    for e in classified:
        if e["file"] not in reviews:
            sp = e["top_prediction"]["common_name"] if "top_prediction" in e else "unknown"
            species_set.add(sp)
            if species and sp != species:
                continue
            pending.append({
                "file": e["file"],
                "timestamp": e.get("source_timestamp") or e["timestamp"],
                "species": sp,
                "confidence": e.get("best_detection", {}).get("confidence", 0),
                "raw_score": e.get("top_prediction", {}).get("raw_score", 0),
                "top3": e.get("top3", []),
                "raw_top3": e.get("raw_top3", []),
                "birds": e.get("birds", []),
            })

    total_classified = len(classified)
    total_reviewed = len(reviews)

    return {
        "pending": pending,
        "total_classified": total_classified,
        "total_reviewed": total_reviewed,
        "remaining": len(pending),
        "species_list": sorted(species_set),
    }


@app.post("/api/review/{filename}")
def submit_review(filename: str, verdict: str, correct_species: str = "", missed_birds: str = "false"):
    """Submit a review verdict for a classification."""
    safe_name = os.path.basename(filename)
    if verdict not in ("correct", "wrong", "skip", "trash", "reclassify"):
        raise HTTPException(status_code=400, detail="verdict must be 'correct', 'wrong', 'skip', 'trash', or 'reclassify'")

    correct_species = normalize_species(correct_species) if correct_species else ""

    # Convert string to boolean (FastAPI query params come as strings)
    missed_birds_bool = missed_birds.lower() in ("true", "1", "yes")

    review = {
        "file": safe_name,
        "verdict": verdict,
        "correct_species": correct_species if verdict == "wrong" else "",
        "missed_birds": missed_birds_bool,
        "timestamp": datetime.now().isoformat(),
    }

    with open(REVIEWS_PATH, "a") as f:
        f.write(json.dumps(review) + "\n")

    # Move trashed images out of annotated dir
    if verdict == "trash":
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        src = ANNOTATED_DIR / safe_name
        if src.exists():
            shutil.move(str(src), str(TRASH_DIR / safe_name))

    return {"status": "ok", "review": review}


@app.get("/api/review/classified")
def review_classified(species: str = "", verdict: str = "", limit: int = 50, offset: int = 0):
    """Get reviewed classifications (correct, wrong, reclassify verdicts)."""
    reviews = load_reviews()
    classifications = {e["file"]: e for e in load_classifications() if e["action"] == "classified"}

    items = []
    species_set = set()
    for fname, r in reviews.items():
        if r["verdict"] not in ("correct", "wrong", "reclassify"):
            continue
        cls = classifications.get(fname, {})
        sp = cls.get("top_prediction", {}).get("common_name", "Unknown") if cls else "Unknown"
        species_set.add(sp)
        if species and sp != species:
            continue
        if verdict and r["verdict"] != verdict:
            continue
        items.append({
            "file": fname,
            "species": sp,
            "confidence": cls.get("best_detection", {}).get("confidence", 0) if cls else 0,
            "verdict": r["verdict"],
            "correct_species": r.get("correct_species", ""),
            "missed_birds": r.get("missed_birds", False),
            "review_timestamp": r.get("timestamp", ""),
            "source_timestamp": cls.get("source_timestamp", "") if cls else "",
        })

    items.sort(key=lambda x: x["review_timestamp"], reverse=True)
    total = len(items)
    page = items[offset:offset + limit]
    species_list = sorted(species_set)

    return {"items": page, "total": total, "species_list": species_list}


@app.post("/api/review/{filename}/update")
def update_review(filename: str, verdict: str, correct_species: str = "", missed_birds: str = "false"):
    """Update an existing review verdict (appends new entry, load_reviews picks latest)."""
    safe_name = os.path.basename(filename)
    if verdict not in ("correct", "wrong", "skip", "trash", "reclassify"):
        raise HTTPException(status_code=400, detail="Invalid verdict")

    correct_species = normalize_species(correct_species) if correct_species else ""
    missed_birds_bool = missed_birds.lower() in ("true", "1", "yes")

    review = {
        "file": safe_name,
        "verdict": verdict,
        "correct_species": correct_species if verdict == "wrong" else "",
        "missed_birds": missed_birds_bool,
        "timestamp": datetime.now().isoformat(),
    }

    with open(REVIEWS_PATH, "a") as f:
        f.write(json.dumps(review) + "\n")

    return {"status": "ok", "review": review}


@app.get("/api/dates")
def available_dates():
    """Return list of dates that have classified detections, newest first."""
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]
    dates = set()
    for e in classified:
        ts = e.get("source_timestamp") or ""
        if len(ts) >= 10:
            dates.add(ts[:10])
    return sorted(dates, reverse=True)


@app.get("/api/species-info/{name}")
def species_info(name: str):
    """Return cached species info (description, photos, audio)."""
    if not SPECIES_INFO_PATH.exists():
        raise HTTPException(status_code=404, detail="Species info cache not found")

    with open(SPECIES_INFO_PATH) as f:
        cache = json.load(f)

    # Exact match first
    if name in cache:
        return cache[name]

    # Case-insensitive fallback
    lower = name.lower()
    for key, val in cache.items():
        if key.lower() == lower:
            return val

    raise HTTPException(status_code=404, detail=f"No info for species '{name}'")


import re as _re
import ssl as _ssl
import urllib.request as _urlreq


def _sanitize_species_filename(name: str) -> str:
    """Convert species name to safe filename (matches download_species_images.py)."""
    return _re.sub(r'[^a-zA-Z0-9_-]', '_', name.replace("'", "")).strip('_')


def _find_cached_image(safe: str):
    """Return (path, media_type) if a cached image exists, else None."""
    for ext, media in ((".jpg", "image/jpeg"), (".png", "image/png")):
        path = SPECIES_IMAGES_DIR / f"{safe}{ext}"
        if path.exists() and path.stat().st_size > 500:
            return path, media
    return None


_AAB_NAME_MAP = {
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

_AAB_CDN = "https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{}/1200"
_AAB_UA = "VivesBirdObservatory/1.0 (personal bird dashboard)"


def _download_and_cache(name: str, safe: str):
    """Download species image from All About Birds (Cornell Lab). Returns (path, media_type) or None."""
    try:
        # Build All About Birds URL
        slug = _AAB_NAME_MAP.get(name, name.replace(' ', '_'))
        url = f"https://www.allaboutbirds.org/guide/{_urlreq.quote(slug)}/id"
        req = _urlreq.Request(url, headers={'User-Agent': _AAB_UA})
        try:
            with _urlreq.urlopen(req, timeout=15) as resp:
                html = resp.read().decode('utf-8', errors='replace')
        except Exception:
            # Try without "American " prefix
            if name.startswith("American "):
                slug2 = name.replace("American ", "").replace(' ', '_')
                url2 = f"https://www.allaboutbirds.org/guide/{_urlreq.quote(slug2)}/id"
                req2 = _urlreq.Request(url2, headers={'User-Agent': _AAB_UA})
                with _urlreq.urlopen(req2, timeout=15) as resp2:
                    html = resp2.read().decode('utf-8', errors='replace')
            else:
                return None

        # Find first photo asset ID (skip videos)
        video_ids = set(_re.findall(r'macaulaylibrary\.org/video/(\d+)', html))
        photo_ids = _re.findall(r'/photo-gallery/(\d+)', html)
        asset_id = None
        for pid in photo_ids:
            if pid not in video_ids:
                asset_id = pid
                break
        if not asset_id:
            return None

        # Download from Macaulay Library CDN
        cdn_url = _AAB_CDN.format(asset_id)
        req3 = _urlreq.Request(cdn_url, headers={'User-Agent': _AAB_UA})
        with _urlreq.urlopen(req3, timeout=20) as resp3:
            data = resp3.read()
        if len(data) < 2000:
            return None

        SPECIES_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        dest = SPECIES_IMAGES_DIR / f"{safe}.jpg"
        dest.write_bytes(data)
        return dest, "image/jpeg"
    except Exception:
        return None


@app.get("/api/species-image/{name}")
def species_image(name: str):
    """Serve a locally-cached species image. Downloads from Wikimedia on first access."""
    safe = _sanitize_species_filename(name)

    # Check local cache first
    cached = _find_cached_image(safe)
    if cached:
        path, media = cached
        return FileResponse(
            str(path), media_type=media,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # Not cached — try to download and cache on demand
    result = _download_and_cache(name, safe)
    if result:
        path, media = result
        return FileResponse(
            str(path), media_type=media,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    raise HTTPException(status_code=404, detail=f"No image available for '{name}'")


@app.get("/api/species-gallery/{name}")
def species_gallery(name: str):
    """Return gallery metadata for a species (images + captions)."""
    name = normalize_species(name)
    if not SPECIES_GALLERY_PATH.exists():
        return {"images": []}
    with open(SPECIES_GALLERY_PATH) as f:
        gallery = json.load(f)
    entry = gallery.get(name)
    if not entry:
        return {"images": []}
    # Return images with full API URLs
    images = []
    for img in entry.get("images", []):
        fname = img["file"]
        safe = _sanitize_species_filename(fname.replace(".jpg", "").replace(".png", ""))
        if _find_cached_image(safe):
            images.append({
                "url": f"/api/species-image/{fname.replace('.jpg', '').replace('.png', '')}",
                "caption": img.get("caption", ""),
            })
    return {"images": images}


@app.get("/api/regional-species")
def regional_species():
    """Return the regional species filter list (for the annotation dropdown)."""
    return load_regional_species()


# ──────────────────────────────────────────────────
# Skipped Frame Review
# ──────────────────────────────────────────────────

@app.get("/api/skipped")
def skipped_list(limit: int = 200, offset: int = 0):
    """List user-skipped images (verdict='skip' in reviews), most recent first."""
    reviews = load_reviews()
    classifications = {e["file"]: e for e in load_classifications() if e["action"] == "classified"}

    # Get all user-skipped items, sorted newest first
    skipped = []
    for fname, r in reviews.items():
        if r.get("verdict") != "skip":
            continue
        cls = classifications.get(fname, {})
        species = cls.get("top_prediction", {}).get("common_name", "Unknown") if cls else "Unknown"
        skipped.append({
            "file": fname,
            "species": species,
            "timestamp": r.get("timestamp", ""),
            "source_timestamp": cls.get("source_timestamp", "") if cls else "",
        })

    skipped.sort(key=lambda x: x["timestamp"], reverse=True)
    total = len(skipped)
    page = skipped[offset:offset + limit]

    return {"files": page, "total": total}


def _find_classified_image(filename: str):
    """Find a classified image in any species subdirectory."""
    for species_dir in CLASSIFIED_DIR.iterdir():
        if not species_dir.is_dir():
            continue
        path = species_dir / filename
        if path.exists():
            return path
    return None


def extract_timestamp_from_filename(filename):
    """Extract timestamp from filename, handling camera-prefixed names.

    '2026-03-02_11-10-42.jpg'          → '2026-03-02 11:10:42'
    'feeder_2026-03-14_16-11-09.jpg'   → '2026-03-14 16:11:09'
    'ground_2026-03-14_16-11-09.jpg'   → '2026-03-14 16:11:09'
    """
    try:
        stem = filename.rsplit(".", 1)[0]
        # Strip camera prefix if present (non-date first segment)
        first = stem.split("_", 1)[0]
        if first and not first[:4].isdigit():
            stem = stem.split("_", 1)[1] if "_" in stem else stem
        parts = stem.split("_", 1)
        if len(parts) == 2:
            return parts[0] + " " + parts[1].replace("-", ":")
        return stem
    except Exception:
        return None
