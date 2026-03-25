"""Overlap-based detection confirmation for BirdNET audio analysis.

Replaces the deep detection accumulator with BirdNET-Go's proven approach:
analyze overlapping 3-second windows, require N confirmations of the same
species within a flush window to accept a detection.

Used by audio_analyzer.py.
"""

import logging
import time

log = logging.getLogger(__name__)


class OverlapConfirmation:
    """Accumulate detections across overlapping analysis windows.

    For each species, tracks detections within a flush window (default 6s).
    When the window expires, accepts species with >= min_confirmations hits,
    using the highest-confidence detection as the representative.

    No cooldown — consecutive windows can both produce accepted detections.
    This matches BirdNET-Go's overlap confirmation model.
    """

    def __init__(self, flush_window=6.0, min_confirmations=2):
        self.flush_window = flush_window
        self.min_confirmations = min_confirmations
        self._pending = {}

    def add(self, species, confidence, det_dict, now=None):
        """Add a detection candidate from an overlapping window.

        Returns list of accepted detections if any pending species
        have expired windows (auto-flush on each add).
        """
        if now is None:
            now = time.time()

        if species not in self._pending:
            self._pending[species] = {
                "first_seen": now,
                "count": 1,
                "best_conf": confidence,
                "best_det": dict(det_dict),  # copy to avoid mutation
            }
        else:
            entry = self._pending[species]
            entry["count"] += 1
            if confidence > entry["best_conf"]:
                entry["best_conf"] = confidence
                entry["best_det"] = dict(det_dict)

        return self.flush(now)

    def flush(self, now=None):
        """Check for expired windows and return accepted detections.

        Returns list of detection dicts (with added 'confirmations' key)
        for species that met the min_confirmations threshold.
        """
        if now is None:
            now = time.time()

        accepted = []
        expired = []

        for species, entry in self._pending.items():
            age = now - entry["first_seen"]
            if age >= self.flush_window:
                if entry["count"] >= self.min_confirmations:
                    det = dict(entry["best_det"])
                    det["confirmations"] = entry["count"]
                    accepted.append(det)
                    log.info("Confirmed: %s (%d/%d windows, best %.0f%%)",
                             species, entry["count"], self.min_confirmations,
                             entry["best_conf"] * 100)
                else:
                    log.debug("Discarded: %s (%d/%d windows, insufficient)",
                              species, entry["count"], self.min_confirmations)
                expired.append(species)

        for species in expired:
            del self._pending[species]

        return accepted

    def flush_all(self):
        """Force-flush all pending detections regardless of window age.

        Call when the analysis stream disconnects to avoid losing
        detections that were accumulating when the stream dropped.
        Returns accepted detections (same as flush).
        """
        far_future = time.time() + self.flush_window + 1
        return self.flush(far_future)

    @property
    def pending_count(self):
        """Number of species currently accumulating."""
        return len(self._pending)
