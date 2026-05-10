"""Greedy nearest 1:1 annotation/event matcher.

Per spec §6 I3: matching is greedy-nearest, 1:1. Annotations sorted by
identifiable midpoint; for each annotation in order, find the closest
unclaimed event with matching species (or any species if annotation has
no identifiable window). Annotation has 1:1 claim on the event.

This is NOT optimal (Hungarian would be); flagged in spec §6 N-I2.
Sufficient for v1 because feeder visits rarely overlap within ±500ms.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from tools.annotation_parser import Visit


@dataclass
class Event:
    pts: float
    species: Optional[str] = None       # canonical lowercased
    tracks: list = field(default_factory=list)
    raw: Optional[dict] = None


@dataclass
class AnnotationResult:
    visit_id: str
    detection_matched: bool
    species_matched: Optional[bool]      # None if no species required
    matched_event: Optional[Event] = None
    lag_ms: Optional[float] = None       # event.pts - identifiable_midpoint
    fail_reason: Optional[str] = None


@dataclass
class MatchSummary:
    results: list[AnnotationResult]
    false_positives: list[Event]
    unclaimed_events: list[Event]


def _id_midpoint(v: Visit) -> Optional[float]:
    if v.first_identifiable_s is not None and v.last_identifiable_s is not None:
        return (v.first_identifiable_s + v.last_identifiable_s) / 2
    return None


def match_annotations_to_events(
    visits: list[Visit],
    events: list[Event],
    *,
    detection_window_ms: int = 500,
    species_window_ms: int = 1000,
) -> MatchSummary:
    # Sort annotations by identifiable midpoint when available, else by
    # in-frame midpoint.
    def sort_key(v: Visit) -> float:
        if v.first_identifiable_s is not None and v.last_identifiable_s is not None:
            return _id_midpoint(v) or 0
        return ((v.first_in_frame_s or 0) + (v.last_in_frame_s or 0)) / 2

    visits_sorted = sorted(visits, key=sort_key)
    claimed = set()
    results = []

    for v in visits_sorted:
        result = AnnotationResult(
            visit_id=v.id,
            detection_matched=False,
            species_matched=None,
        )
        id_mid = _id_midpoint(v)
        # 1) Detection assertion: any event inside [first_in_frame, last_in_frame]
        #    +/- detection_window
        in_frame_lo = (v.first_in_frame_s or 0) - detection_window_ms / 1000
        in_frame_hi = (v.last_in_frame_s or 0) + detection_window_ms / 1000
        # Find nearest unclaimed event in this range
        best_idx = None
        best_dist = float("inf")
        for i, e in enumerate(events):
            if i in claimed: continue
            if e.pts < in_frame_lo or e.pts > in_frame_hi: continue
            d = abs(e.pts - ((v.first_in_frame_s or 0) + (v.last_in_frame_s or 0)) / 2)
            if d < best_dist:
                best_dist = d; best_idx = i

        if best_idx is not None:
            result.detection_matched = True

        # 2) Species assertion (only if annotation has species + id window)
        if v.species and id_mid is not None:
            best_sp_idx = None
            best_sp_dist = float("inf")
            for i, e in enumerate(events):
                if i in claimed: continue
                if abs(e.pts - id_mid) > species_window_ms / 1000: continue
                if (e.species or "").lower() != v.species: continue
                d = abs(e.pts - id_mid)
                if d < best_sp_dist:
                    best_sp_dist = d; best_sp_idx = i
            if best_sp_idx is not None:
                result.species_matched = True
                claimed.add(best_sp_idx)
                result.matched_event = events[best_sp_idx]
                result.lag_ms = (events[best_sp_idx].pts - id_mid) * 1000
            else:
                result.species_matched = False
                result.fail_reason = "no event with matching species in window"
        elif v.species and id_mid is None:
            # Species given but no identifiable window → can't assert species
            result.species_matched = None
        # else: no species required

        results.append(result)

    # False positives: events outside ALL in-frame windows
    fps = []
    for i, e in enumerate(events):
        in_any = False
        for v in visits:
            lo = (v.first_in_frame_s or 0) - detection_window_ms / 1000
            hi = (v.last_in_frame_s or 0) + detection_window_ms / 1000
            if lo <= e.pts <= hi:
                in_any = True; break
        if not in_any:
            fps.append(e)

    unclaimed = [e for i, e in enumerate(events) if i not in claimed]
    return MatchSummary(results=results, false_positives=fps, unclaimed_events=unclaimed)
