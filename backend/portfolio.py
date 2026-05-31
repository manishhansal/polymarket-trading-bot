"""Portfolio bookkeeper: bankroll, positions, PnL, risk gates.

This is the single source of truth for:
    * Current cash balance (paper or live)
    * Open positions (count, exposure)
    * Realized + unrealized PnL
    * Peak bankroll / drawdown
    * Circuit-breaker state
    * Performance stats (win rate, Sharpe, average edge, ROI)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlmodel import select

from .config import get_active_stage, get_settings
from .db import (
    Alert,
    AlertLevel,
    BankrollSnapshot,
    Position,
    StageEvent,
    Trade,
    TradeMode,
    TradeStatus,
    get_kv,
    latest_bankroll,
    peak_bankroll,
    session_scope,
    set_kv,
)
from .kelly import circuit_breaker_tripped, drawdown_pct
from .wallet import WalletState, cached_wallet_state


# ============================================================
# DATACLASSES
# ============================================================


@dataclass
class PortfolioState:
    bankroll: float                  # bankroll for the CURRENTLY effective mode
    cash: float
    open_position_value: float
    open_position_count: int
    realized_pnl: float
    unrealized_pnl: float
    peak_bankroll: float
    drawdown_pct: float
    stage_name: str
    mode: str                        # effective mode ("paper" | "live")
    configured_mode: str             # what TRADING_MODE was set to ("auto" | …)
    circuit_breaker: bool
    paper_bankroll: float            # always populated (the paper ledger)
    live_bankroll: float             # 0.0 when wallet unavailable
    wallet_address: Optional[str]    # None when no PRIVATE_KEY
    wallet_pusd: float               # Polymarket USD — the tradeable balance
    wallet_usdce: float              # USDC.e — unwrapped (needs CollateralOnramp)
    wallet_matic: float
    wallet_available: bool
    wallet_needs_wrap: bool          # True iff usdce > 0 → "wrap me" hint
    wallet_error: Optional[str]
    auto_threshold: float            # AUTO_MODE_MIN_BALANCE_USD


@dataclass
class PerformanceStats:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_edge: float
    total_pnl: float
    roi: float
    sharpe: float
    biggest_win: float
    biggest_loss: float


# ============================================================
# BANKROLL
# ============================================================


def effective_mode(wallet: Optional[WalletState] = None) -> TradeMode:
    """Resolve which TradeMode the NEXT trade should use.

    - configured "paper" → always PAPER (manual override)
    - configured "live"  → always LIVE  (manual override)
    - configured "auto"  → LIVE if wallet has at least the auto threshold,
                          PAPER otherwise (or when wallet is unreachable)
    """
    s = get_settings()
    if s.trading_mode == "paper":
        return TradeMode.PAPER
    if s.trading_mode == "live":
        return TradeMode.LIVE
    # auto — Polymarket settles in pUSD (post-Apr-2026 migration), so the
    # threshold check looks at pUSD, NOT raw USDC.e. Funds sitting in USDC.e
    # need to be wrapped via scripts/wrap_usdc.py before they count.
    if wallet is None:
        wallet = cached_wallet_state()
    if wallet.available and wallet.pusd_balance >= s.auto_mode_min_balance_usd:
        return TradeMode.LIVE
    return TradeMode.PAPER


def _bankroll_for_mode(mode: TradeMode, wallet: WalletState,
                       open_positions: list[Position],
                       all_trades_in_mode: list[Trade]) -> tuple[float, float, float, float, float]:
    """Compute (bankroll, cash, open_value, realized_pnl, unrealized_pnl) for a mode.

    - PAPER: synthetic ledger starting from settings.initial_bankroll plus the
      sum of paper-trade PnL. Positions opened in PAPER count here.
    - LIVE:  starts from the LIVE wallet's USDC balance plus locked cost-basis
      of any open LIVE positions (so swapping cash → shares doesn't appear as
      a loss). Open-position value uses the mark-to-market price.
    """
    settings = get_settings()

    # Only consider positions opened in THIS mode for the bankroll calc.
    mode_positions = [p for p in open_positions
                      if _position_mode(p, all_trades_in_mode) == mode]

    realized_pnl = sum(t.pnl_usd for t in all_trades_in_mode
                       if t.status in {TradeStatus.RESOLVED_WIN,
                                       TradeStatus.RESOLVED_LOSS,
                                       TradeStatus.STOPPED_OUT})
    unrealized_pnl = sum(p.unrealized_pnl_usd for p in mode_positions)
    open_value = sum(p.current_price * p.size_shares for p in mode_positions)
    cost_basis = sum(p.cost_basis_usd for p in mode_positions)

    if mode == TradeMode.LIVE:
        # The wallet shows free cash (pUSD not locked in shares). Re-add the
        # cost basis so bankroll = wallet_cash + position_value reflects
        # *total* exposure rather than penalising us for having open trades.
        cash = wallet.pusd_balance
        bankroll = cash + open_value
    else:
        cash = settings.initial_bankroll + realized_pnl - cost_basis
        bankroll = cash + open_value

    return bankroll, cash, open_value, realized_pnl, unrealized_pnl


def _position_mode(pos: Position, mode_trades: list[Trade]) -> Optional[TradeMode]:
    """Reverse-lookup the TradeMode that opened this Position."""
    for t in mode_trades:
        if t.id == pos.trade_id:
            return t.mode
    return None


def get_state() -> PortfolioState:
    """Snapshot of the current portfolio for the currently effective mode."""
    settings = get_settings()
    wallet = cached_wallet_state()
    eff = effective_mode(wallet)

    with session_scope() as s:
        paper_trades = s.exec(select(Trade).where(Trade.mode == TradeMode.PAPER)).all()
        live_trades = s.exec(select(Trade).where(Trade.mode == TradeMode.LIVE)).all()
        open_positions = s.exec(select(Position)).all()

    paper_bankroll, *_ = _bankroll_for_mode(
        TradeMode.PAPER, wallet, open_positions, paper_trades,
    )
    live_bankroll, *_ = _bankroll_for_mode(
        TradeMode.LIVE, wallet, open_positions, live_trades,
    )

    # The active bankroll = whichever mode is currently effective.
    if eff == TradeMode.LIVE:
        bankroll, cash, open_value, realized, unrealized = _bankroll_for_mode(
            TradeMode.LIVE, wallet, open_positions, live_trades,
        )
        mode_positions = [p for p in open_positions
                          if _position_mode(p, live_trades) == TradeMode.LIVE]
    else:
        bankroll, cash, open_value, realized, unrealized = _bankroll_for_mode(
            TradeMode.PAPER, wallet, open_positions, paper_trades,
        )
        mode_positions = [p for p in open_positions
                          if _position_mode(p, paper_trades) == TradeMode.PAPER]

    peak = max(peak_bankroll(settings.initial_bankroll), bankroll)
    dd = drawdown_pct(peak, bankroll)
    stage = get_active_stage(bankroll)

    return PortfolioState(
        bankroll=bankroll,
        cash=cash,
        open_position_value=open_value,
        open_position_count=len(mode_positions),
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        peak_bankroll=peak,
        drawdown_pct=dd,
        stage_name=stage["name"],
        mode=eff.value,
        configured_mode=settings.trading_mode,
        circuit_breaker=circuit_breaker_tripped(peak, bankroll),
        paper_bankroll=paper_bankroll,
        live_bankroll=live_bankroll,
        wallet_address=wallet.address,
        wallet_pusd=wallet.pusd_balance,
        wallet_usdce=wallet.usdce_balance,
        wallet_matic=wallet.matic_balance,
        wallet_available=wallet.available,
        wallet_needs_wrap=wallet.needs_wrap,
        wallet_error=wallet.error,
        auto_threshold=settings.auto_mode_min_balance_usd,
    )


def snapshot_bankroll() -> BankrollSnapshot:
    """Persist a bankroll point for the dashboard chart.

    The snapshot uses the currently *effective* bankroll so the chart matches
    what the user sees in the header. Both paper and live bankrolls are
    preserved separately in their respective Trade rows, so historical data
    can always be reconstructed.
    """
    state = get_state()
    snap = BankrollSnapshot(
        bankroll=state.bankroll,
        cash=state.cash,
        open_position_value=state.open_position_value,
        realized_pnl=state.realized_pnl,
        unrealized_pnl=state.unrealized_pnl,
        mode=TradeMode(state.mode),
        stage_name=state.stage_name,
    )
    with session_scope() as s:
        s.add(snap)
        s.commit()
        s.refresh(snap)
    _maybe_log_stage_transition(state.bankroll)
    return snap


def bankroll_history(hours: int = 168) -> list[BankrollSnapshot]:
    """Return bankroll snapshots from the last `hours` (default 7 days)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with session_scope() as s:
        stmt = (
            select(BankrollSnapshot)
            .where(BankrollSnapshot.timestamp >= cutoff)
            .order_by(BankrollSnapshot.timestamp.asc())
        )
        return list(s.exec(stmt).all())


