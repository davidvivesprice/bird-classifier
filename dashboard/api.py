"""
Bird Dashboard API — serves classifier data for the bird observatory dashboard.

Phase 3: all data (classifications + reviews) served from SQLite.
No in-memory caches — RAM usage minimal.
JSONL backup still written for reviews during transition.

Run: uvicorn dashboard.api:app --host 0.0.0.0 --port 8099
"""
from __future__ import annotations


import glob as _glob
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

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# Phase 2: direct SQL queries replace in-memory cache
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import classifications_db as cdb
import reviews_db as rdb
import visits_db as vdb
from bird_inference import SPECIES_ALIASES, normalize_species
from classifications_db import _safe_json

# --- Paths ---
# Derive from runtime locations so iMac (~/= /Users/vives) and
# Pi 5 (~/= /home/vives) both work without environmental hacks.
# Snapshots live under the user's home dir on both hosts; repo-internal
# assets are anchored relative to this file's location in the repo.
BASE_DIR = Path.home() / "bird-snapshots"
CLASSIFIED_DIR = BASE_DIR / "classified"
ANNOTATED_DIR = BASE_DIR / "annotated"
SKIPPED_DIR = BASE_DIR / "skipped"
TRASH_DIR = BASE_DIR / "trash"
BACKGROUND_DIR = BASE_DIR / "classified" / "background"
_REPO_ROOT = Path(__file__).resolve().parent.parent
REGIONAL_SPECIES_PATH = _REPO_ROOT / "models" / "chilmark_feeder_species.txt"
SPECIES_INFO_PATH = _REPO_ROOT / "dashboard" / "species_info.json"
SPECIES_IMAGES_DIR = _REPO_ROOT / "dashboard" / "species_images"
SPECIES_GALLERY_PATH = _REPO_ROOT / "dashboard" / "species_gallery.json"
CULL_CONFIG_PATH = _REPO_ROOT / "config" / "cull_config.json"

app = FastAPI(title="Bird Dashboard API", version="1.0")


# ── URL rewrite middleware: /bird-api/* → /api/* for direct access ──
from starlette.requests import Request as StarletteRequest


class BirdAPIRewriteMiddleware:
    """Rewrite `/bird-api/*` paths to `/api/*` so the dashboard works both
    through Cloudflare routing (which prefixes `/bird-api/`) and via direct
    LAN / Tailscale access.

    Pure ASGI implementation — does NOT extend `BaseHTTPMiddleware`. That
    base class buffers every response body into memory in order to compute
    `Content-Length`, which breaks all streaming responses: SSE endpoints
    (/api/pipeline/events/sse, /api/audio-detections), HLS proxies,
    video/MP4 fallback, JPEG frame serves, and more. When the buffered
    body size doesn't match the declared `Content-Length`, h11 raises
    `LocalProtocolError: Too much/Too little data for declared Content-Length`.
    Pure ASGI is transparent — it just rewrites the scope and delegates.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path.startswith("/bird-api/"):
                scope = dict(scope)  # don't mutate caller's scope
                scope["path"] = "/api/" + path[len("/bird-api/"):]
                raw = scope.get("raw_path")
                if isinstance(raw, (bytes, bytearray)) and raw.startswith(b"/bird-api/"):
                    scope["raw_path"] = b"/api/" + bytes(raw)[len(b"/bird-api/"):]
        await self.app(scope, receive, send)


app.add_middleware(BirdAPIRewriteMiddleware)


@app.on_event("startup")
def warm_cache():
    """Verify SQLite DB is accessible on startup. Creates tables on empty DBs."""
    import logging
    t0 = _time.time()
    cdb.init_db()
    # Ensure reviews + review_history tables exist before any JOIN query runs.
    # On a fresh Pi install, count_species() below uses a LEFT JOIN on reviews.
    rdb.get_conn(readonly=False)
    try:
        total = cdb.count_total()
        species = cdb.count_species()
        review_count = rdb.count_reviews()
    except Exception as e:
        # Empty DB / missing table: don't crash startup.
        logging.warning("Startup: DB warm-up hit %s — continuing with zeros", e)
        total = species = review_count = 0
    t1 = _time.time()
    logging.info("Startup: SQLite DB has %d entries (%d species), %d reviews in %.1fs",
                 total, species, review_count, t1 - t0)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Serve dashboard HTML directly (for Tailscale / direct access) ──

DASHBOARD_DIR = Path(__file__).parent

@app.get("/pi")
def serve_pi_dash():
    """Pi-specific dashboard — model lab, theme picker, Hailo-aware."""
    return FileResponse(str(DASHBOARD_DIR / "pi_dash.html"), media_type="text/html")


@app.get("/")
def serve_dashboard():
    """Serve the main dashboard HTML. On Pi (PI_MODE=1), serve pi_dash.html
    which is the Pi-specific rebuild with model lab + themes."""
    if os.environ.get("PI_MODE", "0") == "1":
        return FileResponse(str(DASHBOARD_DIR / "pi_dash.html"), media_type="text/html")
    return FileResponse(str(DASHBOARD_DIR / "index.html"), media_type="text/html")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
@app.get("/api/apple-touch-icon.png")
def serve_apple_touch_icon():
    """Serve the iOS home screen icon."""
    return FileResponse(str(DASHBOARD_DIR / "apple-touch-icon.png"), media_type="image/png")


@app.get("/logo.png")
def serve_logo():
    """Serve the Vives Ornithology brand logo. Intended to be reachable by
    Cloudflare Access's login page renderer, which requires an unauth'd URL.
    Cf Access side must have a Bypass policy for /logo.png."""
    return FileResponse(str(DASHBOARD_DIR / "vives-logo.png"), media_type="image/png")


@app.get("/game-audio.png")
def serve_game_audio_icon():
    return FileResponse(str(DASHBOARD_DIR / "game-audio.png"), media_type="image/png")


@app.get("/game-visual.png")
def serve_game_visual_icon():
    return FileResponse(str(DASHBOARD_DIR / "game-visual.png"), media_type="image/png")


@app.get("/live")
def serve_live_html():
    """Serve the lightweight live video page (used as iframe in dashboard)."""
    return FileResponse(str(DASHBOARD_DIR / "live.html"), media_type="text/html")


@app.get("/audio")
def serve_audio_prototype():
    """Serve the audio browser prototype page."""
    return FileResponse(str(DASHBOARD_DIR / "audio-prototype.html"), media_type="text/html")


@app.get("/hls-test")
def serve_hls_test():
    """Serve the HLS delayed playback test page."""
    return FileResponse(str(DASHBOARD_DIR / "hls-test.html"), media_type="text/html")


@app.get("/sync-test")
def serve_sync_test():
    """Serve the overlay-sync diagnostic page (main + sub streams side-by-side)."""
    return FileResponse(str(DASHBOARD_DIR / "sync-test.html"), media_type="text/html")


@app.get("/ideas")
def serve_ideas():
    """Serve the Ideas page — mockups of pending UX proposals."""
    return FileResponse(str(DASHBOARD_DIR / "ideas.html"), media_type="text/html")


@app.get("/review-ideas")
def serve_review_ideas():
    """Interactive mockup for the gamified review page.
    Pairs with docs/superpowers/specs/2026-04-23-airtight-review-system.md
    (the airtight-plumbing design doc). Uses fake data — no real writes."""
    return FileResponse(str(DASHBOARD_DIR / "review-ideas.html"), media_type="text/html")


@app.get("/work")
def serve_work():
    """Single-page review of all work shipped in the 2026-04-22 → 2026-04-23 revamp.
    Includes hover-tooltip + drawer glossary for jargon terms."""
    return FileResponse(str(DASHBOARD_DIR / "work.html"), media_type="text/html")


@app.get("/motion-sandbox")
def serve_motion_sandbox():
    """Label motion sandbox — real track replayed through four smoothing strategies.
    Used for brainstorming the label-on-bird interpolation design."""
    return FileResponse(str(DASHBOARD_DIR / "motion-sandbox.html"), media_type="text/html")


@app.get("/api/species-facts")
def serve_species_facts():
    """Serve the hand-curated species facts library."""
    return FileResponse(str(DASHBOARD_DIR / "species_facts.json"),
                        media_type="application/json",
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/tmp-preview/{name}")
def serve_tmp_preview(name: str):
    """Serve preview images from /tmp for brainstorm/diagnostic use.

    Whitelisted filenames only (no traversal). Used for one-off PNGs like
    AOI previews that don't belong in the repo.
    """
    import os
    allowed = {"feeder-sub-aoi.png", "feeder-sub-grid.png", "feeder-sub-weights.png"}
    if name not in allowed:
        raise HTTPException(status_code=404, detail="Not in allowlist")
    path = f"/tmp/{name}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/api/hls-live/{camera}/{path:path}")
def serve_hls_segments(camera: str, path: str):
    """Serve HLS playlist and segments from the pipeline's HLS recorder output.

    The pipeline writes to ~/bird-snapshots/hls/{camera}/live.m3u8 + *.ts segments.
    This route serves them as static files for hls.js consumption.

    Per ffmpeg docs: segments are -c copy remuxed (no transcode), with
    #EXT-X-PROGRAM-DATE-TIME tags for wall-clock timestamp correlation.
    """
    from pathlib import Path
    HLS_ROOT = Path.home() / "bird-snapshots" / "hls"
    # Validate camera name — prevent traversal
    if camera not in ("feeder", "ground", "feeder-sub", "ground-sub"):
        raise HTTPException(status_code=400, detail="Invalid camera")
    # Validate path — no traversal
    safe_path = Path(path)
    if ".." in safe_path.parts:
        raise HTTPException(status_code=403, detail="Access denied")
    full_path = HLS_ROOT / camera / safe_path
    resolved = full_path.resolve()
    if not str(resolved).startswith(str((HLS_ROOT / camera).resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Not found")
    # Content type based on extension
    if path.endswith(".m3u8"):
        media_type = "application/vnd.apple.mpegurl"
    elif path.endswith(".ts"):
        media_type = "video/mp2t"
    elif path.endswith(".m4s"):
        media_type = "video/iso.segment"
    elif path.endswith(".mp4"):
        media_type = "video/mp4"
    elif path.endswith(".json"):
        media_type = "application/json"
    else:
        media_type = "application/octet-stream"
    return FileResponse(
        str(resolved),
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/video-rtc.js")
def serve_video_rtc_js():
    """Serve go2rtc's video-rtc.js player component."""
    return FileResponse(str(DASHBOARD_DIR / "video-rtc.js"), media_type="application/javascript")


@app.get("/docs.html")
def serve_docs_html():
    """Serve the docs viewer HTML."""
    return FileResponse(str(DASHBOARD_DIR / "docs.html"), media_type="text/html")


# ── Atomic JSONL writer ──

# ── Classification data: all queries go directly to SQLite via cdb ──
# No in-memory cache. RAM usage drops from ~1.2GB to near zero.
# See classifications_db.py for all query functions.


# ── Reviews: served from SQLite via reviews_db (rdb) ──
# JSONL retired March 22, 2026. Historical files preserved as archive.


# ── Result cache with TTL ──

_result_cache: dict[str, tuple[float, object]] = {}
_RESULT_CACHE_MAX = 200  # evict oldest when exceeded


def cached_result(key: str, ttl: float, fn):
    """Return cached result if fresh, otherwise compute and cache."""
    now = _time.time()
    if key in _result_cache and _result_cache[key][0] > now:
        return _result_cache[key][1]
    result = fn()
    _result_cache[key] = (now + ttl, result)
    # Evict expired and oldest entries if cache grows too large
    if len(_result_cache) > _RESULT_CACHE_MAX:
        expired = [k for k, (exp, _) in _result_cache.items() if exp <= now]
        for k in expired:
            del _result_cache[k]
        if len(_result_cache) > _RESULT_CACHE_MAX:
            # Still too big — remove oldest by expiry time
            oldest = sorted(_result_cache.items(), key=lambda x: x[1][0])
            for k, _ in oldest[:len(_result_cache) - _RESULT_CACHE_MAX // 2]:
                del _result_cache[k]
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


def _apply_verdict_files(filename, verdict, correct_species,
                         classified_dir=None, annotated_dir=None,
                         trash_dir=None, skipped_dir=None):
    """Move files to match verdict. Pure file logic, no DB writes.

    Accepts optional dir overrides for testing.
    Returns {"moved": bool, "from_dir": str|None, "to_dir": str|None, "error": str|None}
    """
    classified_dir = classified_dir or CLASSIFIED_DIR
    annotated_dir = annotated_dir or ANNOTATED_DIR
    trash_dir = trash_dir or TRASH_DIR
    skipped_dir = skipped_dir or Path(str(BASE_DIR)) / "skipped"

    def _find(name):
        for d in classified_dir.iterdir():
            if d.is_dir():
                candidate = d / name
                if candidate.exists():
                    return candidate
        return None

    def _sanitize(species):
        return species.replace(" ", "_").replace("'", "").replace("/", "-")

    result = {"moved": False, "from_dir": None, "to_dir": None, "error": None}

    if verdict in ("correct", "reclassify"):
        return result

    src = _find(filename)

    if verdict == "trash" or (verdict == "wrong" and correct_species == "not_a_bird"):
        trash_dir.mkdir(parents=True, exist_ok=True)
        if src:
            result["from_dir"] = src.parent.name
            shutil.move(str(src), str(trash_dir / filename))
            result["moved"] = True
            result["to_dir"] = "trash"
        else:
            result["error"] = f"File not found in classified/: {filename}"
        # Delete annotated copy (not needed after trash)
        ann = annotated_dir / filename
        if ann.exists():
            ann.unlink()

    elif verdict == "wrong" and correct_species:
        safe_name = _sanitize(correct_species)
        dst_dir = classified_dir / safe_name
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src:
            result["from_dir"] = src.parent.name
            shutil.move(str(src), str(dst_dir / filename))
            result["moved"] = True
            result["to_dir"] = safe_name
        else:
            result["error"] = f"File not found in classified/: {filename}"

    elif verdict == "skip" or (verdict == "wrong" and not correct_species):
        skipped_dir.mkdir(parents=True, exist_ok=True)
        if src:
            result["from_dir"] = src.parent.name
            shutil.move(str(src), str(skipped_dir / filename))
            result["moved"] = True
            result["to_dir"] = "skipped"
        else:
            result["error"] = f"File not found in classified/: {filename}"

    return result


def apply_verdict(filename, verdict, correct_species=""):
    """Move file + update DB to match verdict. Single source of truth.

    Called by all review endpoints.
    """
    result = _apply_verdict_files(filename, verdict, correct_species)

    # NOTE: We do NOT update classifications.common_name here.
    # common_name stays as the AI's original classification — that's the honest data.
    # The review's correct_species field records what the human said it actually is.
    # The file is moved to the correct folder by _apply_verdict_files().

    # Mark trashed items so they don't appear in species grids and queries
    if verdict == "trash" or (verdict == "wrong" and correct_species == "not_a_bird"):
        try:
            cdb.get_conn(readonly=False).execute(
                "UPDATE classifications SET action = 'trashed:review' WHERE file = ?",
                (filename,)
            )
            cdb.get_conn(readonly=False).commit()
        except Exception as e:
            logging.warning("Failed to mark %s as trashed: %s", filename, e)

    if result["moved"]:
        logging.info("apply_verdict: %s → %s (%s → %s)",
                     filename, verdict, result["from_dir"], result["to_dir"])
    elif result["error"]:
        logging.warning("apply_verdict: %s — %s", filename, result["error"])

    return result


def _find_classified_file(filename: str) -> Path | None:
    """Find a classified file across species subdirectories."""
    safe = os.path.basename(filename)
    for d in CLASSIFIED_DIR.iterdir():
        if d.is_dir():
            p = d / safe
            if p.exists():
                return p
    return None


def _find_any_image(filename: str) -> Path | None:
    """Find an image file anywhere — classified, annotated, trash, or skipped."""
    safe = os.path.basename(filename)
    # Try classified subdirectories first (raw originals)
    found = _find_classified_file(safe)
    if found:
        return found
    # Annotated (has bounding boxes but always available)
    p = ANNOTATED_DIR / safe
    if p.exists():
        return p
    # Trash
    p = TRASH_DIR / safe
    if p.exists():
        return p
    # Skipped
    p = SKIPPED_DIR / safe
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



# ── Camera Auto-Focus (REMOVED 2026-04-20) ──
#
# The G3 Dome cameras on this install have fixed-focus lenses (canTouchFocus
# reports false), so triggering auto-focus was always a no-op. The UI button
# was already removed earlier. The backend endpoint itself was removed here
# in favour of a future Protect API surface that uses the single
# `UNIFI_API_KEY` bearer token (public v1 API at
# /proxy/protect/integration/v1/), not a separate PROTECT_USERNAME/PASSWORD
# local account with cookie+CSRF gymnastics. One credential is enough for
# everything Protect-related and keeps future setup simple: create an API
# Key in Settings → Control Plane → Integrations, drop it in
# ~/.bird-observatory-env, done.


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
    try:
        conn = _get_birdnet_conn()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), MAX(date || ' ' || time) FROM notes WHERE date = ?",
                        (datetime.now().strftime("%Y-%m-%d"),))
            row = cur.fetchone()
            metrics["detections_today"] = row[0] or 0
            metrics["last_detection"] = row[1] or "none"
    except Exception:
        pass

    # Always set detail string (not just when status is "ok")
    today = metrics.get("detections_today", 0)
    last = metrics.get("last_detection", "none")
    if metrics.get("status") == "ok":
        metrics["detail"] = f"Running, {today} detections today, last: {last}"
    else:
        metrics["detail"] = f"Paused, {today} detections today, last: {last}"
    return metrics


