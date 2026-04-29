"""
Within-track disagreement detection for yard model confidence correction.

Problem (from forget_me_nots.md): Yard model emits single-frame confidences
at 100% regardless of actual certainty, defeating the confidence-gated AIY
fallback. A single bird tracked across 3 frames might get species A→B→C
predictions (all 100% confident), but this inconsistency indicates true
uncertainty that the single-frame confidence metric misses.

Solution: Detect when a track's species labels DISAGREE across consecutive
frames (>60% species disagreement over the track window). When disagreement
is detected, mark the track as uncertain and trigger AIY fallback regardless
of per-frame softmax confidence.

This is the most actionable fix from project_forget_me_nots.md, fixing
the root cause of unreliable training data quality.
"""

from collections import defaultdict, deque
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field


@dataclass
class TrackSpeciesWindow:
    """Track species predictions over a sliding window."""
    track_id: int
    species_history: deque = field(default_factory=lambda: deque(maxlen=5))  # Last 5 predictions
    confidence_history: deque = field(default_factory=lambda: deque(maxlen=5))

    def add(self, species: str, confidence: float):
        """Record a species prediction for this track."""
        self.species_history.append(species)
        self.confidence_history.append(confidence)

    def is_disagreed(self, threshold: float = 0.6) -> bool:
        """
        Return True if this track shows significant species disagreement.

        Disagreement = (unique species count / window size) > threshold
        Example: if last 5 frames are [Cardinal, Chickadee, Cardinal, Wren, Chickadee],
        then unique=3, window=5, ratio=0.6 → disagreed=True (should defer to AIY)
        """
        if len(self.species_history) < 3:
            return False  # Not enough data to judge

        unique_species = len(set(self.species_history))
        disagreement_ratio = unique_species / len(self.species_history)
        return disagreement_ratio > threshold

    def disagreement_score(self) -> float:
        """Return disagreement ratio (0.0=all same, 1.0=all unique)."""
        if not self.species_history:
            return 0.0
        unique_species = len(set(self.species_history))
        return unique_species / len(self.species_history)

    def most_common_species(self) -> Optional[str]:
        """Return the most frequently predicted species in the window."""
        if not self.species_history:
            return None
        from collections import Counter
        counts = Counter(self.species_history)
        return counts.most_common(1)[0][0]


class TrackDisagreementDetector:
    """
    Detect within-track species disagreement to identify uncertain predictions.

    Usage in SmartClassifier:
        detector = TrackDisagreementDetector()
        for detection in detections:
            is_uncertain = detector.check(track_id=detection.track_id,
                                         species=detection.species,
                                         confidence=detection.confidence)
            if is_uncertain:
                # Override yard_model confidence; use AIY instead
                classifier.use_aiy_fallback = True
    """

    def __init__(self, track_window_size: int = 5, disagreement_threshold: float = 0.6):
        """
        Args:
            track_window_size: how many recent predictions to keep per track
            disagreement_threshold: ratio of unique species at which a track is deemed uncertain
        """
        self.track_windows: Dict[int, TrackSpeciesWindow] = {}
        self.window_size = track_window_size
        self.disagreement_threshold = disagreement_threshold
        self.uncertainty_detections = 0  # Metric for monitoring

    def check(self, track_id: int, species: str, confidence: float) -> bool:
        """
        Check if a track shows disagreement; update window.

        Args:
            track_id: norfair track ID
            species: yard model's top-1 species prediction
            confidence: yard model's top-1 confidence score

        Returns:
            True if track shows significant disagreement (should use AIY fallback)
        """
        if track_id not in self.track_windows:
            self.track_windows[track_id] = TrackSpeciesWindow(track_id)

        window = self.track_windows[track_id]
        window.add(species, confidence)

        is_disagreed = window.is_disagreed(self.disagreement_threshold)
        if is_disagreed:
            self.uncertainty_detections += 1

        return is_disagreed

    def cleanup_expired_tracks(self, active_track_ids: List[int]):
        """Remove tracks that are no longer active (e.g., bird left frame)."""
        expired = [tid for tid in self.track_windows if tid not in active_track_ids]
        for tid in expired:
            del self.track_windows[tid]

    def get_stats(self) -> Dict:
        """Return monitoring statistics."""
        total_tracks = len(self.track_windows)
        disagreed_tracks = sum(1 for w in self.track_windows.values()
                              if w.is_disagreed(self.disagreement_threshold))

        return {
            "total_tracks": total_tracks,
            "disagreed_tracks": disagreed_tracks,
            "disagreement_ratio": disagreed_tracks / total_tracks if total_tracks > 0 else 0.0,
            "uncertainty_detections": self.uncertainty_detections,
        }

    def get_track_report(self, track_id: int) -> Optional[Dict]:
        """Get detailed report for a specific track."""
        if track_id not in self.track_windows:
            return None

        window = self.track_windows[track_id]
        return {
            "track_id": track_id,
            "species_history": list(window.species_history),
            "confidence_history": list(window.confidence_history),
            "is_disagreed": window.is_disagreed(self.disagreement_threshold),
            "disagreement_score": window.disagreement_score(),
            "most_common_species": window.most_common_species(),
        }


