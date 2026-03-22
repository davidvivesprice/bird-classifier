"""
Bird Dashboard API — serves classifier data for the bird observatory dashboard.

Phase 3: all data (classifications + reviews) served from SQLite.
No in-memory caches — RAM usage minimal.
JSONL backup still written for reviews during transition.

Run: uvicorn dashboard.api:app --host 0.0.0.0 --port 8099
"""

import fcntl
import json
import logging
import os
import re
import shutil
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta as _timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# Phase 2: direct SQL queries replace in-memory cache
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import classifications_db as cdb
import reviews_db as rdb
from bird_inference import SPECIES_ALIASES, normalize_species

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

app = FastAPI(title="Bird Dashboard API", version="1.0")


@app.on_event("startup")
def warm_cache():
    """Verify SQLite DB is accessible on startup."""
    import logging
    t0 = _time.time()
    cdb.init_db()
    total = cdb.count_total()
    species = cdb.count_species()
    review_count = rdb.count_reviews()
    t1 = _time.time()
    logging.info("Startup: SQLite DB has %d entries (%d species), %d reviews in %.1fs",
                 total, species, review_count, t1 - t0)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Atomic JSONL writer ──

def _append_jsonl(path: Path, entry: dict):
    """Append a JSON entry to a JSONL file with exclusive locking."""
    line = json.dumps(entry) + "\n"
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ── Classification data: Phase 2 — all queries go directly to SQLite via cdb ──
# No in-memory cache. RAM usage drops from ~1.2GB to near zero.
# See classifications_db.py for all query functions.


# ── Reviews: now served from SQLite via reviews_db (rdb) ──
# JSONL backup still written via _append_jsonl for transition safety.


# ── Result cache with TTL ──

_result_cache: dict[str, tuple[float, object]] = {}


def cached_result(key: str, ttl: float, fn):
    """Return cached result if fresh, otherwise compute and cache."""
    now = _time.time()
    if key in _result_cache and _result_cache[key][0] > now:
        return _result_cache[key][1]
    result = fn()
    _result_cache[key] = (now + ttl, result)
    return result


def invalidate_cache(*prefixes):
    """Invalidate result cache entries matching any prefix."""
    keys_to_drop = [k for k in _result_cache if any(k.startswith(p) for p in prefixes)]
    for k in keys_to_drop:
        del _result_cache[k]


# ── Shared helpers ──

VALID_VERDICTS = frozenset(("correct", "wrong", "skip", "trash", "reclassify"))


def _create_review_entry(filename: str, verdict: str, correct_species: str = "",
                         missed_birds: str = "false", bird_index: str = "0") -> dict:
    """Build and validate a review entry dict."""
    safe_name = os.path.basename(filename)
    if verdict not in VALID_VERDICTS:
        raise HTTPException(status_code=400, detail=f"verdict must be one of {', '.join(sorted(VALID_VERDICTS))}")
    correct_species = normalize_species(correct_species) if correct_species else ""
    return {
        "file": safe_name,
        "verdict": verdict,
        "correct_species": correct_species if verdict == "wrong" else "",
        "missed_birds": missed_birds.lower() in ("true", "1", "yes"),
        "bird_index": int(bird_index),
        "timestamp": datetime.now().isoformat(),
    }


def _find_classified_file(filename: str) -> Path | None:
    """Find a classified file across species subdirectories."""
    safe = os.path.basename(filename)
    for d in CLASSIFIED_DIR.iterdir():
        if d.is_dir():
            p = d / safe
            if p.exists():
                return p
    return None


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


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(date_str: str | None) -> str | None:
    """Validate date parameter format. Returns the date string or None."""
    if not date_str or date_str == "all":
        return date_str
    if not _DATE_RE.match(date_str):
        raise HTTPException(status_code=400, detail=f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD or 'all'.")
    return date_str



@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/test")
def serve_test_page():
    """Serve the API smoke test page."""
    test_path = Path(__file__).parent / "test.html"
    if not test_path.exists():
        raise HTTPException(status_code=404, detail="test.html not found")
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=test_path.read_text(), status_code=200)


# ── System Health Aggregator ──────────────────────────────────────────────
import ssl
import sqlite3 as _sqlite3
import urllib.request as _urllib_request