def _check_go2rtc():
    """Check go2rtc reachability (runs locally on the iMac)."""
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "3", "http://127.0.0.1:1984/api/streams"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return {"status": "ok", "detail": "go2rtc healthy (local)"}
        return {"status": "warn", "detail": f"go2rtc not responding (exit {result.returncode})"}
    except Exception as e:
        err = str(e)
        detail = f"go2rtc unreachable ({err})"
        if "Connection refused" in err:
            detail += ". Docker container may need restart: docker restart go2rtc"
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
            "pipeline_v3": _fetch_service(f"{_PIPELINE_HEALTH_URL}/api/pipeline/health", "Pipeline v3"),
            "enhanced_audio": _fetch_service("http://localhost:8096/metrics", "Enhanced Audio"),
            "audio_analyzer": _check_audio_analyzer_health(),
            "go2rtc": _check_go2rtc(),
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


@app.get("/api/audio-health")
def get_audio_health():
    """Return health status of audio stream services.

    Reads per-service health files written by RTSPStreamManager.
    Returns status for each audio service (analyzer, enhanced).
    """
    services = {}
    health_files = _glob.glob("/tmp/audio-stream-health-*.json")
    for path in health_files:
        try:
            with open(path) as f:
                data = json.load(f)
            # Check staleness — if updated > 5 min ago, mark unknown
            updated = data.get("updated", "")
            if updated:
                try:
                    updated_dt = datetime.fromisoformat(updated)
                    age = (datetime.now() - updated_dt).total_seconds()
                    if age > 300:
                        data["status"] = "unknown"
                        data["stale"] = True
                except (ValueError, TypeError):
                    pass
            service_name = data.get("service", "unknown")
            services[service_name] = data
        except Exception:
            continue

    if not services:
        return {"analyzer": {"status": "unknown"}, "enhanced": {"status": "unknown"}}

    return services


@app.get("/api/cameras")
def cameras_list():
    """List cameras with detection counts and last seen times."""
    return cdb.get_cameras()


@app.get("/api/daily-highlights")
def daily_highlights(date: Optional[str] = Query(None)):
    """Today's story — highlights, firsts, audio-visual comparison."""
    _validate_date(date)
    today = date or datetime.now().strftime("%Y-%m-%d")

    def _compute():
        conn = cdb.get_conn(readonly=True)

        # Visual species today
        visual = conn.execute(
            "SELECT common_name, COUNT(*) as cnt FROM classifications "
            "WHERE action='classified' AND source_date=? AND common_name IS NOT NULL "
            "GROUP BY common_name ORDER BY cnt DESC", (today,)
        ).fetchall()
        visual_species = {r[0] for r in visual}
        visual_total = sum(r[1] for r in visual)

        # Peak hour
        hours = conn.execute(
            "SELECT CAST(SUBSTR(source_timestamp, 12, 2) AS INTEGER) as hr, COUNT(*) as cnt "
            "FROM classifications WHERE action='classified' AND source_date=? "
            "GROUP BY hr ORDER BY cnt DESC LIMIT 1", (today,)
        ).fetchone()
        peak_hour = hours[0] if hours else None
        peak_count = hours[1] if hours else 0

        # First-ever sightings (species seen today that have never been seen before today)
        firsts = conn.execute(
            "SELECT common_name, MIN(source_date) as first_date "
            "FROM classifications WHERE action='classified' AND common_name IS NOT NULL "
            "GROUP BY common_name HAVING first_date = ?", (today,)
        ).fetchall()
        new_species = [r[0] for r in firsts]

        # Audio species today
        audio_species = set()
        audio_total = 0
        birdnet_conn = _birdnet_db()
        if birdnet_conn:
            try:
                audio_rows = birdnet_conn.execute(
                    "SELECT common_name, COUNT(*) as cnt FROM notes "
                    "WHERE date=? GROUP BY common_name", (today,)
                ).fetchall()
                audio_species = {r[0] for r in audio_rows}
                audio_total = sum(r[1] for r in audio_rows)
            except Exception:
                pass

        # Audio-visual comparison
        heard_not_seen = sorted(audio_species - visual_species)
        seen_not_heard = sorted(visual_species - audio_species)
        both = sorted(visual_species & audio_species)

        # Top visual species
        top_visual = [{"species": r[0], "count": r[1]} for r in visual[:5]]

        result = {
            "date": today,
            "visual_species": len(visual_species),
            "visual_total": visual_total,
            "audio_species": len(audio_species),
            "audio_total": audio_total,
            "total_species": len(visual_species | audio_species),
            "peak_hour": peak_hour,
            "peak_count": peak_count,
            "new_species": new_species,
            "heard_not_seen": heard_not_seen,
            "seen_not_heard": seen_not_heard,
            "confirmed_both": both,
            "top_visual": top_visual,
        }
        return result

    return cached_result(f"highlights:{today}", 60, _compute)


@app.get("/api/weekly-snapshot")
def weekly_snapshot():
    """Weekly trends and notable species behavior."""
    def _compute():
        conn = cdb.get_conn(readonly=True)
        today = datetime.now().strftime("%Y-%m-%d")

        # Daily species + sightings for past 7 days
        daily = conn.execute(
            "SELECT source_date, COUNT(DISTINCT common_name) as species, COUNT(*) as sightings "
            "FROM classifications WHERE action='classified' AND common_name IS NOT NULL "
            "AND source_date >= date(?, '-7 days') "
            "GROUP BY source_date ORDER BY source_date", (today,)
        ).fetchall()
        trend = [{"date": r[0], "species": r[1], "sightings": r[2]} for r in daily]

        # New arrivals this week (first-ever sightings)
        arrivals = conn.execute(
            "SELECT common_name, MIN(source_date) as first_seen "
            "FROM classifications WHERE action='classified' AND common_name IS NOT NULL "
            "GROUP BY common_name HAVING first_seen >= date(?, '-7 days') "
            "ORDER BY first_seen", (today,)
        ).fetchall()
        new_arrivals = [{"species": r[0], "date": r[1]} for r in arrivals]

        # Most active hour (all-time)
        peak = conn.execute(
            "SELECT CAST(SUBSTR(source_timestamp, 12, 2) AS INTEGER) as hour, COUNT(*) as cnt "
            "FROM classifications WHERE action='classified' "
            "GROUP BY hour ORDER BY cnt DESC LIMIT 1"
        ).fetchone()

        # Visit behavior — most interesting patterns
        try:
            vconn = vdb.get_conn(readonly=True)
            visitors = vconn.execute(
                "SELECT species, ROUND(AVG(duration_sec)) as avg_dur, "
                "ROUND(AVG(bird_count),1) as avg_flock, COUNT(*) as visits "
                "FROM visits WHERE source_date >= date(?, '-7 days') "
                "GROUP BY species HAVING visits >= 3 "
                "ORDER BY avg_dur DESC LIMIT 5", (today,)
            ).fetchall()
            longest_visitors = [
                {"species": r[0], "avg_seconds": int(r[1] or 0),
                 "avg_flock": float(r[2] or 1), "visits": r[3]}
                for r in visitors
            ]

            # Biggest flocks
            flocks = vconn.execute(
                "SELECT species, MAX(bird_count) as max_flock, "
                "ROUND(AVG(bird_count),1) as avg_flock, COUNT(*) as visits "
                "FROM visits WHERE source_date >= date(?, '-7 days') AND bird_count > 1 "
                "GROUP BY species ORDER BY max_flock DESC LIMIT 5", (today,)
            ).fetchall()
            biggest_flocks = [
                {"species": r[0], "max_flock": r[1],
                 "avg_flock": float(r[2] or 1), "visits": r[3]}
                for r in flocks
            ]
        except Exception:
            longest_visitors = []
            biggest_flocks = []

        return {
            "trend": trend,
            "new_arrivals": new_arrivals,
            "all_time_peak_hour": peak[0] if peak else None,
            "longest_visitors": longest_visitors,
            "biggest_flocks": biggest_flocks,
        }

    return cached_result("weekly_snapshot", 300, _compute)


@app.get("/api/audio-verified")
def audio_verified(date: Optional[str] = Query(None)):
    """Find visual detections corroborated by audio within +/-30 seconds.

    When auto_confirm=true, automatically inserts 'correct' reviews for
    unreviewed detections that have audio corroboration. This is safe
    because both systems independently agree on the species.

    Returns count of verified and (optionally) auto-confirmed detections.
    """
    today = date or datetime.now().strftime("%Y-%m-%d")
    conn = cdb.get_conn(readonly=True)
    bconn = _birdnet_db()
    if not bconn:
        return {"verified": 0, "auto_confirmed": 0, "error": "BirdNET DB unavailable"}

    # Find visual detections with matching audio within +/-30s
    # Only look at unreviewed classifications
    try:
        matches = conn.execute("""
            SELECT c.file, c.common_name, c.source_timestamp, c.confidence
            FROM classifications c
            LEFT JOIN reviews r ON r.file = c.file
            WHERE c.action = 'classified'
            AND c.common_name IS NOT NULL
            AND c.source_date = ?
            AND r.file IS NULL
        """, (today,)).fetchall()
    except Exception as e:
        return {"verified": 0, "auto_confirmed": 0, "error": str(e)}

    verified = []
    for m in matches:
        species = m["common_name"]
        ts = m["source_timestamp"]
        if not ts:
            continue
        try:
            audio_match = bconn.execute("""
                SELECT common_name, confidence, time
                FROM notes
                WHERE date = ? AND common_name = ?
                AND ABS(
                    (CAST(SUBSTR(?, 12, 2) AS INTEGER)*3600 +
                     CAST(SUBSTR(?, 15, 2) AS INTEGER)*60 +
                     CAST(SUBSTR(?, 18, 2) AS INTEGER))
                    -
                    (CAST(SUBSTR(time, 1, 2) AS INTEGER)*3600 +
                     CAST(SUBSTR(time, 4, 2) AS INTEGER)*60 +
                     CAST(SUBSTR(time, 7, 2) AS INTEGER))
                ) <= 30
                LIMIT 1
            """, (today, species, ts, ts, ts)).fetchone()
            if audio_match:
                verified.append({
                    "file": m["file"],
                    "species": species,
                    "visual_confidence": round(m["confidence"] or 0, 3),
                    "audio_confidence": round(audio_match["confidence"], 3),
                })
        except Exception:
            pass

    return {
        "date": today,
        "verified": len(verified),
        "matches": verified[:20],
    }


@app.get("/api/bulk-reclassify/preview")
def bulk_reclassify_preview(from_species: str, to_species: str, limit: int = 20):
    """Preview images that would be reclassified. Shows sample images for spot-checking."""
    from_species = normalize_species(from_species)
    to_species = normalize_species(to_species)
    limit = min(limit, 100)
    conn = cdb.get_conn(readonly=True)
    # Count total
    total = conn.execute(
        "SELECT COUNT(*) FROM classifications "
        "WHERE action='classified' AND common_name=?", (from_species,)
    ).fetchone()[0]
    # Already reviewed count
    reviewed = conn.execute(
        "SELECT COUNT(*) FROM classifications c "
        "JOIN reviews r ON r.file = c.file "
        "WHERE c.action='classified' AND c.common_name=?", (from_species,)
    ).fetchone()[0]
    # Sample images (newest first, mix of confidence levels)
    samples = conn.execute(
        "SELECT file, source_timestamp, confidence, raw_score "
        "FROM classifications "
        "WHERE action='classified' AND common_name=? "
        "ORDER BY RANDOM() LIMIT ?", (from_species, limit)
    ).fetchall()
    return {
        "from_species": from_species,
        "to_species": to_species,
        "total": total,
        "already_reviewed": reviewed,
        "unreviewed": total - reviewed,
        "samples": [
            {"file": r[0], "timestamp": r[1], "confidence": round(r[2] or 0, 3),
             "raw_score": round(r[3] or 0, 1)}
            for r in samples
        ],
    }


@app.post("/api/bulk-reclassify")
def bulk_reclassify(from_species: str, to_species: str):
    """Bulk reclassify: mark all unreviewed images of from_species as wrong,
    with correct_species=to_species. Only affects unreviewed images.

    This creates review entries with verdict='wrong' and correct_species set,
    which feeds into the retraining pipeline.
    """
    from_species = normalize_species(from_species)
    to_species = normalize_species(to_species)
    conn = cdb.get_conn(readonly=True)
    # Get all unreviewed files for this species
    files = conn.execute(
        "SELECT c.file FROM classifications c "
        "LEFT JOIN reviews r ON r.file = c.file "
        "WHERE c.action='classified' AND c.common_name=? "
        "AND r.file IS NULL", (from_species,)
    ).fetchall()

    if not files:
        return {"reclassified": 0, "message": "No unreviewed images found"}

    # Insert reviews
    rw_conn = rdb.get_conn(readonly=False)
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    for f in files:
        try:
            rw_conn.execute(
                "INSERT OR IGNORE INTO reviews (file, verdict, correct_species, timestamp, reviewer) "
                "VALUES (?, 'wrong', ?, ?, 'bulk-reclassify')",
                (f[0], to_species, now_ts),
            )
            count += 1
        except Exception:
            pass
    rw_conn.commit()
    # Move files to match reclassification
    for f in files:
        apply_verdict(f[0], "wrong", to_species)
    invalidate_cache("pending", "stats", "species", "highlights", "profile", "weekly_snapshot")

    return {
        "reclassified": count,
        "from_species": from_species,
        "to_species": to_species,
        "message": f"Marked {count} images as {to_species} (was {from_species})",
    }


_yard_prior_instance = None

@app.get("/api/yard-prior")
def yard_prior_stats():
    """View the yard prior's current state — what corrections it would make."""
    global _yard_prior_instance
    if _yard_prior_instance is None:
        from yard_prior import YardPrior
        _yard_prior_instance = YardPrior()
    return _yard_prior_instance.get_stats()


@app.get("/api/review/smart-queue")
def review_smart_queue():
    """Smart review queue — surfaces only what needs human eyes.

    Three categories, prioritized:
    1. Corrections: prior/signals say it's wrong (~500-1000)
    2. Training samples: best shot per species needing more data (~500)
    3. Uncertain: classifier confused, need human judgment (~500)

    Everything else is deprioritized (not deleted, just not shown).
    Returns species-grouped batches for fast review.
    """
    conn = cdb.get_conn(readonly=True)

    # How many confirmed per species?
    confirmed = {}
    for r in conn.execute(
        "SELECT c.common_name, COUNT(*) as cnt "
        "FROM classifications c JOIN reviews r ON r.file = c.file "
        "WHERE r.verdict = 'correct' GROUP BY c.common_name"
    ).fetchall():
        confirmed[r[0]] = r[1]

    TARGET_PER_SPECIES = 50  # enough for retraining

    # Build the smart queue
    queue = {"corrections": [], "training": [], "uncertain": []}
    species_counts = {}

    # 1. Corrections — visit hero shots where signals say "probably wrong"
    #    Uses visits.best_file for one-per-visit dedup
    correction_rows = conn.execute("""
        SELECT v.best_file as file, v.species, v.best_confidence as confidence,
               v.frame_count, c.extra_json, c.source_timestamp
        FROM visits v
        JOIN classifications c ON c.file = v.best_file
        LEFT JOIN reviews r ON r.file = v.best_file
        WHERE r.file IS NULL AND v.best_file IS NOT NULL
        AND (c.extra_json LIKE '%probably_wrong%'
             OR c.extra_json LIKE '%prior_suggestion%'
             OR c.extra_json LIKE '%time_implausible%')
        ORDER BY v.best_confidence ASC
        LIMIT 500
    """).fetchall()

    for r in correction_rows:
        extra = _safe_json(r["extra_json"]) or {}
        queue["corrections"].append({
            "file": r["file"],
            "species": r["species"],
            "confidence": round(r["confidence"] or 0, 3),
            "frame_count": r["frame_count"],
            "trust_level": extra.get("trust_level", "normal"),
            "prior_suggestion": extra.get("prior_suggestion"),
            "reason": "correction",
        })

    # 2. Training samples — best shot per species that needs more confirmed data
    #    One per visit, highest confidence, for species below TARGET
    for r in conn.execute("""
        SELECT c.common_name, COUNT(DISTINCT v.id) as unreviewed_visits
        FROM visits v
        JOIN classifications c ON c.file = v.best_file
        LEFT JOIN reviews r ON r.file = v.best_file
        WHERE r.file IS NULL AND v.best_file IS NOT NULL AND c.common_name IS NOT NULL
        GROUP BY c.common_name
        ORDER BY unreviewed_visits DESC
    """).fetchall():
        species_name = r[0]
        have = confirmed.get(species_name, 0)
        need = max(0, TARGET_PER_SPECIES - have)
        species_counts[species_name] = {"have": have, "need": need, "unreviewed": r[1]}

        if need > 0:
            # Get the best N unreviewed visit hero shots
            samples = conn.execute("""
                SELECT v.best_file as file, v.species, v.best_confidence as confidence,
                       v.frame_count, c.source_timestamp
                FROM visits v
                JOIN classifications c ON c.file = v.best_file
                LEFT JOIN reviews r ON r.file = v.best_file
                WHERE r.file IS NULL AND c.common_name = ?
                ORDER BY v.best_confidence DESC
                LIMIT ?
            """, (species_name, min(need, 30))).fetchall()

            for s in samples:
                queue["training"].append({
                    "file": s["file"],
                    "species": s["species"],
                    "confidence": round(s["confidence"] or 0, 3),
                    "frame_count": s["frame_count"],
                    "reason": "training",
                    "have": have,
                    "need": need,
                })

    # 3. Uncertain — classifier confused (low score gap), one per visit
    uncertain_rows = conn.execute("""
        SELECT v.best_file as file, v.species, v.best_confidence as confidence,
               v.frame_count, c.extra_json
        FROM visits v
        JOIN classifications c ON c.file = v.best_file
        LEFT JOIN reviews r ON r.file = v.best_file
        WHERE r.file IS NULL AND v.best_file IS NOT NULL
        AND c.extra_json LIKE '%"classifier_uncertain": true%'
        AND c.extra_json NOT LIKE '%probably_wrong%'
        ORDER BY v.best_confidence ASC
        LIMIT 500
    """).fetchall()

    for r in uncertain_rows:
        queue["uncertain"].append({
            "file": r["file"],
            "species": r["species"],
            "confidence": round(r["confidence"] or 0, 3),
            "frame_count": r["frame_count"],
            "reason": "uncertain",
        })

    total_queue = len(queue["corrections"]) + len(queue["training"]) + len(queue["uncertain"])

    return {
        "total_review_queue": total_queue,
        "total_unreviewed_frames": sum(v["unreviewed"] for v in species_counts.values()),
        "corrections": len(queue["corrections"]),
        "training_samples": len(queue["training"]),
        "uncertain": len(queue["uncertain"]),
        "skipped": sum(v["unreviewed"] for v in species_counts.values()) - total_queue,
        "species_progress": dict(sorted(
            species_counts.items(), key=lambda x: -x[1]["need"]
        )[:15]),
        "queue": queue,
    }


