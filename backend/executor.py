"""Trade executor — paper + live, routed per-trade.

Routing is decided by `portfolio.effective_mode()` at the moment of execution,
not at process start. This means:

  * `TRADING_MODE=paper` → every trade goes through the paper simulator
  * `TRADING_MODE=live`  → every trade goes through the live CLOB
  * `TRADING_MODE=auto`  → wallet USDC ≥ AUTO_MODE_MIN_BALANCE_USD goes live,
                          otherwise paper-trades into a separate ledger.

PAPER MODE:
    * Simulated fills at mid-price (with synthetic adverse slippage)
    * Persists Trade(mode=PAPER) + Position rows to SQLite
    * Zero network calls, zero risk

LIVE MODE:
    * Lazily constructs a `py_clob_client.ClobClient`
    * Posts limit orders, polls fills, cancels stale orders
    * Slippage guard: aborts if fill price deviates more than 2% from expected
    * Persists Trade(mode=LIVE) — these are the rows that touch real money
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlmodel import select

from .config import get_settings
from .db import (
    Position,
    Trade,
    TradeMode,
    TradeSide,
    TradeStatus,
    session_scope,
)
from .kelly import KellySizing
from .scanner import MarketOpportunity


SLIPPAGE_TOLERANCE = 0.02       # 2% reject threshold for live fills
PAPER_SYNTH_SLIPPAGE = 0.0025   # 25 bps simulated slippage in paper mode


@dataclass
class ExecutionResult:
    success: bool
    trade_id: Optional[int]
    message: str
    fill_price: Optional[float] = None
    order_id: Optional[str] = None
    tx_hash: Optional[str] = None
    extra: dict = field(default_factory=dict)


# ============================================================
# LIVE CLIENT (lazy)
# ============================================================


_live_client = None


def _get_live_client():
    """Lazily build the py-clob-client-v2 ClobClient. Returns None if creds missing.

    Polymarket migrated to CLOB V2 in 2026 and the original `py-clob-client`
    package is archived/non-functional. The V2 init signature is the same but
    method names changed slightly (e.g. `create_or_derive_api_key` instead of
    `_creds`, and order placement takes structured `OrderArgs` + `OrderType`).
    """
    global _live_client
    if _live_client is not None:
        return _live_client

    s = get_settings()
    if not s.has_live_credentials:
        logger.warning("Live credentials missing — cannot build CLOB client")
        return None

    try:
        from py_clob_client_v2 import ApiCreds, ClobClient

        creds = ApiCreds(
            api_key=s.polymarket_api_key,
            api_secret=s.polymarket_secret,
            api_passphrase=s.polymarket_passphrase,
        )
        _live_client = ClobClient(
            host=s.clob_host,
            key=s.private_key,
            chain_id=s.chain_id,
            creds=creds,
        )
        logger.info("Polymarket CLOB V2 client initialized (LIVE)")
        return _live_client
    except Exception as e:
        logger.error(f"Failed to initialize CLOB V2 client: {e}")
        return None


def _estimate_gas() -> Optional[int]:
    """Best-effort gas estimate on Polygon — used for sanity checks only."""
    s = get_settings()
    if not s.private_key:
        return None
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(s.polygon_rpc_url))
        if not w3.is_connected():
            return None
        return w3.eth.gas_price
    except Exception as e:
        logger.debug(f"Gas estimation failed: {e}")
        return None


# ============================================================
# CORE EXECUTION
# ============================================================


def _persist_trade(
    *,
    mode: TradeMode,
    status: TradeStatus,
    opp: MarketOpportunity,
    sizing: KellySizing,
    side: TradeSide,
    fill_price: Optional[float],
    order_id: Optional[str] = None,
    tx_hash: Optional[str] = None,
    error: Optional[str] = None,
) -> int:
    """Insert a Trade row (and open Position if filled). Returns the trade id."""
    market = opp.market
    edge = opp.edge

    trade = Trade(
        mode=mode,
        status=status,
        market_id=market.market_id,
        market_question=market.question,
        token_id=market.yes_token_id if side == TradeSide.YES else market.no_token_id,
        side=side,
        entry_price=market.yes_price if side == TradeSide.YES else market.no_price,
        fill_price=fill_price,
        size_shares=sizing.shares,
        size_usd=sizing.stake_usd,
        model_probability=edge.model_probability,
        edge=edge.edge,
        confidence=edge.confidence,
        kelly_fraction_used=sizing.capped_fraction,
        stage_name=sizing.stage_name,
        order_id=order_id,
        tx_hash=tx_hash,
        error=error,
        filled_at=datetime.now(timezone.utc) if status == TradeStatus.FILLED else None,
    )

    with session_scope() as s:
        s.add(trade)
        s.commit()
        s.refresh(trade)

        if status == TradeStatus.FILLED and fill_price is not None:
            pos = Position(
                trade_id=trade.id,
                market_id=market.market_id,
                token_id=trade.token_id,
                side=side,
                size_shares=sizing.shares,
                avg_price=fill_price,
                cost_basis_usd=sizing.shares * fill_price,
                current_price=fill_price,
            )
            s.add(pos)
            s.commit()

        return trade.id


# ------------- PAPER -------------


async def _execute_paper(opp: MarketOpportunity, sizing: KellySizing) -> ExecutionResult:
    """Simulate a fill at mid-price ± synthetic slippage."""
    side = TradeSide(opp.edge.side)
    base_price = opp.market.yes_price if side == TradeSide.YES else opp.market.no_price
    # Always adversely slip the fill, even in simulation.
    fill_price = round(base_price * (1.0 + PAPER_SYNTH_SLIPPAGE), 6)
    fill_price = min(max(fill_price, 0.0001), 0.9999)

    tid = _persist_trade(
        mode=TradeMode.PAPER,
        status=TradeStatus.FILLED,
        opp=opp,
        sizing=sizing,
        side=side,
        fill_price=fill_price,
        order_id=f"PAPER-{int(datetime.now(timezone.utc).timestamp())}-{opp.market.market_id[:8]}",
    )

    logger.info(
        f"[PAPER FILL] {opp.market.question[:50]!r} {side.value} "
        f"{sizing.shares:.4f}sh @ ${fill_price:.4f} (edge={opp.edge.edge:+.2%})"
    )
    return ExecutionResult(
        success=True,
        trade_id=tid,
        message="Paper fill simulated at mid-price",
        fill_price=fill_price,
        order_id=f"PAPER-{tid}",
    )


# ------------- LIVE -------------


async def _execute_live(opp: MarketOpportunity, sizing: KellySizing) -> ExecutionResult:
    """Submit a real CLOB limit order and wait briefly for a fill."""
    s = get_settings()
    side = TradeSide(opp.edge.side)
    base_price = opp.market.yes_price if side == TradeSide.YES else opp.market.no_price

    client = _get_live_client()
    if client is None:
        msg = "Live mode requested but CLOB client unavailable"
        tid = _persist_trade(
            mode=TradeMode.LIVE, status=TradeStatus.REJECTED,
            opp=opp, sizing=sizing, side=side, fill_price=None, error=msg,
        )
        return ExecutionResult(False, tid, msg)

    gas = _estimate_gas()
    if gas is None:
        logger.warning("Gas estimation unavailable — proceeding cautiously")

    try:
        from py_clob_client_v2 import (
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        token_id = opp.market.yes_token_id if side == TradeSide.YES else opp.market.no_token_id
        order_args = OrderArgs(
            token_id=token_id,
            price=round(base_price, 4),
            size=round(sizing.shares, 4),
            side=Side.BUY,
        )

        # V2 requires explicit tick size + OrderType. GTC = resting limit order
        # that stays on the book until filled, cancelled, or the timeout below.
        signed = await asyncio.to_thread(
            client.create_and_post_order,
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.GTC,
        )
        order_id = signed.get("orderID") or signed.get("orderId") or signed.get("id")
        logger.info(f"[LIVE ORDER] posted {order_id} for {opp.market.market_id}")

        # Poll for fill up to the order timeout.
        deadline = datetime.now(timezone.utc) + timedelta(seconds=s.order_timeout_seconds)
        fill_price: Optional[float] = None
        while datetime.now(timezone.utc) < deadline:
            await asyncio.sleep(5)
            try:
                status = await asyncio.to_thread(client.get_order, order_id)
                if status and status.get("status") in {"MATCHED", "FILLED"}:
                    fill_price = float(status.get("price") or base_price)
                    break
            except Exception as poll_err:
                logger.debug(f"Order poll error: {poll_err}")

        if fill_price is None:
            try:
                await asyncio.to_thread(client.cancel_order, order_id)
            except Exception:
                pass
            msg = f"Order {order_id} timed out — cancelled"
            tid = _persist_trade(
                mode=TradeMode.LIVE, status=TradeStatus.CANCELLED,
                opp=opp, sizing=sizing, side=side, fill_price=None,
                order_id=order_id, error=msg,
            )
            return ExecutionResult(False, tid, msg, order_id=order_id)

        deviation = abs(fill_price - base_price) / base_price
        if deviation > SLIPPAGE_TOLERANCE:
            msg = (
                f"Slippage guard: fill ${fill_price:.4f} vs expected "
                f"${base_price:.4f} ({deviation:.2%}) > tolerance"
            )
            tid = _persist_trade(
                mode=TradeMode.LIVE, status=TradeStatus.REJECTED,
                opp=opp, sizing=sizing, side=side, fill_price=fill_price,
                order_id=order_id, error=msg,
            )
            return ExecutionResult(False, tid, msg, fill_price=fill_price, order_id=order_id)

        tid = _persist_trade(
            mode=TradeMode.LIVE, status=TradeStatus.FILLED,
            opp=opp, sizing=sizing, side=side, fill_price=fill_price,
            order_id=order_id,
        )
        logger.info(
            f"[LIVE FILL] {opp.market.question[:50]!r} {side.value} "
            f"{sizing.shares:.4f}sh @ ${fill_price:.4f}"
        )
        return ExecutionResult(True, tid, "Filled", fill_price=fill_price, order_id=order_id)

    except Exception as e:
        msg = f"Live execution error: {e}"
        logger.exception(msg)
        tid = _persist_trade(
            mode=TradeMode.LIVE, status=TradeStatus.REJECTED,
            opp=opp, sizing=sizing, side=side, fill_price=None, error=str(e),
        )
        return ExecutionResult(False, tid, msg)


# ------------- ENTRYPOINT -------------


async def execute(opp: MarketOpportunity, sizing: KellySizing) -> ExecutionResult:
    """Route to paper or live execution based on the currently effective mode."""
    if not sizing.should_trade:
        return ExecutionResult(False, None, sizing.reject_reason or "No trade")

    # Resolved per-trade so auto-mode can swap between paper and live as the
    # wallet balance crosses the threshold. Importing here to break the
    # portfolio ↔ executor import cycle (portfolio depends on db, executor
    # depends on portfolio for mode resolution).
    from .portfolio import effective_mode
    from .db import TradeMode

    if effective_mode() == TradeMode.LIVE:
        return await _execute_live(opp, sizing)
    return await _execute_paper(opp, sizing)


# ============================================================
# CLOSEOUT / RESOLUTION  (paper-mode price walking)
# ============================================================


async def mark_to_market(current_prices: dict[str, float]) -> None:
    """Update open positions' unrealized PnL given a map of token_id → price."""
    with session_scope() as s:
        rows = s.exec(select(Position)).all()
        for pos in rows:
            new_px = current_prices.get(pos.token_id)
            if new_px is None:
                continue
            pos.current_price = new_px
            pos.unrealized_pnl_usd = (new_px - pos.avg_price) * pos.size_shares
            s.add(pos)
        s.commit()


async def close_position(position_id: int, exit_price: float,
                         status: TradeStatus = TradeStatus.RESOLVED_WIN) -> Optional[float]:
    """Close an open position at `exit_price`. Returns realized PnL or None."""
    with session_scope() as s:
        pos = s.get(Position, position_id)
        if pos is None:
            return None

        proceeds = pos.size_shares * exit_price
        pnl = proceeds - pos.cost_basis_usd

        trade = s.get(Trade, pos.trade_id)
        if trade:
            trade.status = status
            trade.fill_price = trade.fill_price or pos.avg_price
            trade.pnl_usd = pnl
            trade.pnl_pct = (pnl / pos.cost_basis_usd) if pos.cost_basis_usd else 0.0
            trade.resolved_at = datetime.now(timezone.utc)
            s.add(trade)

        s.delete(pos)
        s.commit()
        return pnl