_health_cache = {"data": None, "time": 0}
_HEALTH_CACHE_TTL = 10  # seconds
_HEALTH_TIMEOUT = 3     # per-service timeout

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _fetch_service(url, name):
    """Fetch a service's /metrics or /health endpoint. Returns parsed JSON or error dict."""
    try:
        req = _urllib_request.Request(url)
        with _urllib_request.urlopen(req, timeout=_HEALTH_TIMEOUT) as resp:
            data = json.loads(resp.read())
            return {"status": "ok", **data}
    except Exception as e:
        return {
            "status": "error",
            "detail": f"{name} unreachable ({e})",
            "error": str(e),
        }


def _check_audio_analyzer_health():
    """Check audio_analyzer via metrics endpoint + DB for detection counts."""
    # Try metrics endpoint first (includes full metrics data)
    metrics = _fetch_service("http://localhost:8098/metrics", "Audio Analyzer")

    # Augment with DB detection counts
    db_path = Path(os.path.expanduser("~/bird-snapshots/birdnet-audio/birdnet_local.db"))
    try:
        if db_path.exists():
            conn = _sqlite3.connect(str(db_path), timeout=2)
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), MAX(date || ' ' || time) FROM notes WHERE date = ?",
                        (datetime.now().strftime("%Y-%m-%d"),))
            row = cur.fetchone()
            conn.close()
            metrics["detections_today"] = row[0] or 0
            metrics["last_detection"] = row[1] or "none"
    except Exception:
        pass

    # Build detail string
    if metrics.get("status") == "ok":
        today = metrics.get("detections_today", 0)
        last = metrics.get("last_detection", "none")
        metrics["detail"] = f"Running, {today} detections today, last: {last}"
    return metrics


def _check_nas():
    """Check NAS reachability via /healthz endpoint.

    Uses curl subprocess because Python urllib is blocked by macOS Application
    Firewall when running from LaunchAgent (unsigned binary restriction).
    """
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-sk", "--max-time", "3",
             "https://192.168.5.92:9444/healthz",
             "-H", "Host: birds.vivessyn.duckdns.org"],
            capture_output=True, timeout=5,
        )
        body = result.stdout.decode().strip()
        if result.returncode == 0 and body == "ok":
            return {"status": "ok", "detail": "NAS proxy healthy"}
        return {"status": "warn", "detail": f"NAS returned: {body or 'empty'} (exit {result.returncode})"}
    except Exception as e:
        err = str(e)
        detail = f"NAS unreachable ({err})"
        if "502" in err:
            detail += ". Likely: Docker container IP changed. Fix: ssh NAS, docker restart birds-share"
        return {"status": "error", "detail": detail, "error": err}


@app.get("/api/system-health")
def system_health():
    """Aggregated health status of all services. Cached for 10 seconds."""
    now = _time.time()
    if _health_cache["data"] and (now - _health_cache["time"]) < _HEALTH_CACHE_TTL:
        return _health_cache["data"]

    entry_count = cdb.count_total()
    species_count = cdb.count_species()
    result = {
        "timestamp": datetime.now().isoformat(),
        "services": {
            "api": {
                "status": "ok",
                "detail": f"{entry_count} entries, {species_count} species",
                "entries": entry_count,
                "species_count": species_count,
                "backend": "sqlite",
            },
            "live_detector": _fetch_service("http://localhost:8097/metrics", "Live Detector"),
            "enhanced_audio": _fetch_service("http://localhost:8096/metrics", "Enhanced Audio"),
            "audio_analyzer": _check_audio_analyzer_health(),
            "nas": _check_nas(),
        },
    }

    # Add disk free
    try:
        usage = shutil.disk_usage("/")
        result["disk_free_gb"] = round(usage.free / (1024**3), 1)
        result["disk_total_gb"] = round(usage.total / (1024**3), 1)
    except Exception:
        pass

    _health_cache["data"] = result
    _health_cache["time"] = now
    return result


@app.get("/api/cameras")
def cameras_list():
    """List cameras with detection counts and last seen times."""
    return cdb.get_cameras()


