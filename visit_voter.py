"""visit_voter — Use multi-frame visit consistency to improve accuracy.

When a bird visits for multiple frames, the classifier usually gets most
frames right. A single outlier frame (classified differently) is almost
certainly wrong. This module uses the visit's species consensus to flag
or correct outlier classifications.

Example: 9 frames say "Song Sparrow", 1 says "Lincoln's Sparrow"
→ The Lincoln's Sparrow is flagged as likely wrong.

Used by classify.py after initial classification.
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path("/Users/vives/bird-snapshots/logs/classifications.db")

# Minimum frames in a time window to establish consensus
MIN_FRAMES_FOR_VOTE = 3

# Time window to look for related frames (seconds before current)
VOTE_WINDOW_SEC = 120

# Minimum consensus ratio to flag an outlier
MIN_CONSENSUS_RATIO = 0.7


def check_visit_consensus(camera, species, timestamp, source_date):
    """Check if this classification agrees with recent frames from the same camera.

    Looks at classifications from the same camera within the last VOTE_WINDOW_SEC
    seconds. If there's a strong consensus on a different species, this frame
    is likely an outlier.

    Args:
        camera: Camera name (feeder, ground)
        species: The classifier's prediction for this frame
        timestamp: Source timestamp string (YYYY-MM-DD HH:MM:SS)
        source_date: Date string (YYYY-MM-DD)

    Returns:
        dict with:
            consensus_species: What most frames say (may equal species)
            consensus_count: How many frames agree
            total_frames: Total frames in window
            is_outlier: True if this frame disagrees with consensus
            confidence_boost: Positive if consensus agrees, negative if outlier
    """
    if not timestamp or not source_date:
        return None

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row

        # Get recent classifications from the same camera
        rows = conn.execute("""
            SELECT common_name, COUNT(*) as cnt
            FROM classifications
            WHERE action = 'classified'
            AND camera = ?
            AND source_date = ?
            AND common_name IS NOT NULL
            AND julianday(?) - julianday(source_timestamp) BETWEEN 0 AND ?
            GROUP BY common_name
            ORDER BY cnt DESC
        """, (camera, source_date, timestamp, VOTE_WINDOW_SEC / 86400.0)).fetchall()
        conn.close()

        if not rows:
            return None

        total = sum(r["cnt"] for r in rows)
        if total < MIN_FRAMES_FOR_VOTE:
            return None

        # What's the consensus?
        top = rows[0]
        consensus_species = top["common_name"]
        consensus_count = top["cnt"]
        consensus_ratio = consensus_count / total

        is_outlier = (species != consensus_species and
                      consensus_ratio >= MIN_CONSENSUS_RATIO)

        # Confidence adjustment
        if species == consensus_species:
            # Agrees with consensus — boost proportional to strength
            confidence_boost = 0.05 * min(consensus_ratio, 0.95)
        elif is_outlier:
            # Disagrees with strong consensus — penalty
            confidence_boost = -0.15
        else:
            # Mixed results, no clear consensus
            confidence_boost = 0

        return {
            "consensus_species": consensus_species,
            "consensus_count": consensus_count,
            "total_frames": total,
            "consensus_ratio": round(consensus_ratio, 2),
            "is_outlier": is_outlier,
            "confidence_boost": confidence_boost,
        }

    except Exception as e:
        log.debug("Visit consensus check failed: %s", e)
        return None
