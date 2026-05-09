"""
Shared solar calculation utilities — NOAA gamma/equation-of-time algorithm.

Originally extracted from classify.py to eliminate duplicated code across
the (now retired) live_detector.py and audio_analyzer.py. Today imported
by classify.py and audio_analyzer.py; bird_pipeline_v3.py reads daylight
state directly from health/SSE rather than calling these helpers.

All functions use Chilmark, MA (41.35, -70.75) as defaults so existing
zero-argument call sites work unchanged.
"""

import math
from datetime import date, datetime

# Default location: Chilmark, Martha's Vineyard, MA
DEFAULT_LAT = 41.35
DEFAULT_LON = -70.75


def solar_times(lat, lon, dt=None):
    """Calculate sunrise and sunset hours (UTC) using NOAA simplified algorithm."""
    if dt is None:
        dt = date.today()
    doy = dt.timetuple().tm_yday
    lat_rad = math.radians(lat)
    gamma = 2 * math.pi / 365 * (doy - 1)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    cos_ha = math.cos(math.radians(90.833)) / (
        math.cos(lat_rad) * math.cos(decl)
    ) - math.tan(lat_rad) * math.tan(decl)
    cos_ha = max(-1.0, min(1.0, cos_ha))
    ha = math.degrees(math.acos(cos_ha))
    noon_utc = 720 - 4 * lon - eqtime
    sunrise_utc = (noon_utc - ha * 4) / 60  # hours
    sunset_utc = (noon_utc + ha * 4) / 60   # hours
    return sunrise_utc, sunset_utc


def _utc_offset_for_date(dt):
    """Return UTC offset for a specific date using the system's local timezone.

    Uses noon on the given date to query the OS timezone database, which
    correctly handles DST transitions (which happen at 2:00 AM, not noon).
    Previously this ignored the dt parameter and always used the current time,
    causing sunrise/sunset to be off by 1 hour on dates across DST boundaries.
    """
    # Create a datetime at noon on the given date and get its local UTC offset
    # Using noon avoids the 2 AM DST transition edge case
    noon = datetime(dt.year, dt.month, dt.day, 12, 0, 0).astimezone()
    return int(noon.utcoffset().total_seconds() / 3600)


def is_nighttime(lat=DEFAULT_LAT, lon=DEFAULT_LON, offset_minutes=30):
    """Check if it's past sunset+offset or before sunrise for the given location.

    Call with zero arguments to use Chilmark, MA defaults (backward-compatible
    with existing call sites that pass no arguments).
    """
    now = datetime.now()
    today = now.date()
    sunrise_utc, sunset_utc = solar_times(lat, lon, today)
    offset = _utc_offset_for_date(today)
    sunrise_local = sunrise_utc + offset
    sunset_local = sunset_utc + offset
    current_hours = now.hour + now.minute / 60.0
    sunrise_cutoff = sunrise_local - offset_minutes / 60.0
    sunset_cutoff = sunset_local + offset_minutes / 60.0
    return current_hours >= sunset_cutoff or current_hours < sunrise_cutoff


def is_twilight_window(lat=DEFAULT_LAT, lon=DEFAULT_LON, window_minutes=30):
    """Check if current time is within window_minutes of sunrise or sunset.

    Call with zero arguments to use Chilmark, MA defaults (backward-compatible
    with existing call sites that pass no arguments).
    """
    now = datetime.now()
    today = now.date()
    offset = _utc_offset_for_date(today)
    sunrise_utc, sunset_utc = solar_times(lat, lon, today)
    sunrise_local = sunrise_utc + offset
    sunset_local = sunset_utc + offset
    current_hours = now.hour + now.minute / 60.0
    window_hours = window_minutes / 60.0
    return (abs(current_hours - sunrise_local) < window_hours or
            abs(current_hours - sunset_local) < window_hours)
