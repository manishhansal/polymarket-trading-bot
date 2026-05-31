"""Unit tests for the Kelly Criterion sizer."""

from __future__ import annotations

import math

import pytest

from backend.kelly import (
    KellySizing,
    circuit_breaker_tripped,
    drawdown_pct,
    kelly_fraction,
    size_position,
)


# ============================================================
# RAW KELLY
# ============================================================


def test_kelly_zero_when_no_edge():
    # market is fair → no edge
    assert kelly_fraction(model_prob=0.50, market_price=0.50) == 0.0


def test_kelly_positive_when_market_underprices_yes():
    # we think 70%, market says 50% → big edge
    f = kelly_fraction(model_prob=0.70, market_price=0.50)
    # Closed form: (m - p)/(1 - p) = 0.20/0.50 = 0.40
    assert f == pytest.approx(0.40, abs=1e-9)
    assert f > 0


def test_kelly_zero_when_market_overprices_yes():
    # we think 30%, market says 50% → don't BUY YES (would bet NO upstream)
    assert kelly_fraction(model_prob=0.30, market_price=0.50) == 0.0


def test_kelly_handles_boundary_prices():
    assert kelly_fraction(0.5, 0.0) == 0.0
    assert kelly_fraction(0.5, 1.0) == 0.0
    assert kelly_fraction(0.0, 0.5) == 0.0
    assert kelly_fraction(1.0, 0.5) == 0.0


def test_kelly_matches_textbook_formula():
    """Independent check against the binary-bet form: f* = (b·m - (1-m)) / b."""
    m, price = 0.65, 0.40
    b = (1 - price) / price        # net odds
    q = 1 - m
    expected = (b * m - q) / b     # classic textbook form
    # Closed-form simplification we use in code: (m - price) / (1 - price)
    assert expected == pytest.approx((m - price) / (1 - price), rel=1e-9)
    assert kelly_fraction(m, price) == pytest.approx(expected, rel=1e-9)


# ============================================================
# DRAWDOWN / CIRCUIT BREAKER
# ============================================================


def test_drawdown_pct_basic():
    assert drawdown_pct(peak=100.0, current=70.0) == pytest.approx(0.30)
    assert drawdown_pct(peak=100.0, current=110.0) == 0.0
    assert drawdown_pct(peak=0.0, current=10.0) == 0.0


def test_circuit_breaker_threshold():
    # default threshold is 30%
    assert circuit_breaker_tripped(peak=100.0, current=69.99) is True
    assert circuit_breaker_tripped(peak=100.0, current=75.0) is False


# ============================================================
# SIZE_POSITION — full integration
# ============================================================


def _sizing_kwargs(**overrides):
    base = dict(
        bankroll=100.0,
        model_prob=0.70,
        market_price=0.50,
        confidence=0.80,
        edge=0.20,
        open_position_count=0,
    )
    base.update(overrides)
    return base


def test_size_position_happy_path():
    r = size_position(**_sizing_kwargs())
    assert isinstance(r, KellySizing)
    assert r.should_trade
    assert r.stake_usd > 0
    assert r.shares > 0
    # Stake must not exceed max position fraction (20% of bankroll = $20)
    assert r.stake_usd <= 20.0


def test_size_position_rejects_low_edge():
    # In Stage 3 ($100), min edge is 3.5% — give it 2%
    r = size_position(**_sizing_kwargs(edge=0.02))
    assert not r.should_trade
    assert "edge" in (r.reject_reason or "").lower()


def test_size_position_rejects_low_confidence():
    r = size_position(**_sizing_kwargs(confidence=0.40))
    assert not r.should_trade
    assert "confidence" in (r.reject_reason or "").lower()


def test_size_position_rejects_when_max_positions_reached():
    # Stage 3 caps at 3 positions
    r = size_position(**_sizing_kwargs(open_position_count=3))
    assert not r.should_trade
    assert "positions" in (r.reject_reason or "").lower()


def test_size_position_rejects_below_min_bet():
    # Tiny bankroll → tiny stake → below $0.50 floor
    r = size_position(**_sizing_kwargs(bankroll=0.10))
    assert not r.should_trade


def test_size_position_capped_at_max_fraction():
    # Huge edge + lots of confidence — Kelly would say "bet 60%" but cap is 20%.
    r = size_position(**_sizing_kwargs(
        bankroll=1000.0, model_prob=0.95, market_price=0.30,
        confidence=1.0, edge=0.65,
    ))
    assert r.should_trade
    assert r.capped_fraction <= 0.20 + 1e-9
    assert r.stake_usd <= 200.0 + 1e-6


def test_size_position_stage_2_aggression():
    """At $50 bankroll → Stage 2 → 3/4 Kelly multiplier."""
    r = size_position(**_sizing_kwargs(bankroll=50.0))
    assert r.stage_name.startswith("Stage 2")
    assert r.should_trade


def test_size_position_stage_4_conservation():
    """At $750 bankroll → Stage 4 → 1/4 Kelly."""
    r = size_position(**_sizing_kwargs(bankroll=750.0))
    assert r.stage_name.startswith("Stage 4")


def test_size_position_rejects_negative_bankroll():
    r = size_position(**_sizing_kwargs(bankroll=-1.0))
    assert not r.should_trade


def test_shares_consistent_with_stake():
    market_price = 0.50
    r = size_position(**_sizing_kwargs(market_price=market_price))
    if r.should_trade:
        assert math.isclose(r.shares, r.stake_usd / market_price, rel_tol=0.01)


def test_stage_2_minimum_edge_gate():
    """Stage 2 floor at $25-$100 requires min 5% edge — give 4% → reject."""
    r = size_position(**_sizing_kwargs(bankroll=50.0, edge=0.04))
    assert not r.should_trade
