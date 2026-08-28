from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    """Return naive UTC for compatibility with the existing database columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def local_date_bounds_utc(value: str, timezone_name: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(value)
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day, time.max, tzinfo=zone).astimezone(UTC)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)
