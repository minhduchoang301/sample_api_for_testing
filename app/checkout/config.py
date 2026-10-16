"""Runtime configuration read from the environment once at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    test_mode: bool = field(default_factory=lambda: _flag("TEST_MODE", "1"))
    fake_now: str | None = field(default_factory=lambda: os.environ.get("FAKE_NOW") or None)
    jwt_secret: str = field(
        default_factory=lambda: os.environ.get("JWT_SECRET", "paylab-dev-jwt-secret")
    )
    jwt_ttl_minutes: int = field(
        default_factory=lambda: int(os.environ.get("JWT_TTL_MINUTES", "720"))
    )
    webhook_secret: str = field(
        default_factory=lambda: os.environ.get("WEBHOOK_SECRET", "whsec_paylab_dev")
    )
    provider_url: str = field(
        default_factory=lambda: os.environ.get("PROVIDER_URL", "http://localhost:8001").rstrip("/")
    )
    provider_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("PROVIDER_TIMEOUT_S", "3"))
    )
    callback_base_url: str = field(
        default_factory=lambda: os.environ.get("CALLBACK_BASE_URL", "http://localhost:8000").rstrip(
            "/"
        )
    )
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./data/paylab.db")
    )
    bugs_raw: list[str] = field(default_factory=lambda: _csv("BUGS"))


settings = Settings()