def integrate_with_smart_classifier():
    """
    Integration example for SmartClassifier.

    In classify.py or smart_classifier.py:

    ```python
    from pipeline.track_disagreement_detector import TrackDisagreementDetector

    detector = TrackDisagreementDetector(
        track_window_size=5,
        disagreement_threshold=0.6  # >60% unique species = uncertain
    )

    # In the per-track classification loop:
    for track in tracker.tracks.values():
        yard_species, yard_conf = yard_model.predict(crop)

        # Check for within-track disagreement
        is_uncertain = detector.check(track.id, yard_species, yard_conf)

        if is_uncertain:
            # Yard model is uncertain (inconsistent); use AIY instead
            aiy_species, aiy_conf = aiy_model.predict(crop)
            final_species = aiy_species
            final_conf = aiy_conf
            confidence_source = "aiy_fallback_track_disagreement"
        elif yard_conf >= confident_threshold:
            # Yard model is confident and consistent
            final_species = yard_species
            final_conf = yard_conf
            confidence_source = "yard_confident"
        else:
            # Yard model is uncertain (low per-frame confidence); use AIY
            aiy_species, aiy_conf = aiy_model.predict(crop)
            final_species = aiy_species
            final_conf = aiy_conf
            confidence_source = "aiy_fallback_low_confidence"

        # Log or metric the confidence_source for analysis
        vote_lock.add_vote(final_species, final_conf, source=confidence_source)

    # Cleanup expired tracks
    detector.cleanup_expired_tracks([t.id for t in tracker.tracks.values()])

    # Monitor disagreement detection
    stats = detector.get_stats()
    health.update_shared("disagreement_detector", stats)
    ```
    """
    pass


if __name__ == "__main__":
    # Simple test
    detector = TrackDisagreementDetector(track_window_size=5, disagreement_threshold=0.6)

    # Simulate a track with consistent species
    print("Track 1: consistent species (Northern Cardinal)")
    for i in range(5):
        is_uncertain = detector.check(1, "Northern Cardinal", 0.95)
        print(f"  Frame {i}: uncertain={is_uncertain}")

    # Simulate a track with disagreeing species
    print("\nTrack 2: disagreeing species")
    species_sequence = ["Cardinal", "Chickadee", "Cardinal", "Wren", "Chickadee"]
    for i, species in enumerate(species_sequence):
        is_uncertain = detector.check(2, species, 0.95)  # All confident but inconsistent
        print(f"  Frame {i} ({species}): uncertain={is_uncertain}")

    # Report
    print("\nDetector Stats:")
    print(detector.get_stats())
    print("\nTrack 2 Report:")
    print(detector.get_track_report(2))
