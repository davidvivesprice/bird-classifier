"""
Geographic range filtering for bird species.

Validates that detected species are plausible for the detection location and date.
Filters out impossible detections (e.g., Carolina Chickadees in Cape Cod, seabirds inland).
"""

import json
import os
from datetime import datetime

# Location constants
DEFAULT_LATITUDE = 41.39  # Cape Cod, MA
DEFAULT_LONGITUDE = -70.61


class RangeFilter:
    def __init__(self, ranges_file=None):
        """
        Initialize range filter with species range database.

        Args:
            ranges_file: Path to species_ranges.json. Defaults to models/species_ranges.json
        """
        if ranges_file is None:
            # Get path relative to this file
            base_dir = os.path.dirname(os.path.abspath(__file__))
            ranges_file = os.path.join(base_dir, "models", "species_ranges.json")

        with open(ranges_file, "r") as f:
            self.data = json.load(f)

        self.species_db = self.data.get("species", {})
        self.location = self.data.get("metadata", {})
        self.default_lat = self.location.get("latitude", DEFAULT_LATITUDE)
        self.default_lon = self.location.get("longitude", DEFAULT_LONGITUDE)

    def is_species_valid_at_location(self, species_name, latitude=None, longitude=None,
                                     date=None, confidence=None):
        """
        Check if a species is valid (plausible) at a given location and date.

        Args:
            species_name: Common or scientific name of the species
            latitude: Detection latitude (default: Cape Cod)
            longitude: Detection longitude (default: Cape Cod)
            date: Detection date (datetime or "YYYY-MM-DD"). Default: today
            confidence: Detection confidence (0-1). Returns dict if provided for adjustment.

        Returns:
            If confidence is None:
                dict: {
                    "valid": bool,
                    "reason": str,
                    "flags": list,
                    "caution_level": str (none/low/medium/high)
                }

            If confidence is provided:
                dict: {
                    "valid": bool,
                    "reason": str,
                    "adjusted_confidence": float,
                    "flags": list,
                    "caution_level": str
                }
        """
        # Default to location/date if not provided
        if latitude is None:
            latitude = self.default_lat
        if longitude is None:
            longitude = self.default_lon
        if date is None:
            date = datetime.now()
        elif isinstance(date, str):
            date = datetime.strptime(date[:10], "%Y-%m-%d")

        month = date.month
        result = {
            "valid": False,
            "reason": "Unknown species",
            "flags": [],
            "caution_level": "none"
        }

        # Look up species in database — if not present, allow through (no data = no rejection)
        if species_name not in self.species_db:
            result["valid"] = True
            result["reason"] = "Species not in range database (allowed)"
            return result

        species_info = self.species_db[species_name]

        # Check latitude bounds
        north_limit = species_info.get("north_limit")
        south_limit = species_info.get("south_limit")

        if north_limit and latitude > north_limit:
            result["reason"] = f"North of valid range ({latitude}°N > {north_limit}°N)"
            result["flags"].append("range_boundary_violation")
            return result

        if south_limit and latitude < south_limit:
            result["reason"] = f"South of valid range ({latitude}°N < {south_limit}°N)"
            result["flags"].append("range_boundary_violation")
            return result

        # Check coastal-only species (seabirds)
        if species_info.get("coastal_only", False):
            # Simple check: if not near coast, flag as invalid
            # Cape Cod is coastal, so this is valid. For inland locations, would need to check.
            # For now, we'll flag all "coastal_only" seabirds detected inland as suspicious
            if "seabird_inland" in species_info.get("flags", []):
                result["reason"] = "Seabird detected inland (extremely high false positive rate in BirdNET)"
                result["flags"].extend(species_info.get("flags", []))
                result["valid"] = False
                return result

        # Check seasonal validity
        status = species_info.get("status")
        valid_months = species_info.get("valid_months", [])

        if status == "seasonal_summer" and valid_months:
            if month not in valid_months:
                result["reason"] = f"Out of season (detected in month {month}, valid: {valid_months})"
                result["flags"].append("out_of_season")
                return result

        elif status == "winter_irruptive" and valid_months:
            if month not in valid_months:
                result["reason"] = f"Out of season (winter visitor detected in month {month})"
                result["flags"].append("out_of_season")
                return result

        elif status == "vagrant":
            # Vagrants are rare and should be flagged for review
            result["valid"] = False
            result["reason"] = "Vagrant species (rare, requires documentation)"
            result["flags"].extend(species_info.get("flags", []))
            result["caution_level"] = "high"
            return result

        # If we got here, species is valid
        result["valid"] = species_info.get("valid", False)
        result["reason"] = "Valid detection"

        # Add caution level if present
        if "caution_level" in species_info:
            result["caution_level"] = species_info["caution_level"]

        # Add any flags from species info
        result["flags"].extend(species_info.get("flags", []))

        # Adjust confidence if requested
        if confidence is not None:
            adjusted_confidence = confidence

            # Lower confidence for high-caution species
            if result["caution_level"] == "high":
                adjusted_confidence *= 0.7
            elif result["caution_level"] == "medium":
                adjusted_confidence *= 0.85

            result["adjusted_confidence"] = adjusted_confidence

        return result

    def filter_detection(self, species_name, confidence=None, latitude=None,
                        longitude=None, date=None):
        """
        Apply range filter to a detection. If invalid, returns alternate status.

        Args:
            species_name: Detected species name
            confidence: Detection confidence (0-1)
            latitude, longitude, date: Detection metadata

        Returns:
            dict: {
                "original_species": str,
                "filtered_species": str (or "unidentified" if invalid),
                "valid": bool,
                "reason": str,
                "flags": list,
                "adjusted_confidence": float (if input confidence provided)
            }
        """
        validation = self.is_species_valid_at_location(
            species_name, latitude, longitude, date, confidence
        )

        result = {
            "original_species": species_name,
            "filtered_species": species_name if validation["valid"] else "unidentified",
            "valid": validation["valid"],
            "reason": validation["reason"],
            "flags": validation.get("flags", [])
        }

        if "adjusted_confidence" in validation:
            result["adjusted_confidence"] = validation["adjusted_confidence"]

        return result

    def get_high_risk_species(self):
        """Return list of species that commonly have false positives."""
        return self.data.get("high_risk_detections", [])

    def is_high_risk(self, species_name):
        """Check if a species is in the high-risk false positive list."""
        return species_name in self.get_high_risk_species()


