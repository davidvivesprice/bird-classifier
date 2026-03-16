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
CULL_CONFIG_PATH = Path("/Users/vives/bird-classifier/config/cull_config.json")

# Subspecies / regional forms → canonical parent species
SPECIES_ALIASES = {
    "Slate-colored Junco": "Dark-eyed Junco",
    "Myrtle Warbler": "Yellow-rumped Warbler",
    "Feral Pigeon": "Rock Pigeon",
}

def normalize_species(name: str) -> str:
    return SPECIES_ALIASES.get(name, name)

app = FastAPI(title="Bird Dashboard API", version="1.0")


@app.on_event("startup")
def warm_cache():
    """Pre-load JSONL caches on startup so the first request is fast."""
    import logging
    t0 = _time.time()
    entries = load_classifications()
    reviews = load_reviews()
    t1 = _time.time()
    logging.info("Cache warmed: %d classifications, %d reviews in %.1fs", len(entries), len(reviews), t1 - t0)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


_classifications_cache: list = []
_classifications_size: int = 0  # byte offset — only read new bytes on append


def _normalize_entry(e: dict) -> dict:
    """Normalize species names in a classification entry."""
    if "top_prediction" in e and "common_name" in e["top_prediction"]:
        e["top_prediction"]["common_name"] = normalize_species(e["top_prediction"]["common_name"])
    for b in e.get("birds", []):
        if "common_name" in b:
            b["common_name"] = normalize_species(b["common_name"])
        for t in b.get("top3", []):
            if "common_name" in t:
                t["common_name"] = normalize_species(t["common_name"])
    return e


def load_classifications():
    """Load classification entries from JSONL with incremental append caching.

    Only reads bytes added since the last load.  If the file shrinks (truncated
    or replaced), does a full reload.  This keeps the common case — classifier
    appending new entries — down to microseconds instead of 7+ seconds.
    """
    global _classifications_cache, _classifications_size
    if not JSONL_PATH.exists():
        _classifications_cache = []
        _classifications_size = 0
        return []
    st = JSONL_PATH.stat()
    current_size = st.st_size

    # No change
    if current_size == _classifications_size:
        return _classifications_cache

    # File shrunk or replaced — full reload
    if current_size < _classifications_size:
        _classifications_cache = []
        _classifications_size = 0

    # Read only new bytes from the last known position
    new_entries = []
    with open(JSONL_PATH, "rb") as f:
        f.seek(_classifications_size)
        for line in f:
            line = line.strip()
            if line:
                try:
                    e = json.loads(line)
                    new_entries.append(_normalize_entry(e))
                except json.JSONDecodeError:
                    pass  # skip partial/corrupt lines

    _classifications_cache.extend(new_entries)
    _classifications_size = current_size
    return _classifications_cache


_reviews_cache: dict = {}
_reviews_size: int = 0


def load_reviews():
    """Load review verdicts with incremental append caching."""
    global _reviews_cache, _reviews_size
    if not REVIEWS_PATH.exists():
        _reviews_cache = {}
        _reviews_size = 0
        return {}
    st = REVIEWS_PATH.stat()
    current_size = st.st_size

    if current_size == _reviews_size:
        return _reviews_cache

    if current_size < _reviews_size:
        _reviews_cache = {}
        _reviews_size = 0

    with open(REVIEWS_PATH, "rb") as f:
        f.seek(_reviews_size)
        for line in f:
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    _reviews_cache[r["file"]] = r
                except (json.JSONDecodeError, KeyError):
                    pass

    _reviews_size = current_size
    return _reviews_cache


_regional_cache: list = []
_regional_mtime: float = 0.0


def load_regional_species():
    """Load the regional species list (cached until file changes)."""
    global _regional_cache, _regional_mtime
    if not REGIONAL_SPECIES_PATH.exists():
        return []
    mt = REGIONAL_SPECIES_PATH.stat().st_mtime
    if mt == _regional_mtime and _regional_cache:
        return _regional_cache
    with open(REGIONAL_SPECIES_PATH) as f:
        _regional_cache = [line.strip() for line in f if line.strip() and line.strip() != "background"]
    _regional_mtime = mt
    return _regional_cache


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
def review_pending(species: str = "", offset: int = 0, limit: int = 50, multibird: str = ""):
    """Get unreviewed classifications for the annotation GUI (paginated).

    Returns `limit` items starting from `offset`.  The full count and
    species list are always included so the UI can show progress and
    the species filter without needing all items up-front.
    """
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]
    reviews = load_reviews()

    pending = []
    species_set = set()
    for e in classified:
        if e["file"] not in reviews or reviews[e["file"]]["verdict"] == "requeued":
            sp = e["top_prediction"]["common_name"] if "top_prediction" in e else "unknown"
            species_set.add(sp)
            if species and sp != species:
                continue
            if multibird and len(e.get("birds", [])) < 2:
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
    total_pending = len(pending)

    # Paginate: only return the requested slice
    page = pending[offset:offset + limit]

    return {
        "pending": page,
        "total_classified": total_classified,
        "total_reviewed": total_reviewed,
        "remaining": total_pending,
        "species_list": sorted(species_set),
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total_pending,
    }


INCOMING_DIR = BASE_DIR / "incoming"


@app.get("/api/review/rerun-count")
def rerun_count():
    """Count files flagged with verdict=reclassify (missed birds)."""
    reviews = load_reviews()
    count = sum(1 for r in reviews.values() if r["verdict"] == "reclassify")
    return {"count": count}


@app.post("/api/review/rerun-missed")
def rerun_missed():
    """Move all reclassify-flagged files back to incoming/ for reprocessing.

    For each file with verdict=reclassify:
    1. Find in classified/*/ → move to incoming/
    2. Delete annotated version (new one will be generated)
    3. Write verdict=requeued entry so it shows as pending after re-classification
    """
    reviews = load_reviews()
    flagged = [f for f, r in reviews.items() if r["verdict"] == "reclassify"]

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    not_found = 0

    for fname in flagged:
        # Find in classified subdirectories
        src = None
        for species_dir in CLASSIFIED_DIR.iterdir():
            if species_dir.is_dir():
                candidate = species_dir / fname
                if candidate.exists():
                    src = candidate
                    break

        if src:
            dst = INCOMING_DIR / fname
            shutil.move(str(src), str(dst))
            # Remove annotated version
            ann = ANNOTATED_DIR / fname
            if ann.exists():
                ann.unlink()
            moved += 1
        else:
            not_found += 1

        # Write requeued verdict so pending filter picks it up after reclassification
        requeue_entry = {
            "file": fname,
            "verdict": "requeued",
            "correct_species": "",
            "missed_birds": False,
            "bird_index": 0,
            "timestamp": datetime.now().isoformat(),
        }
        with open(REVIEWS_PATH, "a") as f:
            f.write(json.dumps(requeue_entry) + "\n")

    return {
        "moved": moved,
        "not_found": not_found,
        "message": f"Requeued {moved} files for reclassification" + (f" ({not_found} not found on disk)" if not_found else ""),
    }


@app.get("/api/review/goals")
def review_goals(threshold: int = 20):
    """Species classification goals — which species need more confirmed reviews for training.

    Counts confirmed reviews per species (correct → classified species,
    wrong with correct_species → corrected species).  Returns species from
    the regional list that have at least 1 but fewer than `threshold` confirmed shots.
    """
    reviews = load_reviews()
    regional = set(load_regional_species())

    confirmed: dict[str, int] = defaultdict(int)
    for r in reviews.values():
        if r["verdict"] == "correct":
            # Need to look up the classified species for this file
            pass  # handled below
        elif r["verdict"] == "wrong" and r.get("correct_species"):
            sp = normalize_species(r["correct_species"])
            confirmed[sp] += 1

    # For correct verdicts, look up species from classifications
    entries = load_classifications()
    file_species: dict[str, str] = {}
    for e in entries:
        if e.get("action") == "classified" and "top_prediction" in e:
            file_species[e["file"]] = e["top_prediction"]["common_name"]

    for r in reviews.values():
        if r["verdict"] == "correct":
            sp = file_species.get(r["file"], "")
            if sp:
                confirmed[sp] += 1

    goals = []
    for sp in regional:
        count = confirmed.get(sp, 0)
        if count > 0 and count < threshold:
            goals.append({
                "species": sp,
                "confirmed": count,
                "target": threshold,
                "complete": round(count / threshold * 100),
            })
        elif count >= threshold:
            goals.append({
                "species": sp,
                "confirmed": count,
                "target": threshold,
                "complete": 100,
            })

    # Sort: furthest from goal first (completed at bottom)
    goals.sort(key=lambda g: (g["complete"] >= 100, g["complete"]))

    return {
        "goals": goals,
        "threshold": threshold,
        "total_species_with_data": sum(1 for g in goals if g["confirmed"] > 0),
        "total_species_complete": sum(1 for g in goals if g["complete"] >= 100),
    }