@app.get("/api/review/batch")
def review_batch(species: str = "", trust: str = "", limit: int = 12):
    """Get a batch of same-species images for fast grid review.

    Args:
        species: Filter to one species (required for batch mode)
        trust: Filter by trust level: "likely_correct", "audio_confirmed", "uncertain", "probably_wrong"
        limit: Number of images (default 12, max 48)
    """
    limit = min(limit, 48)

    sp = normalize_species(species) if species else None
    rows = rdb.get_classifications(status="pending", species=sp, limit=limit)

    # Enrich with trust signals
    bconn = _birdnet_db()
    items = []
    for r in rows:
        extra = _safe_json(r["extra_json"]) if r["extra_json"] else {}
        if not isinstance(extra, dict):
            extra = {}

        also_heard = False
        if bconn and r["source_timestamp"] and r["source_date"] and r["original_species"]:
            ts = r["source_timestamp"]
            try:
                match = bconn.execute(
                    "SELECT 1 FROM notes WHERE date=? AND common_name=? "
                    "AND ABS("
                    "(CAST(SUBSTR(?,12,2) AS INTEGER)*3600+"
                    "CAST(SUBSTR(?,15,2) AS INTEGER)*60+"
                    "CAST(SUBSTR(?,18,2) AS INTEGER))"
                    "-"
                    "(CAST(SUBSTR(time,1,2) AS INTEGER)*3600+"
                    "CAST(SUBSTR(time,4,2) AS INTEGER)*60+"
                    "CAST(SUBSTR(time,7,2) AS INTEGER))"
                    ") <= 30 LIMIT 1",
                    (r["source_date"], r["original_species"], ts, ts, ts),
                ).fetchone()
                also_heard = match is not None
            except Exception:
                pass

        items.append({
            "file": r["file"],
            "species": r["original_species"],
            "confidence": round(r["confidence"] or 0, 3),
            "timestamp": r["source_timestamp"] or "",
            "also_heard": also_heard,
            "trust_level": extra.get("trust_level", "normal"),
            "prior_suggestion": extra.get("prior_suggestion"),
        })

    # Species summary for the batch
    conn = cdb.get_conn(readonly=True)
    species_counts = conn.execute(
        "SELECT c.common_name, COUNT(*) as cnt "
        "FROM classifications c "
        "LEFT JOIN reviews r ON r.file = c.file "
        "WHERE c.action='classified' AND c.common_name IS NOT NULL AND r.file IS NULL "
        "GROUP BY c.common_name ORDER BY cnt DESC LIMIT 20"
    ).fetchall()

    return {
        "items": items,
        "species_queue": [{"species": r[0], "count": r[1]} for r in species_counts],
        "total_unreviewed": sum(r[1] for r in species_counts),
    }


# 2026-04-25: legacy batch-confirm + batch-reject RETIRED — they used raw
# INSERT OR IGNORE INTO reviews and bypassed review_history. Replaced by
# /api/review2/batch-confirm and /api/review2/batch-reject which use
# insert_review per file. These stubs return 410 Gone if anything external
# still hits them.

@app.post("/api/review/batch-confirm")
async def batch_confirm_retired():
    raise HTTPException(status_code=410,
                        detail="Use /api/review2/batch-confirm (JSON body: {files, client_id?})")


@app.post("/api/review/batch-reject")
async def batch_reject_retired():
    raise HTTPException(status_code=410,
                        detail="Use /api/review2/batch-reject (JSON body: {files, correct_species?, client_id?})")


@app.get("/api/activity/daily-rhythm")
def daily_rhythm(window_days: int = Query(30, ge=1, le=365),
                 top_n: int = Query(12, ge=1, le=50),
                 camera: Optional[str] = Query(None)):
    """Daily-rhythm data: per-species visit counts bucketed by hour-of-day.

    Returns up to `top_n` species ranked by total activity in the window,
    each with a 24-length array of counts (one per hour, 00–23 local time).
    Frontend can normalize per-species and sort by peak hour for display.
    """
    from datetime import datetime, timedelta
    import sqlite3

    def _compute():
        end = datetime.now().date()
        start = end - timedelta(days=window_days)
        start_str = start.strftime("%Y-%m-%d")

        cam_clause = ""
        params = [start_str]
        if camera and camera != "all":
            cam_clause = " AND camera = ?"
            params.append(camera)

        conn = cdb.get_conn(readonly=True)
        rows = conn.execute(
            f"""
            SELECT common_name,
                   CAST(strftime('%H', source_timestamp) AS INTEGER) AS hour,
                   COUNT(*) AS n
            FROM classifications
            WHERE action = 'classified'
              AND source_date >= ?
              AND common_name IS NOT NULL AND common_name != ''
              {cam_clause}
            GROUP BY common_name, hour
            """,
            params,
        ).fetchall()

        # Pivot: species → [24 hourly counts]
        hours_by_species: dict = {}
        totals: dict = {}
        for common_name, hour, n in rows:
            if hour is None:
                continue
            arr = hours_by_species.setdefault(common_name, [0] * 24)
            arr[int(hour)] = int(n)
            totals[common_name] = totals.get(common_name, 0) + int(n)

        # Top-N by total activity
        top_species = sorted(totals, key=totals.get, reverse=True)[:top_n]

        species_out = []
        for name in top_species:
            arr = hours_by_species[name]
            peak_hour = max(range(24), key=lambda h: arr[h])
            species_out.append({
                "name": name,
                "hours": arr,
                "peak_hour": peak_hour,
                "total": totals[name],
            })

        return {
            "window_days": window_days,
            "window_start": start_str,
            "window_end": end.strftime("%Y-%m-%d"),
            "species": species_out,
        }

    cache_key = f"daily-rhythm:{window_days}:{top_n}:{camera}"
    return cached_result(cache_key, 300, _compute)


@app.get("/api/activity/season-stream")
def activity_season_stream(year: Optional[int] = Query(None)):
    """Season Stream — per-species weekly visit counts for a full year.

    Returns 52 weeks × N species, with each species' hottest weeks flagged.
    Species ordered by when they first appear in the year.
    """
    from datetime import datetime

    def _compute():
        conn = cdb.get_conn(readonly=True)
        target_year = year if year else datetime.now().year
        y_str = str(target_year)

        rows = conn.execute("""
            SELECT common_name,
                   CAST(strftime('%W', source_timestamp) AS INTEGER) AS week,
                   COUNT(*) AS n,
                   MIN(source_timestamp) AS first_ts
            FROM classifications
            WHERE action = 'classified'
              AND common_name IS NOT NULL AND common_name != ''
              AND strftime('%Y', source_timestamp) = ?
            GROUP BY common_name, week
            ORDER BY common_name, week
        """, (y_str,)).fetchall()

        # Pivot: species → 52 weekly counts
        by_species: dict = {}
        first_by_species: dict = {}
        for common_name, week, n, first_ts in rows:
            if week is None:
                continue
            arr = by_species.setdefault(common_name, [0] * 53)  # 0-52 weeks
            arr[int(week)] = int(n)
            # Track the species' first seen across all weeks for sort order
            prev = first_by_species.get(common_name)
            if prev is None or first_ts < prev:
                first_by_species[common_name] = first_ts

        # Sort species by their first appearance in the year (migrants last)
        species_sorted = sorted(by_species.keys(), key=lambda s: first_by_species.get(s, ""))

        species_out = []
        for name in species_sorted:
            hist = by_species[name]
            peak = max(hist) or 1
            total = sum(hist)
            species_out.append({
                "name": name,
                "weeks": hist[:52],  # trim to 52 (week 52 sometimes empty)
                "peak_week": max(range(52), key=lambda w: hist[w]),
                "first_seen": first_by_species.get(name),
                "total": total,
                "peak_count": peak,
            })

        return {
            "year": target_year,
            "species": species_out,
        }

    return cached_result(f"season-stream:{year}", 300, _compute)


@app.get("/api/activity/heard-vs-seen")
def activity_heard_vs_seen(window_days: int = Query(30, ge=1, le=365),
                           audio_confidence: float = Query(0.5, ge=0.1, le=1.0)):
    """Heard vs Seen — Venn data contrasting BirdNET audio detections
    against camera classifications over a time window.

    Returns three sets of species:
      - heard_only: in audio, not in cameras
      - both: in both
      - seen_only: in cameras, not in audio
    """
    from datetime import datetime, timedelta
    import sqlite3

    def _compute():
        end = datetime.now().date()
        start = end - timedelta(days=window_days)
        start_str = start.strftime("%Y-%m-%d")

        # Camera species
        conn = cdb.get_conn(readonly=True)
        seen_rows = conn.execute("""
            SELECT common_name, COUNT(*) AS n
            FROM classifications
            WHERE action = 'classified'
              AND source_date >= ?
              AND common_name IS NOT NULL AND common_name != ''
            GROUP BY common_name
        """, (start_str,)).fetchall()
        seen = {r[0]: r[1] for r in seen_rows}

        # Audio species
        heard = {}
        if BIRDNET_DB_PATH.exists():
            try:
                aconn = sqlite3.connect(str(BIRDNET_DB_PATH), timeout=5)
                audio_rows = aconn.execute("""
                    SELECT common_name, COUNT(*) AS n
                    FROM notes
                    WHERE date >= ?
                      AND confidence >= ?
                      AND common_name IS NOT NULL AND common_name != ''
                    GROUP BY common_name
                """, (start_str, audio_confidence)).fetchall()
                heard = {r[0]: r[1] for r in audio_rows}
                aconn.close()
            except Exception:
                heard = {}

        # Normalize names — strip whitespace so we compare fairly
        seen_names = {k.strip() for k in seen}
        heard_names = {k.strip() for k in heard}

        both = sorted(seen_names & heard_names)
        heard_only = sorted(heard_names - seen_names)
        seen_only = sorted(seen_names - heard_names)

        # Sort within each set by total count
        def by_count(name, bag):
            return bag.get(name) or bag.get(name.strip(), 0)

        both.sort(key=lambda n: (seen.get(n, 0) + heard.get(n, 0)), reverse=True)
        heard_only.sort(key=lambda n: heard.get(n, 0), reverse=True)
        seen_only.sort(key=lambda n: seen.get(n, 0), reverse=True)

        return {
            "window_days": window_days,
            "window_start": start_str,
            "window_end": end.strftime("%Y-%m-%d"),
            "audio_confidence": audio_confidence,
            "heard_only": {
                "count": len(heard_only),
                "species": [{"name": n, "audio_detections": heard.get(n, 0)} for n in heard_only],
            },
            "both": {
                "count": len(both),
                "species": [{
                    "name": n,
                    "audio_detections": heard.get(n, 0),
                    "camera_detections": seen.get(n, 0),
                } for n in both],
            },
            "seen_only": {
                "count": len(seen_only),
                "species": [{"name": n, "camera_detections": seen.get(n, 0)} for n in seen_only],
            },
        }

    cache_key = f"heard-vs-seen:{window_days}:{audio_confidence}"
    return cached_result(cache_key, 300, _compute)


@app.get("/api/activity/records")
def activity_records():
    """Record Book — superlative stats from the full classification history.

    Returns a handful of all-time superlatives that can populate the Activity
    tab's Record Book section. Queries are individually cheap and the whole
    response is cached for 5 minutes.
    """
    def _compute():
        conn = cdb.get_conn(readonly=True)
        out = {}

        # 1. Busiest day (most classifications in a single day)
        row = conn.execute("""
            SELECT source_date, COUNT(*) AS n, COUNT(DISTINCT common_name) AS species
            FROM classifications
            WHERE action = 'classified' AND source_date IS NOT NULL
            GROUP BY source_date
            ORDER BY n DESC
            LIMIT 1
        """).fetchone()
        if row:
            out["busiest_day"] = {
                "date": row[0], "visits": row[1], "species_count": row[2],
            }

        # 2. Most species in a day
        row = conn.execute("""
            SELECT source_date, COUNT(DISTINCT common_name) AS species, COUNT(*) AS visits
            FROM classifications
            WHERE action = 'classified' AND source_date IS NOT NULL
              AND common_name IS NOT NULL AND common_name != ''
            GROUP BY source_date
            ORDER BY species DESC, visits DESC
            LIMIT 1
        """).fetchone()
        if row:
            out["most_species_in_a_day"] = {
                "date": row[0], "species_count": row[1], "visits": row[2],
            }

        # 3. Rarest species (≥1 sighting, fewest across all time)
        row = conn.execute("""
            SELECT common_name, COUNT(*) AS n, MIN(source_timestamp) AS first_seen,
                   MAX(source_timestamp) AS last_seen
            FROM classifications
            WHERE action = 'classified'
              AND common_name IS NOT NULL AND common_name != ''
            GROUP BY common_name
            ORDER BY n ASC, last_seen DESC
            LIMIT 1
        """).fetchone()
        if row:
            out["rarest_species"] = {
                "species": row[0], "total_sightings": row[1],
                "first_seen": row[2], "last_seen": row[3],
            }

        # 4. Earliest sighting of the day (any day — the pre-dawn record)
        row = conn.execute("""
            SELECT common_name, source_timestamp,
                   substr(source_timestamp, 12, 8) AS time_of_day
            FROM classifications
            WHERE action = 'classified'
              AND common_name IS NOT NULL AND common_name != ''
              AND source_timestamp IS NOT NULL
              AND length(source_timestamp) >= 19
            ORDER BY time_of_day ASC, source_timestamp DESC
            LIMIT 1
        """).fetchone()
        if row:
            out["earliest_riser"] = {
                "species": row[0], "timestamp": row[1], "time": row[2],
            }

        # 5. Longest single visit (from visits table, if populated)
        try:
            row = conn.execute("""
                SELECT species, start_time, end_time, duration_sec
                FROM visits
                WHERE duration_sec IS NOT NULL AND species IS NOT NULL
                ORDER BY duration_sec DESC
                LIMIT 1
            """).fetchone()
            if row:
                out["longest_visit"] = {
                    "species": row[0], "start": row[1], "end": row[2],
                    "duration_sec": float(row[3]) if row[3] is not None else None,
                }
        except Exception:
            pass  # visits table absent or schema mismatch — skip gracefully

        # 6. First arrival of the current year
        from datetime import datetime
        year = datetime.now().strftime("%Y")
        row = conn.execute("""
            SELECT common_name, source_timestamp
            FROM classifications
            WHERE action = 'classified'
              AND common_name IS NOT NULL AND common_name != ''
              AND strftime('%Y', source_timestamp) = ?
            ORDER BY source_timestamp ASC
            LIMIT 1
        """, (year,)).fetchone()
        if row:
            out["first_arrival_this_year"] = {
                "species": row[0], "timestamp": row[1], "year": year,
            }

        # Total all-time for context
        row = conn.execute("""
            SELECT COUNT(*) FROM classifications
            WHERE action = 'classified' AND common_name IS NOT NULL
        """).fetchone()
        if row:
            out["total_classifications"] = row[0]

        return out

    return cached_result("activity-records", 300, _compute)


