from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = "Forex Binary Signal Hub"
    db_path: str = os.getenv("DB_PATH", "data.db")
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
