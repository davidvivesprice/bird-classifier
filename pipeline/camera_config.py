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

    2026-04-18: Thresholds were calibrated for the pre-fix yard model which
    emitted raw_score=100 on every prediction (see commit that fixed
    softmax-over-top-3 in yard_classifier.py). With honest full-distribution
    softmax:
      - uniform across ~50 classes = 0.02
      - genuinely confident top-1 lands ~0.25–0.35
      - confused/noisy top-1 lands ~0.05–0.15
    Old defaults (0.6 / 0.3) would reject essentially every yard prediction
    post-fix. New defaults below match the real distribution. These may need
    further empirical tuning once we observe a few hours of live data.

    Note AIY scale mismatch: AIY's 'confidence' is raw_score/100 from the
    uint8 model output (0–2.55 range), not a softmax probability. Same
    threshold value means different things for each model — a known
    technical-debt item, filed as a follow-up. Until the scales are
    harmonized, these thresholds err toward letting yard answers through and
    relying on the tracker's vote-lock (≥3 votes, ≥60% agreement) for
    downstream quality filtering.
    """
    use_yard: bool
    confident_threshold: float = 0.25
    uncertain_low: float = 0.10
