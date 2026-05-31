"""Unit tests for the edge calculator (oracle blend + heuristic fallback)."""

from __future__ import annotations

import pytest

from backend import edge_calc
from backend.edge_calc import EdgeResult, _blend, _tokenize, compute_edge


# ============================================================
# HELPERS
# ============================================================


def test_tokenize_removes_stopwords():
    toks = _tokenize("Will the President of France visit the moon by 2030?")
    assert "the" not in toks
    assert "will" not in toks
    assert "president" in toks
    assert "france" in toks
    assert "moon" in toks


def test_blend_geometric_logit_mean():
    # Two perfectly-agreeing 70% probs blend to 70%.
    assert _blend([0.70, 0.70]) == pytest.approx(0.70, abs=1e-9)
    # Empty blend defaults to 50/50.
    assert _blend([]) == pytest.approx(0.50)
    # Filters invalid entries.
    assert _blend([0.0, 1.0, 0.6, None]) == pytest.approx(0.60, abs=1e-9)


def test_blend_is_symmetric():
    """Order should not matter in logit space."""
    assert _blend([0.3, 0.8]) == pytest.approx(_blend([0.8, 0.3]), abs=1e-12)


# ============================================================
# COMPUTE_EDGE — heuristic fallback path
# ============================================================


async def _none(*a, **k):
    return None


def _patch_oracles(monkeypatch, *, kalshi=None, metaculus=None):
    """Replace the two oracle fetchers with constants for testing."""
    async def _kalshi(*a, **k): return kalshi
    async def _meta(*a, **k): return metaculus
    monkeypatch.setattr(edge_calc, "_fetch_kalshi_match", _kalshi)
    monkeypatch.setattr(edge_calc, "_fetch_metaculus_match", _meta)


# --- HONEST FALLBACK ---


@pytest.mark.asyncio
async def test_no_oracle_match_produces_zero_edge_and_zero_confidence(monkeypatch):
    """REGRESSION: prior to 2026-05-30 the heuristic produced phantom 3-5%
    edge on every tail-priced market. The honest contract is now:
    no oracle → model = price → edge = 0 → confidence = 0."""
    _patch_oracles(monkeypatch, kalshi=None, metaculus=None)

    for yes_price in (0.05, 0.30, 0.50, 0.80, 0.95):
        r = await compute_edge(
            market_id="m", question="Will event X happen?",
            yes_price=yes_price, spread=0.02,
            volume_24h=10_000.0, liquidity=50_000.0, hours_to_close=24.0,
        )
        assert r.model_probability == pytest.approx(yes_price, abs=1e-9), \
            f"phantom edge at price {yes_price}"
        assert r.edge == 0.0
        assert r.abs_edge == 0.0
        assert r.confidence == 0.0
        assert r.score == 0.0
        assert r.sources == ["no-oracle"]


@pytest.mark.asyncio
async def test_no_oracle_returns_valid_edge_result(monkeypatch):
    _patch_oracles(monkeypatch, kalshi=None, metaculus=None)
    r = await compute_edge(
        market_id="m1", question="Will it rain tomorrow in Tokyo?",
        yes_price=0.55, spread=0.02,
        volume_24h=10_000.0, liquidity=50_000.0, hours_to_close=24.0,
    )
    assert isinstance(r, EdgeResult)
    assert r.market_id == "m1"
    assert r.sources == ["no-oracle"]


# --- KALSHI PATH ---


@pytest.mark.asyncio
async def test_compute_edge_with_kalshi_only(monkeypatch):
    _patch_oracles(monkeypatch, kalshi=0.80, metaculus=None)
    r = await compute_edge(
        market_id="m2", question="A clearly mispriced question",
        yes_price=0.30, spread=0.01,
        volume_24h=100_000.0, liquidity=200_000.0, hours_to_close=72.0,
    )
    assert r.model_probability == pytest.approx(0.80, abs=1e-6)
    assert r.edge == pytest.approx(0.50, abs=1e-6)
    assert r.side == "YES"
    assert r.sources == ["kalshi"]
    assert r.confidence > 0.6


@pytest.mark.asyncio
async def test_compute_edge_two_oracles_boost_confidence(monkeypatch):
    _patch_oracles(monkeypatch, kalshi=0.80, metaculus=0.70)
    r = await compute_edge(
        market_id="m3", question="another",
        yes_price=0.50, spread=0.01,
        volume_24h=10_000.0, liquidity=100_000.0, hours_to_close=48.0,
    )
    assert set(r.sources) == {"kalshi", "metaculus"}
    # 2 agreeing sources → +0.25 bonus → > 0.85 baseline
    assert r.confidence > 0.85


@pytest.mark.asyncio
async def test_compute_edge_side_flips_when_market_overprices(monkeypatch):
    _patch_oracles(monkeypatch, kalshi=0.20, metaculus=None)
    r = await compute_edge(
        market_id="m4", question="overpriced",
        yes_price=0.80, spread=0.01,
        volume_24h=5_000.0, liquidity=20_000.0, hours_to_close=12.0,
    )
    assert r.side == "NO"
    assert r.edge < 0
    assert r.abs_edge == pytest.approx(0.60, abs=1e-6)


@pytest.mark.asyncio
async def test_score_is_zero_when_no_edge(monkeypatch):
    _patch_oracles(monkeypatch, kalshi=0.50, metaculus=None)
    r = await compute_edge(
        market_id="m5", question="fair",
        yes_price=0.50, spread=0.01,
        volume_24h=10_000.0, liquidity=10_000.0, hours_to_close=24.0,
    )
    assert r.abs_edge == pytest.approx(0.0, abs=1e-9)
    assert r.score == pytest.approx(0.0, abs=1e-9)


# --- METACULUS AUTH GATE ---


@pytest.mark.asyncio
async def test_metaculus_skipped_when_no_token(monkeypatch):
    """No METACULUS_TOKEN → return None immediately, no HTTP call."""
    from backend.config import get_settings
    get_settings.cache_clear()
    monkeypatch.delenv("METACULUS_TOKEN", raising=False)
    monkeypatch.setenv("METACULUS_TOKEN", "")
    get_settings.cache_clear()

    called = {"n": 0}

    class _DummyResp:
        def raise_for_status(self): pass
        def json(self): called["n"] += 1; return {"results": []}

    class _DummyClient:
        async def get(self, *a, **k):
            called["n"] += 1
            return _DummyResp()

    result = await edge_calc._fetch_metaculus_match("Q?", _DummyClient())
    assert result is None
    assert called["n"] == 0  # No HTTP attempted
