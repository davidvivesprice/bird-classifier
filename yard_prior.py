"""yard_prior — Bayesian species correction using your yard's review history.

Adjusts classifier predictions based on what species actually appear in your
yard. When the classifier says "White-throated Sparrow" at 60% confidence but
your yard data says 95% of sparrows are Song Sparrows, the prior shifts the
prediction toward Song Sparrow.

Also uses audio corroboration: if BirdNET hears a matching species within ±30s,
that boosts the prediction. If BirdNET hears a DIFFERENT species, that's a
signal the visual classifier might be wrong.

Updates automatically as you review more images.
"""

import json
import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

CLASSIFICATIONS_DB = Path("/Users/vives/bird-snapshots/logs/classifications.db")
BIRDNET_DB = Path("/Users/vives/bird-snapshots/birdnet-audio/birdnet_local.db")

# Minimum reviews before the prior has effect
MIN_REVIEWS_FOR_PRIOR = 20

# How much to trust the prior vs the classifier (0-1, higher = more prior)
PRIOR_WEIGHT = 0.4

# Audio corroboration boost
AUDIO_MATCH_BOOST = 0.15      # add to confidence when audio matches
AUDIO_MISMATCH_PENALTY = 0.10 # subtract when audio hears a different species


class YardPrior:
    """Learns from your review history to correct species predictions."""

    def __init__(self):
        self._confusion = {}     # classified_as -> {actually_was: count}
        self._species_freq = {}  # species -> total confirmed count
        self._species_hours = {} # species -> (earliest_hour, latest_hour)
        self._total_reviews = 0
        self._last_refresh = 0
        self._refresh_interval = 300  # rebuild every 5 min

    def _refresh(self):
        """Rebuild prior from review data."""
        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return
        self._last_refresh = now

        try:
            conn = sqlite3.connect(f"file:{CLASSIFICATIONS_DB}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row

            # Build confusion matrix from reviews
            rows = conn.execute("""
                SELECT
                    c.common_name as classified_as,
                    CASE WHEN r.verdict = 'correct' THEN c.common_name
                         WHEN r.verdict = 'wrong' AND r.correct_species != '' THEN r.correct_species
                         ELSE NULL END as actually_was
                FROM classifications c
                JOIN reviews r ON r.file = c.file
                WHERE c.action = 'classified'
                AND r.verdict IN ('correct', 'wrong')
                AND c.common_name IS NOT NULL
            """).fetchall()

            confusion = defaultdict(lambda: defaultdict(int))
            species_freq = defaultdict(int)
            for r in rows:
                if r["actually_was"]:
                    confusion[r["classified_as"]][r["actually_was"]] += 1
                    species_freq[r["actually_was"]] += 1

            self._confusion = dict(confusion)
            self._species_freq = dict(species_freq)
            self._total_reviews = len(rows)

            # Build activity hours from confirmed detections
            hour_rows = conn.execute("""
                SELECT c.common_name,
                    MIN(CAST(SUBSTR(c.source_timestamp, 12, 2) AS INTEGER)) as earliest,
                    MAX(CAST(SUBSTR(c.source_timestamp, 12, 2) AS INTEGER)) as latest
                FROM classifications c
                JOIN reviews r ON r.file = c.file
                WHERE r.verdict = 'correct' AND c.source_timestamp IS NOT NULL
                GROUP BY c.common_name
                HAVING COUNT(*) >= 3
            """).fetchall()
            self._species_hours = {
                r["common_name"]: (r["earliest"], r["latest"])
                for r in hour_rows
            }
            conn.close()

            if self._total_reviews >= MIN_REVIEWS_FOR_PRIOR:
                log.info("Yard prior loaded: %d reviews, %d species in confusion matrix",
                         self._total_reviews, len(confusion))
        except Exception as e:
            log.warning("Failed to refresh yard prior: %s", e)

    def correct(self, classified_as, confidence, top3, source_timestamp=None, source_date=None):
        """Apply yard prior to a classification result.

        Args:
            classified_as: The classifier's top prediction (species name)
            confidence: The classifier's confidence (0-1)
            top3: List of top 3 predictions [{"common_name": ..., "raw_score": ...}, ...]
            source_timestamp: For audio corroboration lookup
            source_date: For audio corroboration lookup

        Returns:
            dict with:
                corrected_species: The adjusted prediction (may differ from classified_as)
                corrected_confidence: Adjusted confidence
                correction_reason: Why it was changed (or None)
                audio_corroborated: Whether audio confirmed this species
        """
        self._refresh()

        result = {
            "corrected_species": classified_as,
            "corrected_confidence": confidence,
            "correction_reason": None,
            "trust_level": "normal",  # normal, likely_correct, unusual, probably_wrong
            "audio_corroborated": False,
            "audio_species": None,
        }

        if self._total_reviews < MIN_REVIEWS_FOR_PRIOR:
            return result  # not enough data yet

        # Step 1: Check confusion matrix — what does this species usually turn out to be?
        confusion_row = self._confusion.get(classified_as)
        if confusion_row:
            total = sum(confusion_row.values())
            if total >= 5:  # need at least 5 reviews for this species
                # What's the most common actual species when classifier says X?
                correct_count = confusion_row.get(classified_as, 0)
                correct_rate = correct_count / total

                if correct_rate < 0.5:
                    # Classifier is wrong more than half the time for this species!
                    # Find the most likely actual species
                    most_likely = max(confusion_row.items(), key=lambda x: x[1])
                    if most_likely[0] != classified_as and most_likely[1] / total > 0.4:
                        result["corrected_species"] = most_likely[0]
                        result["corrected_confidence"] = confidence * (most_likely[1] / total)
                        result["correction_reason"] = (
                            f"Prior: {classified_as} is usually {most_likely[0]} "
                            f"({most_likely[1]}/{total} reviews)"
                        )

        # Time-of-day plausibility check
        if source_timestamp and len(source_timestamp) >= 13:
            try:
                hour = int(source_timestamp[11:13])
                hours = self._species_hours.get(classified_as)
                if hours:
                    earliest, latest = hours
                    # Add 1 hour buffer on each side
                    if hour < earliest - 1 or hour > latest + 1:
                        result["time_implausible"] = True
                        result["trust_level"] = "probably_wrong"
                        result["correction_reason"] = (
                            f"Time implausible: {classified_as} seen {earliest}:00-{latest}:00, "
                            f"detected at {hour}:00"
                        )
            except (ValueError, IndexError):
                pass

        # Set trust level based on yard frequency
        if classified_as in self._species_freq:
            freq = self._species_freq[classified_as]
            if freq >= 20:
                result["trust_level"] = "likely_correct"
            elif freq >= 5:
                result["trust_level"] = "normal"
            else:
                result["trust_level"] = "unusual"
        else:
            # Never confirmed in this yard — could be real but worth checking
            result["trust_level"] = "unusual"

        # Step 2: Audio corroboration
        if source_timestamp and source_date:
            audio_species = self._check_audio(source_date, source_timestamp)
            if audio_species:
                result["audio_species"] = audio_species
                if audio_species == result["corrected_species"]:
                    result["audio_corroborated"] = True
                    result["corrected_confidence"] = min(1.0,
                        result["corrected_confidence"] + AUDIO_MATCH_BOOST)
                elif audio_species == classified_as and result["corrected_species"] != classified_as:
                    # Audio agrees with original classifier, disagrees with prior correction
                    # Trust audio over prior
                    result["corrected_species"] = classified_as
                    result["corrected_confidence"] = confidence + AUDIO_MATCH_BOOST
                    result["audio_corroborated"] = True
                    result["correction_reason"] = "Audio overrides prior correction"
                elif audio_species != result["corrected_species"]:
                    # Audio hears something else entirely
                    result["corrected_confidence"] = max(0.1,
                        result["corrected_confidence"] - AUDIO_MISMATCH_PENALTY)

        return result

    def _check_audio(self, date, timestamp):
        """Check if BirdNET heard anything within ±30s of this timestamp."""
        try:
            conn = sqlite3.connect(f"file:{BIRDNET_DB}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT common_name, confidence FROM notes
                WHERE date = ? AND ABS(
                    (CAST(SUBSTR(?, 12, 2) AS INTEGER)*3600 +
                     CAST(SUBSTR(?, 15, 2) AS INTEGER)*60 +
                     CAST(SUBSTR(?, 18, 2) AS INTEGER))
                    -
                    (CAST(SUBSTR(time, 1, 2) AS INTEGER)*3600 +
                     CAST(SUBSTR(time, 4, 2) AS INTEGER)*60 +
                     CAST(SUBSTR(time, 7, 2) AS INTEGER))
                ) <= 30
                ORDER BY confidence DESC LIMIT 1
            """, (date, timestamp, timestamp, timestamp)).fetchone()
            conn.close()
            return row["common_name"] if row else None
        except Exception:
            return None

    def get_stats(self):
        """Return current prior stats for debugging/display."""
        self._refresh()
        corrections = {}
        for classified_as, actuals in self._confusion.items():
            total = sum(actuals.values())
            correct = actuals.get(classified_as, 0)
            if total >= 5 and correct / total < 0.5:
                most_likely = max(actuals.items(), key=lambda x: x[1])
                corrections[classified_as] = {
                    "usually_is": most_likely[0],
                    "rate": round(most_likely[1] / total, 2),
                    "reviews": total,
                }
        return {
            "total_reviews": self._total_reviews,
            "active_corrections": corrections,
            "species_frequencies": dict(sorted(
                self._species_freq.items(), key=lambda x: -x[1]
            )[:15]),
        }
