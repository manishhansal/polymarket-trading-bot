"""APScheduler job graph.

Three jobs:
    1. scan_and_trade   (every SCAN_INTERVAL_SECONDS) — find + execute opportunities
    2. mark_to_market   (every 30s)                   — refresh open-position prices
    3. snapshot         (every 60s)                   — write a bankroll point

The scheduler also broadcasts WebSocket updates to the dashboard after every
job so the UI feels real-time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from .config import get_settings
from .db import AlertLevel, TradeMode, TradeStatus, get_kv, set_kv
from .executor import close_position, execute, mark_to_market
from .kelly import size_position
from .portfolio import (
    effective_mode,
    get_state,
    open_position_count,
    open_positions,
    positions_breaching_stop_loss,
    push_alert,
    snapshot_bankroll,
    trading_allowed,
)
from .scanner import scan
from .wallet import refresh_balance


def _oracle_warning_emitted() -> bool:
    return get_kv("oracle_warning_emitted", "false") == "true"


def _set_oracle_warning_emitted() -> None:
    set_kv("oracle_warning_emitted", "true")


def _clear_oracle_warning_emitted() -> None:
    set_kv("oracle_warning_emitted", "false")


# A callable the API layer registers so that scheduler jobs can broadcast
# updates over the WebSocket without importing FastAPI here (no circular dep).
_broadcaster: Optional[Callable[[dict], asyncio.Future]] = None


def register_broadcaster(fn: Callable[[dict], asyncio.Future]) -> None:
    global _broadcaster
    _broadcaster = fn


def _broadcast(payload: dict) -> None:
    if _broadcaster is None:
        return
    try:
        asyncio.ensure_future(_broadcaster(payload))
    except Exception as e:
        logger.debug(f"Broadcast failed: {e}")


# ============================================================
# JOBS
# ============================================================


async def scan_and_trade() -> None:
    """Find opportunities, size them, route to paper/live executor."""
    allowed, reason = trading_allowed()
    if not allowed:
        logger.warning(f"Trading halted: {reason}")
        push_alert(AlertLevel.WARNING, "Trading halted", reason or "Unknown reason")
        _broadcast({"event": "trading_halted", "reason": reason})
        return

    try:
        result = await scan()
    except Exception as e:
        logger.exception(f"Scanner failure: {e}")
        push_alert(AlertLevel.ERROR, "Scanner error", str(e))
        return

    opportunities = result.opportunities

    # Health check: if EVERY evaluated market returned no-oracle, surface a
    # one-shot alert so the user knows WHY trades aren't happening. Without
    # external probabilities the bot will (correctly) refuse to trade — see
    # edge_calc invariant #3 in CONTEXT.md.
    #
    # CRITICAL: this must be based on `evaluated_count` vs `oracleless_count`,
    # NOT on the qualified opportunity list. No-oracle markets get edge=0
    # and are filtered out before reaching `opportunities`, so the old
    # `if opportunities:` guard meant this alert never fired in the all-down
    # case — exactly when the user needs it most. Bug caught 2026-05-30.
    if result.evaluated_count > 0:
        if result.oracleless_count == result.evaluated_count:
            if not _oracle_warning_emitted():
                _set_oracle_warning_emitted()
                push_alert(
                    AlertLevel.WARNING,
                    "All oracles unavailable",
                    f"Evaluated {result.evaluated_count} markets — none had a "
                    "matching Kalshi or Metaculus probability. The bot will not "
                    "trade until at least one oracle returns a match. Set "
                    "METACULUS_TOKEN in .env to unlock the largest source.",
                )
        elif _oracle_warning_emitted():
            _clear_oracle_warning_emitted()
            push_alert(AlertLevel.SUCCESS, "Oracles recovered",
                       "At least one probability source is responding again.")

    state = get_state()
    _broadcast({"event": "scan_complete",
                "opportunities": len(opportunities),
                "evaluated": result.evaluated_count,
                "oracleless": result.oracleless_count,
                "bankroll": state.bankroll})

    for opp in opportunities:
        if open_position_count() >= get_settings().max_concurrent_bets:
            break

        sizing = size_position(
            bankroll=get_state().bankroll,
            model_prob=opp.edge.model_probability,
            market_price=opp.market.yes_price if opp.edge.side == "YES" else opp.market.no_price,
            confidence=opp.edge.confidence,
            edge=opp.edge.abs_edge,
            open_position_count=open_position_count(),
        )

        if not sizing.should_trade:
            logger.debug(f"Skip {opp.market.market_id}: {sizing.reject_reason}")
            continue

        push_alert(
            AlertLevel.OPPORTUNITY,
            f"Mispriced market: {opp.edge.abs_edge:+.1%} edge",
            f"{opp.market.question[:80]} — model {opp.edge.model_probability:.2%} "
            f"vs market {opp.market.yes_price:.2%}",
            market_id=opp.market.market_id,
        )

        result = await execute(opp, sizing)
        _broadcast({
            "event": "trade_attempt",
            "success": result.success,
            "message": result.message,
            "market": opp.market.question,
            "edge": opp.edge.edge,
            "stake": sizing.stake_usd,
        })

        if result.success:
            push_alert(
                AlertLevel.SUCCESS,
                "Trade filled",
                f"{opp.edge.side} ${sizing.stake_usd:.2f} @ ${result.fill_price:.4f}",
                market_id=opp.market.market_id,
            )
        else:
            push_alert(AlertLevel.WARNING, "Trade rejected", result.message,
                       market_id=opp.market.market_id)


async def refresh_marks() -> None:
    """Refresh current market prices for open positions and enforce stop-loss."""
    positions = open_positions()
    if not positions:
        return

    token_ids = {p.token_id for p in positions}
    s = get_settings()
    prices: dict[str, float] = {}

    async with httpx.AsyncClient(timeout=8.0) as client:
        for tid in token_ids:
            try:
                r = await client.get(f"{s.clob_host}/price",
                                     params={"token_id": tid, "side": "buy"})
                if r.status_code == 200:
                    data = r.json()
                    px = data.get("price")
                    if px is not None:
                        prices[tid] = float(px)
            except Exception as e:
                logger.debug(f"Price fetch failed for {tid[:10]}: {e}")

    if prices:
        await mark_to_market(prices)

    # Enforce stop-loss: forcibly close any position that breached.
    for pos in positions_breaching_stop_loss():
        exit_price = prices.get(pos.token_id, pos.current_price)
        pnl = await close_position(pos.id, exit_price, status=TradeStatus.STOPPED_OUT)
        push_alert(
            AlertLevel.WARNING,
            "Stop-loss triggered",
            f"Position {pos.id} closed at ${exit_price:.4f} (PnL ${pnl:.2f})",
            market_id=pos.market_id,
        )
        _broadcast({"event": "stop_loss", "position_id": pos.id, "pnl": pnl})


async def write_snapshot() -> None:
    snap = snapshot_bankroll()
    _broadcast({
        "event": "bankroll_snapshot",
        "bankroll": snap.bankroll,
        "timestamp": snap.timestamp.isoformat(),
        "mode": snap.mode.value,
        "stage": snap.stage_name,
    })


async def poll_wallet() -> None:
    """Refresh the on-chain wallet balance and fire mode-transition alerts.

    In auto-mode, crossing the AUTO_MODE_MIN_BALANCE_USD threshold flips the
    bot between paper and live. We surface these transitions clearly so the
    user always knows whether real money is about to move.
    """
    settings = get_settings()
    wallet = await refresh_balance()

    if not wallet.available:
        # Don't spam alerts on unavailability — it's the default for users
        # who haven't set PRIVATE_KEY. Only log once at start.
        if get_kv("wallet_warned_unavailable", "false") != "true":
            set_kv("wallet_warned_unavailable", "true")
            if settings.is_auto:
                push_alert(
                    AlertLevel.INFO,
                    "Wallet not configured",
                    (wallet.error or "PRIVATE_KEY missing")
                    + " — auto-mode will stay in PAPER until a wallet is set up.",
                )
        return

    # Wallet recovered after being unavailable.
    if get_kv("wallet_warned_unavailable", "false") == "true":
        set_kv("wallet_warned_unavailable", "false")

    if settings.is_auto:
        new_mode = effective_mode(wallet)
        last_mode = get_kv("last_effective_mode", TradeMode.PAPER.value)
        if new_mode.value != last_mode:
            set_kv("last_effective_mode", new_mode.value)
            if new_mode == TradeMode.LIVE:
                push_alert(
                    AlertLevel.SUCCESS,
                    f"AUTO → LIVE (wallet ${wallet.pusd_balance:.2f} pUSD)",
                    f"Wallet pUSD crossed ${settings.auto_mode_min_balance_usd:.2f}. "
                    "Real-money trading is now active.",
                )
            else:
                push_alert(
                    AlertLevel.WARNING,
                    f"AUTO → PAPER (wallet ${wallet.pusd_balance:.2f} pUSD)",
                    f"Wallet pUSD fell below ${settings.auto_mode_min_balance_usd:.2f}. "
                    "Bot reverted to paper trading — fund the wallet to resume live.",
                )

    # One-shot hint when the user has USDC.e but hasn't wrapped it yet.
    if wallet.needs_wrap and get_kv("wallet_needs_wrap_warned", "false") != "true":
        set_kv("wallet_needs_wrap_warned", "true")
        push_alert(
            AlertLevel.WARNING,
            "USDC.e detected — needs wrapping",
            f"Wallet holds ${wallet.usdce_balance:.2f} USDC.e but Polymarket "
            "settles in pUSD. Run `python scripts/wrap_usdc.py` to wrap, or "
            "visit polymarket.com once with this wallet (the UI wraps automatically).",
        )
    elif not wallet.needs_wrap and get_kv("wallet_needs_wrap_warned", "false") == "true":
        set_kv("wallet_needs_wrap_warned", "false")

    _broadcast({
        "event": "wallet_update",
        "address": wallet.address,
        "pusd": wallet.pusd_balance,
        "usdce": wallet.usdce_balance,
        "matic": wallet.matic_balance,
        "needs_wrap": wallet.needs_wrap,
        "available": wallet.available,
    })


# ============================================================
# SCHEDULER FACTORY
# ============================================================


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    sched = AsyncIOScheduler(timezone="UTC")

    # next_run_time=now ensures the first scan fires immediately on boot
    # instead of waiting a full scan_interval. Crucially, do NOT pass
    # next_run_time=None — that flag puts the job in a paused state and the
    # scheduler will silently never fire it (caught 2026-05-30).
    sched.add_job(scan_and_trade, "interval",
                  seconds=s.scan_interval_seconds,
                  id="scan_and_trade", max_instances=1, coalesce=True,
                  next_run_time=datetime.now(timezone.utc))
    sched.add_job(refresh_marks, "interval",
                  seconds=30,
                  id="refresh_marks", max_instances=1, coalesce=True)
    sched.add_job(write_snapshot, "interval",
                  seconds=60,
                  id="write_snapshot", max_instances=1, coalesce=True)
    sched.add_job(poll_wallet, "interval",
                  seconds=30,
                  id="poll_wallet", max_instances=1, coalesce=True)

    return sched