@app.get("/api/stats")
def stats(date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD, or 'all'"),
          camera: Optional[str] = Query(None, description="Filter by camera: feeder, ground, or all")):
    """Overall classification statistics, optionally filtered by date and camera."""
    _validate_date(date)
    def _compute():
        s = cdb.get_stats(date, camera)
        _lt = _time.localtime()
        tz_offset_min = (_time.altzone if _time.daylight and _lt.tm_isdst else _time.timezone) // 60
        s["server_tz_offset"] = tz_offset_min
        # last_updated is always the global latest timestamp (not filtered)
        s["last_updated"] = cdb.get_last_timestamp()
        return s
    return cached_result(f"stats:{date}:{camera}", 30, _compute)


@app.get("/api/species")
def species_list(date: Optional[str] = Query(None, description="Filter by date YYYY-MM-DD, or 'all'"),
                 camera: Optional[str] = Query(None, description="Filter by camera: feeder, ground, or all")):
    """List all detected species with counts and metadata, optionally filtered by date and camera."""
    _validate_date(date)
    def _compute():
        return cdb.get_species_list(date, camera)
    return cached_result(f"species:{date}:{camera}", 30, _compute)


@app.get("/api/species/{name}")
def species_detail(name: str):
    """Detailed data for a single species."""
    result = cdb.get_species_detail(name)
    if not result:
        raise HTTPException(status_code=404, detail=f"Species '{name}' not found")
    return result


@app.get("/api/recent")
def recent(limit: int = 50, camera: Optional[str] = Query(None, description="Filter by camera: feeder, ground, or all")):
    """Most recent classified detections, optionally filtered by camera."""
    return cdb.get_recent(limit, camera=camera)


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
    path = _find_classified_file(filename)
    if not path:
        raise HTTPException(status_code=404, detail="Raw image not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/api/image-crop/{filename}")
def get_image_crop(filename: str, box: str = ""):
    """Serve a cropped region from the raw image (bounding box area with padding)."""
    import io
    from PIL import Image as PILImage
    path = _find_classified_file(filename)
    if not path:
        raise HTTPException(status_code=404, detail="Raw image not found")
    if not box:
        # Try to find box from classification DB
        entry = cdb.get_entry_by_file(os.path.basename(filename))
        if entry and entry.get("best_detection"):
            b = entry["best_detection"]["box"]
            box = f"{b[0]},{b[1]},{b[2]},{b[3]}"
    if not box:
        # No box available, return the full raw image
        return FileResponse(str(path), media_type="image/jpeg")
    try:
        coords = [int(x) for x in box.split(",")]
        x1, y1, x2, y2 = coords[:4]
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid box format. Use x1,y1,x2,y2")
    img = PILImage.open(path)
    w, h = img.size
    # Add 15% padding
    bw, bh = x2 - x1, y2 - y1
    pad_x, pad_y = int(bw * 0.15), int(bh * 0.15)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)
    cropped = img.crop((x1, y1, x2, y2))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    from starlette.responses import StreamingResponse
    return StreamingResponse(buf, media_type="image/jpeg")


@app.get("/api/review/pending")
def review_pending(species: str = "", offset: int = 0, limit: int = 50, multibird: str = ""):
    """Get unreviewed classifications for the annotation GUI (paginated).

    Uses SQL LEFT JOIN via reviews_db — no in-memory cross-reference needed.
    """
    sp = species or None
    mb = bool(multibird)

    rows = rdb.get_pending_classifications(species=sp, multibird=mb, offset=offset, limit=limit)
    remaining = rdb.count_pending(species=sp, multibird=mb)

    # Build response items from SQL rows
    pending = []
    for r in rows:
        birds = json.loads(r["birds_json"]) if r.get("birds_json") else []
        top3 = json.loads(r["top3_json"]) if r.get("top3_json") else []
        raw_top3 = json.loads(r["raw_top3_json"]) if r.get("raw_top3_json") else []
        pending.append({
            "file": r["file"],
            "timestamp": r.get("source_timestamp") or "",
            "species": r["common_name"],
            "confidence": r["confidence"],
            "raw_score": r.get("raw_score", 0),
            "top3": top3,
            "raw_top3": raw_top3,
            "birds": birds,
        })

    total_classified = cdb.count_classified()
    total_reviewed = rdb.count_reviews()

    # Species list: distinct species from unreviewed classifications
    conn = rdb.get_conn(readonly=True)
    species_rows = conn.execute(
        "SELECT DISTINCT c.common_name FROM classifications c "
        "LEFT JOIN reviews r ON c.file = r.file "
        "WHERE c.action='classified' AND c.common_name IS NOT NULL "
        "AND (r.file IS NULL OR r.verdict = 'requeued') "
        "ORDER BY c.common_name"
    ).fetchall()
    species_list = [row[0] for row in species_rows]

    return {
        "pending": pending,
        "total_classified": total_classified,
        "total_reviewed": total_reviewed,
        "remaining": remaining,
        "species_list": species_list,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < remaining,
    }


