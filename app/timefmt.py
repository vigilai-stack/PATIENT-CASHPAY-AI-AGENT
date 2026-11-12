"""Clinic-local time for anything a patient reads. The database stores naive UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

CLINIC_TZ_NAME = "America/Chicago"
CLINIC_TZ_LABEL = "CT"

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo(CLINIC_TZ_NAME)
except Exception:  # pragma: no cover - tz database missing; use the US Central rule below
    _TZ = None


def _central_offset(utc: datetime) -> timedelta:
    """US Central: CDT (UTC-5) from 2nd Sunday of March 08:00 UTC to 1st Sunday of November 07:00 UTC."""
    y = utc.year
    march = datetime(y, 3, 8)
    start = march + timedelta(days=(6 - march.weekday()) % 7, hours=8)
    nov = datetime(y, 11, 1)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7, hours=7)
    return timedelta(hours=-5) if start <= utc.replace(tzinfo=None) < end else timedelta(hours=-6)


def to_clinic(dt: datetime) -> datetime:
    """Naive UTC (or aware) datetime -> clinic-local aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if _TZ is not None:
        return dt.astimezone(_TZ)
    utc = dt.astimezone(UTC)
    return utc.astimezone(timezone(_central_offset(utc)))


def when_ct(dt: datetime) -> str:
    """'Wed Nov 11, 8:00 AM CT'."""
    local = to_clinic(dt)
    hour = local.hour % 12 or 12
    return f"{local:%a %b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'} {CLINIC_TZ_LABEL}"
