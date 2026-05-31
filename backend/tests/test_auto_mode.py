"""Tests for the AUTO trading-mode logic.

Covers:
    - effective_mode() decision in every (configured_mode, wallet_state) combo
    - executor routing per-trade (paper vs live) based on effective_mode
    - wallet.py caching and unavailability handling
"""

from __future__ import annotations

import os
import time

import pytest

from backend import wallet as wallet_module
from backend.db import TradeMode


def _make_wallet(available=True, pusd=0.0, usdce=0.0, matic=0.5,
                 address="0xABC", error=None):
    """Build a test WalletState. `pusd` is the auto-mode threshold input."""
    return wallet_module.WalletState(
        available=available,
        address=address if available else None,
        pusd_balance=pusd,
        usdce_balance=usdce,
        matic_balance=matic,
        last_updated=time.time(),
        error=error,
    )


# ============================================================
# effective_mode
# ============================================================


def test_effective_mode_paper_override(monkeypatch):
    """configured=paper → always PAPER, even with a million dollars in wallet."""
    monkeypatch.setenv("TRADING_MODE", "paper")
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.portfolio import effective_mode

    assert effective_mode(_make_wallet(pusd=1_000_000.0)) == TradeMode.PAPER
    assert effective_mode(_make_wallet(available=False)) == TradeMode.PAPER


def test_effective_mode_live_override(monkeypatch):
    """configured=live → always LIVE, even with empty wallet."""
    monkeypatch.setenv("TRADING_MODE", "live")
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.portfolio import effective_mode

    assert effective_mode(_make_wallet(pusd=0.0)) == TradeMode.LIVE
    assert effective_mode(_make_wallet(available=False)) == TradeMode.LIVE


def test_effective_mode_auto_with_funded_wallet(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "auto")
    monkeypatch.setenv("AUTO_MODE_MIN_BALANCE_USD", "5.00")
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.portfolio import effective_mode

    assert effective_mode(_make_wallet(pusd=10.0)) == TradeMode.LIVE
    assert effective_mode(_make_wallet(pusd=5.00)) == TradeMode.LIVE  # at exactly threshold


def test_effective_mode_auto_with_empty_wallet(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "auto")
    monkeypatch.setenv("AUTO_MODE_MIN_BALANCE_USD", "5.00")
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.portfolio import effective_mode

    assert effective_mode(_make_wallet(pusd=0.0)) == TradeMode.PAPER
    assert effective_mode(_make_wallet(pusd=4.99)) == TradeMode.PAPER


def test_effective_mode_auto_ignores_unwrapped_usdce(monkeypatch):
    """Regression: USDC.e alone doesn't count — only pUSD unlocks LIVE.

    Polymarket migrated to pUSD on Apr 28, 2026. A user who deposits USDC.e
    but forgets to wrap should stay in PAPER until they run wrap_usdc.py.
    """
    monkeypatch.setenv("TRADING_MODE", "auto")
    monkeypatch.setenv("AUTO_MODE_MIN_BALANCE_USD", "5.00")
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.portfolio import effective_mode

    only_usdce = _make_wallet(pusd=0.0, usdce=1000.0)
    assert effective_mode(only_usdce) == TradeMode.PAPER
    assert only_usdce.needs_wrap is True


def test_effective_mode_auto_with_no_wallet(monkeypatch):
    """No PRIVATE_KEY → wallet unavailable → safely default to PAPER in auto."""
    monkeypatch.setenv("TRADING_MODE", "auto")
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.portfolio import effective_mode

    assert effective_mode(_make_wallet(available=False)) == TradeMode.PAPER


def test_effective_mode_auto_custom_threshold(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "auto")
    monkeypatch.setenv("AUTO_MODE_MIN_BALANCE_USD", "100.00")
    from backend.config import get_settings
    get_settings.cache_clear()
    from backend.portfolio import effective_mode

    assert effective_mode(_make_wallet(pusd=50.0)) == TradeMode.PAPER
    assert effective_mode(_make_wallet(pusd=100.0)) == TradeMode.LIVE


# ============================================================
# wallet caching
# ============================================================


def test_wallet_cached_state_returns_unavailable_when_unprimed(monkeypatch):
    wallet_module.reset_cache()
    state = wallet_module.cached_wallet_state()
    assert state.available is False
    assert state.pusd_balance == 0.0
    assert state.usdce_balance == 0.0


@pytest.mark.asyncio
async def test_wallet_get_state_unavailable_without_private_key(monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "")
    from backend.config import get_settings
    get_settings.cache_clear()
    wallet_module.reset_cache()

    state = await wallet_module.get_wallet_state(force=True)
    assert state.available is False
    assert state.pusd_balance == 0.0
    assert state.error is not None and "PRIVATE_KEY" in state.error


@pytest.mark.asyncio
async def test_wallet_get_state_uses_cache(monkeypatch):
    """Second call within cache window should NOT hit RPC."""
    monkeypatch.setenv("PRIVATE_KEY", "")
    from backend.config import get_settings
    get_settings.cache_clear()
    wallet_module.reset_cache()

    call_count = {"n": 0}
    real_fetch = wallet_module._fetch_sync

    def _counting_fetch():
        call_count["n"] += 1
        return real_fetch()

    monkeypatch.setattr(wallet_module, "_fetch_sync", _counting_fetch)

    await wallet_module.get_wallet_state(force=True)
    await wallet_module.get_wallet_state()  # cached
    await wallet_module.get_wallet_state()  # cached
    assert call_count["n"] == 1

    await wallet_module.get_wallet_state(force=True)  # force refresh
    assert call_count["n"] == 2


# ============================================================
# end-to-end: executor routes per-trade based on effective mode
# ============================================================


@pytest.mark.asyncio
async def test_executor_routes_to_paper_in_auto_with_empty_wallet(monkeypatch, fresh_db):
    """In auto mode with no wallet funding, every trade should land in PAPER ledger."""
    monkeypatch.setenv("TRADING_MODE", "auto")
    monkeypatch.setenv("PRIVATE_KEY", "")
    from backend.config import get_settings
    get_settings.cache_clear()
    wallet_module.reset_cache()

    from datetime import datetime, timezone
    from backend.edge_calc import EdgeResult
    from backend.executor import execute
    from backend.kelly import KellySizing
    from backend.scanner import MarketOpportunity, MarketSnapshot
    from sqlmodel import select

    market = MarketSnapshot(
        market_id="mkt-auto-1", condition_id="c", question="?",
        yes_token_id="y", no_token_id="n",
        yes_price=0.5, no_price=0.5, spread=0.01,
        liquidity=10_000.0, volume_24h=5_000.0,
        end_date=datetime.now(timezone.utc), hours_to_close=24.0,
    )
    edge = EdgeResult(
        market_id="mkt-auto-1", market_price=0.5,
        model_probability=0.7, edge=0.2, abs_edge=0.2,
        confidence=0.85, side="YES", sources=["test"], score=0.1,
    )
    sizing = KellySizing(
        raw_kelly=0.3, fractional_kelly=0.15, capped_fraction=0.15,
        stake_usd=2.0, shares=4.0, stage_name="Stage 1",
    )

    result = await execute(MarketOpportunity(market=market, edge=edge), sizing)
    assert result.success is True

    db = fresh_db
    with db.session_scope() as s:
        trade = s.exec(select(db.Trade)).first()
    assert trade is not None
    assert trade.mode == db.TradeMode.PAPER  # routed to paper because wallet empty