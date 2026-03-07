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
REGIONAL_SPECIES_PATH = Path("/Users/vives/bird-classifier/models/cape_cod_species.txt")
SPECIES_INFO_PATH = Path("/Users/vives/bird-classifier/dashboard/species_info.json")
SPECIES_IMAGES_DIR = Path("/Users/vives/bird-classifier/dashboard/species_images")

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
                entries.append(json.loads(line))
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


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/stats")
def stats(date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD, or 'all'")):
    """Overall classification statistics, optionally filtered by date."""
    entries = load_classifications()
    filtered = filter_by_date(entries, date)
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
def species_list(date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD, or 'all'")):
    """List all detected species with counts and metadata, optionally filtered by date."""
    entries = load_classifications()
    filtered = filter_by_date(entries, date)
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
def recent(limit: int = 50):
    """Most recent classified detections."""
    entries = load_classifications()
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
def review_pending():
    """Get unreviewed classifications for the annotation GUI."""
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]
    reviews = load_reviews()

    pending = []
    for e in classified:
        if e["file"] not in reviews:
            pending.append({
                "file": e["file"],
                "timestamp": e.get("source_timestamp") or e["timestamp"],
                "species": e["top_prediction"]["common_name"] if "top_prediction" in e else "unknown",
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
    }


@app.post("/api/review/{filename}")
def submit_review(filename: str, verdict: str, correct_species: str = "", missed_birds: str = "false"):
    """Submit a review verdict for a classification."""
    safe_name = os.path.basename(filename)
    if verdict not in ("correct", "wrong", "skip", "trash", "reclassify"):
        raise HTTPException(status_code=400, detail="verdict must be 'correct', 'wrong', 'skip', 'trash', or 'reclassify'")

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


def _download_and_cache(name: str, safe: str):
    """Download species image via Wikipedia REST API and cache locally. Returns (path, media_type) or None."""
    try:
        # First: try Wikipedia REST API for a reliable thumbnail
        wiki_title = name.replace(' ', '_')
        # Check species_info.json for a better wiki_url
        if SPECIES_INFO_PATH.exists():
            with open(SPECIES_INFO_PATH) as f:
                cache = json.load(f)
            info = cache.get(name)
            if not info:
                for k, v in cache.items():
                    if k.lower() == name.lower():
                        info = v
                        break
            if info and info.get("wiki_url"):
                wiki_title = info["wiki_url"].rstrip('/').split('/')[-1]

        api_url = f'https://en.wikipedia.org/api/rest_v1/page/summary/{_urlreq.quote(wiki_title)}'
        req = _urlreq.Request(api_url, headers={
            'User-Agent': 'VivesBirdObservatory/1.0 (personal bird dashboard)'
        })
        with _urlreq.urlopen(req, timeout=10) as resp:
            api_data = json.loads(resp.read())

        thumb = api_data.get('thumbnail', {}).get('source', '')
        if thumb:
            # Upgrade to 600px
            thumb = _re.sub(r'/\d+px-', '/600px-', thumb)

        img_url = thumb or api_data.get('originalimage', {}).get('source', '')
        if not img_url:
            return None

        ext = ".png" if ".png" in img_url.lower() else ".jpg"
        media = "image/png" if ext == ".png" else "image/jpeg"

        req2 = _urlreq.Request(img_url, headers={
            'User-Agent': 'VivesBirdObservatory/1.0 (personal bird dashboard)'
        })
        with _urlreq.urlopen(req2, timeout=15) as resp2:
            data = resp2.read()
        if len(data) < 1000:
            return None

        SPECIES_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        dest = SPECIES_IMAGES_DIR / f"{safe}{ext}"
        dest.write_bytes(data)
        return dest, media
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
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Not cached — try to download and cache on demand
    result = _download_and_cache(name, safe)
    if result:
        path, media = result
        return FileResponse(
            str(path), media_type=media,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    raise HTTPException(status_code=404, detail=f"No image available for '{name}'")


@app.get("/api/regional-species")
def regional_species():
    """Return the regional species filter list (for the annotation dropdown)."""
    return load_regional_species()


# ──────────────────────────────────────────────────
# Skipped Frame Review
# ──────────────────────────────────────────────────

@app.get("/api/skipped")
def skipped_list(limit: int = 200, offset: int = 0):
    """List skipped images, most recent first."""
    if not SKIPPED_DIR.exists():
        return {"files": [], "total": 0}

    files = sorted(SKIPPED_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
    total = len(files)

    # Load reviews to filter out already-reviewed skipped frames
    reviews = load_reviews()
    skipped_reviews = {r["file"]: r for r in reviews.values() if r.get("source") == "skipped"}

    page = files[offset:offset + limit]
    result = []
    for f in page:
        fname = f.name
        reviewed = fname in skipped_reviews
        result.append({
            "file": fname,
            "timestamp": extract_timestamp_from_filename(fname),
            "reviewed": reviewed,
            "verdict": skipped_reviews[fname]["verdict"] if reviewed else None,
        })

    return {"files": result, "total": total}


@app.get("/api/skipped/image/{filename}")
def get_skipped_image(filename: str):
    """Serve a skipped image."""
    safe_name = os.path.basename(filename)
    path = SKIPPED_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Skipped image not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.post("/api/skipped/{filename}/requeue")
def requeue_skipped(filename: str):
    """Move a skipped image back to incoming/ for re-classification."""
    safe_name = os.path.basename(filename)
    src = SKIPPED_DIR / safe_name
    if not src.exists():
        raise HTTPException(status_code=404, detail="Skipped image not found")

    incoming = BASE_DIR / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    dest = incoming / safe_name
    shutil.move(str(src), str(dest))
    return {"status": "ok", "action": "requeued", "file": safe_name}


@app.post("/api/skipped/{filename}/trash")
def trash_skipped(filename: str):
    """Move a skipped image to trash."""
    safe_name = os.path.basename(filename)
    src = SKIPPED_DIR / safe_name
    if not src.exists():
        raise HTTPException(status_code=404, detail="Skipped image not found")

    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(TRASH_DIR / safe_name))
    return {"status": "ok", "action": "trashed", "file": safe_name}


@app.post("/api/skipped/{filename}/confirm-empty")
def confirm_empty(filename: str):
    """Save a skipped image as a confirmed background/empty training sample."""
    safe_name = os.path.basename(filename)
    src = SKIPPED_DIR / safe_name
    if not src.exists():
        raise HTTPException(status_code=404, detail="Skipped image not found")

    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(BACKGROUND_DIR / safe_name))
    # Remove from skipped after confirming
    src.unlink()
    return {"status": "ok", "action": "confirmed_empty", "file": safe_name}


def extract_timestamp_from_filename(filename):
    """Extract timestamp from filename like 2026-03-02_11-10-42.jpg → '2026-03-02 11:10:42'."""
    try:
        stem = filename.rsplit(".", 1)[0]
        parts = stem.split("_", 1)
        if len(parts) == 2:
            return parts[0] + " " + parts[1].replace("-", ":")
        return stem
    except Exception:
        return None
