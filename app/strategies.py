from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SignalDecision:
    strategy_code: str
    direction: str  # put | call
    confidence: float
    reason: str
    timeframe: str
    expiry_minutes: int


def ema_last(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for price in values[period:]:
        ema_val = (price * k) + (ema_val * (1 - k))
    return ema_val


def rsi_last(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = abs(min(delta, 0.0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def bollinger_last(values: list[float], period: int = 20, stdev: float = 2.0) -> tuple[float, float, float] | None:
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((value - mean) ** 2 for value in window) / period
    sigma = math.sqrt(variance)
    upper = mean + (stdev * sigma)
    lower = mean - (stdev * sigma)
    return upper, mean, lower


def evaluate_ema_trend(closes: list[float], params: dict[str, Any]) -> SignalDecision | None:
    fast_n = int(params.get("ema_fast", 9))
    slow_n = int(params.get("ema_slow", 21))
    expiry = int(params.get("expiry_minutes", 5))
    if len(closes) < max(fast_n, slow_n) + 5:
        return None

    fast = ema_last(closes, fast_n)
    slow = ema_last(closes, slow_n)
    if fast is None or slow is None:
        return None

    last = closes[-1]
    diff_ratio = abs(fast - slow) / last if last > 0 else 0
    if diff_ratio < 0.00008:
        return None

    confidence = min(0.92, 0.55 + (diff_ratio * 18000))
    if fast > slow and last >= fast:
        return SignalDecision(
            strategy_code="ema-trend",
            direction="call",
            confidence=round(confidence, 3),
            reason=f"EMA{fast_n}>{slow_n} with bullish alignment",
            timeframe="1m",
            expiry_minutes=expiry,
        )
    if fast < slow and last <= fast:
        return SignalDecision(
            strategy_code="ema-trend",
            direction="put",
            confidence=round(confidence, 3),
            reason=f"EMA{fast_n}<{slow_n} with bearish alignment",
            timeframe="1m",
            expiry_minutes=expiry,
        )
    return None


def evaluate_rsi_reversal(closes: list[float], params: dict[str, Any]) -> SignalDecision | None:
    period = int(params.get("rsi_period", 14))
    rsi_low = float(params.get("rsi_low", 30))
    rsi_high = float(params.get("rsi_high", 70))
    expiry = int(params.get("expiry_minutes", 3))
    rsi = rsi_last(closes, period=period)
    if rsi is None:
        return None

    if rsi <= rsi_low:
        confidence = min(0.9, 0.58 + ((rsi_low - rsi) / 120))
        return SignalDecision(
            strategy_code="rsi-reversal",
            direction="call",
            confidence=round(confidence, 3),
            reason=f"RSI {rsi:.2f} below {rsi_low}",
            timeframe="1m",
            expiry_minutes=expiry,
        )
    if rsi >= rsi_high:
        confidence = min(0.9, 0.58 + ((rsi - rsi_high) / 120))
        return SignalDecision(
            strategy_code="rsi-reversal",
            direction="put",
            confidence=round(confidence, 3),
            reason=f"RSI {rsi:.2f} above {rsi_high}",
            timeframe="1m",
            expiry_minutes=expiry,
        )
    return None


def evaluate_bollinger_reversion(closes: list[float], params: dict[str, Any]) -> SignalDecision | None:
    period = int(params.get("period", 20))
    stdev = float(params.get("stdev", 2.0))
    expiry = int(params.get("expiry_minutes", 2))
    bands = bollinger_last(closes, period=period, stdev=stdev)
    if bands is None:
        return None
    upper, _, lower = bands
    last = closes[-1]
    width = max(upper - lower, 1e-8)
    if last > upper:
        stretch = (last - upper) / width
        confidence = min(0.9, 0.56 + (stretch * 0.8))
        return SignalDecision(
            strategy_code="bollinger-reversion",
            direction="put",
            confidence=round(confidence, 3),
            reason="Price above upper Bollinger band",
            timeframe="1m",
            expiry_minutes=expiry,
        )
    if last < lower:
        stretch = (lower - last) / width
        confidence = min(0.9, 0.56 + (stretch * 0.8))
        return SignalDecision(
            strategy_code="bollinger-reversion",
            direction="call",
            confidence=round(confidence, 3),
            reason="Price below lower Bollinger band",
            timeframe="1m",
            expiry_minutes=expiry,
        )
    return None


def evaluate_strategy(strategy_code: str, closes: list[float], params: dict[str, Any]) -> SignalDecision | None:
    if strategy_code == "ema-trend":
        return evaluate_ema_trend(closes, params)
    if strategy_code == "rsi-reversal":
        return evaluate_rsi_reversal(closes, params)
    if strategy_code == "bollinger-reversion":
        return evaluate_bollinger_reversion(closes, params)
    return None
