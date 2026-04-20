from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings
from .db import Database
from .market_data import Candle, YahooForexProvider
from .strategies import SignalDecision, evaluate_strategy
from .ws import LiveConnectionManager


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class SignalEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        provider: YahooForexProvider,
        ws_manager: LiveConnectionManager,
    ) -> None:
        self._settings = settings
        self._db = db
        self._provider = provider
        self._ws = ws_manager
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._candles_by_pair: dict[str, list[Candle]] = {}

    def snapshot_prices(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for pair, candles in self._candles_by_pair.items():
            if not candles:
                continue
            last = candles[-1]
            out[pair] = {
                "price": last.close,
                "time": last.timestamp,
            }
        return out

    def latest_price(self, pair: str) -> float | None:
        candles = self._candles_by_pair.get(pair, [])
        if not candles:
            return None
        return candles[-1].close

    def start(self) -> None:
        if self._task:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def step(self) -> None:
        await self._refresh_market_data()
        new_signals = self._generate_internal_signals()
        await self._dispatch_signals(new_signals)
        self._settle_expired_signals()
        await self._broadcast_dashboard()

    async def publish_dashboard(self) -> None:
        await self._broadcast_dashboard()

    async def dispatch_external_signal(self, signal: dict[str, Any]) -> None:
        await self._dispatch_signals([signal])

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.step()
            except Exception as exc:
                await self._ws.broadcast_json({"event": "engine_error", "message": str(exc)})
            await asyncio.sleep(max(3, self._settings.polling_seconds))

    async def _refresh_market_data(self) -> None:
        pairs = self._settings.pairs
        coroutines = [
            self._provider.fetch_candles(
                pair,
                interval=self._settings.default_timeframe,
                points=self._settings.history_points,
            )
            for pair in pairs
        ]
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        for pair, result in zip(pairs, results):
            if isinstance(result, Exception):
                continue
            self._candles_by_pair[pair] = result

    def _active_strategies(self) -> list[dict[str, Any]]:
        selected = self._db.get_selected_strategy()
        all_enabled = [s for s in self._db.list_strategies() if s["enabled"]]
        if selected:
            selected_rows = [s for s in all_enabled if s["code"] == selected]
            if selected_rows:
                return selected_rows
        return all_enabled

    def _generate_internal_signals(self) -> list[dict[str, Any]]:
        strategies = self._active_strategies()
        if not strategies:
            return []

        now = utc_now()
        min_gap = timedelta(seconds=self._settings.min_signal_gap_seconds)
        created: list[dict[str, Any]] = []

        for pair, candles in self._candles_by_pair.items():
            if len(candles) < 35:
                continue
            closes = [bar.close for bar in candles]
            last_bar = candles[-1]

            for strategy in strategies:
                decision = evaluate_strategy(strategy["code"], closes, strategy["params"])
                if not decision:
                    continue
                assert isinstance(decision, SignalDecision)

                entry_time = last_bar.timestamp
                if self._db.has_signal_for_candle(strategy["code"], pair, entry_time):
                    continue

                since = (now - min_gap).isoformat()
                if self._db.has_recent_signal(strategy["code"], pair, since):
                    continue

                entry_dt = parse_iso(entry_time)
                expiry_dt = entry_dt + timedelta(minutes=decision.expiry_minutes)
                signal_id = self._db.create_signal(
                    source="internal",
                    strategy_code=strategy["code"],
                    pair=pair,
                    timeframe=decision.timeframe,
                    direction=decision.direction,
                    entry_price=last_bar.close,
                    entry_time=entry_dt.isoformat(),
                    expiry_time=expiry_dt.isoformat(),
                    confidence=decision.confidence,
                    reason=decision.reason,
                    status="open",
                    metadata={"engine": "internal"},
                )
                created.append(
                    {
                        "id": signal_id,
                        "source": "internal",
                        "strategy_code": strategy["code"],
                        "pair": pair,
                        "timeframe": decision.timeframe,
                        "direction": decision.direction,
                        "entry_price": last_bar.close,
                        "entry_time": entry_dt.isoformat(),
                        "expiry_time": expiry_dt.isoformat(),
                        "confidence": decision.confidence,
                        "reason": decision.reason,
                    }
                )
        return created

    async def _dispatch_signals(self, signals: list[dict[str, Any]]) -> None:
        if not signals:
            return
        if not self._settings.broker_webhook_url:
            for signal in signals:
                self._db.mark_signal_dispatch(int(signal["id"]), status="skipped", error="BROKER_WEBHOOK_URL not set")
            return

        headers = {"Content-Type": "application/json"}
        if self._settings.broker_auth_token:
            headers["Authorization"] = f"Bearer {self._settings.broker_auth_token}"

        timeout = float(max(1, self._settings.broker_dispatch_timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout) as client:
            for signal in signals:
                payload = {
                    "signal_id": signal["id"],
                    "pair": signal["pair"],
                    "direction": signal["direction"],
                    "timeframe": signal["timeframe"],
                    "entry_price": signal["entry_price"],
                    "entry_time": signal["entry_time"],
                    "expiry_time": signal["expiry_time"],
                    "confidence": signal["confidence"],
                    "strategy_code": signal["strategy_code"],
                    "reason": signal["reason"],
                    "source": signal["source"],
                }
                try:
                    response = await client.post(
                        self._settings.broker_webhook_url,
                        json=payload,
                        headers=headers,
                    )
                    if 200 <= response.status_code < 300:
                        self._db.mark_signal_dispatch(int(signal["id"]), status="sent")
                    else:
                        self._db.mark_signal_dispatch(
                            int(signal["id"]),
                            status="failed",
                            error=f"HTTP {response.status_code}",
                        )
                except Exception as exc:
                    self._db.mark_signal_dispatch(int(signal["id"]), status="failed", error=str(exc))

    def _settle_expired_signals(self) -> None:
        now_iso = utc_now().isoformat()
        due = self._db.list_open_signals_due(now_iso)
        if not due:
            return

        for signal in due:
            close_price = self.latest_price(signal["pair"])
            if close_price is None:
                # No market snapshot available now. Keep it open and settle in next cycle.
                continue

            direction = signal["direction"]
            entry = float(signal["entry_price"])
            if direction == "call":
                outcome = "win" if close_price > entry else "loss"
            else:
                outcome = "win" if close_price < entry else "loss"

            self._db.settle_signal(
                signal_id=int(signal["id"]),
                close_price=close_price,
                settled_at=now_iso,
                outcome=outcome,
            )

    async def _broadcast_dashboard(self) -> None:
        await self._ws.broadcast_json(
            {
                "event": "dashboard",
                "data": {
                    "prices": self.snapshot_prices(),
                    "ranking": self._db.get_strategy_ranking(),
                    "open_signals": self._db.list_signals(limit=40, status="open"),
                    "recent_signals": self._db.list_signals(limit=40),
                    "selected_strategy": self._db.get_selected_strategy(),
                    "generated_at": utc_now().isoformat(),
                },
            }
        )