INCOMING_DIR = BASE_DIR / "incoming"


@app.get("/api/review/rerun-count")
def rerun_count():
    """Count files flagged with verdict=reclassify (missed birds)."""
    reclassify_reviews = rdb.get_reviews_by_verdict("reclassify")
    return {"count": len(reclassify_reviews)}


@app.post("/api/review/rerun-missed")
def rerun_missed():
    """Move all reclassify-flagged files back to incoming/ for reprocessing.

    For each file with verdict=reclassify:
    1. Find in classified/*/ → move to incoming/
    2. Delete annotated version (new one will be generated)
    3. Write verdict=requeued entry so it shows as pending after re-classification
    """
    flagged_reviews = rdb.get_reviews_by_verdict("reclassify")
    flagged = [r["file"] for r in flagged_reviews]

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    not_found = 0

    for fname in flagged:
        src = _find_classified_file(fname)
        if src:
            dst = INCOMING_DIR / fname
            shutil.move(str(src), str(dst))
            ann = ANNOTATED_DIR / fname
            if ann.exists():
                ann.unlink()
            moved += 1
        else:
            not_found += 1

        requeue_entry = {
            "file": fname,
            "verdict": "requeued",
            "correct_species": "",
            "missed_birds": False,
            "bird_index": 0,
            "timestamp": datetime.now().isoformat(),
        }
        rdb.insert_review(requeue_entry)
        _append_jsonl(REVIEWS_PATH, requeue_entry)

    return {
        "moved": moved,
        "not_found": not_found,
        "message": f"Requeued {moved} files for reclassification" + (f" ({not_found} not found on disk)" if not_found else ""),
    }


