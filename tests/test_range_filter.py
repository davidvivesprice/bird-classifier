"""Tests for geographic range filtering (regional species filter).

The SpeciesClassifier accepts a regional_species set and filters predictions
so only locally-expected species appear in the filtered results.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bird_inference import SpeciesClassifier, parse_label, normalize_species


# ── Regional species filtering on SpeciesClassifier ──

class TestRegionalFilter:
    """Test that SpeciesClassifier.classify() respects the regional species set."""

    def test_filtered_contains_only_regional_species(
        self, species_model_path, labels_path, regional_species
    ):
        """All species in filtered predictions must be in the regional set."""
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
        dummy = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        filtered, raw = classifier.classify(dummy)

        for pred in filtered:
            name = pred["common_name"]
            # "unidentified" is a valid fallback when no regional match found
            if name != "unidentified":
                assert name in regional_species, (
                    f"Filtered prediction '{name}' not in regional species set"
                )

    def test_raw_may_contain_non_regional_species(
        self, species_model_path, labels_path, regional_species
    ):
        """Raw predictions are unrestricted — can include non-regional species."""
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
        dummy = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        filtered, raw = classifier.classify(dummy)
        # raw should have at least one entry
        assert len(raw) > 0

    def test_no_regional_filter_returns_same(
        self, species_model_path, labels_path
    ):
        """When regional_species=None, filtered == raw."""
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species=None)
        dummy = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        filtered, raw = classifier.classify(dummy)
        assert filtered == raw

    def test_empty_regional_set_returns_unidentified(
        self, species_model_path, labels_path
    ):
        """Empty regional set means nothing passes — should get 'unidentified' fallback."""
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species=set())
        dummy = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        filtered, raw = classifier.classify(dummy)
        assert len(filtered) > 0
        assert filtered[0]["common_name"] == "unidentified bird"

    def test_single_species_filter(
        self, species_model_path, labels_path
    ):
        """With a single-species regional set, filtered has at most 1 match or unidentified."""
        one_species = {"Northern Cardinal"}
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species=one_species)
        dummy = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        filtered, raw = classifier.classify(dummy)
        for pred in filtered:
            assert pred["common_name"] in ("Northern Cardinal", "unidentified")


# ── Regional species list validation ──

class TestRegionalSpeciesList:
    """Tests for the regional species list file itself."""

    def test_regional_file_has_entries(self, regional_species):
        assert len(regional_species) > 10, "Regional species file seems too small"

    def test_known_local_species_in_list(self, regional_species):
        """Common backyard species for Chilmark, MA should be in the list."""
        expected = {
            "Black-capped Chickadee",
            "Northern Cardinal",
            "Blue Jay",
            "American Goldfinch",
            "Downy Woodpecker",
        }
        for sp in expected:
            assert sp in regional_species, f"Expected '{sp}' in regional list"

    def test_tropical_species_not_in_list(self, regional_species):
        """Tropical species should NOT be in a Massachusetts regional list."""
        tropical = [
            "Scarlet Macaw",
            "Toucan",
            "King Penguin",
            "Kookaburra",
            "Resplendent Quetzal",
        ]
        for sp in tropical:
            assert sp not in regional_species, f"'{sp}' should not be in MA regional list"

    def test_no_empty_lines_in_species(self, regional_species):
        """The regional species set should not contain empty strings."""
        assert "" not in regional_species


# ── normalize_species interacts with range filtering ──

class TestNormalizeSpeciesForFilter:
    """Subspecies/regional forms should be normalized before range filtering."""

    def test_slate_colored_junco_maps_to_regional(self, regional_species):
        """Slate-colored Junco should normalize to Dark-eyed Junco (which is regional)."""
        normalized = normalize_species("Slate-colored Junco")
        assert normalized == "Dark-eyed Junco"
        assert normalized in regional_species

    def test_myrtle_warbler_maps_to_regional(self, regional_species):
        """Myrtle Warbler should normalize to Yellow-rumped Warbler."""
        normalized = normalize_species("Myrtle Warbler")
        assert normalized == "Yellow-rumped Warbler"
        # Yellow-rumped Warbler may or may not be in regional list,
        # but the normalization itself must work
        assert normalized != "Myrtle Warbler"

    def test_unknown_species_passes_through(self):
        """A species with no alias should pass through unchanged."""
        assert normalize_species("Imaginary Parrot") == "Imaginary Parrot"

    def test_empty_string_passes_through(self):
        assert normalize_species("") == ""