# Module-level convenience function
_filter = None

def get_filter():
    """Get or create the global RangeFilter instance."""
    global _filter
    if _filter is None:
        _filter = RangeFilter()
    return _filter


def validate_species(species_name, confidence=None, latitude=None,
                    longitude=None, date=None):
    """
    Convenience function: validate a species detection.

    Args:
        species_name: Detected species name
        confidence: Detection confidence (optional, for adjustment)
        latitude, longitude, date: Detection metadata (optional)

    Returns:
        dict: Validation result with valid/reason/adjusted_confidence
    """
    return get_filter().is_species_valid_at_location(
        species_name, latitude, longitude, date, confidence
    )


def filter_detection(species_name, confidence=None, latitude=None,
                    longitude=None, date=None):
    """
    Convenience function: apply range filter to a detection.

    Returns:
        dict: With filtered_species (or "unidentified" if invalid)
    """
    return get_filter().filter_detection(
        species_name, confidence, latitude, longitude, date
    )


# Example usage for testing
if __name__ == "__main__":
    import sys

    filter_obj = RangeFilter()

    # Test cases
    test_species = [
        ("Carolina Chickadee", 0.95, 41.39, -70.61),  # Invalid for Cape Cod
        ("Black-capped Chickadee", 0.95, 41.39, -70.61),  # Valid
        ("Northern Cardinal", 0.92, 41.39, -70.61),  # Valid
        ("Auk", 0.88, 41.39, -70.61),  # Invalid (seabird inland)
        ("Western Tanager", 0.85, 41.39, -70.61),  # Invalid (vagrant)
    ]

    print("Range Filter Test Results:")
    print("=" * 80)

    for species, conf, lat, lon in test_species:
        result = filter_obj.filter_detection(species, conf, lat, lon)
        print(f"\n{species} (conf={conf})")
        print(f"  Valid: {result['valid']}")
        print(f"  Filtered: {result['filtered_species']}")
        print(f"  Reason: {result['reason']}")
        if "adjusted_confidence" in result:
            print(f"  Adjusted conf: {result['adjusted_confidence']:.3f}")
        if result['flags']:
            print(f"  Flags: {', '.join(result['flags'])}")