@app.get("/api/review/goals")
def review_goals(threshold: int = 20):
    """Species classification goals — which species need more confirmed reviews for training.

    Uses SQL aggregation via reviews_db instead of in-memory iteration.
    """
    regional = load_regional_species()
    raw_goals = rdb.get_review_goals(regional, threshold)

    goals = []
    for g in raw_goals:
        count = g["confirmed"]
        if count > 0:
            goals.append({
                "species": g["species"],
                "confirmed": count,
                "target": threshold,
                "complete": 100 if count >= threshold else round(count / threshold * 100),
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
    review = _create_review_entry(filename, verdict, correct_species, missed_birds, bird_index)
    rdb.insert_review(review)
    _append_jsonl(REVIEWS_PATH, review)  # JSONL backup during transition
    invalidate_cache("stats:", "species:", "goals:")

    # Move trashed images out of annotated dir
    if verdict == "trash":
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        src = ANNOTATED_DIR / review["file"]
        if src.exists():
            shutil.move(str(src), str(TRASH_DIR / review["file"]))

    return {"status": "ok", "review": review}


@app.get("/api/review/classified")
def review_classified(species: str = "", verdict: str = "", limit: int = 50, offset: int = 0):
    """Get reviewed classifications (correct, wrong, reclassify verdicts).

    Uses SQL JOIN via reviews_db instead of batch file lookup.
    """
    sp = species or None
    v = verdict or None
    rows = rdb.get_reviewed_entries(species=sp, verdict=v, offset=offset, limit=limit)

    items = []
    for r in rows:
        best_det = json.loads(r["best_detection_json"]) if r.get("best_detection_json") else {}
        items.append({
            "file": r["file"],
            "species": r.get("common_name", "Unknown") or "Unknown",
            "confidence": best_det.get("confidence", 0) if best_det else r.get("confidence", 0),
            "verdict": r["verdict"],
            "correct_species": r.get("correct_species", ""),
            "missed_birds": bool(r.get("missed_birds", False)),
            "review_timestamp": r.get("review_timestamp", ""),
            "source_timestamp": r.get("source_timestamp", ""),
        })

    # Get total count (without pagination) for the same filters
    conn = rdb.get_conn(readonly=True)
    where_parts = []
    params = []
    if sp:
        where_parts.append("c.common_name = ?")
        params.append(sp)
    if v:
        where_parts.append("r.verdict = ?")
        params.append(v)
    extra = (" AND " + " AND ".join(where_parts)) if where_parts else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM reviews r JOIN classifications c ON r.file = c.file WHERE 1=1" + extra,
        params
    ).fetchone()[0]

    # Species list for filter dropdown
    species_rows = conn.execute(
        "SELECT DISTINCT c.common_name FROM reviews r "
        "JOIN classifications c ON r.file = c.file "
        "WHERE r.verdict IN ('correct','wrong','reclassify') "
        "ORDER BY c.common_name"
    ).fetchall()
    species_list = [row[0] for row in species_rows if row[0]]

    return {"items": items, "total": total, "species_list": species_list}


@app.post("/api/review/{filename}/update")
def update_review(filename: str, verdict: str, correct_species: str = "", missed_birds: str = "false", bird_index: str = "0"):
    """Update an existing review verdict (INSERT OR REPLACE in SQLite)."""
    review = _create_review_entry(filename, verdict, correct_species, missed_birds, bird_index)
    rdb.insert_review(review)
    _append_jsonl(REVIEWS_PATH, review)  # JSONL backup during transition
    invalidate_cache("stats:", "species:", "goals:")
    return {"status": "ok", "review": review}


@app.get("/api/dates")
def available_dates():
    """Return list of dates that have classified detections, newest first."""
    def _compute():
        return cdb.get_dates()
    return cached_result("dates:all", 60, _compute)


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
        except Exception as exc:
            # Try without "American " prefix
            logging.debug("AAB fetch failed for '%s': %s", name, exc)
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
        # Atomic write: temp file + rename prevents corrupt images on partial download
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.rename(dest)
        return dest, "image/jpeg"
    except Exception as exc:
        logging.warning("Failed to download species image for '%s': %s", name, exc)
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
    rows = rdb.get_reviewed_entries(verdict="skip", offset=offset, limit=limit)

    skipped = []
    for r in rows:
        skipped.append({
            "file": r["file"],
            "species": r.get("common_name", "Unknown") or "Unknown",
            "timestamp": r.get("review_timestamp", ""),
            "source_timestamp": r.get("source_timestamp", ""),
        })

    # Total count of skip reviews
    conn = rdb.get_conn(readonly=True)
    total = conn.execute(
        "SELECT COUNT(*) FROM reviews r JOIN classifications c ON r.file = c.file "
        "WHERE r.verdict = 'skip'"
    ).fetchone()[0]

    return {"files": skipped, "total": total}


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
    except Exception as exc:
        logging.debug("Timestamp parse failed for '%s': %s", filename, exc)
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

            except Exception as exc:
                logging.warning("[BirdNET SSE] DB poll error: %s", exc)
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

    full_path = (BIRDNET_CLIPS_DIR / safe_path).resolve()
    # Verify resolved path stays within allowed directory
    if not str(full_path).startswith(str(BIRDNET_CLIPS_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")

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
        except Exception as exc:
            logging.warning("Failed to load cull config: %s", exc)
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
    regional = load_regional_species()
    raw_goals = rdb.get_review_goals(regional, threshold=999999)

    confirmed: dict[str, int] = defaultdict(int)
    for g in raw_goals:
        if g["confirmed"] > 0:
            confirmed[g["species"]] = g["confirmed"]

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

    if keep < 0 or keep > 10000:
        raise HTTPException(status_code=400, detail="keep must be between 0 and 10000")

    if sort_by not in ("date", "confidence"):
        raise HTTPException(status_code=400, detail="sort_by must be 'date' or 'confidence'")

    if sort_by == "confidence":
        # Batch lookup for score-based sorting
        files = list(src_dir.glob("*.jpg"))
        file_entries = cdb.get_entries_by_files([f.name for f in files])
        files.sort(
            key=lambda f: (
                file_entries.get(f.name, {}).get("top_prediction", {}).get("raw_score", 0),
                f.stat().st_mtime,
            ),
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
    failed = 0
    for f in to_trash:
        dst = TRASH_DIR / f.name
        try:
            shutil.move(str(f), str(dst))
            # Remove annotated version
            ann = ANNOTATED_DIR / f.name
            if ann.exists():
                ann.unlink()
            trashed += 1
        except Exception as exc:
            logging.warning("Failed to trash %s: %s", f.name, exc)
            failed += 1

    msg = f"Trashed {trashed} {safe_dir.replace('_', ' ')} files, kept {keep} {sort_label}"
    if failed:
        msg += f" ({failed} failed)"
    return {
        "trashed": trashed,
        "kept": keep,
        "failed": failed,
        "message": msg,
    }


# ── Food Log ──────────────────────────────────────────────────────────────
# Tracks what food is in the feeder for species-food correlation analysis.

_FOOD_DB = Path(os.path.expanduser("~/bird-snapshots/birdnet-audio/birdnet_local.db"))

FOOD_TYPES = [
    "sunflower", "mixed_songbird", "suet", "nyjer", "peanut",
    "safflower", "mealworm", "fruit", "nectar", "empty",
]


def _init_food_log():
    """Create the food_log table if it doesn't exist."""
    try:
        conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                food_type TEXT NOT NULL,
                feeder TEXT DEFAULT 'main',
                notes TEXT DEFAULT ''
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        import logging
        logging.warning("Could not init food_log table: %s", e)


# Init on import
_init_food_log()


from pydantic import BaseModel


class FoodLogEntry(BaseModel):
    food_type: str
    feeder: str = "main"
    notes: str = ""
    timestamp: str = ""  # optional ISO timestamp for backfilling


@app.post("/api/food-log")
def add_food_log(entry: FoodLogEntry):
    """Log a food change in the feeder."""
    if entry.food_type not in FOOD_TYPES and not entry.food_type.startswith("custom:"):
        return JSONResponse(
            status_code=400,
            content={"error": f"Unknown food type. Use one of: {', '.join(FOOD_TYPES)} or custom:name"}
        )
    ts = entry.timestamp if entry.timestamp else datetime.now().isoformat()
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
    try:
        conn.execute(
            "INSERT INTO food_log (timestamp, food_type, feeder, notes) VALUES (?, ?, ?, ?)",
            (ts, entry.food_type, entry.feeder, entry.notes),
        )
        conn.commit()
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
    return {"id": row_id, "timestamp": ts, "food_type": entry.food_type, "feeder": entry.feeder}


@app.get("/api/food-log")
def get_food_log():
    """List all food log entries, newest first."""
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, timestamp, food_type, feeder, notes FROM food_log ORDER BY timestamp DESC")
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "timestamp": r[1], "food_type": r[2], "feeder": r[3], "notes": r[4]}
        for r in rows
    ]


@app.get("/api/food-log/current")
def get_current_food():
    """Get the most recent food entry (what's currently in the feeder)."""
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, timestamp, food_type, feeder, notes FROM food_log ORDER BY timestamp DESC LIMIT 1")
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"food_type": "unknown", "timestamp": None, "detail": "No food logged yet"}
    return {"id": row[0], "timestamp": row[1], "food_type": row[2], "feeder": row[3], "notes": row[4]}


@app.delete("/api/food-log/{entry_id}")
def delete_food_log(entry_id: int):
    """Delete a food log entry."""
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM food_log WHERE id = ?", (entry_id,))
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"deleted": entry_id}


