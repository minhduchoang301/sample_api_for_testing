"""The single source of time for all business logic."""

from __future__ import annotations

from datetime import UTC, datetime

from app.checkout.config import settings


def parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_override: datetime | None = parse_instant(settings.fake_now) if settings.fake_now else None


def now() -> datetime:
    """Current application time (timezone-aware UTC)."""
    return _override if _override is not None else datetime.now(UTC)


def set_fixed(instant: datetime) -> None:
    global _override
    _override = instant.astimezone(UTC)


def clear() -> None:
    global _override
    _override = None


def is_fixed() -> bool:
    return _override is not None


def isoformat(value: datetime) -> str:
    """Serialize a datetime as ISO-8601 UTC with a trailing Z."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