@app.post("/api/review/{filename}")
def submit_review(filename: str, verdict: str, correct_species: str = "", missed_birds: str = "false", bird_index: str = "0"):
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
        "bird_index": int(bird_index),
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
def update_review(filename: str, verdict: str, correct_species: str = "", missed_birds: str = "false", bird_index: str = "0"):
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
        "bird_index": int(bird_index),
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


# ──────────────────────────────────────────────────
# BirdNET Audio Detection Endpoints
# Replaces: NAS birdnet_sse.py, export_birdnet.sh, summary.json
# ──────────────────────────────────────────────────

import asyncio
import sqlite3
import threading

BIRDNET_DB_PATH = Path(os.path.expanduser("~/bird-snapshots/birdnet-audio/birdnet_local.db"))
BIRDNET_CLIPS_DIR = Path(os.path.expanduser("~/bird-snapshots/birdnet-audio/clips"))

# Cache for birdnet summary (regenerated when DB changes)
_birdnet_summary_cache = None
_birdnet_summary_mtime = 0.0
_birdnet_last_id = 0  # for SSE polling


def _birdnet_db():
    """Get a read-only SQLite connection to the BirdNET database."""
    if not BIRDNET_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(BIRDNET_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _birdnet_tz_offset():
    """Return server timezone offset in minutes west of UTC."""
    lt = _time.localtime()
    return (_time.altzone if _time.daylight and lt.tm_isdst else _time.timezone) // 60


@app.get("/api/birdnet-summary")
def birdnet_summary():
    """BirdNET audio detection summary — replaces static summary.json.

    Returns species counts (all-time + per-date), recent detections, and metadata.
    """
    global _birdnet_summary_cache, _birdnet_summary_mtime

    # Cache for 30 seconds
    now = _time.time()
    if _birdnet_summary_cache and (now - _birdnet_summary_mtime) < 30:
        return _birdnet_summary_cache

    conn = _birdnet_db()
    if not conn:
        return {"total_detections": 0, "species_count": 0, "species": [],
                "by_date": {}, "dates": [], "recent": [], "tz_offset": _birdnet_tz_offset()}

    try:
        cur = conn.cursor()

        # All-time species counts
        cur.execute("""
            SELECT common_name, scientific_name,
                   COUNT(*) as count,
                   ROUND(AVG(confidence), 3) as avg_confidence,
                   MAX(date || ' ' || time) as last_seen
            FROM notes
            GROUP BY common_name
            ORDER BY count DESC
        """)
        species = []
        for row in cur.fetchall():
            species.append({
                "common_name": normalize_species(row["common_name"]),
                "scientific_name": row["scientific_name"],
                "count": row["count"],
                "avg_confidence": row["avg_confidence"],
                "last_seen": row["last_seen"],
            })

        # Per-date breakdowns
        cur.execute("""
            SELECT date, common_name, scientific_name,
                   COUNT(*) as count,
                   ROUND(AVG(confidence), 3) as avg_confidence
            FROM notes
            GROUP BY date, common_name
            ORDER BY date DESC, count DESC
        """)
        by_date = {}
        for row in cur.fetchall():
            d = row["date"]
            if d not in by_date:
                by_date[d] = {"species": [], "total_detections": 0, "species_count": 0}
            by_date[d]["species"].append({
                "common_name": normalize_species(row["common_name"]),
                "scientific_name": row["scientific_name"],
                "count": row["count"],
                "avg_confidence": row["avg_confidence"],
            })
            by_date[d]["total_detections"] += row["count"]

        for d in by_date:
            by_date[d]["species_count"] = len(by_date[d]["species"])

        dates = sorted(by_date.keys(), reverse=True)

        # Total counts
        cur.execute("SELECT COUNT(*) as total, COUNT(DISTINCT common_name) as species FROM notes")
        totals = cur.fetchone()

        # Recent 200 detections (for "In the Yard" panel)
        cur.execute("""
            SELECT common_name, confidence, date || ' ' || time as time, clip_name
            FROM notes
            WHERE date >= date('now', 'localtime', '-1 day')
            ORDER BY id DESC
            LIMIT 200
        """)
        recent = []
        for row in cur.fetchall():
            recent.append({
                "species": normalize_species(row["common_name"]),
                "confidence": row["confidence"],
                "time": row["time"],
                "clip_name": row["clip_name"] or "",
            })

        result = {
            "total_detections": totals["total"],
            "species_count": totals["species"],
            "species": species,
            "by_date": by_date,
            "dates": dates,
            "recent": recent,
            "tz_offset": _birdnet_tz_offset(),
        }

        _birdnet_summary_cache = result
        _birdnet_summary_mtime = now
        return result

    finally:
        conn.close()


from starlette.responses import StreamingResponse


@app.get("/api/birdnet-events")
async def birdnet_events():
    """Server-Sent Events stream for real-time BirdNET audio detections.

    Polls the local SQLite DB every 3 seconds for new rows and pushes them
    as SSE events. Replaces the separate birdnet_sse.py process.
    """
    global _birdnet_last_id

    # Initialize last_id from DB if needed
    if _birdnet_last_id == 0:
        conn = _birdnet_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT MAX(id) FROM notes")
                row = cur.fetchone()
                _birdnet_last_id = row[0] or 0
            finally:
                conn.close()

    async def event_stream():
        global _birdnet_last_id
        last_id = _birdnet_last_id
        heartbeat_counter = 0

        yield "data: {\"type\": \"connected\"}\n\n"

        while True:
            await asyncio.sleep(3)
            heartbeat_counter += 1

            # Send heartbeat every 15 seconds (5 cycles)
            if heartbeat_counter % 5 == 0:
                yield ": heartbeat\n\n"

            conn = _birdnet_db()
            if not conn:
                continue

            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, common_name, scientific_name, confidence,
                           date, time, clip_name
                    FROM notes
                    WHERE id > ?
                    ORDER BY id ASC
                """, (last_id,))

                for row in cur.fetchall():
                    det_id = row["id"]
                    # Build ISO timestamp with explicit timezone
                    naive_ts = f"{row['date']}T{row['time']}"
                    tz_off = _birdnet_tz_offset()
                    tz_sign = "-" if tz_off >= 0 else "+"
                    tz_hours = abs(tz_off) // 60
                    tz_mins = abs(tz_off) % 60
                    iso_time = f"{naive_ts}{tz_sign}{tz_hours:02d}:{tz_mins:02d}"

                    event = {
                        "id": det_id,
                        "common_name": normalize_species(row["common_name"]),
                        "scientific_name": row["scientific_name"],
                        "confidence": round(row["confidence"], 3),
                        "date": row["date"],
                        "time": row["time"],
                        "iso_time": iso_time,
                        "clip_name": row["clip_name"] or "",
                    }
                    yield f"data: {json.dumps(event)}\n\n"
                    last_id = det_id
                    _birdnet_last_id = det_id

                    # Invalidate summary cache on new detection
                    global _birdnet_summary_mtime
                    _birdnet_summary_mtime = 0

            except Exception:
                pass
            finally:
                conn.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/birdnet-clip/{clip_path:path}")
def birdnet_clip(clip_path: str):
    """Serve a BirdNET audio clip (WAV file)."""
    # Sanitize path to prevent directory traversal
    safe_path = Path(clip_path)
    if ".." in safe_path.parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    full_path = BIRDNET_CLIPS_DIR / safe_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")

    return FileResponse(
        str(full_path),
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Culling System ──

def load_cull_config() -> dict:
    """Load cull config from JSON file, returning defaults if missing."""
    defaults = {"default_max_keep": 100, "species_caps": {}, "sufficient_species": []}
    if CULL_CONFIG_PATH.exists():
        try:
            with open(CULL_CONFIG_PATH) as f:
                cfg = json.load(f)
            return {**defaults, **cfg}
        except Exception:
            pass
    return defaults


def save_cull_config(cfg: dict):
    """Write cull config to JSON file."""
    CULL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CULL_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


@app.get("/api/cull/config")
def get_cull_config():
    """Read current cull configuration."""
    return load_cull_config()


@app.post("/api/cull/config")
def update_cull_config(
    default_max_keep: Optional[int] = None,
    species_caps: Optional[str] = None,
    sufficient_species: Optional[str] = None,
):
    """Update cull configuration.

    Parameters are optional — only provided fields are updated.
    species_caps and sufficient_species are JSON strings.
    """
    cfg = load_cull_config()
    if default_max_keep is not None:
        cfg["default_max_keep"] = default_max_keep
    if species_caps is not None:
        cfg["species_caps"] = json.loads(species_caps)
    if sufficient_species is not None:
        cfg["sufficient_species"] = json.loads(sufficient_species)
    save_cull_config(cfg)
    return {"status": "ok", "config": cfg}


@app.get("/api/cull/inventory")
def cull_inventory():
    """Per-species file counts on disk + confirmed review counts."""
    reviews = load_reviews()
    entries = load_classifications()

    # Build file→species map from classifications
    file_species: dict[str, str] = {}
    for e in entries:
        if e.get("action") == "classified" and "top_prediction" in e:
            file_species[e["file"]] = e["top_prediction"]["common_name"]

    # Count confirmed reviews per species
    confirmed: dict[str, int] = defaultdict(int)
    for r in reviews.values():
        if r["verdict"] == "correct":
            sp = file_species.get(r["file"], "")
            if sp:
                confirmed[sp] += 1
        elif r["verdict"] == "wrong" and r.get("correct_species"):
            confirmed[normalize_species(r["correct_species"])] += 1

    # Count files on disk per species directory
    cfg = load_cull_config()
    inventory = []
    if CLASSIFIED_DIR.exists():
        for species_dir in sorted(CLASSIFIED_DIR.iterdir()):
            if species_dir.is_dir() and species_dir.name != "background":
                sp_name = species_dir.name.replace("_", " ")
                files = list(species_dir.glob("*.jpg"))
                cap = cfg["species_caps"].get(sp_name, cfg["default_max_keep"])
                inventory.append({
                    "species": sp_name,
                    "dir_name": species_dir.name,
                    "file_count": len(files),
                    "confirmed": confirmed.get(sp_name, 0),
                    "cap": cap,
                    "over_cap": max(0, len(files) - cap),
                    "sufficient": sp_name in cfg.get("sufficient_species", []),
                })

    inventory.sort(key=lambda x: x["file_count"], reverse=True)
    return {"inventory": inventory, "config": cfg}


@app.post("/api/cull/trash-species")
def cull_trash_species(species_dir: str, keep: int = 50, sort_by: str = "date"):
    """Bulk trash files for a species, keeping the best N.

    sort_by: "date" (keep newest) or "confidence" (keep highest scoring).
    Also removes corresponding annotated versions.
    """
    safe_dir = os.path.basename(species_dir)
    src_dir = CLASSIFIED_DIR / safe_dir
    if not src_dir.exists() or not src_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Species directory '{safe_dir}' not found")

    if keep < 0:
        raise HTTPException(status_code=400, detail="keep must be >= 0")

    if sort_by == "confidence":
        # Build filename→score map from JSONL
        entries = load_classifications()
        file_scores: dict[str, int] = {}
        for e in entries:
            if e.get("action") == "classified" and "top_prediction" in e:
                file_scores[e["file"]] = e["top_prediction"].get("raw_score", 0)
        # Sort by score (highest first), ties broken by mtime (newest first)
        files = sorted(
            src_dir.glob("*.jpg"),
            key=lambda f: (file_scores.get(f.name, 0), f.stat().st_mtime),
            reverse=True,
        )
        sort_label = "highest confidence"
    else:
        # Sort by modification time (newest first)
        files = sorted(src_dir.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
        sort_label = "newest"

    if len(files) <= keep:
        return {"trashed": 0, "kept": len(files), "message": f"Only {len(files)} files — nothing to trash"}

    to_trash = files[keep:]
    TRASH_DIR.mkdir(parents=True, exist_ok=True)

    trashed = 0
    for f in to_trash:
        dst = TRASH_DIR / f.name
        shutil.move(str(f), str(dst))
        # Remove annotated version
        ann = ANNOTATED_DIR / f.name
        if ann.exists():
            ann.unlink()
        trashed += 1

    return {
        "trashed": trashed,
        "kept": keep,
        "message": f"Trashed {trashed} {safe_dir.replace('_', ' ')} files, kept {keep} {sort_label}",
    }
