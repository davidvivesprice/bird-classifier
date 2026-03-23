"""Tests for overlap_confirmation.py — BirdNET-Go style overlap confirmation."""
import time
import pytest


class TestOverlapConfirmation:

    def test_single_detection_below_min_not_accepted(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        results = oc.add("House Finch", 0.75, {"common_name": "House Finch", "confidence": 0.75}, now=100.0)
        assert results == []
        results = oc.flush(now=107.0)
        assert results == []

    def test_two_detections_accepted(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65, "start_time": 0}, now=100.0)
        oc.add("House Finch", 0.72, {"common_name": "House Finch", "confidence": 0.72, "start_time": 1}, now=101.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1
        assert results[0]["common_name"] == "House Finch"
        assert results[0]["confidence"] == 0.72
        assert results[0]["confirmations"] == 2

    def test_three_detections_returns_best(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("Blue Jay", 0.60, {"common_name": "Blue Jay", "confidence": 0.60}, now=100.0)
        oc.add("Blue Jay", 0.80, {"common_name": "Blue Jay", "confidence": 0.80}, now=101.0)
        oc.add("Blue Jay", 0.70, {"common_name": "Blue Jay", "confidence": 0.70}, now=102.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1
        assert results[0]["confidence"] == 0.80
        assert results[0]["confirmations"] == 3

    def test_different_species_tracked_separately(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65}, now=100.0)
        oc.add("Blue Jay", 0.70, {"common_name": "Blue Jay", "confidence": 0.70}, now=100.5)
        oc.add("House Finch", 0.72, {"common_name": "House Finch", "confidence": 0.72}, now=101.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1
        assert results[0]["common_name"] == "House Finch"

    def test_level0_accepts_everything(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=1)
        oc.add("Cardinal", 0.55, {"common_name": "Cardinal", "confidence": 0.55}, now=100.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1

    def test_expired_detections_flushed_automatically(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65}, now=100.0)
        oc.add("House Finch", 0.70, {"common_name": "House Finch", "confidence": 0.70}, now=101.0)
        results = oc.flush(now=105.0)
        assert results == []
        results = oc.flush(now=107.0)
        assert len(results) == 1

    def test_new_window_after_flush(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65}, now=100.0)
        oc.add("House Finch", 0.70, {"common_name": "House Finch", "confidence": 0.70}, now=101.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1
        oc.add("House Finch", 0.68, {"common_name": "House Finch", "confidence": 0.68}, now=108.0)
        oc.add("House Finch", 0.75, {"common_name": "House Finch", "confidence": 0.75}, now=109.0)
        results = oc.flush(now=115.0)
        assert len(results) == 1
        assert results[0]["confidence"] == 0.75

    def test_no_cooldown(self):
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("X", 0.6, {"common_name": "X", "confidence": 0.6}, now=100.0)
        oc.add("X", 0.7, {"common_name": "X", "confidence": 0.7}, now=101.0)
        r1 = oc.flush(now=107.0)
        assert len(r1) == 1
        oc.add("X", 0.65, {"common_name": "X", "confidence": 0.65}, now=107.0)
        oc.add("X", 0.72, {"common_name": "X", "confidence": 0.72}, now=108.0)
        r2 = oc.flush(now=114.0)
        assert len(r2) == 1

    def test_auto_flush_on_add(self):
        """Adding a detection auto-flushes expired windows."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("A", 0.6, {"common_name": "A", "confidence": 0.6}, now=100.0)
        oc.add("A", 0.7, {"common_name": "A", "confidence": 0.7}, now=101.0)
        # Adding a different species at t=107 should auto-flush A
        results = oc.add("B", 0.5, {"common_name": "B", "confidence": 0.5}, now=107.0)
        assert len(results) == 1
        assert results[0]["common_name"] == "A"
