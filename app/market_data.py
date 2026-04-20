from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


def to_utc_iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class YahooForexProvider:
    """
    Public market data adapter.
    Endpoint can be slightly delayed depending on symbol and exchange source.
    """

    def __init__(self, *, timeout_seconds: float = 12.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _pair_to_symbol(pair: str) -> str:
        raw = pair.replace("/", "").replace("-", "").upper()
        if len(raw) != 6:
            raise ValueError(f"Invalid pair format: {pair}")
        return f"{raw}=X"

    async def fetch_candles(self, pair: str, *, interval: str = "1m", points: int = 180) -> list[Candle]:
        symbol = self._pair_to_symbol(pair)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "interval": interval,
            "range": "1d",
            "includePrePost": "true",
            "events": "div,splits",
        }
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()

        chart = payload.get("chart", {})
        errors = chart.get("error")
        if errors:
            raise RuntimeError(f"Yahoo data error for {pair}: {errors}")

        results = chart.get("result") or []
        if not results:
            return []

        first = results[0]
        timestamps = first.get("timestamp") or []
        quote = (((first.get("indicators") or {}).get("quote") or [{}])[0]) or {}
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        candles: list[Candle] = []
        size = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
        for i in range(size):
            if (
                timestamps[i] is None
                or opens[i] is None
                or highs[i] is None
                or lows[i] is None
                or closes[i] is None
            ):
                continue

            volume = float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0
            candles.append(
                Candle(
                    timestamp=to_utc_iso_from_unix(int(timestamps[i])),
                    open=float(opens[i]),
                    high=float(highs[i]),
                    low=float(lows[i]),
                    close=float(closes[i]),
                    volume=volume,
                )
            )

        if points <= 0:
            return candles
        return candles[-points:]
