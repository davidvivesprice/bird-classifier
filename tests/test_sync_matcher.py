import pytest
from tools.annotation_parser import Visit
from tools.sync_matcher import match_annotations_to_events, MatchSummary, Event


def make_visit(id, first_in, last_in, first_id=None, last_id=None, species=None):
    return Visit(
        id=id,
        first_in_frame_s=first_in,
        first_identifiable_s=first_id,
        last_identifiable_s=last_id,
        last_in_frame_s=last_in,
        species=species,
    )


def make_event(pts, species=None):
    return Event(pts=pts, species=species, tracks=[{"species": species}])


def test_matcher_detection_only_pass():
    visits = [make_visit("01", 1.0, 5.0)]
    events = [make_event(3.0, "house finch")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].detection_matched
    assert res.results[0].species_matched is None  # species not asserted (annotation blank)


def test_matcher_species_required_pass():
    visits = [make_visit("01", 1.0, 5.0, first_id=2.0, last_id=4.0, species="house finch")]
    events = [make_event(2.5, "house finch")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].detection_matched
    assert res.results[0].species_matched is True


def test_matcher_species_required_fail_wrong_species():
    visits = [make_visit("01", 1.0, 5.0, first_id=2.0, last_id=4.0, species="house finch")]
    events = [make_event(2.5, "northern cardinal")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].detection_matched
    assert res.results[0].species_matched is False


def test_matcher_no_event_in_window_fail():
    visits = [make_visit("01", 1.0, 5.0, first_id=2.0, last_id=4.0, species="house finch")]
    events = [make_event(10.0, "house finch")]  # way out of window
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    assert not res.results[0].detection_matched
    assert res.results[0].species_matched is False


def test_matcher_1to1_no_double_claim():
    # Two annotations could share an event; matcher must give it to only one
    v1 = make_visit("01", 1.0, 2.0, first_id=1.5, last_id=1.8, species="house finch")
    v2 = make_visit("02", 1.7, 3.0, first_id=2.0, last_id=2.5, species="house finch")
    events = [
        make_event(1.6, "house finch"),  # closer to v1.id_midpoint=1.65
        make_event(2.2, "house finch"),  # closer to v2.id_midpoint=2.25
    ]
    res = match_annotations_to_events([v1, v2], events, detection_window_ms=500, species_window_ms=1000)
    assert res.results[0].species_matched is True
    assert res.results[1].species_matched is True
    # Each event claimed exactly once
    assert len(res.unclaimed_events) == 0


def test_matcher_false_positive_detection():
    visits = [make_visit("01", 1.0, 2.0)]
    events = [make_event(1.5, "house finch"), make_event(10.0, "house finch")]
    res = match_annotations_to_events(visits, events, detection_window_ms=500, species_window_ms=1000)
    # Event at 10.0 is outside all in-frame windows → false positive
    assert any(e.pts == 10.0 for e in res.false_positives)