@app.get("/api/activity/first-arrivals")
def activity_first_arrivals(years: int = Query(2, ge=1, le=5)):
    """First Arrivals archive — first date each species was seen in each year.

    Returns the last `years` calendar years of first-arrival dates per species.
    Species flagged `first_ever=True` when the MIN timestamp in a given year
    is ALSO the species' all-time MIN.
    """
    from datetime import datetime

    def _compute():
        conn = cdb.get_conn(readonly=True)
        current_year = datetime.now().year
        year_list = list(range(current_year, current_year - years, -1))  # newest first

        # All-time first appearance per species (for the `first_ever` flag)
        all_time_firsts = {
            r[0]: r[1] for r in conn.execute("""
                SELECT common_name, MIN(source_timestamp)
                FROM classifications
                WHERE action = 'classified' AND common_name IS NOT NULL
                  AND common_name != ''
                GROUP BY common_name
            """).fetchall()
        }

        out = []
        for y in year_list:
            y_str = str(y)
            rows = conn.execute("""
                SELECT common_name, MIN(source_timestamp) AS first_in_year
                FROM classifications
                WHERE action = 'classified'
                  AND common_name IS NOT NULL AND common_name != ''
                  AND strftime('%Y', source_timestamp) = ?
                GROUP BY common_name
                ORDER BY first_in_year ASC
            """, (y_str,)).fetchall()
            entries = []
            for common_name, first_ts in rows:
                entries.append({
                    "species": common_name,
                    "first_seen": first_ts,
                    "first_ever": (all_time_firsts.get(common_name) == first_ts),
                })
            if entries:
                out.append({"year": y, "entries": entries})
        return {"years": out, "current_year": current_year}

    return cached_result(f"first-arrivals:{years}", 300, _compute)


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
        species_data = cdb.get_species_list(date, camera)
        # Enrich with visit counts
        try:
            cam = None if (not camera or camera == "all") else camera
            visit_summary = vdb.get_visit_summary(date) if date and date != "all" else []
            visit_map = {s["species"]: s["visits"] for s in visit_summary}
            if isinstance(species_data, list):
                for item in species_data:
                    name = item.get("species") or item.get("name", "")
                    item["visit_count"] = visit_map.get(name, 0)
            elif isinstance(species_data, dict) and "species" in species_data:
                for item in species_data["species"]:
                    name = item.get("species") or item.get("name", "")
                    item["visit_count"] = visit_map.get(name, 0)
        except Exception:
            pass  # Don't break existing endpoint if visit enrichment fails
        return species_data
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
    """Serve an annotated image, falling back to classified or raw image."""
    safe_name = os.path.basename(filename)
    # Try annotated first (has bounding boxes)
    path = ANNOTATED_DIR / safe_name
    if path.exists():
        return FileResponse(str(path), media_type="image/jpeg")
    # Fall back to classified directory (any species subdirectory)
    classified = _find_classified_file(safe_name)
    if classified:
        return FileResponse(str(classified), media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Image not found")


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
        # Boxes from the classifications DB come back as floats (YOLO box
        # coords are floats); accept both int- and float-string forms.
        coords = [int(float(x)) for x in box.split(",")]
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


SECOND_OPINION_DIR = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Second Opinion"


# 2026-04-25: legacy /api/review/second-opinion/{filename} RETIRED.
# Replaced by /api/review2/second-opinion/{filename}.

@app.post("/api/review/second-opinion/{filename}")
def save_second_opinion_retired(filename: str):
    raise HTTPException(status_code=410,
                        detail="Use /api/review2/second-opinion/{filename}")


@app.get("/api/review/second-opinion")
def list_second_opinions():
    """List images in the second-opinion folder."""
    SECOND_OPINION_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(SECOND_OPINION_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
    return {
        "count": len(files),
        "files": [{"name": f.name, "size": f.stat().st_size} for f in files[:50]],
    }


@app.get("/api/review/pending")
def review_pending(species: str = "", offset: int = 0, limit: int = 50, multibird: str = "", camera: str = ""):
    """Get unreviewed classifications for the annotation GUI (paginated).

    Uses SQL LEFT JOIN via reviews_db — no in-memory cross-reference needed.
    """
    sp = species or None
    mb = bool(multibird)
    cam = camera or None

    rows = rdb.get_classifications(status="pending", species=sp, multibird=mb, camera=cam, offset=offset, limit=limit)
    remaining = rdb.count_classifications(status="pending", species=sp, multibird=mb, camera=cam)

    # Build response items from SQL rows, with audio corroboration check
    bconn = _birdnet_db()
    pending = []
    for r in rows:
        birds = _safe_json(r["birds_json"]) if r.get("birds_json") else []
        top3 = _safe_json(r["top3_json"]) if r.get("top3_json") else []
        raw_top3 = _safe_json(r["raw_top3_json"]) if r.get("raw_top3_json") else []
        # Extract intelligence signals from extra_json
        extra = _safe_json(r.get("extra_json")) if r.get("extra_json") else {}
        if not isinstance(extra, dict):
            extra = {}
        item = {
            "file": r["file"],
            "timestamp": r.get("source_timestamp") or "",
            "species": r["original_species"],
            "confidence": r["confidence"],
            "raw_score": r.get("raw_score", 0),
            "top3": top3 or [],
            "raw_top3": raw_top3 or [],
            "birds": birds or [],
            "also_heard": False,
            # Intelligence signals (from classify.py's yard_prior + visit_voter)
            "trust_level": extra.get("trust_level", "normal"),
            "prior_suggestion": extra.get("prior_suggestion"),
            "prior_reason": extra.get("prior_reason"),
            "classifier_uncertain": extra.get("classifier_uncertain", False),
            "score_gap": extra.get("score_gap"),
            "audio_corroborated": extra.get("audio_corroborated", False),
        }
        # Visit consensus
        vc = extra.get("visit_consensus")
        if vc and vc.get("is_outlier"):
            item["visit_outlier"] = True
            item["consensus_species"] = vc.get("consensus_species")
            item["trust_level"] = "probably_wrong"
        # Check if BirdNET heard the same species within +/-30s
        ts = r.get("source_timestamp") or ""
        date_part = r.get("source_date") or ""
        if bconn and ts and date_part and r["original_species"]:
            try:
                audio_match = bconn.execute(
                    "SELECT 1 FROM notes WHERE date=? AND common_name=? "
                    "AND ABS("
                    "(CAST(SUBSTR(?,12,2) AS INTEGER)*3600+"
                    "CAST(SUBSTR(?,15,2) AS INTEGER)*60+"
                    "CAST(SUBSTR(?,18,2) AS INTEGER))"
                    "-"
                    "(CAST(SUBSTR(time,1,2) AS INTEGER)*3600+"
                    "CAST(SUBSTR(time,4,2) AS INTEGER)*60+"
                    "CAST(SUBSTR(time,7,2) AS INTEGER))"
                    ") <= 30 LIMIT 1",
                    (date_part, r["original_species"], ts, ts, ts),
                ).fetchone()
                if audio_match:
                    item["also_heard"] = True
            except Exception:
                pass
        pending.append(item)

    total_classified = cdb.count_classified()
    total_reviewed = rdb.count_reviews()

    # Species list: distinct species from unreviewed classifications
    species_list = rdb.list_classification_species(status="pending", camera=cam)

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


# 2026-04-25: legacy /api/review/rerun-missed RETIRED.
# Replaced by /api/review2/rerun-missed (accepts optional client_id).

@app.post("/api/review/rerun-missed")
def rerun_missed_retired():
    raise HTTPException(status_code=410,
                        detail="Use /api/review2/rerun-missed")


@app.get("/api/review/goals")
def review_goals(threshold: int = 50, camera: str = ""):
    """Species classification goals — which species need more confirmed reviews for training.

    Uses SQL aggregation via reviews_db instead of in-memory iteration.
    """
    regional = load_regional_species()
    cam = camera or None
    raw_goals = rdb.get_review_goals(regional, threshold, camera=cam)

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


# 2026-04-25: legacy /api/review/{filename} RETIRED.
# UI migrated to /api/review2/{filename} (JSON body, client_id idempotent).

@app.post("/api/review/{filename}")
def submit_review_retired(filename: str):
    raise HTTPException(status_code=410,
                        detail="Use /api/review2/{filename} (JSON body: {verdict, correct_species?, client_id?})")


# ── Airtight review API (2026-04-24) ──
# New endpoint family: /api/review2/*. Accepts JSON body with client_id for
# idempotency. DB write happens first (via reviews_db.insert_review which
# appends to review_history and upserts reviews), then file move via
# apply_verdict. See docs/superpowers/specs/2026-04-23-airtight-review-system.md
#
# IMPORTANT: specific routes (batch-confirm, batch-reject, etc.) must be
# declared BEFORE the catch-all /api/review2/{filename} so FastAPI's
# first-match router doesn't swallow them as filename="batch-confirm".


@app.post("/api/review2/batch-confirm")
async def batch_confirm2(body: dict = Body(default=None)):
    """Airtight bulk confirm. Body: {files: list, client_id?}.

    Writes one review_history row per file (verdict='correct') via
    reviews_db.insert_review. This replaces the legacy /api/review/batch-confirm
    which used raw SQL and bypassed the audit trail.

    Idempotency: pass a `client_id` (e.g. a UUID v4 generated client-side).
    Each file gets its own per-file client_id of `{client_id}:{filename}` so
    replays are no-ops at the file level.
    """
    body = body or {}
    files = body.get("files") or []
    if not isinstance(files, list):
        files = []
    client_id = body.get("client_id")
    count = 0
    for fname in files:
        try:
            review = _create_review_entry(fname, "correct", "", "false", "0")
            review["reviewer"] = "batch-review"
            if client_id:
                review["client_id"] = f"{client_id}:{fname}"
            rdb.insert_review(review)
            count += 1
        except Exception as e:
            logging.warning("batch-confirm2 failed for %s: %s", fname, e)
    if count:
        invalidate_cache("stats:", "species:", "goals:", "highlights:",
                         "profile:", "weekly_snapshot")
    return {"confirmed": count}


@app.post("/api/review2/batch-reject")
async def batch_reject2(body: dict = Body(default=None)):
    """Airtight bulk reject. Body: {files: list, correct_species?, client_id?}.

    Writes one review_history row per file (verdict='wrong') and calls
    apply_verdict to move files into the correct species directory (or trash
    if correct_species == 'not_a_bird'). Replaces the legacy raw-SQL version.
    """
    body = body or {}
    files = body.get("files") or []
    if not isinstance(files, list):
        files = []
    correct_species_raw = body.get("correct_species", "")
    client_id = body.get("client_id")
    # Preserve the 'not_a_bird' sentinel through normalize_species.
    if correct_species_raw == "not_a_bird":
        correct = "not_a_bird"
    else:
        correct = normalize_species(correct_species_raw) if correct_species_raw else ""
    count = 0
    for fname in files:
        try:
            review = _create_review_entry(fname, "wrong", correct, "false", "0")
            review["reviewer"] = "batch-review"
            if client_id:
                review["client_id"] = f"{client_id}:{fname}"
            result = rdb.insert_review(review)
            # apply_verdict only on non-duplicates to keep idempotency honest.
            if not result.get("duplicate"):
                apply_verdict(fname, "wrong", correct)
            count += 1
        except Exception as e:
            logging.warning("batch-reject2 failed for %s: %s", fname, e)
    if count:
        invalidate_cache("stats:", "species:", "goals:", "highlights:",
                         "profile:", "weekly_snapshot")
    return {"rejected": count, "correct_species": correct}


@app.post("/api/review2/rerun-missed")
def rerun_missed2(body: dict = Body(default=None)):
    """Airtight rerun-missed. Body: {client_id?}.

    For each file with verdict=reclassify:
    1. Move from classified/*/ → incoming/
    2. Delete annotated copy
    3. Write verdict=requeued review_history row via insert_review
    """
    body = body or {}
    client_id = body.get("client_id")

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
        if client_id:
            requeue_entry["client_id"] = f"{client_id}:{fname}"
        rdb.insert_review(requeue_entry)

    if moved:
        invalidate_cache("stats:", "species:", "goals:", "highlights:",
                         "profile:", "weekly_snapshot")
    return {
        "moved": moved,
        "not_found": not_found,
        "message": f"Requeued {moved} files for reclassification"
                   + (f" ({not_found} not found on disk)" if not_found else ""),
    }


@app.post("/api/review2/second-opinion/{filename}")
def save_second_opinion2(filename: str, body: dict = Body(default=None)):
    """Airtight second-opinion: save a cropped bird image for external ID.

    This is not a DB mutation — it's a file save. Idempotency here is "same
    file name wins, last write stays". Body is optional; {client_id?} is
    accepted for future use but not required (crop is always safe to rewrite).
    """
    import io
    from PIL import Image as PILImage

    safe_name = os.path.basename(filename)
    path = _find_any_image(safe_name)
    if not path:
        raise HTTPException(status_code=404, detail="Image not found")

    entry = cdb.get_entry_by_file(safe_name)
    if not entry or not entry.get("best_detection"):
        raise HTTPException(status_code=400, detail="No detection data for this image")

    box = entry["best_detection"].get("box")
    species = entry.get("common_name", "unknown")

    img = PILImage.open(path)
    w, h = img.size

    if box:
        x1, y1, x2, y2 = [int(b) for b in box]
        bw, bh = x2 - x1, y2 - y1
        pad_x, pad_y = int(bw * 0.15), int(bh * 0.15)
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(w, x2 + pad_x)
        y2 = min(h, y2 + pad_y)
        crop = img.crop((x1, y1, x2, y2))
    else:
        crop = img

    SECOND_OPINION_DIR.mkdir(parents=True, exist_ok=True)
    safe_species = species.replace(" ", "_").replace("'", "")
    ts = entry.get("source_timestamp", "").replace(" ", "_").replace(":", "-")
    out_name = f"{safe_species}_{ts}.jpg"
    out_path = SECOND_OPINION_DIR / out_name
    crop.save(str(out_path), quality=95)
    img.close()
    crop.close()

    logging.info("Second opinion saved: %s → %s", safe_name, out_name)
    return {"status": "ok", "saved": out_name, "path": str(out_path)}


@app.post("/api/review2/{filename}")
def submit_review2(filename: str, body: dict = Body(...)):
    """Airtight review submit. Body: {verdict, correct_species?, missed_birds?, bird_index?, client_id?}.
    Same client_id twice = no-op; returns first history_id + duplicate=True.
    """
    verdict = body.get("verdict")
    if not verdict:
        raise HTTPException(status_code=400, detail="verdict required")
    correct_species = body.get("correct_species", "")
    missed_birds = str(body.get("missed_birds", False)).lower()
    bird_index = str(body.get("bird_index", 0))
    client_id = body.get("client_id")

    review = _create_review_entry(filename, verdict, correct_species, missed_birds, bird_index)
    if client_id:
        review["client_id"] = client_id
    result = rdb.insert_review(review)
    invalidate_cache("stats:", "species:", "goals:", "highlights:", "profile:", "weekly_snapshot")

    # File move happens AFTER DB commit. If this fails, the audit catches it.
    # A duplicate (same client_id) skips the file move — idempotent.
    if not result.get("duplicate"):
        apply_verdict(review["file"], verdict, review.get("correct_species", ""))

    return {
        "ok": True,
        "history_id": result["history_id"],
        "prev_row_id": result.get("prev_row_id"),
        "duplicate": result["duplicate"],
    }


@app.get("/api/review2/history/{filename}")
def review2_history(filename: str):
    """Full audit trail for a file. Oldest first. Empty list if unreviewed."""
    return {"file": filename, "history": rdb.get_history(filename)}


@app.get("/api/review2/queue")
def review2_queue(limit: int = 20, after: str = "", species: str = "",
                  camera: str = ""):
    """Pending review queue with KEYSET pagination.

    Parameters:
      limit:   page size (default 20)
      after:   cursor — the `timestamp` of the last item on the previous page.
               Omit for first page. Cursor is stable under trash-mutations:
               the query uses `WHERE timestamp < after` so items earlier
               than the cursor are unaffected by anything reviewed since.
      species: filter by effective species name
      camera:  filter by camera

    Returns: {items: [...], next_cursor: str|null}
    """
    conn = rdb.get_conn(readonly=True)

    where = [
        "c.action = 'classified'",
        "c.common_name IS NOT NULL",
        "(r.file IS NULL OR r.verdict = 'requeued')",
    ]
    params = []
    if after:
        where.append("c.timestamp < ?")
        params.append(after)
    if species:
        where.append("c.common_name = ?")
        params.append(species)
    if camera:
        where.append("c.camera = ?")
        params.append(camera)
    where_sql = " AND ".join(where)

    # Fetch limit+1 to know if there's a next page without a separate count query
    sql = (
        f"SELECT c.file, c.common_name, c.camera, c.confidence, c.raw_score, "
        f"       c.timestamp, c.source_timestamp, c.best_detection_json, c.top3_json "
        f"FROM classifications c "
        f"LEFT JOIN reviews r ON r.file = c.file "
        f"WHERE {where_sql} "
        f"ORDER BY c.timestamp DESC "
        f"LIMIT ?"
    )
    params_with_limit = params + [limit + 1]
    rows = conn.execute(sql, params_with_limit).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]

    items = []
    for r in page:
        items.append({
            "file": r["file"],
            "species": r["common_name"],
            "camera": r["camera"],
            "confidence": r["confidence"] or 0.0,
            "raw_score": r["raw_score"] or 0,
            "timestamp": r["timestamp"],
            "source_timestamp": r["source_timestamp"],
        })

    next_cursor = page[-1]["timestamp"] if has_more and page else None
    return {"items": items, "next_cursor": next_cursor}


# ── Model Lab: registry of candidate classifiers for A/B demo ──
# Lazily-built so it doesn't slow dashboard startup if models aren't present.
# Two views:
#   _lab_registry: for the upload-test box. Exposes all candidates including
#     Hailo ones (classifier runs alone in this code path).
#   _pipeline_view_registry: for "what can the live pipeline actually use".
#     Excludes Hailo candidates because the pipeline also owns a Hailo
#     detector slot (Hailo-8L has 1 vdevice). Used by /api/models/list and
#     the /switch validation.
_lab_registry = None
_pipeline_view_registry = None


def _get_model_registry():
    """Lab registry (includes Hailo candidates for upload-test)."""
    global _lab_registry
    if _lab_registry is None:
        import sys
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from pipeline.model_registry import build_default_registry
        _lab_registry = build_default_registry(str(repo / "models"))
    return _lab_registry


def _get_pipeline_view_registry():
    """Registry reflecting the PIPELINE's constraints. With HailoEngine
    cohabitation (playbook §9 Path 1), Hailo classifiers can run alongside
    the live Hailo detector, so this is now structurally identical to
    _get_model_registry — kept as a separate state holder to preserve the
    cache lifetime semantics callers depend on (and so we can re-introduce
    pipeline-specific filtering later if needed)."""
    global _pipeline_view_registry
    if _pipeline_view_registry is None:
        import sys
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from pipeline.model_registry import build_default_registry
        _pipeline_view_registry = build_default_registry(str(repo / "models"))
    return _pipeline_view_registry


def _pi_active_classifier() -> Optional[str]:
    """Read the pipeline's active classifier on Pi by parsing the env file.

    The dashboard's in-process registry is for the Lab's upload-test only.
    The PIPELINE process owns the real classifier — and its startup selection
    is driven by PI_CLASSIFIER in ~/.bird-observatory-env. That file is the
    single source of truth for what's classifying birds right now.
    """
    env_path = Path.home() / ".bird-observatory-env"
    if not env_path.exists():
        return None
    for ln in env_path.read_text().splitlines():
        if ln.strip().startswith("PI_CLASSIFIER="):
            return ln.split("=", 1)[1].strip()
    return None


@app.get("/api/models/list")
def api_models_list():
    """List candidate classifiers + which one drives the live pipeline.

    On Pi (PI_MODE=1): `current` is the pipeline's classifier as recorded
    in ~/.bird-observatory-env (PI_CLASSIFIER=...). The Lab's in-process
    registry is irrelevant for "what's classifying birds right now"; it's
    only for upload-test. If the pipeline is mid-restart, `current` is
    still the target name — the pipeline will match it when it comes back.

    On iMac: `current` is the Lab's in-process registry.
    """
    try:
        # Use the pipeline-view registry so availability reflects what the
        # live pipeline can actually run.
        r = _get_pipeline_view_registry()
        candidates = r.list()
        if os.environ.get("PI_MODE", "0") == "1":
            pi_active = _pi_active_classifier()
            # Mark the pipeline's active one as active in the UI.
            for c in candidates:
                c["active"] = (c["name"] == pi_active)
            return {"current": pi_active, "candidates": candidates}
        return {"current": r.current_name, "candidates": candidates}
    except Exception as e:
        return JSONResponse({"error": str(e), "candidates": []}, status_code=500)


def _pi_update_env_classifier(name: str) -> dict:
    """On the Pi, rewrite ~/.bird-observatory-env so PI_CLASSIFIER=<name>, then
    systemctl-restart bird-pipeline so the running pipeline picks it up.

    This is the "reboot-based hot-swap": not true live IPC, but a real switch
    — the actual running classifier changes after ~5-10 seconds. Honest.

    Returns {ok: True, restart_in_progress: True} on success, or raises.
    """
    import subprocess
    env_path = Path.home() / ".bird-observatory-env"
    if not env_path.exists():
        # First-time: create with just the classifier line.
        env_path.write_text(f"PI_CLASSIFIER={name}\n")
    else:
        # Rewrite: update PI_CLASSIFIER=... in place (preserve other vars).
        lines = env_path.read_text().splitlines()
        new_lines = []
        found = False
        for ln in lines:
            if ln.strip().startswith("PI_CLASSIFIER="):
                new_lines.append(f"PI_CLASSIFIER={name}")
                found = True
            else:
                new_lines.append(ln)
        if not found:
            new_lines.append(f"PI_CLASSIFIER={name}")
        env_path.write_text("\n".join(new_lines) + "\n")
    # Restart the pipeline via systemd-user.
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "bird-pipeline"],
            check=True, capture_output=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail=f"systemctl restart failed: {e.stderr.decode('utf-8', 'replace')[:200]}"
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500, detail="systemctl not available on this host"
        )
    return {"ok": True, "restart_in_progress": True, "active": name}


