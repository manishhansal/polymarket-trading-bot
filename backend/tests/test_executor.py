"""Unit tests for the trade executor (paper mode end-to-end)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from backend.edge_calc import EdgeResult
from backend.executor import close_position, execute, mark_to_market
from backend.kelly import KellySizing
from backend.scanner import MarketOpportunity, MarketSnapshot


def _opp(
    *,
    yes_price=0.50,
    edge=0.20,
    side="YES",
    model_p=0.70,
    confidence=0.85,
) -> MarketOpportunity:
    m = MarketSnapshot(
        market_id="mkt-1",
        condition_id="cond-1",
        question="Will event X happen?",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        spread=0.01,
        liquidity=50_000.0,
        volume_24h=10_000.0,
        end_date=datetime.now(timezone.utc),
        hours_to_close=48.0,
    )
    e = EdgeResult(
        market_id="mkt-1",
        market_price=yes_price,
        model_probability=model_p,
        edge=edge if side == "YES" else -edge,
        abs_edge=abs(edge),
        confidence=confidence,
        side=side,
        sources=["test"],
        score=edge * 0.5,
    )
    return MarketOpportunity(market=m, edge=e)


def _sizing(stake=2.50, shares=5.0) -> KellySizing:
    return KellySizing(
        raw_kelly=0.40,
        fractional_kelly=0.20,
        capped_fraction=0.20,
        stake_usd=stake,
        shares=shares,
        stage_name="Stage 1 — Ignition",
    )


# ============================================================
# PAPER PATH
# ============================================================


@pytest.mark.asyncio
async def test_paper_execute_creates_trade_and_position(fresh_db):
    db = fresh_db
    result = await execute(_opp(), _sizing())

    assert result.success is True
    assert result.trade_id is not None
    assert result.fill_price is not None
    assert 0.0 < result.fill_price < 1.0
    assert (result.order_id or "").startswith("PAPER-")

    with db.session_scope() as s:
        trades = s.exec(select(db.Trade)).all()
        positions = s.exec(select(db.Position)).all()

    assert len(trades) == 1
    assert trades[0].status == db.TradeStatus.FILLED
    assert trades[0].mode == db.TradeMode.PAPER
    assert trades[0].side == db.TradeSide.YES
    assert len(positions) == 1
    assert positions[0].trade_id == trades[0].id
    assert positions[0].size_shares == 5.0


@pytest.mark.asyncio
async def test_paper_execute_records_no_side_token(fresh_db):
    db = fresh_db
    await execute(_opp(side="NO", edge=0.25), _sizing())

    with db.session_scope() as s:
        pos = s.exec(select(db.Position)).first()

    assert pos is not None
    assert pos.side == db.TradeSide.NO
    assert pos.token_id == "tok-no"


@pytest.mark.asyncio
async def test_execute_rejects_when_sizing_says_no(fresh_db):
    db = fresh_db
    bad = KellySizing(
        raw_kelly=0.0, fractional_kelly=0.0, capped_fraction=0.0,
        stake_usd=0.0, shares=0.0,
        stage_name="Stage 1 — Ignition",
        reject_reason="Edge too small",
    )
    res = await execute(_opp(), bad)
    assert res.success is False
    assert "Edge too small" in res.message

    with db.session_scope() as s:
        assert s.exec(select(db.Trade)).all() == []
        assert s.exec(select(db.Position)).all() == []


@pytest.mark.asyncio
async def test_mark_to_market_updates_unrealized_pnl(fresh_db):
    db = fresh_db
    await execute(_opp(yes_price=0.50, edge=0.20), _sizing(stake=2.50, shares=5.0))

    # Mark to a 60 cent price — should be a $0.50 unrealized gain
    # (price went up but we paid the synthetic-slippage adjusted entry ≈ 0.5012)
    await mark_to_market({"tok-yes": 0.60})

    with db.session_scope() as s:
        pos = s.exec(select(db.Position)).first()

    assert pos.current_price == 0.60
    expected_pnl = (0.60 - pos.avg_price) * pos.size_shares
    assert pos.unrealized_pnl_usd == pytest.approx(expected_pnl, abs=1e-6)
    assert pos.unrealized_pnl_usd > 0


@pytest.mark.asyncio
async def test_close_position_realizes_pnl_and_removes_position(fresh_db):
    db = fresh_db
    await execute(_opp(yes_price=0.50, edge=0.20), _sizing(stake=2.50, shares=5.0))

    with db.session_scope() as s:
        pos = s.exec(select(db.Position)).first()
        pos_id = pos.id
        cost = pos.cost_basis_usd

    pnl = await close_position(pos_id, exit_price=1.00,
                               status=db.TradeStatus.RESOLVED_WIN)

    assert pnl == pytest.approx(5.0 * 1.00 - cost, abs=1e-6)
    assert pnl > 0

    with db.session_scope() as s:
        assert s.exec(select(db.Position)).all() == []
        trade = s.exec(select(db.Trade)).first()
        assert trade.status == db.TradeStatus.RESOLVED_WIN
        assert trade.pnl_usd == pytest.approx(pnl, abs=1e-6)
        assert trade.resolved_at is not None


@pytest.mark.asyncio
async def test_close_position_returns_none_for_unknown_id(fresh_db):
    pnl = await close_position(99999, exit_price=0.5)
    assert pnl is None
