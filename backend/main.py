"""FastAPI entrypoint.

Routes:
    GET    /api/health                — liveness probe
    GET    /api/state                 — current portfolio snapshot
    GET    /api/stats                 — win-rate, ROI, Sharpe, etc.
    GET    /api/bankroll/history      — chart data (last N hours)
    GET    /api/trades                — recent trades (paged)
    GET    /api/positions             — open positions
    GET    /api/alerts                — recent alerts feed
    GET    /api/config                — runtime config (read)
    POST   /api/config                — runtime config (mutate kelly / risk)
    POST   /api/mode                  — toggle paper ↔ live (with confirmation)
    POST   /api/pause                 — pause / resume trading
    POST   /api/scan                  — force an immediate scan
    WS     /ws                        — live dashboard heartbeat
"""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel, Field

from .config import STAGES, get_active_stage, get_settings
from .db import (
    AlertLevel,
    TradeMode,
    get_kv,
    init_db,
    set_kv,
)
from .portfolio import (
    bankroll_history,
    get_state,
    open_positions,
    performance_stats,
    push_alert,
    recent_alerts,
    recent_trades,
    snapshot_bankroll,
)
from .scheduler import build_scheduler, poll_wallet, register_broadcaster, scan_and_trade
from .wallet import get_wallet_state


# ============================================================
# WEBSOCKET MANAGER
# ============================================================


class WSManager:
    """Tracks connected clients and broadcasts JSON payloads to all of them."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        message = json.dumps(payload, default=str)
        dead: list[WebSocket] = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


ws_manager = WSManager()


# ============================================================
# LIFESPAN
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Prime the wallet cache BEFORE the first snapshot so that snapshot's
    # bankroll reflects the live wallet balance (in auto / live modes).
    await poll_wallet()

    snapshot_bankroll()
    push_alert(AlertLevel.INFO, "Bot started",
               f"Configured mode: {get_settings().trading_mode.upper()}")

    register_broadcaster(ws_manager.broadcast)

    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler

    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    logger.info("FastAPI + scheduler running")
    try:
        yield
    finally:
        heartbeat_task.cancel()
        scheduler.shutdown(wait=False)
        push_alert(AlertLevel.INFO, "Bot stopped", "Graceful shutdown")


async def _heartbeat_loop() -> None:
    """Push a state snapshot to all websocket clients every N seconds."""
    interval = get_settings().websocket_heartbeat_seconds
    while True:
        try:
            state = get_state()
            await ws_manager.broadcast({
                "event": "heartbeat",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "state": _serialize_state(state),
            })
        except Exception as e:
            logger.debug(f"Heartbeat error: {e}")
        await asyncio.sleep(interval)


# ============================================================
# APP
# ============================================================


settings = get_settings()
app = FastAPI(
    title="Polymarket Viral Trading Bot",
    version="0.1.0",
    description="Edge-driven Kelly Criterion bot — paper + live modes",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# SERIALIZERS
# ============================================================


def _serialize_state(state) -> dict:
    return {
        "bankroll": round(state.bankroll, 4),
        "cash": round(state.cash, 4),
        "open_position_value": round(state.open_position_value, 4),
        "open_position_count": state.open_position_count,
        "realized_pnl": round(state.realized_pnl, 4),
        "unrealized_pnl": round(state.unrealized_pnl, 4),
        "peak_bankroll": round(state.peak_bankroll, 4),
        "drawdown_pct": round(state.drawdown_pct, 4),
        "stage_name": state.stage_name,
        "mode": state.mode,
        "configured_mode": state.configured_mode,
        "circuit_breaker": state.circuit_breaker,
        "trading_paused": get_kv("trading_paused", "false") == "true",
        # Dual-bankroll tracking — both populated regardless of effective mode.
        "paper_bankroll": round(state.paper_bankroll, 4),
        "live_bankroll": round(state.live_bankroll, 4),
        # Wallet — driven by the cached pUSD/USDC.e/MATIC balances, refreshed every 30s.
        # `pusd` is what auto-mode tests against; `usdce > 0` means the user
        # has un-wrapped funds and should run scripts/wrap_usdc.py.
        "wallet": {
            "available": state.wallet_available,
            "address": state.wallet_address,
            "pusd": round(state.wallet_pusd, 4),
            "usdce": round(state.wallet_usdce, 4),
            "matic": round(state.wallet_matic, 6),
            "needs_wrap": state.wallet_needs_wrap,
            "error": state.wallet_error,
        },
        "auto_threshold": state.auto_threshold,
    }


def _serialize_stats(stats) -> dict:
    return {
        "total_trades": stats.total_trades,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": round(stats.win_rate, 4),
        "avg_edge": round(stats.avg_edge, 4),
        "total_pnl": round(stats.total_pnl, 4),
        "roi": round(stats.roi, 4),
        "sharpe": round(stats.sharpe, 4),
        "biggest_win": round(stats.biggest_win, 4),
        "biggest_loss": round(stats.biggest_loss, 4),
    }


def _serialize_trade(t) -> dict:
    return {
        "id": t.id,
        "mode": t.mode.value,
        "status": t.status.value,
        "market_id": t.market_id,
        "market_question": t.market_question,
        "side": t.side.value,
        "entry_price": t.entry_price,
        "fill_price": t.fill_price,
        "size_shares": t.size_shares,
        "size_usd": t.size_usd,
        "model_probability": t.model_probability,
        "edge": t.edge,
        "confidence": t.confidence,
        "kelly_fraction_used": t.kelly_fraction_used,
        "stage_name": t.stage_name,
        "pnl_usd": t.pnl_usd,
        "pnl_pct": t.pnl_pct,
        "order_id": t.order_id,
        "tx_hash": t.tx_hash,
        "error": t.error,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "filled_at": t.filled_at.isoformat() if t.filled_at else None,
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
    }


def _serialize_position(p) -> dict:
    return {
        "id": p.id,
        "trade_id": p.trade_id,
        "market_id": p.market_id,
        "side": p.side.value,
        "size_shares": p.size_shares,
        "avg_price": p.avg_price,
        "current_price": p.current_price,
        "cost_basis_usd": p.cost_basis_usd,
        "unrealized_pnl_usd": p.unrealized_pnl_usd,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
    }


def _serialize_alert(a) -> dict:
    return {
        "id": a.id,
        "level": a.level.value,
        "title": a.title,
        "message": a.message,
        "market_id": a.market_id,
        "timestamp": a.timestamp.isoformat() if a.timestamp else None,
    }


def _json_safe_number(v):
    """Coerce inf/-inf/NaN to None so vanilla json.dumps doesn't choke."""
    if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
        return None
    return v