@app.post("/api/models/switch")
def api_models_switch(body: dict = Body(...)):
    """Switch the live pipeline's classifier.

    On Pi (PI_MODE=1): writes PI_CLASSIFIER=<name> to ~/.bird-observatory-env
    and `systemctl --user restart bird-pipeline`. The running classifier
    actually changes after the restart (~5-10s). Returns
    {ok, restart_in_progress: True, active}.

    On iMac (no PI_MODE): in-process switch of the dashboard's lab registry.
    Returns {ok, current, noop?}.

    Body: {name: str}.
    """
    name = (body or {}).get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    # Validate against the PIPELINE-view registry — on Pi this rejects Hailo
    # classifiers because they'd conflict with the Hailo detector slot.
    r = _get_pipeline_view_registry()
    cand = r.candidates.get(name)
    if cand is None:
        raise HTTPException(status_code=400, detail=f"unknown model: {name}")
    if cand.is_placeholder() or not cand.available:
        raise HTTPException(
            status_code=400,
            detail=f"not available: {name} ({cand.notes or 'no availability reason'})",
        )

    if os.environ.get("PI_MODE", "0") == "1":
        return _pi_update_env_classifier(name)
    # iMac path: in-process switch via the lab registry too (so upload-test
    # reflects the new choice).
    lab = _get_model_registry()
    result = lab.switch(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "switch failed"))
    return result


@app.post("/api/models/classify-upload")
async def api_models_classify_upload(file: UploadFile = File(...)):
    """Classify an uploaded image with the Lab's current model.
    Returns {model, predictions: [{common_name, scientific_name, raw_score}]}.
    """
    from PIL import Image
    import io
    r = _get_model_registry()
    if r.current_name is None:
        raise HTTPException(status_code=400, detail="no model loaded")
    data = await file.read()
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad image: {e}")
    preds = r.classify(img)
    return {"model": r.current_name, "predictions": preds}


@app.post("/api/review2/undo/{history_id}")
def review2_undo(history_id: int, body: dict = Body(default=None)):
    """Append an `undone` entry + restore the previous state in `reviews`.
    Body: {client_id?} for idempotency.
    """
    client_id = (body or {}).get("client_id") if body else None
    result = rdb.undo(history_id, client_id=client_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "undo failed"))
    invalidate_cache("stats:", "species:", "goals:", "highlights:", "profile:", "weekly_snapshot")
    return result


@app.get("/api/review/classified")
def review_classified(species: str = "", verdict: str = "", limit: int = 50, offset: int = 0):
    """Get reviewed classifications (correct, wrong, reclassify verdicts).

    Uses SQL JOIN via reviews_db instead of batch file lookup.
    """
    sp = species or None
    v = verdict or None
    rows = rdb.get_classifications(status="reviewed", species=sp, verdict=v, offset=offset, limit=limit)
    total = rdb.count_classifications(status="reviewed", species=sp, verdict=v)
    species_list = rdb.list_classification_species(status="reviewed")

    items = []
    for r in rows:
        best_det = json.loads(r["best_detection_json"]) if r.get("best_detection_json") else {}
        is_corrected = (r["verdict"] == "wrong" and r.get("correct_species")
                        and r["species"] != r["original_species"])
        items.append({
            "file": r["file"],
            "species": r["species"],
            "original_species": r["original_species"],
            "confidence": best_det.get("confidence", 0) if best_det else r.get("confidence", 0),
            "verdict": r["verdict"],
            "correct_species": r.get("correct_species", ""),
            "is_corrected": is_corrected,
            "missed_birds": bool(r.get("missed_birds", False)),
            "review_timestamp": r.get("review_timestamp", ""),
            "source_timestamp": r.get("source_timestamp", ""),
        })

    return {"items": items, "total": total, "species_list": species_list}


# 2026-04-25: legacy /api/review/{filename}/update RETIRED.
# UI migrated to /api/review2/{filename} which handles updates via
# append-only review_history (newest row wins in the reviews cache).

@app.post("/api/review/{filename}/update")
def update_review_retired(filename: str):
    raise HTTPException(status_code=410,
                        detail="Use /api/review2/{filename}")


@app.get("/api/dates")
def available_dates():
    """Return list of dates that have classified detections, newest first."""
    def _compute():
        return cdb.get_dates()
    return cached_result("dates:all", 60, _compute)


@app.get("/api/species-profile/{name}")
def species_profile(name: str):
    """Your yard's data for a species — first seen, visits, peak hours, flock size."""
    def _compute():
        conn = cdb.get_conn(readonly=True)
        n = normalize_species(name)

        # First and last seen
        dates = conn.execute(
            "SELECT MIN(source_date) as first, MAX(source_date) as last, COUNT(*) as total "
            "FROM classifications WHERE action='classified' AND common_name=?", (n,)
        ).fetchone()

        if not dates or not dates[0]:
            return {"species": n, "found": False}

        # Days active
        days = conn.execute(
            "SELECT COUNT(DISTINCT source_date) FROM classifications "
            "WHERE action='classified' AND common_name=?", (n,)
        ).fetchone()[0]

        # Peak hour
        peak = conn.execute(
            "SELECT CAST(SUBSTR(source_timestamp, 12, 2) AS INTEGER) as hr, COUNT(*) as cnt "
            "FROM classifications WHERE action='classified' AND common_name=? "
            "GROUP BY hr ORDER BY cnt DESC LIMIT 3", (n,)
        ).fetchall()
        peak_hours = [{"hour": r[0], "count": r[1]} for r in peak]

        # Visit stats
        avg_visit = None
        avg_flock = None
        total_visits = 0
        try:
            vconn = vdb.get_conn(readonly=True)
            vstats = vconn.execute(
                "SELECT COUNT(*) as visits, ROUND(AVG(duration_sec)) as avg_dur, "
                "ROUND(AVG(bird_count),1) as avg_flock, MAX(bird_count) as max_flock "
                "FROM visits WHERE species=?", (n,)
            ).fetchone()
            if vstats and vstats[0]:
                total_visits = vstats[0]
                avg_visit = int(vstats[1] or 0)
                avg_flock = float(vstats[2] or 1)
                max_flock = vstats[3] or 1
        except Exception:
            max_flock = 1

        # Audio detections
        audio_count = 0
        bconn = _birdnet_db()
        if bconn:
            try:
                arow = bconn.execute(
                    "SELECT COUNT(*) FROM notes WHERE common_name=?", (n,)
                ).fetchone()
                audio_count = arow[0] if arow else 0
            except Exception:
                pass

        # Recent trend (last 7 days by day)
        recent = conn.execute(
            "SELECT source_date, COUNT(*) FROM classifications "
            "WHERE action='classified' AND common_name=? "
            "AND source_date >= date('now', '-7 days') "
            "GROUP BY source_date ORDER BY source_date", (n,)
        ).fetchall()
        recent_trend = [{"date": r[0], "count": r[1]} for r in recent]

        return {
            "species": n,
            "found": True,
            "first_seen": dates[0],
            "last_seen": dates[1],
            "total_sightings": dates[2],
            "days_active": days,
            "audio_detections": audio_count,
            "total_visits": total_visits,
            "avg_visit_seconds": avg_visit,
            "avg_flock_size": avg_flock,
            "max_flock_size": max_flock if total_visits else None,
            "peak_hours": peak_hours,
            "recent_trend": recent_trend,
        }

    return cached_result(f"profile:{name}", 120, _compute)


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
    rows = rdb.get_classifications(status="reviewed", verdict="skip", offset=offset, limit=limit)
    total = rdb.count_classifications(status="reviewed", verdict="skip")

    skipped = []
    for r in rows:
        skipped.append({
            "file": r["file"],
            "species": r.get("original_species", "Unknown") or "Unknown",
            "timestamp": r.get("review_timestamp", ""),
            "source_timestamp": r.get("source_timestamp", ""),
        })

    return {"files": skipped, "total": total}


@app.get("/api/review/missed")
def missed_birds_list(limit: int = 200, offset: int = 0):
    """List images flagged as missed birds (verdict='reclassify')."""
    rows = rdb.get_classifications(status="missed", offset=offset, limit=limit)
    total = rdb.count_classifications(status="missed")

    items = []
    for r in rows:
        best_det = json.loads(r["best_detection_json"]) if r.get("best_detection_json") else {}
        items.append({
            "file": r["file"],
            "species": r["species"],
            "original_species": r["original_species"],
            "confidence": best_det.get("confidence", 0) if best_det else r.get("confidence", 0),
            "source_timestamp": r.get("source_timestamp", ""),
        })

    return {"items": items, "total": total}


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
_FOOD_DB = BIRDNET_DB_PATH  # food_log table lives in the same DB as birdnet notes

# --- Thread-local connection pools for food_log and birdnet DBs ---
_food_db_local = threading.local()
_birdnet_db_local = threading.local()


def _get_food_conn():
    """Thread-local pooled connection to the food/birdnet DB (same file)."""
    if not hasattr(_food_db_local, 'conn') or _food_db_local.conn is None:
        _food_db_local.conn = sqlite3.connect(str(_FOOD_DB), timeout=5)
        _food_db_local.conn.execute("PRAGMA journal_mode=WAL")
        _food_db_local.conn.row_factory = sqlite3.Row
    return _food_db_local.conn


def _get_birdnet_conn():
    """Thread-local pooled connection to the BirdNET database."""
    if not BIRDNET_DB_PATH.exists():
        return None
    if not hasattr(_birdnet_db_local, 'conn') or _birdnet_db_local.conn is None:
        _birdnet_db_local.conn = sqlite3.connect(str(BIRDNET_DB_PATH), timeout=5)
        _birdnet_db_local.conn.execute("PRAGMA journal_mode=WAL")
        _birdnet_db_local.conn.row_factory = sqlite3.Row
    return _birdnet_db_local.conn


# Cache for birdnet summary (regenerated when DB changes)
_birdnet_summary_cache = None
_birdnet_summary_mtime = 0.0
_birdnet_last_id = 0  # for SSE polling


def _birdnet_db():
    """Get a thread-local pooled connection to the BirdNET database."""
    return _get_birdnet_conn()


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

    except Exception:
        raise


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
            cur = conn.cursor()
            cur.execute("SELECT MAX(id) FROM notes")
            row = cur.fetchone()
            _birdnet_last_id = row[0] or 0

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
                    # Note: do NOT write _birdnet_last_id here — multiple SSE
                    # clients would race on the global. Each generator tracks
                    # its own local last_id independently.

                    # Invalidate summary cache on new detection
                    global _birdnet_summary_mtime
                    _birdnet_summary_mtime = 0

            except Exception as exc:
                logging.warning("[BirdNET SSE] DB poll error: %s", exc)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/audio-best-clips")
def audio_best_clips(date: str = None, since: str = None, source: str = None):
    """Return the single best (highest confidence) clip per species.

    Used by the Audio tab to show a play button on each species card
    without fetching all detections. One SQL query, one row per species.

    Params:
      date: specific date (YYYY-MM-DD) or "all"
      since: clips from this date onwards (YYYY-MM-DD) — used for all-time
             view where old clips may have been deleted from disk
      source: filter by mic source (ground, magnolia)
    """
    conn = _birdnet_db()
    if not conn:
        return {"clips": []}

    where = []
    params = []
    if since:
        where.append("date >= ?")
        params.append(since)
    elif date and date != "all":
        where.append("date = ?")
        params.append(date)
    if source:
        where.append("source = ?")
        params.append(source)
    # Only rows that have a clip
    where.append("clip_name IS NOT NULL AND clip_name != '' AND has_clip = 1")
    where_clause = "WHERE " + " AND ".join(where)

    try:
        cur = conn.cursor()
        # Get the row with highest confidence per species using SQLite window function.
        # ROW_NUMBER() picks exactly one row per species — the one with the highest
        # confidence and a valid clip_name.
        cur.execute(f"""
            SELECT common_name, confidence as best_conf,
                   clip_name, date, time, source
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY common_name
                    ORDER BY confidence DESC
                ) as rn
                FROM notes
                {where_clause}
            )
            WHERE rn = 1
            ORDER BY best_conf DESC
        """, params)
        clips = []
        for row in cur.fetchall():
            clips.append({
                "species": normalize_species(row["common_name"]),
                "confidence": round(row["best_conf"], 3),
                "clip_name": row["clip_name"],
                "date": row["date"],
                "time": row["time"],
                "source": row["source"] or "ground",
            })
        return {"clips": clips}
    except Exception as exc:
        logging.warning("[audio-best-clips] Query error: %s", exc)
        return {"clips": []}


