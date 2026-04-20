from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from .config import settings
from .db import Database
from .engine import SignalEngine
from .market_data import YahooForexProvider
from .ws import LiveConnectionManager


class SelectStrategyRequest(BaseModel):
    strategy_code: str | None = Field(default=None, description="Set null to run all enabled strategies.")


class ToggleStrategyRequest(BaseModel):
    enabled: bool


class TradingViewWebhookPayload(BaseModel):
    pair: str
    direction: Literal["put", "call"]
    timeframe: str = "1m"
    expiry_minutes: int = 5
    strategy_code: str = "tradingview-manual"
    confidence: float = 0.6
    reason: str = "TradingView alert"
    entry_price: float | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def normalize_pair(pair: str) -> str:
    raw = pair.strip().upper().replace("/", "").replace("-", "")
    if len(raw) != 6:
        raise ValueError("Pair must be like EURUSD or EUR/USD")
    return raw


async def maybe_serverless_tick() -> None:
    if settings.serverless_mode:
        await engine.step()


app = FastAPI(title=settings.app_name, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

base_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(base_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")

db = Database(settings.db_path)
provider = YahooForexProvider()
ws_manager = LiveConnectionManager()
engine = SignalEngine(settings=settings, db=db, provider=provider, ws_manager=ws_manager)


@app.on_event("startup")
async def on_startup() -> None:
    db.init()
    if not settings.serverless_mode:
        engine.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if not settings.serverless_mode:
        await engine.stop()
    await provider.close()
    db.close()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "pairs": settings.pairs,
            "polling_seconds": settings.polling_seconds,
            "serverless_mode": settings.serverless_mode,
        },
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "serverless" if settings.serverless_mode else "worker"}


@app.get("/api/pairs")
async def pairs() -> dict[str, list[str]]:
    return {"pairs": list(settings.pairs)}


@app.get("/api/strategies")
async def list_strategies() -> dict[str, object]:
    return {
        "selected_strategy": db.get_selected_strategy(),
        "strategies": db.list_strategies(),
    }


@app.patch("/api/strategies/{strategy_code}")
async def toggle_strategy(strategy_code: str, payload: ToggleStrategyRequest) -> dict[str, object]:
    changed = db.set_strategy_enabled(strategy_code, payload.enabled)
    if not changed:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await engine.publish_dashboard()
    return {"ok": True, "strategy_code": strategy_code, "enabled": payload.enabled}


@app.post("/api/strategies/select")
async def select_strategy(payload: SelectStrategyRequest) -> dict[str, object]:
    code = payload.strategy_code
    if code:
        row = db.get_strategy(code)
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        if not row["enabled"]:
            raise HTTPException(status_code=400, detail="Strategy is disabled")
    db.set_selected_strategy(code)
    await engine.publish_dashboard()
    return {"ok": True, "selected_strategy": code}


@app.get("/api/dashboard")
async def dashboard() -> dict[str, object]:
    await maybe_serverless_tick()
    ranking = db.get_strategy_ranking()
    selected = db.get_selected_strategy()
    prices = engine.snapshot_prices()
    return {
        "generated_at": utc_now().isoformat(),
        "prices": prices,
        "ranking": ranking,
        "selected_strategy": selected,
        "open_signals": db.list_signals(limit=40, status="open"),
        "recent_signals": db.list_signals(limit=40),
    }


@app.get("/api/signals")
async def signals(status: str | None = None, limit: int = 80) -> dict[str, object]:
    await maybe_serverless_tick()
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1..500")
    valid_status = {"open", "settled", "suppressed", "expired", None}
    if status not in valid_status:
        raise HTTPException(status_code=400, detail="invalid status")
    return {"signals": db.list_signals(limit=limit, status=status)}


@app.post("/api/tradingview/webhook")
async def tradingview_webhook(payload: TradingViewWebhookPayload) -> dict[str, object]:
    try:
        pair = normalize_pair(payload.pair)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = utc_now()
    entry_price = payload.entry_price
    if entry_price is None:
        await maybe_serverless_tick()
        entry_price = engine.latest_price(pair)
    if entry_price is None:
        raise HTTPException(
            status_code=422,
            detail=f"No current market price for {pair}. Wait for next refresh or send entry_price.",
        )

    expiry = now + timedelta(minutes=max(1, payload.expiry_minutes))
    signal_id = db.create_signal(
        source="tradingview",
        strategy_code=payload.strategy_code,
        pair=pair,
        timeframe=payload.timeframe,
        direction=payload.direction,
        entry_price=float(entry_price),
        entry_time=now.isoformat(),
        expiry_time=expiry.isoformat(),
        confidence=float(max(0.0, min(payload.confidence, 1.0))),
        reason=payload.reason,
        status="open",
        metadata={"source": "tradingview_webhook"},
    )
    await engine.dispatch_external_signal(
        {
            "id": signal_id,
            "source": "tradingview",
            "strategy_code": payload.strategy_code,
            "pair": pair,
            "timeframe": payload.timeframe,
            "direction": payload.direction,
            "entry_price": float(entry_price),
            "entry_time": now.isoformat(),
            "expiry_time": expiry.isoformat(),
            "confidence": float(max(0.0, min(payload.confidence, 1.0))),
            "reason": payload.reason,
        }
    )
    await engine.publish_dashboard()
    return {"ok": True, "signal_id": signal_id}


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    if settings.serverless_mode:
        await websocket.accept()
        await websocket.send_json({"event": "disabled", "message": "WebSocket disabled in serverless mode"})
        await websocket.close(code=1001)
        return
    await ws_manager.connect(websocket)
    try:
        await websocket.send_json({"event": "connected", "message": "live stream connected"})
        await engine.publish_dashboard()
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:
        await ws_manager.disconnect(websocket)
