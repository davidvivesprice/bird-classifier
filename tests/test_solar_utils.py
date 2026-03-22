"""
Tests for solar_utils.py — NOAA gamma/equation-of-time solar calculations.
"""

import math
from datetime import date

import pytest


# ---------------------------------------------------------------------------
# solar_times
# ---------------------------------------------------------------------------

def test_solar_times_returns_tuple():
    from solar_utils import solar_times
    result = solar_times(41.35, -70.75)
    assert isinstance(result, tuple) and len(result) == 2


def test_solar_times_reasonable_range_chilmark():
    """Sunrise and sunset UTC hours should be plausible for Chilmark, MA."""
    from solar_utils import solar_times
    sunrise_utc, sunset_utc = solar_times(41.35, -70.75)
    # UTC offset for Eastern time is -5 (EST) to -4 (EDT).
    # Sunrise local ≈ 5–8 AM → UTC ≈ 9–13h.
    # Sunset local ≈ 4–8 PM → UTC ≈ 20–24h.
    assert 8.0 <= sunrise_utc <= 14.0, f"Unexpected sunrise UTC: {sunrise_utc}"
    assert 19.0 <= sunset_utc <= 26.0, f"Unexpected sunset UTC: {sunset_utc}"


def test_solar_times_summer_longer_than_winter():
    """Summer days should be longer than winter days (more hours of daylight)."""
    from solar_utils import solar_times
    summer = date(2024, 6, 21)   # summer solstice
    winter = date(2024, 12, 21)  # winter solstice
    sr_s, ss_s = solar_times(41.35, -70.75, summer)
    sr_w, ss_w = solar_times(41.35, -70.75, winter)
    day_length_summer = ss_s - sr_s
    day_length_winter = ss_w - sr_w
    assert day_length_summer > day_length_winter, (
        f"Summer day ({day_length_summer:.2f}h) should be longer than "
        f"winter day ({day_length_winter:.2f}h)"
    )


def test_solar_times_explicit_date():
    """solar_times with an explicit date returns different values than today."""
    from solar_utils import solar_times
    known_date = date(2024, 3, 20)   # spring equinox
    sr, ss = solar_times(41.35, -70.75, known_date)
    # At equinox, day ≈ 12h; half-angle ≈ 6h → sunset - sunrise ≈ 12h
    day_length = ss - sr
    assert 11.5 <= day_length <= 12.5, f"Equinox day length {day_length:.2f}h not near 12h"


# ---------------------------------------------------------------------------
# is_nighttime
# ---------------------------------------------------------------------------

def test_is_nighttime_returns_bool_default_args():
    """is_nighttime() called with zero arguments must return a bool."""
    from solar_utils import is_nighttime
    result = is_nighttime()
    assert isinstance(result, bool)


def test_is_nighttime_accepts_lat_lon():
    """is_nighttime accepts explicit lat/lon/offset_minutes kwargs."""
    from solar_utils import is_nighttime
    result = is_nighttime(lat=41.35, lon=-70.75, offset_minutes=30)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# is_twilight_window
# ---------------------------------------------------------------------------

def test_is_twilight_window_returns_bool_default_args():
    """is_twilight_window() called with zero arguments must return a bool."""
    from solar_utils import is_twilight_window
    result = is_twilight_window()
    assert isinstance(result, bool)


def test_is_twilight_window_accepts_kwargs():
    """is_twilight_window accepts explicit lat/lon/window_minutes kwargs."""
    from solar_utils import is_twilight_window
    result = is_twilight_window(lat=41.35, lon=-70.75, window_minutes=30)
    assert isinstance(result, bool)
