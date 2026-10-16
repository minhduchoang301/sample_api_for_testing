"""Feature switches read from the BUGS environment variable."""

from __future__ import annotations

import logging

from app.checkout.config import settings

_KNOWN = frozenset(f"B{n:02d}" for n in range(1, 13))

logger = logging.getLogger("paylab.config")


def _parse(raw: list[str]) -> frozenset[str]:
    enabled = {item for item in raw if item in _KNOWN}
    unknown = sorted(set(raw) - _KNOWN)
    if unknown:
        logger.warning("ignoring unknown entries in BUGS", extra={"unknown_entries": unknown})
    return frozenset(enabled)


_enabled: frozenset[str] = _parse(settings.bugs_raw)


def bug_enabled(bug_id: str) -> bool:
    return bug_id in _enabled