@app.get("/api/audio-detections")
def audio_detections(date: str = None, species: str = None, source: str = None,
                     limit: int = 50, offset: int = 0):
    """Paginated audio detection list with clip paths for playback/verification.

    Returns detections with source camera, confirmations count, and clip path.
    Supports filtering by date, species, and source camera.
    """
    conn = _birdnet_db()
    if not conn:
        return {"detections": [], "total": 0}

    where = []
    params = []
    if date and date != "all":
        where.append("date = ?")
        params.append(date)
    if species:
        # The dropdown shows normalized names but the DB may have raw aliases,
        # so match both the given name and any raw aliases that normalize to it.
        raw_aliases = [raw for raw, canon in SPECIES_ALIASES.items() if canon == species]
        all_names = [species] + raw_aliases
        placeholders = ", ".join("?" for _ in all_names)
        where.append(f"common_name IN ({placeholders})")
        params.extend(all_names)
    if source:
        where.append("source = ?")
        params.append(source)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    try:
        cur = conn.cursor()

        # Total count
        cur.execute(f"SELECT COUNT(*) FROM notes {where_clause}", params)
        total = cur.fetchone()[0]

        # Paginated results — try new columns, fall back if they don't exist
        try:
            cur.execute(f"""
                SELECT id, common_name, scientific_name, confidence,
                       date, time, clip_name, source, confirmations, has_clip
                FROM notes {where_clause}
                ORDER BY id DESC LIMIT ? OFFSET ?
            """, params + [limit, offset])
            has_new_columns = True
        except Exception:
            cur.execute(f"""
                SELECT id, common_name, scientific_name, confidence,
                       date, time, clip_name
                FROM notes {where_clause}
                ORDER BY id DESC LIMIT ? OFFSET ?
            """, params + [limit, offset])
            has_new_columns = False

        detections = []
        for row in cur.fetchall():
            clip = row["clip_name"] or ""
            # has_clip column in DB tracks whether the WAV file exists on disk.
            # Set by migration + updated by audio_analyzer on save.
            has_clip = bool(row["has_clip"]) if (has_new_columns and clip) else bool(clip)
            det = {
                "id": row["id"],
                "species": normalize_species(row["common_name"]),
                "scientific_name": row["scientific_name"],
                "confidence": round(row["confidence"], 3),
                "date": row["date"],
                "time": row["time"],
                "clip_name": clip if has_clip else "",
                "has_clip": has_clip,
            }
            if has_new_columns:
                det["source"] = row["source"] or "ground"
                det["confirmations"] = row["confirmations"] or 1
            detections.append(det)

        return {"detections": detections, "total": total}

    except Exception as exc:
        logging.warning("[audio-detections] Query error: %s", exc)
        return {"detections": [], "total": 0}