# ============================================================
# RISK GATES
# ============================================================


def trading_allowed() -> tuple[bool, Optional[str]]:
    """Top-level gate that the scheduler consults before opening any new trade."""
    if get_kv("trading_paused", "false") == "true":
        return False, "Trading manually paused from dashboard"

    state = get_state()
    if state.circuit_breaker:
        return False, (
            f"Circuit breaker tripped: drawdown {state.drawdown_pct:.1%} "
            f"≥ {get_settings().drawdown_circuit_breaker:.1%}"
        )
    if state.bankroll < get_settings().min_bet_size_usd:
        return False, f"Bankroll ${state.bankroll:.2f} below minimum bet size"
    return True, None


# ============================================================
# STAGE TRACKING
# ============================================================


def _maybe_log_stage_transition(bankroll: float) -> None:
    """Insert a StageEvent + Alert when the bankroll crosses into a new stage."""
    new_stage = get_active_stage(bankroll)["name"]
    last = get_kv("current_stage", "")
    if last == new_stage:
        return

    set_kv("current_stage", new_stage)
    with session_scope() as s:
        s.add(StageEvent(from_stage=last or "Init", to_stage=new_stage, bankroll=bankroll))
        s.add(Alert(
            level=AlertLevel.SUCCESS,
            title="Stage transition",
            message=f"{last or 'Init'} → {new_stage} at ${bankroll:.2f}",
        ))
        s.commit()
    logger.info(f"Stage transition: {last} → {new_stage} (bankroll=${bankroll:.2f})")


