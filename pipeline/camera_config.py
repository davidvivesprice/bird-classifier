"""Per-camera classifier configuration."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraClassifierConfig:
    """Per-camera settings for SmartClassifier decision-tree routing.

    Fields:
        use_yard: If True, the classifier runs yard model first, AIY on fallback.
                  If False, yard is skipped entirely — AIY runs alone.
        confident_threshold: Confidence at/above which yard's answer is accepted.
        uncertain_low: Confidence below which yard is considered "useless"
                       and AIY runs as the only classifier.
    """
    use_yard: bool
    confident_threshold: float = 0.6
    uncertain_low: float = 0.3