def _serialize_stage(stage: dict) -> dict:
    """JSON-safe view of a STAGES entry (ceiling may be float('inf'))."""
    return {k: _json_safe_number(v) for k, v in stage.items()}


# ============================================================
# REST ROUTES
# ============================================================


@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": get_settings().trading_mode}


@app.get("/api/state")
async def state():
    return _serialize_state(get_state())


@app.get("/api/stats")
async def stats():
    return _serialize_stats(performance_stats())


@app.get("/api/bankroll/history")
async def bankroll_history_endpoint(hours: int = 168):
    snaps = bankroll_history(hours=hours)
    return [
        {
            "timestamp": s.timestamp.isoformat(),
            "bankroll": s.bankroll,
            "cash": s.cash,
            "realized_pnl": s.realized_pnl,
            "unrealized_pnl": s.unrealized_pnl,
            "stage": s.stage_name,
            "mode": s.mode.value,
        }
        for s in snaps
    ]


@app.get("/api/trades")
async def trades_endpoint(limit: int = 100, mode: Optional[str] = None):
    m = TradeMode(mode) if mode else None
    return [_serialize_trade(t) for t in recent_trades(limit=limit, mode=m)]


@app.get("/api/positions")
async def positions_endpoint():
    return [_serialize_position(p) for p in open_positions()]


@app.get("/api/alerts")
async def alerts_endpoint(limit: int = 50):
    return [_serialize_alert(a) for a in recent_alerts(limit=limit)]