@app.get("/api/food-types")
def list_food_types():
    """List available food types."""
    return {"food_types": FOOD_TYPES}


# ── Activity Analytics ────────────────────────────────────────────────────

def _get_food_at_time(conn, timestamp_str):
    """Find what food was in the feeder at a given ISO timestamp."""
    cur = conn.cursor()
    cur.execute(
        "SELECT food_type FROM food_log WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
        (timestamp_str,),
    )
    row = cur.fetchone()
    return row[0] if row else "unknown"


def _get_food_periods(conn):
    """Get all food periods as (start, end, food_type) tuples."""
    cur = conn.cursor()
    cur.execute("SELECT timestamp, food_type FROM food_log ORDER BY timestamp ASC")
    rows = cur.fetchall()
    if not rows:
        return []
    periods = []
    for i, (ts, food) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else datetime.now().isoformat()
        periods.append((ts, end, food))
    return periods


def _hours_between(iso_start, iso_end):
    """Calculate hours between two ISO timestamps."""
    try:
        from datetime import datetime as dt_cls
        s = dt_cls.fromisoformat(iso_start)
        e = dt_cls.fromisoformat(iso_end)
        return max(0, (e - s).total_seconds() / 3600)
    except Exception:
        return 0


@app.get("/api/activity/species/{species_name}")
def get_species_activity(species_name: str):
    """Activity analysis for a single species: hourly pattern, food preferences, cameras."""
    species_name = normalize_species(species_name)

    # Gather detections from both camera (SQLite) and audio (BirdNET SQLite)
    sp_rows = cdb.get_species_timestamps(species_name)
    camera_dets = [{"timestamp": r["timestamp"], "camera": r["camera"], "source": "camera"} for r in sp_rows]

    audio_dets = []
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT date, time, confidence FROM notes WHERE common_name = ? ORDER BY date, time",
            (species_name,),
        )
        for date, time_str, conf in cur.fetchall():
            audio_dets.append({
                "timestamp": f"{date} {time_str}",
                "source": "audio",
                "confidence": conf,
            })
    except Exception:
        pass

    # Hourly distribution (0-23)
    by_hour = [0] * 24
    all_timestamps = []
    for d in camera_dets + audio_dets:
        ts = d["timestamp"]
        all_timestamps.append(ts)
        try:
            hour = int(ts.split(" ")[1].split(":")[0]) if " " in ts else int(ts.split("T")[1].split(":")[0])
            by_hour[hour] += 1
        except Exception:
            pass

    # Day of week distribution (0=Mon, 6=Sun)
    by_dow = [0] * 7
    for ts in all_timestamps:
        try:
            date_str = ts.split(" ")[0] if " " in ts else ts.split("T")[0]
            from datetime import date as date_cls
            parts = date_str.split("-")
            d = date_cls(int(parts[0]), int(parts[1]), int(parts[2]))
            by_dow[d.weekday()] += 1
        except Exception:
            pass

    # Camera breakdown
    cameras = {}
    for d in camera_dets:
        cam = d.get("camera", "unknown")
        cameras[cam] = cameras.get(cam, 0) + 1

    # Food preferences
    by_food = {}
    food_periods = _get_food_periods(conn)
    for d in camera_dets + audio_dets:
        food = _get_food_at_time(conn, d["timestamp"])
        if food not in by_food:
            by_food[food] = 0
        by_food[food] += 1

    # Calculate rate per hour for each food
    food_hours = {}
    for start, end, food in food_periods:
        h = _hours_between(start, end)
        food_hours[food] = food_hours.get(food, 0) + h

    food_prefs = {}
    for food, count in by_food.items():
        hours = food_hours.get(food, 1)
        food_prefs[food] = {
            "detections": count,
            "hours_available": round(hours, 1),
            "rate_per_hour": round(count / max(hours, 0.1), 2),
        }

    conn.close()

    # Peak hour
    peak_hour = by_hour.index(max(by_hour)) if max(by_hour) > 0 else -1
    total = len(camera_dets) + len(audio_dets)

    # First/last seen
    sorted_ts = sorted(all_timestamps)
    first_seen = sorted_ts[0].split(" ")[0] if sorted_ts else None
    last_seen = sorted_ts[-1].split(" ")[0] if sorted_ts else None

    # Preferred food
    preferred = max(food_prefs.items(), key=lambda x: x[1]["rate_per_hour"])[0] if food_prefs else "unknown"

    return {
        "species": species_name,
        "total_detections": total,
        "camera_detections": len(camera_dets),
        "audio_detections": len(audio_dets),
        "by_hour": by_hour,
        "peak_hour": peak_hour,
        "peak_description": f"Most active {peak_hour}:00-{(peak_hour+1) % 24}:00" if peak_hour >= 0 else "No data",
        "by_day_of_week": by_dow,
        "by_food": food_prefs,
        "preferred_food": preferred,
        "cameras": cameras,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


@app.get("/api/activity/food/{food_type}")
def get_food_activity(food_type: str):
    """What species does this food attract? Rates per hour for comparison."""
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)

    # Get periods for this food
    food_periods = _get_food_periods(conn)
    matching_periods = [(s, e) for s, e, f in food_periods if f == food_type]

    total_hours = sum(_hours_between(s, e) for s, e in matching_periods)

    if total_hours == 0:
        conn.close()
        return {
            "food_type": food_type,
            "total_hours": 0,
            "species_attracted": [],
            "detail": "No logged periods for this food type",
        }

    # Count detections per species during this food's periods
    species_counts = {}

    # Camera detections (from classification SQLite)
    for ts, name in cdb.get_all_classified_brief():
        name = normalize_species(name)
        if not name:
            continue
        food = _get_food_at_time(conn, ts)
        if food == food_type:
            species_counts[name] = species_counts.get(name, 0) + 1

    # Audio detections
    try:
        cur = conn.cursor()
        cur.execute("SELECT date, time, common_name FROM notes ORDER BY date, time")
        for date, time_str, name in cur.fetchall():
            name = normalize_species(name)
            ts = f"{date} {time_str}"
            food = _get_food_at_time(conn, ts)
            if food == food_type:
                species_counts[name] = species_counts.get(name, 0) + 1
    except Exception:
        pass

    conn.close()

    # Build ranked list
    species_list = []
    for sp, count in sorted(species_counts.items(), key=lambda x: -x[1]):
        species_list.append({
            "species": sp,
            "detections": count,
            "rate_per_hour": round(count / max(total_hours, 0.1), 2),
        })

    return {
        "food_type": food_type,
        "total_hours": round(total_hours, 1),
        "periods": [{"start": s, "end": e} for s, e in matching_periods],
        "species_attracted": species_list[:30],  # top 30
        "total_species": len(species_list),
    }


