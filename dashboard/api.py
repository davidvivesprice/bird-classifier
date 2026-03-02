"""
Bird Dashboard API — serves classifier data for the bird observatory dashboard.

Reads classification results from the JSONL log and serves annotated images.
Also provides an annotation/review endpoint for building training data.

Run: uvicorn dashboard.api:app --host 0.0.0.0 --port 8099
"""

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# --- Paths ---
BASE_DIR = Path("/Users/vives/bird-snapshots")
JSONL_PATH = BASE_DIR / "logs" / "classifications.jsonl"
ANNOTATED_DIR = BASE_DIR / "annotated"
REVIEWS_PATH = Path("/Users/vives/bird-classifier/dashboard/reviews.jsonl")
REGIONAL_SPECIES_PATH = Path("/Users/vives/bird-classifier/models/cape_cod_species.txt")

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


@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/stats")
def stats():
    """Overall classification statistics."""
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]
    skipped = [e for e in entries if e["action"].startswith("skipped")]
    species = set()
    for e in classified:
        if "top_prediction" in e:
            species.add(e["top_prediction"]["common_name"])

    return {
        "total": len(entries),
        "classified": len(classified),
        "skipped": len(skipped),
        "species_count": len(species),
        "last_updated": entries[-1]["timestamp"] if entries else None,
    }


@app.get("/api/species")
def species_list():
    """List all detected species with counts and metadata."""
    entries = load_classifications()
    classified = [e for e in entries if e["action"] == "classified"]

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
    """Serve an annotated image."""
    # Sanitize filename
    safe_name = os.path.basename(filename)
    path = ANNOTATED_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(path), media_type="image/jpeg")


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
def submit_review(filename: str, verdict: str, correct_species: str = ""):
    """Submit a review verdict for a classification."""
    safe_name = os.path.basename(filename)
    if verdict not in ("correct", "wrong", "skip"):
        raise HTTPException(status_code=400, detail="verdict must be 'correct', 'wrong', or 'skip'")

    review = {
        "file": safe_name,
        "verdict": verdict,
        "correct_species": correct_species if verdict == "wrong" else "",
        "timestamp": datetime.now().isoformat(),
    }

    with open(REVIEWS_PATH, "a") as f:
        f.write(json.dumps(review) + "\n")

    return {"status": "ok", "review": review}


@app.get("/api/regional-species")
def regional_species():
    """Return the regional species filter list (for the annotation dropdown)."""
    return load_regional_species()