@app.get("/api/config")
async def config_endpoint():
    s = get_settings()
    bankroll = get_state().bankroll
    return {
        "trading_mode": s.trading_mode,
        "initial_bankroll": s.initial_bankroll,
        "kelly_fraction": float(get_kv("kelly_fraction_override", str(s.kelly_fraction))),
        "min_edge_threshold": float(get_kv("min_edge_override", str(s.min_edge_threshold))),
        "min_confidence": s.min_confidence,
        "max_concurrent_bets": s.max_concurrent_bets,
        "max_position_fraction": s.max_position_fraction,
        "drawdown_circuit_breaker": s.drawdown_circuit_breaker,
        "scan_interval_seconds": s.scan_interval_seconds,
        "stages": [_serialize_stage(st) for st in STAGES],
        "active_stage": _serialize_stage(get_active_stage(bankroll)),
        "live_credentials_present": s.has_live_credentials,
    }


class ConfigUpdate(BaseModel):
    kelly_fraction: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    min_edge_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)


@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    if body.kelly_fraction is not None:
        set_kv("kelly_fraction_override", str(body.kelly_fraction))
        push_alert(AlertLevel.INFO, "Kelly fraction updated",
                   f"Now {body.kelly_fraction:.2f}")
    if body.min_edge_threshold is not None:
        set_kv("min_edge_override", str(body.min_edge_threshold))
        push_alert(AlertLevel.INFO, "Min edge updated",
                   f"Now {body.min_edge_threshold:.2%}")
    return {"ok": True}


class ModeToggle(BaseModel):
    mode: str = Field(pattern="^(paper|live|auto)$")
    confirm: bool = False


@app.post("/api/mode")
async def toggle_mode(body: ModeToggle):
    s = get_settings()
    if body.mode in ("live", "auto"):
        if not s.has_live_credentials:
            raise HTTPException(
                400,
                "Live credentials missing in .env (POLYMARKET_API_KEY, "
                "POLYMARKET_SECRET, POLYMARKET_PASSPHRASE, PRIVATE_KEY).",
            )
    if body.mode == "live" and not body.confirm:
        raise HTTPException(400, "Always-live mode requires explicit confirmation")
    set_kv("trading_mode_override", body.mode)
    # NOTE: an actual mode switch at runtime requires a process restart so the
    # cached settings singleton picks up the new env. We log the intent here
    # and the dashboard prompts the user to restart.
    push_alert(
        AlertLevel.WARNING,
        f"Mode change requested → {body.mode.upper()}",
        "Restart the backend for the new mode to take effect.",
    )
    return {"ok": True, "restart_required": True, "requested_mode": body.mode}


class PauseToggle(BaseModel):
    paused: bool


@app.post("/api/pause")
async def pause_endpoint(body: PauseToggle):
    set_kv("trading_paused", "true" if body.paused else "false")
    push_alert(
        AlertLevel.WARNING if body.paused else AlertLevel.SUCCESS,
        "Trading paused" if body.paused else "Trading resumed",
        "Manual override via dashboard",
    )
    return {"ok": True, "paused": body.paused}


@app.post("/api/scan")
async def force_scan():
    asyncio.create_task(scan_and_trade())
    return {"ok": True, "message": "Scan triggered"}


@app.get("/api/wallet")
async def wallet_endpoint(force: bool = False):
    """Read the cached wallet state (or force a fresh on-chain query).

    `pusd` is the trade-relevant balance (auto-mode threshold checks this).
    `usdce` > 0 means the user funded with bridged USDC but hasn't wrapped
    it into pUSD yet — they should run scripts/wrap_usdc.py.
    """
    w = await get_wallet_state(force=force)
    return {
        "available": w.available,
        "address": w.address,
        "pusd": round(w.pusd_balance, 4),
        "usdce": round(w.usdce_balance, 4),
        "matic": round(w.matic_balance, 6),
        "needs_wrap": w.needs_wrap,
        "last_updated": w.last_updated,
        "error": w.error,
    }


# ============================================================
# WEBSOCKET
# ============================================================


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        await ws.send_text(json.dumps({
            "event": "hello",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "state": _serialize_state(get_state()),
        }, default=str))
        while True:
            await ws.receive_text()  # we don't expect client→server messages
    except WebSocketDisconnect:
        await ws_manager.disconnect(ws)
    except Exception as e:
        logger.debug(f"WS error: {e}")
        await ws_manager.disconnect(ws)