@app.get("/api/activity/heatmap")
def get_activity_heatmap(species: str = "all", days: int = 7):
    """Hour × species detection heatmap for the last N days."""
    cutoff_date = (datetime.now() - _timedelta(days=days)).strftime("%Y-%m-%d")

    # Collect hourly counts per species from audio DB
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
    heatmap = {}  # species → [hour0, hour1, ..., hour23]
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT common_name, time FROM notes WHERE date >= ?",
            (cutoff_date,),
        )
        for name, time_str in cur.fetchall():
            name = normalize_species(name)
            if species != "all" and name != normalize_species(species):
                continue
            try:
                hour = int(time_str.split(":")[0])
            except Exception:
                continue
            if name not in heatmap:
                heatmap[name] = [0] * 24
            heatmap[name][hour] += 1
    except Exception:
        pass
    conn.close()

    # Also add camera detections (from classification SQLite)
    for ts, name in cdb.get_classified_since_brief(cutoff_date, species if species != "all" else None):
        name = normalize_species(name)
        if not name or not ts:
            continue
        try:
            time_part = ts.split(" ")[1] if " " in ts else ts.split("T")[1]
            hour = int(time_part.split(":")[0])
        except Exception:
            continue
        if name not in heatmap:
            heatmap[name] = [0] * 24
        heatmap[name][hour] += 1

    # Sort by total detections
    sorted_species = sorted(heatmap.keys(), key=lambda s: -sum(heatmap[s]))

    return {
        "days": days,
        "cutoff_date": cutoff_date,
        "species": sorted_species[:25],  # top 25
        "heatmap": {s: heatmap[s] for s in sorted_species[:25]},
        "total_detections": sum(sum(v) for v in heatmap.values()),
    }


@app.get("/api/activity/species-list")
def get_species_list():
    """List all detected species with total counts, for autocomplete."""
    # Camera detections from classification SQLite
    counts = {}
    for item in cdb.get_species_counts_for_activity():
        name = item["name"]
        if name and name not in ("background", "unidentified bird", "unidentified"):
            counts[name] = counts.get(name, 0) + item["count"]

    # Also add audio species from BirdNET DB
    conn = _sqlite3.connect(str(_FOOD_DB), timeout=5)
    try:
        cur = conn.cursor()
        cur.execute("SELECT common_name, COUNT(*) FROM notes GROUP BY common_name")
        for name, count in cur.fetchall():
            name = normalize_species(name)
            counts[name] = counts.get(name, 0) + count
    except Exception:
        pass
    conn.close()

    sorted_species = sorted(counts.items(), key=lambda x: -x[1])
    return {"species": [{"name": s, "count": c} for s, c in sorted_species]}
