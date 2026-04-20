from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class StrategySeed:
    code: str
    name: str
    description: str
    params: dict[str, Any]


STRATEGY_SEEDS: tuple[StrategySeed, ...] = (
    StrategySeed(
        code="ema-trend",
        name="EMA Trend Follow",
        description="Follow short trend direction with fast/slow EMA alignment.",
        params={"ema_fast": 9, "ema_slow": 21, "expiry_minutes": 5},
    ),
    StrategySeed(
        code="rsi-reversal",
        name="RSI Reversal",
        description="Use RSI extremes to anticipate short pullback entries.",
        params={"rsi_period": 14, "rsi_low": 30, "rsi_high": 70, "expiry_minutes": 3},
    ),
    StrategySeed(
        code="bollinger-reversion",
        name="Bollinger Reversion",
        description="Fade price moves outside Bollinger bands.",
        params={"period": 20, "stdev": 2, "expiry_minutes": 2},
    ),
)


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path).resolve())
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    @property
    def db_path(self) -> str:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    params_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_selection (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    selected_strategy_code TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    strategy_code TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('put', 'call')),
                    entry_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    expiry_time TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('open', 'expired', 'settled', 'suppressed')),
                    close_price REAL,
                    settled_at TEXT,
                    outcome TEXT,
                    dispatch_status TEXT NOT NULL DEFAULT 'pending',
                    dispatched_at TEXT,
                    dispatch_error TEXT,
                    metadata_json TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

        self._migrate_signal_dispatch_columns()

        self.seed_strategies()

    def _migrate_signal_dispatch_columns(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA table_info(signals)")
            columns = {row["name"] for row in cur.fetchall()}
            if "dispatch_status" not in columns:
                cur.execute("ALTER TABLE signals ADD COLUMN dispatch_status TEXT NOT NULL DEFAULT 'pending'")
            if "dispatched_at" not in columns:
                cur.execute("ALTER TABLE signals ADD COLUMN dispatched_at TEXT")
            if "dispatch_error" not in columns:
                cur.execute("ALTER TABLE signals ADD COLUMN dispatch_error TEXT")
            self._conn.commit()

    def seed_strategies(self) -> None:
        now = utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            for seed in STRATEGY_SEEDS:
                cur.execute(
                    """
                    INSERT INTO strategies (code, name, description, enabled, params_json, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(code) DO NOTHING
                    """,
                    (seed.code, seed.name, seed.description, json.dumps(seed.params), now, now),
                )
            cur.execute("SELECT COUNT(*) AS c FROM strategy_selection")
            count = int(cur.fetchone()["c"])
            if count == 0:
                cur.execute(
                    "INSERT INTO strategy_selection (id, selected_strategy_code, updated_at) VALUES (1, NULL, ?)",
                    (now,),
                )
            self._conn.commit()

    def list_strategies(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM strategies ORDER BY id")
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "code": row["code"],
                    "name": row["name"],
                    "description": row["description"],
                    "enabled": bool(row["enabled"]),
                    "params": json.loads(row["params_json"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return out

    def set_strategy_enabled(self, code: str, enabled: bool) -> bool:
        now = utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE strategies SET enabled = ?, updated_at = ? WHERE code = ?",
                (1 if enabled else 0, now, code),
            )
            changed = cur.rowcount > 0
            self._conn.commit()
        return changed

    def get_strategy(self, code: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM strategies WHERE code = ?", (code,))
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "description": row["description"],
            "enabled": bool(row["enabled"]),
            "params": json.loads(row["params_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def set_selected_strategy(self, strategy_code: str | None) -> None:
        now = utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE strategy_selection SET selected_strategy_code = ?, updated_at = ? WHERE id = 1",
                (strategy_code, now),
            )
            self._conn.commit()

    def get_selected_strategy(self) -> str | None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT selected_strategy_code FROM strategy_selection WHERE id = 1")
            row = cur.fetchone()
        if not row:
            return None
        return row["selected_strategy_code"]

    def has_signal_for_candle(self, strategy_code: str, pair: str, entry_time: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM signals
                WHERE strategy_code = ? AND pair = ? AND entry_time = ?
                LIMIT 1
                """,
                (strategy_code, pair, entry_time),
            )
            row = cur.fetchone()
        return row is not None

    def has_recent_signal(self, strategy_code: str, pair: str, since_iso: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM signals
                WHERE strategy_code = ? AND pair = ? AND entry_time >= ?
                LIMIT 1
                """,
                (strategy_code, pair, since_iso),
            )
            row = cur.fetchone()
        return row is not None

    def create_signal(
        self,
        *,
        source: str,
        strategy_code: str,
        pair: str,
        timeframe: str,
        direction: str,
        entry_price: float,
        entry_time: str,
        expiry_time: str,
        confidence: float,
        reason: str,
        status: str = "open",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        metadata_json = json.dumps(metadata or {})
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO signals (
                    source, strategy_code, pair, timeframe, direction, entry_price, entry_time,
                    expiry_time, confidence, reason, status, dispatch_status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    strategy_code,
                    pair,
                    timeframe,
                    direction,
                    entry_price,
                    entry_time,
                    expiry_time,
                    confidence,
                    reason,
                    status,
                    "pending",
                    metadata_json,
                ),
            )
            signal_id = int(cur.lastrowid)
            self._conn.commit()
        return signal_id

    def list_signals(self, *, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM signals"
        args: list[Any] = []
        if status:
            query += " WHERE status = ?"
            args.append(status)
        query += " ORDER BY entry_time DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, args)
            rows = cur.fetchall()
        return [self._row_to_signal(row) for row in rows]

    def list_open_signals_due(self, now_iso: str) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT * FROM signals
                WHERE status = 'open' AND expiry_time <= ?
                ORDER BY expiry_time ASC
                """,
                (now_iso,),
            )
            rows = cur.fetchall()
        return [self._row_to_signal(row) for row in rows]

    def settle_signal(
        self,
        *,
        signal_id: int,
        close_price: float,
        settled_at: str,
        outcome: str,
    ) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE signals
                SET close_price = ?, settled_at = ?, outcome = ?, status = 'settled'
                WHERE id = ?
                """,
                (close_price, settled_at, outcome, signal_id),
            )
            self._conn.commit()

    def suppress_signal(self, signal_id: int, settled_at: str, reason: str) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE signals
                SET status = 'suppressed', settled_at = ?, outcome = ?
                WHERE id = ?
                """,
                (settled_at, reason, signal_id),
            )
            self._conn.commit()

    def mark_signal_dispatch(self, signal_id: int, *, status: str, error: str | None = None) -> None:
        now = utc_now_iso()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE signals
                SET dispatch_status = ?, dispatched_at = ?, dispatch_error = ?
                WHERE id = ?
                """,
                (status, now, error, signal_id),
            )
            self._conn.commit()

    def get_strategy_ranking(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT
                    s.code AS strategy_code,
                    s.name AS strategy_name,
                    COUNT(sig.id) AS total,
                    COALESCE(SUM(CASE WHEN sig.outcome = 'win' THEN 1 ELSE 0 END), 0) AS wins,
                    COALESCE(SUM(CASE WHEN sig.outcome = 'loss' THEN 1 ELSE 0 END), 0) AS losses
                FROM strategies s
                LEFT JOIN signals sig
                  ON sig.strategy_code = s.code
                 AND sig.status = 'settled'
                GROUP BY s.code, s.name
                ORDER BY s.code
                """
            )
            rows = cur.fetchall()

        ranking: list[dict[str, Any]] = []
        for row in rows:
            total = int(row["total"])
            wins = int(row["wins"])
            losses = int(row["losses"])
            win_rate = (wins / total) if total > 0 else 0.0
            sample_boost = min(total / 40.0, 1.0)
            score = (0.75 * win_rate) + (0.25 * sample_boost)
            ranking.append(
                {
                    "strategy_code": row["strategy_code"],
                    "strategy_name": row["strategy_name"],
                    "total": total,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(win_rate, 4),
                    "score": round(score, 4),
                }
            )

        ranking.sort(key=lambda item: item["score"], reverse=True)
        for index, item in enumerate(ranking, start=1):
            item["rank"] = index
        return ranking

    def _row_to_signal(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source": row["source"],
            "strategy_code": row["strategy_code"],
            "pair": row["pair"],
            "timeframe": row["timeframe"],
            "direction": row["direction"],
            "entry_price": row["entry_price"],
            "entry_time": row["entry_time"],
            "expiry_time": row["expiry_time"],
            "confidence": row["confidence"],
            "reason": row["reason"],
            "status": row["status"],
            "close_price": row["close_price"],
            "settled_at": row["settled_at"],
            "outcome": row["outcome"],
            "dispatch_status": row["dispatch_status"],
            "dispatched_at": row["dispatched_at"],
            "dispatch_error": row["dispatch_error"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }
