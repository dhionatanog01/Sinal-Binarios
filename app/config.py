from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _is_serverless_mode() -> bool:
    truthy = {"1", "true", "yes", "on"}
    return (
        os.getenv("VERCEL", "").strip().lower() in truthy
        or os.getenv("SERVERLESS_MODE", "").strip().lower() in truthy
    )


def _default_db_path() -> str:
    explicit = os.getenv("DB_PATH", "").strip()
    if explicit:
        return explicit
    if _is_serverless_mode():
        return str(Path(tempfile.gettempdir()) / "forex_signal_hub.db")
    return "data.db"


@dataclass(frozen=True)
class Settings:
    app_name: str = "Forex Binary Signal Hub"
    serverless_mode: bool = _is_serverless_mode()
    db_path: str = _default_db_path()
    pairs: tuple[str, ...] = tuple(
        item.strip().upper()
        for item in os.getenv("FOREX_PAIRS", "EURUSD,GBPUSD,USDJPY").split(",")
        if item.strip()
    )
    polling_seconds: int = int(os.getenv("POLLING_SECONDS", "20"))
    history_points: int = int(os.getenv("HISTORY_POINTS", "180"))
    min_signal_gap_seconds: int = int(os.getenv("MIN_SIGNAL_GAP_SECONDS", "60"))
    default_timeframe: str = os.getenv("DEFAULT_TIMEFRAME", "1m")
    default_expiry_minutes: int = int(os.getenv("DEFAULT_EXPIRY_MINUTES", "5"))
    broker_webhook_url: str = os.getenv("BROKER_WEBHOOK_URL", "").strip()
    broker_auth_token: str = os.getenv("BROKER_AUTH_TOKEN", "").strip()
    broker_dispatch_timeout_seconds: int = int(os.getenv("BROKER_DISPATCH_TIMEOUT_SECONDS", "10"))


settings = Settings()