# ============================================================
# ALERTS
# ============================================================


def push_alert(level: AlertLevel, title: str, message: str,
               market_id: Optional[str] = None, payload_json: Optional[str] = None) -> Alert:
    a = Alert(level=level, title=title, message=message,
              market_id=market_id, payload_json=payload_json)
    with session_scope() as s:
        s.add(a)
        s.commit()
        s.refresh(a)
    return a


def recent_alerts(limit: int = 50) -> list[Alert]:
    with session_scope() as s:
        stmt = select(Alert).order_by(Alert.timestamp.desc()).limit(limit)
        return list(s.exec(stmt).all())


# ============================================================
# STOP-LOSS ENFORCEMENT
# ============================================================


def positions_breaching_stop_loss() -> list[Position]:
    """Return open positions whose unrealized loss exceeds the stop-loss fraction."""
    settings = get_settings()
    breached: list[Position] = []
    with session_scope() as s:
        for pos in s.exec(select(Position)).all():
            if pos.cost_basis_usd <= 0:
                continue
            loss_pct = -pos.unrealized_pnl_usd / pos.cost_basis_usd
            if loss_pct >= settings.stop_loss_fraction:
                breached.append(pos)
    return breached


# ============================================================
# PERFORMANCE STATS
# ============================================================


def performance_stats() -> PerformanceStats:
    """Compute headline performance metrics for the dashboard.

    Stats are scoped to the *currently effective* mode so the dashboard's
    headline numbers match what the bot is actually doing right now.
    """
    settings = get_settings()
    mode = effective_mode()

    with session_scope() as s:
        all_trades = s.exec(select(Trade).where(Trade.mode == mode)).all()

    resolved = [t for t in all_trades if t.status in {
        TradeStatus.RESOLVED_WIN, TradeStatus.RESOLVED_LOSS, TradeStatus.STOPPED_OUT
    }]

    wins = sum(1 for t in resolved if t.pnl_usd > 0)
    losses = sum(1 for t in resolved if t.pnl_usd <= 0)
    total_trades = len(resolved)
    win_rate = (wins / total_trades) if total_trades else 0.0
    avg_edge = sum(t.edge for t in all_trades) / len(all_trades) if all_trades else 0.0
    total_pnl = sum(t.pnl_usd for t in resolved)
    roi = (total_pnl / settings.initial_bankroll) if settings.initial_bankroll else 0.0

    # Sharpe: mean / stdev of per-trade returns (annualized assumption ignored
    # here — we report raw per-trade Sharpe which is the standard for bots).
    returns = [t.pnl_pct for t in resolved if t.pnl_pct is not None]
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / sd) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    biggest_win = max((t.pnl_usd for t in resolved), default=0.0)
    biggest_loss = min((t.pnl_usd for t in resolved), default=0.0)

    return PerformanceStats(
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_edge=avg_edge,
        total_pnl=total_pnl,
        roi=roi,
        sharpe=sharpe,
        biggest_win=biggest_win,
        biggest_loss=biggest_loss,
    )


def open_position_count() -> int:
    with session_scope() as s:
        return len(s.exec(select(Position)).all())


def open_positions() -> list[Position]:
    with session_scope() as s:
        return list(s.exec(select(Position)).all())


def recent_trades(limit: int = 100, mode: Optional[TradeMode] = None) -> list[Trade]:
    with session_scope() as s:
        stmt = select(Trade)
        if mode is not None:
            stmt = stmt.where(Trade.mode == mode)
        stmt = stmt.order_by(Trade.created_at.desc()).limit(limit)
        return list(s.exec(stmt).all())