@app.get("/api/birdnet-clip/{clip_path:path}")
def birdnet_clip(clip_path: str):
    """Serve a BirdNET audio clip (WAV file)."""
    safe_path = Path(clip_path)
    if ".." in safe_path.parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    full_path = (BIRDNET_CLIPS_DIR / safe_path).resolve()
    if not full_path.is_relative_to(BIRDNET_CLIPS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")

    return FileResponse(
        str(full_path),
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Enhanced clip cache directory ──
_ENHANCED_CLIPS_DIR = Path.home() / "bird-snapshots" / "birdnet-audio" / "enhanced-cache"
_ENHANCED_CLIPS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/birdnet-clip-enhanced/{clip_path:path}")
def birdnet_clip_enhanced(clip_path: str):
    """Serve an enhanced BirdNET audio clip — bandpass filtered + loudness normalized.

    Matches the enhanced_audio_stream.py processing: 300Hz-15kHz bandpass to
    remove wind/traffic rumble and high-frequency noise, plus dynamic loudness
    normalization so all clips play at consistent volume.

    Processed clips are cached on disk after first request. Subsequent requests
    serve the cached version (Cache-Control: 1 year).

    Processing: ffmpeg -af 'highpass=f=300,lowpass=f=15000,loudnorm=I=-16:LRA=11:TP=-1.5'
    Cost: <100ms per 3-second clip (no re-encode, just filter + WAV output).
    """
    import subprocess

    safe_path = Path(clip_path)
    if ".." in safe_path.parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    raw_path = (BIRDNET_CLIPS_DIR / safe_path).resolve()
    if not raw_path.is_relative_to(BIRDNET_CLIPS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")
    if not raw_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")

    # Check cache
    cache_path = (_ENHANCED_CLIPS_DIR / safe_path).resolve()
    if not cache_path.is_relative_to(_ENHANCED_CLIPS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")

    if cache_path.exists() and cache_path.stat().st_size > 100:
        return FileResponse(
            str(cache_path),
            media_type="audio/wav",
            headers={"Cache-Control": "public, max-age=31536000"},  # 1 year (immutable content)
        )

    # Process: bandpass 300-15kHz + dynamic loudness normalization
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg_path = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
    try:
        result = subprocess.run(
            [
                _ffmpeg_path,
                "-loglevel", "error",
                "-i", str(raw_path),
                "-af", "highpass=f=300,lowpass=f=15000,loudnorm=I=-16:LRA=11:TP=-1.5",
                "-y", str(cache_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logging.warning("[enhanced-clip] ffmpeg error: %s", result.stderr)
            # Fall back to raw clip
            return FileResponse(str(raw_path), media_type="audio/wav")
    except Exception as e:
        logging.warning("[enhanced-clip] Processing failed: %s", e)
        return FileResponse(str(raw_path), media_type="audio/wav")

    return FileResponse(
        str(cache_path),
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=31536000"},
    )


# ── Name That Call Game ──

import sqlite3 as _sqlite3
import random as _random

_GAME_DB_PATH = Path.home() / "bird-snapshots" / "logs" / "game.db"

def _game_db():
    """Get or create the game database."""
    db = _sqlite3.connect(str(_GAME_DB_PATH))
    db.row_factory = _sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS game_players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        total_rounds INTEGER DEFAULT 0,
        total_correct INTEGER DEFAULT 0,
        total_answered INTEGER DEFAULT 0,
        best_streak INTEGER DEFAULT 0
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS game_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER,
        round_id INTEGER,
        clip_id INTEGER,
        correct_species TEXT,
        chosen_species TEXT,
        is_correct INTEGER,
        played_at TEXT DEFAULT (datetime('now'))
    )""")
    db.commit()
    return db


# Species that sound similar — wrong answers drawn from these groups for harder questions
_CONFUSABLE_GROUPS = [
    ["Song Sparrow", "White-throated Sparrow", "Chipping Sparrow", "House Finch"],
    ["Downy Woodpecker", "Hairy Woodpecker", "Red-bellied Woodpecker", "Northern Flicker"],
    ["American Crow", "Fish Crow", "Blue Jay", "Common Grackle"],
    ["Carolina Wren", "House Wren"],
    ["Red-tailed Hawk", "Cooper's Hawk", "Osprey"],
    ["Red-winged Blackbird", "Brown-headed Cowbird"],
    ["American Robin", "Hermit Thrush", "Eastern Bluebird"],
    ["Tufted Titmouse", "Black-capped Chickadee", "White-breasted Nuthatch"],
    ["Mourning Dove", "Rock Pigeon"],
    ["Canada Goose", "Common Merganser"],
]

def _confusables_for(species, all_species):
    """Get species that sound similar to the given one."""
    for group in _CONFUSABLE_GROUPS:
        if species in group:
            return [s for s in group if s != species and s in all_species]
    return []


@app.get("/game")
def serve_game():
    return FileResponse(str(DASHBOARD_DIR / "game.html"), media_type="text/html")


@app.post("/api/game/start")
def game_start(body: dict):
    """Start a new round. Returns 10 questions with clip URLs and choices."""
    player_name = body.get("player_name", "").strip()
    if not player_name:
        raise HTTPException(400, "Player name required")

    # Ensure player exists
    gdb = _game_db()
    gdb.execute("INSERT OR IGNORE INTO game_players (name) VALUES (?)", (player_name,))
    gdb.commit()
    player = gdb.execute("SELECT id FROM game_players WHERE name = ?", (player_name,)).fetchone()
    player_id = player["id"]

    # Get eligible clips: high confidence, file exists, 3+ per species
    bdb = _birdnet_db()
    if not bdb:
        raise HTTPException(500, "Audio database unavailable")

    cur = bdb.cursor()
    cur.execute("""
        SELECT id, common_name, clip_name, confidence
        FROM notes
        WHERE has_clip = 1 AND confidence >= 0.9
          AND clip_name IS NOT NULL AND clip_name != ''
    """)
    all_clips = cur.fetchall()

    # Group by species, filter to 3+ clips
    by_species = {}
    for row in all_clips:
        sp = row["common_name"]
        if sp not in by_species:
            by_species[sp] = []
        by_species[sp].append(row)

    eligible = {sp: clips for sp, clips in by_species.items() if len(clips) >= 3}
    all_species = set(eligible.keys())

    if len(all_species) < 4:
        raise HTTPException(500, "Not enough species for the game")

    # Pick 10 questions — avoid repeating species back-to-back
    questions = []
    species_list = list(all_species)
    last_species = None
    for _ in range(10):
        # Pick a species (avoid repeat)
        candidates = [s for s in species_list if s != last_species]
        species = _random.choice(candidates)
        last_species = species

        # Pick 5 clips for this species — played as a sequence
        sp_clips = list(eligible[species])
        _random.shuffle(sp_clips)
        clip = sp_clips[0]
        clip_sequence = ["/bird-api/birdnet-clip-enhanced/" + c["clip_name"] for c in sp_clips[:5]]

        # Generate wrong answers
        confusable = _confusables_for(species, all_species)
        others = [s for s in species_list if s != species]

        # Mix: sometimes confusable, sometimes random
        wrong = []
        if confusable and _random.random() < 0.6:
            # Use 1-2 confusable + fill with random
            n_conf = min(len(confusable), _random.choice([1, 2]))
            wrong = _random.sample(confusable, n_conf)
            remaining = [s for s in others if s not in wrong]
            wrong += _random.sample(remaining, 3 - len(wrong))
        else:
            wrong = _random.sample(others, 3)

        # Build choices in random order
        choices = wrong + [species]
        _random.shuffle(choices)
        correct_index = choices.index(species)

        questions.append({
            "clip_url": clip_sequence[0],
            "clip_sequence": clip_sequence,  # 5 clips played as a sequence
            "clip_id": clip["id"],
            "choices": choices,
            "correct_index": correct_index,
            "correct_species": species,
        })

    # Create round ID
    round_id = int(_time.time() * 1000)

    gdb.close()
    return {
        "round_id": round_id,
        "player_id": player_id,
        "questions": questions,
    }


@app.post("/api/game/answer")
def game_answer(body: dict):
    """Record a single answer."""
    gdb = _game_db()
    gdb.execute("""
        INSERT INTO game_answers (player_id, round_id, correct_species, chosen_species, is_correct)
        SELECT id, ?, ?, ?, ? FROM game_players WHERE name = (
            SELECT name FROM game_players ORDER BY id DESC LIMIT 1
        )
    """, (body.get("round_id"), body.get("correct_species", ""),
          body.get("chosen_species", ""), 1 if body.get("is_correct") else 0))
    gdb.commit()
    gdb.close()
    return {"ok": True}


@app.post("/api/game/finish-round")
def game_finish_round(body: dict):
    """Update player stats after a round."""
    gdb = _game_db()
    player_name = ""
    saved = None

    # Find the player from the round's answers
    round_id = body.get("round_id")
    score = body.get("score", 0)
    best_streak = body.get("best_streak", 0)

    # Get player name from cookie or most recent player
    # (The client sends player_name implicitly via the round answers)
    # Update all players' stats from their answer history
    for player in gdb.execute("SELECT id, name FROM game_players").fetchall():
        pid = player["id"]
        stats = gdb.execute("""
            SELECT COUNT(*) as total, SUM(is_correct) as correct
            FROM game_answers WHERE player_id = ?
        """, (pid,)).fetchone()
        total = stats["total"] or 0
        correct = stats["correct"] or 0

        # Count rounds (distinct round_ids)
        rounds = gdb.execute(
            "SELECT COUNT(DISTINCT round_id) as cnt FROM game_answers WHERE player_id = ?",
            (pid,)
        ).fetchone()["cnt"]

        # Update best streak if this round's is higher
        cur_best = gdb.execute(
            "SELECT best_streak FROM game_players WHERE id = ?", (pid,)
        ).fetchone()["best_streak"]
        new_best = max(cur_best, best_streak)

        gdb.execute("""
            UPDATE game_players SET total_rounds = ?, total_correct = ?,
                   total_answered = ?, best_streak = ? WHERE id = ?
        """, (rounds, correct, total, new_best, pid))

    gdb.commit()
    gdb.close()
    return {"ok": True}


@app.post("/api/game/trash-clip")
def game_trash_clip(body: dict):
    """Mark a BirdNET clip as bad (wrong identification, noise, etc).

    Sets has_clip=0 in the notes table so the clip is excluded from
    the game and the audio browser. Logs the trash event for tracking.
    """
    clip_id = body.get("clip_id")
    player_name = body.get("player_name", "")
    reason = body.get("reason", "bad_clip")
    if not clip_id:
        raise HTTPException(400, "clip_id required")

    bdb = _birdnet_db()
    if not bdb:
        raise HTTPException(500, "Audio database unavailable")

    # Get clip info before trashing
    cur = bdb.cursor()
    cur.execute("SELECT common_name, clip_name, confidence FROM notes WHERE id = ?", (clip_id,))
    row = cur.fetchone()
    species = row["common_name"] if row else "unknown"
    clip_name = row["clip_name"] if row else ""

    # Mark as bad
    cur.execute("UPDATE notes SET has_clip = 0 WHERE id = ?", (clip_id,))
    bdb.commit()

    # Log to game trash tracking table
    gdb = _game_db()
    gdb.execute("""CREATE TABLE IF NOT EXISTS game_trashed_clips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clip_id INTEGER,
        species TEXT,
        clip_name TEXT,
        trashed_by TEXT,
        reason TEXT,
        trashed_at TEXT DEFAULT (datetime('now'))
    )""")
    gdb.execute("INSERT INTO game_trashed_clips (clip_id, species, clip_name, trashed_by, reason) VALUES (?,?,?,?,?)",
                (clip_id, species, clip_name, player_name, reason))
    gdb.commit()
    gdb.close()

    logging.info("[game] Trashed clip id=%s species=%s by=%s", clip_id, species, player_name)
    return {"ok": True, "clip_id": clip_id, "species": species}


@app.post("/api/game/start-learn")
def game_start_learn(body: dict):
    """Start a Learn mode session: 5 mini-rounds of 3 species each.

    Each mini-round: learn 3 species (hear examples), then answer 3 questions
    where the choices are those same 3 species.
    Returns 5 mini-rounds, each with 3 species + example clips + 3 questions.
    """
    player_name = body.get("player_name", "").strip()

    bdb = _birdnet_db()
    if not bdb:
        raise HTTPException(500, "Audio database unavailable")

    cur = bdb.cursor()
    cur.execute("""
        SELECT id, common_name, clip_name, confidence
        FROM notes
        WHERE has_clip = 1 AND confidence >= 0.9
          AND clip_name IS NOT NULL AND clip_name != ''
    """)
    all_clips = cur.fetchall()

    by_species = {}
    for row in all_clips:
        sp = row["common_name"]
        if sp not in by_species:
            by_species[sp] = []
        by_species[sp].append(row)

    # Need species with 5+ clips (for the sequence) and at least 15 species total (5 rounds x 3)
    eligible = {sp: clips for sp, clips in by_species.items() if len(clips) >= 5}
    species_pool = list(eligible.keys())
    _random.shuffle(species_pool)

    if len(species_pool) < 15:
        # Not enough — reuse some species across rounds
        while len(species_pool) < 15:
            species_pool += list(eligible.keys())
        _random.shuffle(species_pool)

    mini_rounds = []
    for r in range(5):
        # Pick 3 species for this mini-round
        round_species = species_pool[r*3 : r*3+3]
        if len(round_species) < 3:
            break

        # Build example clips for each species (5 clips each)
        species_data = []
        for sp in round_species:
            sp_clips = list(eligible[sp])
            _random.shuffle(sp_clips)
            examples = ["/bird-api/birdnet-clip-enhanced/" + c["clip_name"] for c in sp_clips[:5]]
            species_data.append({
                "species": sp,
                "examples": examples,
            })

        # Build 3 questions — one per species, choices are the 3 round species
        questions = []
        for sp in round_species:
            sp_clips = list(eligible[sp])
            _random.shuffle(sp_clips)
            clip = sp_clips[0]
            clip_sequence = ["/bird-api/birdnet-clip-enhanced/" + c["clip_name"] for c in sp_clips[:5]]

            choices = list(round_species)
            _random.shuffle(choices)

            questions.append({
                "clip_url": clip_sequence[0],
                "clip_sequence": clip_sequence,
                "clip_id": clip["id"],
                "choices": choices,
                "correct_index": choices.index(sp),
                "correct_species": sp,
            })

        _random.shuffle(questions)
        mini_rounds.append({
            "species": species_data,
            "questions": questions,
        })

    return {
        "round_id": int(_time.time() * 1000),
        "mini_rounds": mini_rounds,
    }


@app.post("/api/game/species-examples")
def game_species_examples(body: dict):
    """Return 5 high-confidence example clips per species for Match mode.

    Input: {species: ["Blue Jay", "Song Sparrow", ...]}
    Output: {species_clips: {"Blue Jay": [{clip_url, confidence, time}, ...], ...}}
    """
    species_list = body.get("species", [])
    if not species_list:
        return {"species_clips": {}}

    bdb = _birdnet_db()
    if not bdb:
        return {"species_clips": {}}

    result = {}
    cur = bdb.cursor()
    for sp in species_list:
        cur.execute("""
            SELECT clip_name, confidence, time FROM notes
            WHERE has_clip = 1 AND confidence >= 0.9
              AND common_name = ? AND clip_name IS NOT NULL AND clip_name != ''
            ORDER BY confidence DESC LIMIT 5
        """, (sp,))
        clips = []
        for row in cur.fetchall():
            clips.append({
                "clip_url": "/bird-api/birdnet-clip-enhanced/" + row["clip_name"],
                "confidence": round(row["confidence"], 2),
                "time": row["time"],
            })
        if clips:
            result[sp] = clips

    return {"species_clips": result}


@app.post("/api/game/replacement-question")
def game_replacement_question(body: dict):
    """Generate a single replacement question after a clip is trashed.

    Excludes the trashed clip and any clips already used in this round.
    """
    exclude_ids = body.get("exclude_clip_ids", [])

    bdb = _birdnet_db()
    if not bdb:
        raise HTTPException(500, "Audio database unavailable")

    cur = bdb.cursor()
    placeholders = ",".join("?" for _ in exclude_ids) if exclude_ids else "0"
    cur.execute(f"""
        SELECT id, common_name, clip_name, confidence
        FROM notes
        WHERE has_clip = 1 AND confidence >= 0.9
          AND clip_name IS NOT NULL AND clip_name != ''
          AND id NOT IN ({placeholders})
    """, exclude_ids)
    all_clips = cur.fetchall()

    by_species = {}
    for row in all_clips:
        sp = row["common_name"]
        if sp not in by_species:
            by_species[sp] = []
        by_species[sp].append(row)

    eligible = {sp: clips for sp, clips in by_species.items() if len(clips) >= 3}
    all_species = set(eligible.keys())

    if len(all_species) < 4:
        return {"question": None}

    species = _random.choice(list(all_species))
    clip = _random.choice(eligible[species])

    confusable = _confusables_for(species, all_species)
    others = [s for s in all_species if s != species]
    wrong = []
    if confusable and _random.random() < 0.6:
        n_conf = min(len(confusable), _random.choice([1, 2]))
        wrong = _random.sample(confusable, n_conf)
        remaining = [s for s in others if s not in wrong]
        wrong += _random.sample(remaining, 3 - len(wrong))
    else:
        wrong = _random.sample(list(others), 3)

    choices = wrong + [species]
    _random.shuffle(choices)

    return {"question": {
        "clip_url": "/bird-api/birdnet-clip-enhanced/" + clip["clip_name"],
        "clip_id": clip["id"],
        "choices": choices,
        "correct_index": choices.index(species),
        "correct_species": species,
    }}


@app.post("/api/game/start-visual")
def game_start_visual(body: dict):
    """Start a visual identification round — 10 questions with feeder photos."""
    import sqlite3 as sql3
    player_name = body.get("player_name", "").strip()
    if not player_name:
        raise HTTPException(400, "Player name required")

    gdb = _game_db()
    gdb.execute("INSERT OR IGNORE INTO game_players (name) VALUES (?)", (player_name,))
    gdb.commit()

    # Get classified images — prefer reviewed/confirmed, fall back to high-confidence
    cdb_path = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
    cdb = sql3.connect(str(cdb_path))
    cdb.row_factory = sql3.Row

    # Get confirmed images first
    confirmed = cdb.execute("""
        SELECT c.file, c.common_name
        FROM classifications c
        JOIN reviews r ON c.file = r.file
        WHERE r.verdict = 'correct' AND c.action = 'classified' AND c.common_name IS NOT NULL
    """).fetchall()

    by_species = {}
    for row in confirmed:
        sp = row["common_name"]
        if sp not in by_species: by_species[sp] = []
        by_species[sp].append(row["file"])

    # Fill with unreviewed high-score images for species that need more
    if len(by_species) < 10:
        extra = cdb.execute("""
            SELECT file, common_name FROM classifications
            WHERE action = 'classified' AND common_name IS NOT NULL AND raw_score > 150
        """).fetchall()
        for row in extra:
            sp = row["common_name"]
            if sp not in by_species: by_species[sp] = []
            if row["file"] not in by_species[sp]:
                by_species[sp].append(row["file"])

    eligible = {sp: files for sp, files in by_species.items() if len(files) >= 3}
    species_list = list(eligible.keys())
    cdb.close()

    if len(species_list) < 4:
        raise HTTPException(500, "Not enough species")

    questions = []
    last_species = None
    for _ in range(10):
        candidates = [s for s in species_list if s != last_species]
        species = _random.choice(candidates)
        last_species = species
        file = _random.choice(eligible[species])

        confusable = _confusables_for(species, set(species_list))
        others = [s for s in species_list if s != species]
        wrong = []
        if confusable and _random.random() < 0.6:
            n = min(len(confusable), _random.choice([1, 2]))
            wrong = _random.sample(confusable, n)
            remaining = [s for s in others if s not in wrong]
            wrong += _random.sample(remaining, 3 - len(wrong))
        else:
            wrong = _random.sample(others, 3)

        choices = wrong + [species]
        _random.shuffle(choices)

        questions.append({
            "file": file,
            "choices": choices,
            "correct_index": choices.index(species),
            "correct_species": species,
        })

    return {"round_id": int(_time.time() * 1000), "questions": questions}


@app.post("/api/game/start-visual-learn")
def game_start_visual_learn(body: dict):
    """Start visual Learn mode — 5 mini-rounds of 3 species, with example photos."""
    import sqlite3 as sql3
    cdb_path = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
    cdb = sql3.connect(str(cdb_path))
    cdb.row_factory = sql3.Row

    confirmed = cdb.execute("""
        SELECT c.file, c.common_name
        FROM classifications c
        JOIN reviews r ON c.file = r.file
        WHERE r.verdict = 'correct' AND c.action = 'classified' AND c.common_name IS NOT NULL
    """).fetchall()

    by_species = {}
    for row in confirmed:
        sp = row["common_name"]
        if sp not in by_species: by_species[sp] = []
        by_species[sp].append(row["file"])

    eligible = {sp: files for sp, files in by_species.items() if len(files) >= 5}
    species_pool = list(eligible.keys())
    _random.shuffle(species_pool)
    cdb.close()

    while len(species_pool) < 15:
        species_pool += list(eligible.keys())
    _random.shuffle(species_pool)

    mini_rounds = []
    for r in range(5):
        round_species = species_pool[r*3:r*3+3]
        if len(round_species) < 3: break

        species_data = []
        questions = []
        for sp in round_species:
            files = list(eligible[sp])
            _random.shuffle(files)
            species_data.append({
                "species": sp,
                "example_files": files[:3],
            })
            q_file = files[3] if len(files) > 3 else files[0]
            choices = list(round_species)
            _random.shuffle(choices)
            questions.append({
                "file": q_file,
                "choices": choices,
                "correct_index": choices.index(sp),
                "correct_species": sp,
            })

        _random.shuffle(questions)
        mini_rounds.append({"species": species_data, "questions": questions})

    return {"round_id": int(_time.time() * 1000), "mini_rounds": mini_rounds}


@app.get("/game-visual")
def serve_game_visual():
    return FileResponse(str(DASHBOARD_DIR / "game-visual.html"), media_type="text/html")


@app.get("/api/game/leaderboard")
def game_leaderboard():
    """Return player rankings."""
    gdb = _game_db()
    players = gdb.execute("""
        SELECT name, best_streak, total_correct, total_answered, total_rounds
        FROM game_players
        ORDER BY best_streak DESC, total_correct DESC
    """).fetchall()

    result = []
    for p in players:
        total = p["total_answered"] or 0
        correct = p["total_correct"] or 0
        result.append({
            "name": p["name"],
            "best_streak": p["best_streak"],
            "accuracy_pct": round(correct / total * 100) if total > 0 else 0,
            "total_rounds": p["total_rounds"],
        })

    gdb.close()
    return {"players": result}


# ── Documentation Viewer ──

DOCS_DIR = Path(os.path.expanduser("~/docs/bird-observatory"))
DOCS_HTML = Path(__file__).parent / "docs.html"


@app.get("/docs")
def docs_page():
    """Serve the documentation viewer HTML page."""
    if not DOCS_HTML.exists():
        raise HTTPException(status_code=404, detail="Docs page not found")
    return FileResponse(str(DOCS_HTML), media_type="text/html")


@app.get("/api/docs/{doc_path:path}")
def get_doc(doc_path: str):
    """Serve a markdown documentation file."""
    # Sanitize path
    safe_path = Path(doc_path)
    if ".." in safe_path.parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Try with and without .md extension
    full_path = (DOCS_DIR / safe_path).resolve()
    if not full_path.exists():
        full_path = (DOCS_DIR / (str(safe_path) + ".md")).resolve()

    # Verify path stays within docs directory (is_relative_to prevents prefix collisions)
    if not full_path.is_relative_to(DOCS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        full_path.read_text(),
        media_type="text/plain; charset=utf-8",
    )


# ── go2rtc WebSocket Proxy (for camera feeds through Cloudflare tunnel) ──

GO2RTC_HOST = os.environ.get("GO2RTC_HOST", "127.0.0.1")  # go2rtc runs locally
GO2RTC_PORT = int(os.environ.get("GO2RTC_PORT", "1984"))

# Pipeline v3 service URLs — two ports, two env vars, no hardcoding.
_PIPELINE_HEALTH_URL = os.environ.get("PIPELINE_HEALTH_URL", "http://127.0.0.1:8100")
_PIPELINE_SSE_URL = os.environ.get("PIPELINE_SSE_URL", "http://127.0.0.1:8105")

from fastapi import WebSocket as FastAPIWebSocket


ALLOWED_STREAMS = {"feeder-main", "ground-main"}


@app.get("/api/stream.mp4")
async def proxy_go2rtc_mp4(src: str = "feeder-main"):
    """Proxy MP4 stream from local go2rtc (used by split 'Both' view)."""
    if src not in ALLOWED_STREAMS:
        raise HTTPException(status_code=400, detail="Invalid stream")
    import httpx
    from starlette.responses import StreamingResponse

    go2rtc_url = f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/api/stream.mp4?src={src}"

    async def stream():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", go2rtc_url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk

    return StreamingResponse(stream(), media_type="video/mp4")


@app.get("/enhanced-audio/stream.mp3")
async def proxy_enhanced_audio():
    """Proxy enhanced audio MP3 stream from local enhanced_audio_stream service (port 8096).

    Previously served via nginx on the NAS. Now proxied through FastAPI
    so it works through the Cloudflare tunnel at birds.vivessato.com.
    """
    import httpx
    from starlette.responses import StreamingResponse

    async def stream():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", "http://127.0.0.1:8096/stream.mp3", timeout=None) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    yield chunk

    return StreamingResponse(stream(), media_type="audio/mpeg",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.get("/enhanced-audio/health")
def enhanced_audio_health():
    """Proxy health check for enhanced audio service."""
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:8096/health", timeout=3)
        return resp.json()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/api/hls/{path:path}")
async def proxy_hls(path: str):
    """Proxy HLS segments from local go2rtc (port 1984) for fallback video streaming.

    Previously served via nginx on the NAS at /hls/.
    Now proxied through FastAPI for Cloudflare tunnel access.
    """
    import httpx
    from starlette.responses import StreamingResponse

    go2rtc_url = f"http://{GO2RTC_HOST}:{GO2RTC_PORT}/hls/{path}"

    async def stream():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", go2rtc_url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk

    # Determine content type from extension
    ct = "application/vnd.apple.mpegurl" if path.endswith(".m3u8") else "video/mp2t"
    return StreamingResponse(stream(), media_type=ct,
                             headers={"Cache-Control": "no-cache"})


@app.websocket("/api/ws")
async def proxy_go2rtc_ws(websocket: FastAPIWebSocket, src: str = "feeder-main"):
    """Proxy WebSocket connections to local go2rtc for camera streaming."""
    import asyncio
    import websockets
    from starlette.websockets import WebSocketDisconnect

    if src not in ALLOWED_STREAMS:
        await websocket.close(code=1008, reason="Invalid stream")
        return

    await websocket.accept()
    go2rtc_url = f"ws://{GO2RTC_HOST}:{GO2RTC_PORT}/api/ws?src={src}"
    upstream = None
    done = asyncio.Event()

    try:
        upstream = await asyncio.wait_for(
            websockets.connect(go2rtc_url, max_size=16 * 1024 * 1024),
            timeout=5,
        )

        async def client_to_upstream():
            try:
                while not done.is_set():
                    try:
                        data = await websocket.receive()
                    except WebSocketDisconnect:
                        break
                    if data.get("type") == "websocket.disconnect":
                        break
                    if "text" in data and data["text"]:
                        await upstream.send(data["text"])
                    elif "bytes" in data and data["bytes"]:
                        await upstream.send(data["bytes"])
            except Exception as exc:
                logging.debug("[WS Proxy] client→upstream: %s", exc)
            finally:
                done.set()

        async def upstream_to_client():
            try:
                async for msg in upstream:
                    if done.is_set():
                        break
                    if isinstance(msg, bytes):
                        await websocket.send_bytes(msg)
                    else:
                        await websocket.send_text(msg)
            except Exception as exc:
                logging.debug("[WS Proxy] upstream→client: %s", exc)
            finally:
                done.set()

        await asyncio.gather(client_to_upstream(), upstream_to_client())

    except Exception as e:
        logging.warning("[WS Proxy] go2rtc: %s", e)
    finally:
        if upstream:
            try:
                await upstream.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


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
        # Use SQL ORDER BY to get files ranked by raw_score (highest first)
        sp_name = safe_dir.replace("_", " ")
        cdb_conn = cdb.get_conn(readonly=True)
        ranked_rows = cdb_conn.execute(
            "SELECT file, raw_score FROM classifications "
            "WHERE common_name = ? AND action = 'classified' "
            "ORDER BY raw_score DESC",
            (sp_name,),
        ).fetchall()
        # Build ordered file list from SQL, only including files that exist on disk
        ranked_names = [row["file"] for row in ranked_rows]
        ranked_set = set(ranked_names)
        # Files in SQL order first, then any on-disk files not in DB (by mtime)
        files_on_disk = {f.name: f for f in src_dir.glob("*.jpg")}
        files = []
        for name in ranked_names:
            if name in files_on_disk:
                files.append(files_on_disk[name])
        # Append any files not in DB (sorted by mtime, newest first)
        remaining = [f for name, f in files_on_disk.items() if name not in ranked_set]
        remaining.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        files.extend(remaining)
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

FOOD_TYPES = [
    "sunflower", "mixed_songbird", "suet", "nyjer", "peanut",
    "safflower", "mealworm", "fruit", "nectar", "empty",
]


def _init_food_log():
    """Create the food_log table if it doesn't exist."""
    try:
        conn = _get_food_conn()
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
    conn = _get_food_conn()
    conn.execute(
        "INSERT INTO food_log (timestamp, food_type, feeder, notes) VALUES (?, ?, ?, ?)",
        (ts, entry.food_type, entry.feeder, entry.notes),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"id": row_id, "timestamp": ts, "food_type": entry.food_type, "feeder": entry.feeder}


@app.get("/api/food-log")
def get_food_log():
    """List all food log entries, newest first."""
    conn = _get_food_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, timestamp, food_type, feeder, notes FROM food_log ORDER BY timestamp DESC")
    rows = cur.fetchall()
    return [
        {"id": r["id"], "timestamp": r["timestamp"], "food_type": r["food_type"],
         "feeder": r["feeder"], "notes": r["notes"]}
        for r in rows
    ]


@app.get("/api/food-log/current")
def get_current_food():
    """Get the most recent food entry (what's currently in the feeder)."""
    conn = _get_food_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, timestamp, food_type, feeder, notes FROM food_log ORDER BY timestamp DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return {"food_type": "unknown", "timestamp": None, "detail": "No food logged yet"}
    return {"id": row["id"], "timestamp": row["timestamp"], "food_type": row["food_type"],
            "feeder": row["feeder"], "notes": row["notes"]}


@app.delete("/api/food-log/{entry_id}")
def delete_food_log(entry_id: int):
    """Delete a food log entry."""
    conn = _get_food_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM food_log WHERE id = ?", (entry_id,))
    conn.commit()
    deleted = cur.rowcount
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
    return (row[0] if isinstance(row, tuple) else row["food_type"]) if row else "unknown"


def _get_food_periods(conn=None):
    """Get all food periods as (start, end, food_type) tuples."""
    if conn is None:
        conn = _get_food_conn()
    cur = conn.cursor()
    cur.execute("SELECT timestamp, food_type FROM food_log ORDER BY timestamp ASC")
    rows = cur.fetchall()
    if not rows:
        return []
    periods = []
    for i, row in enumerate(rows):
        ts = row[0] if isinstance(row, tuple) else row["timestamp"]
        food = row[1] if isinstance(row, tuple) else row["food_type"]
        if i + 1 < len(rows):
            next_row = rows[i + 1]
            end = next_row[0] if isinstance(next_row, tuple) else next_row["timestamp"]
        else:
            end = datetime.now().isoformat()
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

    # --- Hourly distribution via SQL (camera) ---
    cdb_conn = cdb.get_conn(readonly=True)
    by_hour = [0] * 24
    cam_hour_rows = cdb_conn.execute(
        "SELECT CAST(SUBSTR(source_timestamp, 12, 2) AS INTEGER) as hour, COUNT(*) as cnt "
        "FROM classifications WHERE common_name = ? AND action = 'classified' "
        "GROUP BY hour",
        (species_name,),
    ).fetchall()
    camera_count = 0
    for row in cam_hour_rows:
        h = row["hour"]
        if 0 <= h <= 23:
            by_hour[h] += row["cnt"]
            camera_count += row["cnt"]

    # --- Hourly distribution via SQL (audio) ---
    conn = _get_food_conn()
    audio_count = 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT CAST(SUBSTR(time, 1, 2) AS INTEGER) as hour, COUNT(*) as cnt "
            "FROM notes WHERE common_name = ? GROUP BY hour",
            (species_name,),
        )
        for row in cur.fetchall():
            h = row[0] if isinstance(row, tuple) else row["hour"]
            cnt = row[1] if isinstance(row, tuple) else row["cnt"]
            if 0 <= h <= 23:
                by_hour[h] += cnt
                audio_count += cnt
    except Exception:
        pass

    # --- Day of week distribution via SQL (camera: SQLite strftime %w = 0=Sun..6=Sat → convert to 0=Mon..6=Sun) ---
    by_dow = [0] * 7
    dow_rows = cdb_conn.execute(
        "SELECT CAST(strftime('%%w', source_timestamp) AS INTEGER) as dow, COUNT(*) as cnt "
        "FROM classifications WHERE common_name = ? AND action = 'classified' "
        "GROUP BY dow",
        (species_name,),
    ).fetchall()
    for row in dow_rows:
        sqlite_dow = row["dow"]  # 0=Sun, 1=Mon, ..., 6=Sat
        if sqlite_dow is None:
            continue
        py_dow = (sqlite_dow - 1) % 7  # convert to 0=Mon, ..., 6=Sun
        by_dow[py_dow] += row["cnt"]

    # Audio day of week
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT CAST(strftime('%%w', date) AS INTEGER) as dow, COUNT(*) as cnt "
            "FROM notes WHERE common_name = ? GROUP BY dow",
            (species_name,),
        )
        for row in cur.fetchall():
            sqlite_dow = row[0] if isinstance(row, tuple) else row["dow"]
            cnt = row[1] if isinstance(row, tuple) else row["cnt"]
            if sqlite_dow is None:
                continue
            py_dow = (sqlite_dow - 1) % 7
            by_dow[py_dow] += cnt
    except Exception:
        pass

    # --- Camera breakdown via SQL ---
    cameras = {}
    cam_rows = cdb_conn.execute(
        "SELECT camera, COUNT(*) as cnt FROM classifications "
        "WHERE common_name = ? AND action = 'classified' GROUP BY camera",
        (species_name,),
    ).fetchall()
    for row in cam_rows:
        cameras[row["camera"] or "unknown"] = row["cnt"]

    # --- Food preferences via SQL: get all timestamps, match against food periods ---
    food_periods = _get_food_periods(conn)
    by_food = {}

    # Camera timestamps for food matching
    cam_ts_rows = cdb_conn.execute(
        "SELECT source_timestamp FROM classifications "
        "WHERE common_name = ? AND action = 'classified'",
        (species_name,),
    ).fetchall()
    for row in cam_ts_rows:
        food = _get_food_at_time(conn, row["source_timestamp"])
        by_food[food] = by_food.get(food, 0) + 1

    # Audio timestamps for food matching
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT date || ' ' || time as ts FROM notes WHERE common_name = ?",
            (species_name,),
        )
        for row in cur.fetchall():
            ts = row[0] if isinstance(row, tuple) else row["ts"]
            food = _get_food_at_time(conn, ts)
            by_food[food] = by_food.get(food, 0) + 1
    except Exception:
        pass

    # Calculate rate per hour for each food
    food_hours = {}
    for start, end, food in food_periods:
        h = _hours_between(start, end)
        food_hours[food] = food_hours.get(food, 0) + h

    food_prefs = {}
    for food, count in by_food.items():
        label = "Songbird Mix" if food == "unknown" else food
        hours = food_hours.get(food, 0)
        food_prefs[label] = {
            "detections": count,
            "hours_available": round(hours, 1) if hours > 0 else None,
            "rate_per_hour": round(count / hours, 2) if hours > 0 else None,
        }

    # Peak hour
    peak_hour = by_hour.index(max(by_hour)) if max(by_hour) > 0 else -1
    total = camera_count + audio_count

    # First/last seen via SQL
    first_seen = None
    last_seen = None
    fl_row = cdb_conn.execute(
        "SELECT MIN(source_date) as first_d, MAX(source_date) as last_d "
        "FROM classifications WHERE common_name = ? AND action = 'classified'",
        (species_name,),
    ).fetchone()
    if fl_row and fl_row["first_d"]:
        first_seen = fl_row["first_d"]
        last_seen = fl_row["last_d"]
    try:
        cur = conn.cursor()
        cur.execute("SELECT MIN(date) as first_d, MAX(date) as last_d FROM notes WHERE common_name = ?",
                    (species_name,))
        a_row = cur.fetchone()
        if a_row:
            a_first = a_row[0] if isinstance(a_row, tuple) else a_row["first_d"]
            a_last = a_row[1] if isinstance(a_row, tuple) else a_row["last_d"]
            if a_first:
                first_seen = min(first_seen, a_first) if first_seen else a_first
            if a_last:
                last_seen = max(last_seen, a_last) if last_seen else a_last
    except Exception:
        pass

    # --- Daily visit counts (last 30 days) ---
    by_date = {}
    daily_rows = cdb_conn.execute(
        "SELECT source_date, COUNT(*) as cnt FROM classifications "
        "WHERE common_name = ? AND action = 'classified' AND source_date IS NOT NULL "
        "GROUP BY source_date ORDER BY source_date",
        (species_name,),
    ).fetchall()
    for row in daily_rows:
        if row["source_date"]:
            by_date[row["source_date"]] = row["cnt"]

    # Add audio daily counts
    try:
        cur = conn.cursor()
        cur.execute("SELECT date, COUNT(*) as cnt FROM notes WHERE common_name = ? GROUP BY date ORDER BY date",
                    (species_name,))
        for row in cur.fetchall():
            d = row[0] if isinstance(row, tuple) else row["date"]
            cnt = row[1] if isinstance(row, tuple) else row["cnt"]
            if d:
                by_date[d] = by_date.get(d, 0) + cnt
    except Exception:
        pass

    # Build last 30 days array (fill gaps with 0)
    today = datetime.now().strftime("%Y-%m-%d")
    daily_labels = []
    daily_values = []
    for i in range(29, -1, -1):
        d = (datetime.now() - _timedelta(days=i)).strftime("%Y-%m-%d")
        daily_labels.append(d)
        daily_values.append(by_date.get(d, 0))

    # Streak: how many consecutive recent days with detections?
    streak = 0
    for i in range(len(daily_values) - 1, -1, -1):
        if daily_values[i] > 0:
            streak += 1
        else:
            break

    # Preferred food
    pref_foods = {k: v for k, v in food_prefs.items() if v.get("rate_per_hour") is not None}
    preferred = max(pref_foods.items(), key=lambda x: x[1]["rate_per_hour"])[0] if pref_foods else "unknown"

    return {
        "species": species_name,
        "total_detections": total,
        "camera_detections": camera_count,
        "audio_detections": audio_count,
        "by_hour": by_hour,
        "peak_hour": peak_hour,
        "peak_description": f"Most active {peak_hour}:00-{(peak_hour+1) % 24}:00" if peak_hour >= 0 else "No data",
        "by_day_of_week": by_dow,
        "by_food": food_prefs,
        "preferred_food": preferred,
        "cameras": cameras,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "daily_labels": daily_labels,
        "daily_values": daily_values,
        "streak": streak,
    }


@app.get("/api/activity/food/{food_type}")
def get_food_activity(food_type: str):
    """What species does this food attract? Rates per hour for comparison."""
    conn = _get_food_conn()

    # Get periods for this food
    food_periods = _get_food_periods(conn)
    matching_periods = [(s, e) for s, e, f in food_periods if f == food_type]

    total_hours = sum(_hours_between(s, e) for s, e in matching_periods)

    if total_hours == 0:
        return {
            "food_type": food_type,
            "total_hours": 0,
            "species_attracted": [],
            "detail": "No logged periods for this food type",
        }

    # Count detections per species during this food's periods using SQL
    # Build a UNION of period ranges for efficient matching
    species_counts = {}

    if matching_periods:
        # Camera detections: use SQL with period-based WHERE clauses
        cdb_conn = cdb.get_conn(readonly=True)
        period_clauses = " OR ".join(
            ["(source_timestamp >= ? AND source_timestamp < ?)"] * len(matching_periods)
        )
        period_params = []
        for s, e in matching_periods:
            period_params.extend([s, e])

        cam_rows = cdb_conn.execute(
            f"SELECT common_name, COUNT(*) as cnt FROM classifications "
            f"WHERE action = 'classified' AND common_name IS NOT NULL "
            f"AND ({period_clauses}) "
            f"GROUP BY common_name",
            period_params,
        ).fetchall()
        for row in cam_rows:
            name = normalize_species(row["common_name"])
            if name:
                species_counts[name] = species_counts.get(name, 0) + row["cnt"]

        # Audio detections: same approach on notes table
        try:
            cur = conn.cursor()
            audio_clauses = " OR ".join(
                ["(date || ' ' || time >= ? AND date || ' ' || time < ?)"] * len(matching_periods)
            )
            cur.execute(
                f"SELECT common_name, COUNT(*) as cnt FROM notes "
                f"WHERE ({audio_clauses}) "
                f"GROUP BY common_name",
                period_params,
            )
            for row in cur.fetchall():
                name_val = row[0] if isinstance(row, tuple) else row["common_name"]
                cnt_val = row[1] if isinstance(row, tuple) else row["cnt"]
                name = normalize_species(name_val)
                if name:
                    species_counts[name] = species_counts.get(name, 0) + cnt_val
        except Exception:
            pass

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
    """Hour x species detection heatmap for the last N days."""
    cutoff_date = (datetime.now() - _timedelta(days=days)).strftime("%Y-%m-%d")

    heatmap = {}  # species -> [hour0, hour1, ..., hour23]

    # Audio detections: SQL GROUP BY for hourly counts per species
    conn = _get_food_conn()
    try:
        cur = conn.cursor()
        if species != "all":
            norm_sp = normalize_species(species)
            cur.execute(
                "SELECT common_name, CAST(SUBSTR(time, 1, 2) AS INTEGER) as hour, COUNT(*) as cnt "
                "FROM notes WHERE date >= ? AND common_name = ? GROUP BY common_name, hour",
                (cutoff_date, norm_sp),
            )
        else:
            cur.execute(
                "SELECT common_name, CAST(SUBSTR(time, 1, 2) AS INTEGER) as hour, COUNT(*) as cnt "
                "FROM notes WHERE date >= ? GROUP BY common_name, hour",
                (cutoff_date,),
            )
        for row in cur.fetchall():
            name_val = row[0] if isinstance(row, tuple) else row["common_name"]
            hour_val = row[1] if isinstance(row, tuple) else row["hour"]
            cnt_val = row[2] if isinstance(row, tuple) else row["cnt"]
            name = normalize_species(name_val)
            if 0 <= hour_val <= 23:
                if name not in heatmap:
                    heatmap[name] = [0] * 24
                heatmap[name][hour_val] += cnt_val
    except Exception:
        pass

    # Camera detections: SQL GROUP BY for hourly counts per species
    cdb_conn = cdb.get_conn(readonly=True)
    if species != "all":
        norm_sp = normalize_species(species)
        cam_rows = cdb_conn.execute(
            "SELECT common_name, CAST(SUBSTR(source_timestamp, 12, 2) AS INTEGER) as hour, COUNT(*) as cnt "
            "FROM classifications "
            "WHERE action = 'classified' AND source_date >= ? AND common_name = ? "
            "GROUP BY common_name, hour",
            (cutoff_date, norm_sp),
        ).fetchall()
    else:
        cam_rows = cdb_conn.execute(
            "SELECT common_name, CAST(SUBSTR(source_timestamp, 12, 2) AS INTEGER) as hour, COUNT(*) as cnt "
            "FROM classifications "
            "WHERE action = 'classified' AND source_date >= ? AND common_name IS NOT NULL "
            "GROUP BY common_name, hour",
            (cutoff_date,),
        ).fetchall()
    for row in cam_rows:
        name = normalize_species(row["common_name"])
        hour_val = row["hour"]
        if name and 0 <= hour_val <= 23:
            if name not in heatmap:
                heatmap[name] = [0] * 24
            heatmap[name][hour_val] += row["cnt"]

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
    conn = _get_food_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT common_name, COUNT(*) as cnt FROM notes GROUP BY common_name")
        for row in cur.fetchall():
            name_val = row[0] if isinstance(row, tuple) else row["common_name"]
            cnt_val = row[1] if isinstance(row, tuple) else row["cnt"]
            name = normalize_species(name_val)
            counts[name] = counts.get(name, 0) + cnt_val
    except Exception:
        pass

    sorted_species = sorted(counts.items(), key=lambda x: -x[1])
    return {"species": [{"name": s, "count": c} for s, c in sorted_species]}


# ── Visit-Based Event Endpoints ──────────────────────────────────────────

def _resolve_visit_date(date: str) -> str:
    """Resolve 'today'/'yesterday' to actual YYYY-MM-DD date string."""
    if date == "today":
        return datetime.now().strftime("%Y-%m-%d")
    elif date == "yesterday":
        return (datetime.now() - _timedelta(days=1)).strftime("%Y-%m-%d")
    return date


@app.get("/api/visits")
def api_get_visits(date: str = "today", camera: str = "all", species: str = "",
                   limit: int = 50, offset: int = 0):
    """Get visits with optional filters."""
    date = _resolve_visit_date(date)
    cam = None if camera == "all" else camera
    sp = species if species else None
    visits = vdb.get_visits(date=date, camera=cam, species=sp, limit=limit, offset=offset)
    total = vdb.count_visits(date=date, camera=cam, species=sp)
    return {
        "visits": visits,
        "total": total,
        "date": date,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
    }


@app.get("/api/visit-summary")
def api_visit_summary(date: str = "today", camera: str = "all"):
    """Species visit counts — enriched detection counts."""
    date = _resolve_visit_date(date)
    summary = vdb.get_visit_summary(date)
    stats = vdb.get_visit_stats(date)
    return {"summary": summary, "stats": stats, "date": date}


@app.get("/api/visit-stats")
def api_visit_stats(date: str = "today"):
    """Aggregate visit statistics."""
    date = _resolve_visit_date(date)
    return vdb.get_visit_stats(date)


@app.get("/api/pipeline/health")
async def pipeline_health_proxy():
    import httpx
    async with httpx.AsyncClient(timeout=2) as c:
        try:
            r = await c.get(f"{_PIPELINE_HEALTH_URL}/api/pipeline/health")
            return r.json()
        except Exception as e:
            return {"overall": "broken", "error": str(e)}


@app.get("/api/pipeline/events/sse")
async def proxy_pipeline_sse(camera: str = "feeder"):
    """Proxy Server-Sent Events from the pipeline v3 SSE server.

    The pipeline runs its own HTTP SSE server (pipeline/sse_events.py) on
    port 8104 (dev) or 8100 (prod). This route forwards the stream so the
    dashboard doesn't need to know where the pipeline process is running.
    """
    import httpx
    from starlette.responses import StreamingResponse

    async def gen():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{_PIPELINE_SSE_URL}/events/sse",
                params={"camera": camera},
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/pipeline/debug/latest.jpg")
async def proxy_debug_latest_jpg(camera: str = "feeder"):
    """Proxy the pipeline's debug frame (latest YOLO-annotated frame)."""
    import httpx
    from starlette.responses import Response
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{_PIPELINE_HEALTH_URL}/debug/latest.jpg?camera={camera}")
            if resp.status_code == 200:
                return Response(content=resp.content, media_type="image/jpeg",
                                headers={"Cache-Control": "no-cache"})
            return Response(status_code=resp.status_code)
    except Exception:
        return Response(status_code=502)


@app.get("/api/pipeline/events")
async def pipeline_events_proxy(camera: str, start: int, end: int):
    """Query the pipeline event store for scrubbing/historical playback."""
    from pathlib import Path
    db_path = Path.home() / "bird-snapshots" / "logs" / "pipeline.db"
    if not db_path.exists():
        return []
    try:
        from pipeline.event_store import EventStore
        store = EventStore(str(db_path))
        try:
            return store.query_events(camera=camera, start_ms=start, end_ms=end)
        finally:
            store.shutdown()
    except Exception as e:
        return {"error": str(e)}
