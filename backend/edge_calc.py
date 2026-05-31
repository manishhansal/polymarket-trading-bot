"""Edge calculator: build a model probability from external oracles and
compare it to the Polymarket implied probability to surface mispricings.

Sources (in priority order):
    * Kalshi      — regulated US prediction exchange, public API, NO AUTH
                    (endpoint: api.elections.kalshi.com — note that despite
                    the subdomain, this serves ALL Kalshi markets, not just
                    elections)
    * Metaculus   — community forecasts; **requires** METACULUS_TOKEN since
                    Metaculus locked their public API behind auth in 2025.
                    If the token is unset we silently skip this source.

When **no** external oracle can match a market topic, we fall back to a
**zero-edge, zero-confidence** baseline: model_probability = market_price.
This is intentional — without independent evidence the only honest answer
is "we don't know", and the Kelly sizer will (correctly) refuse to trade.

Past versions of this module had a "mean reversion toward 0.50" heuristic
that produced phantom 3-5% edge on every tail-priced market, which would
fake-trade once the bot entered Stage 3 (3.5% edge threshold). That bug
was caught in production logs on 2026-05-30 and removed. Never reintroduce
heuristic edge without independent calibration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx
from loguru import logger

from .config import get_settings


@dataclass(frozen=True)
class EdgeResult:
    """Output of the edge calculator for a single market."""

    market_id: str
    market_price: float          # Polymarket YES price ∈ (0, 1)
    model_probability: float     # Our blended fair-value estimate
    edge: float                  # model_probability - market_price (signed)
    abs_edge: float              # |edge|
    confidence: float            # ∈ [0, 1]
    side: str                    # "YES" if model_prob > market_price else "NO"
    sources: list[str]           # Which oracles contributed
    score: float                 # Composite ranking score (higher = better)


# ============================================================
# ORACLE FETCHERS
# ============================================================


async def _fetch_metaculus_match(question: str, client: httpx.AsyncClient) -> Optional[float]:
    """Search Metaculus for a similar question. Requires METACULUS_TOKEN.

    Metaculus closed unauthenticated access in 2025 — without a token we
    return None silently so we don't spam logs with 401s.
    """
    settings = get_settings()
    if not settings.metaculus_token:
        return None
    headers = {"Authorization": f"Token {settings.metaculus_token}"}
    try:
        url = f"{settings.metaculus_api}/posts/"
        params = {
            "search": _short_search_terms(question),
            "statuses": "open",
            "forecast_type": "binary",
            "limit": 5,
        }
        r = await client.get(url, params=params, headers=headers, timeout=8.0)
        r.raise_for_status()
        data = r.json()
        for post in data.get("results", []):
            q = post.get("question") or {}
            aggs = q.get("aggregations") or {}
            rec = aggs.get("recency_weighted") or {}
            latest = rec.get("latest") or {}
            centers = latest.get("centers") or []
            if centers and 0.0 < centers[0] < 1.0:
                return float(centers[0])
    except Exception as e:
        logger.debug(f"Metaculus lookup failed for '{question[:40]}': {e}")
    return None


async def _fetch_kalshi_match(question: str, client: httpx.AsyncClient) -> Optional[float]:
    """Look for a Kalshi market with overlapping keywords. NO AUTH REQUIRED.

    Uses the public elections-subdomain endpoint which (despite the name)
    serves every Kalshi market, not just political ones.
    """
    settings = get_settings()
    try:
        # New Kalshi public API: prices are already in dollars (strings like
        # "0.5280"), and the status filter expects "open".
        r = await client.get(
            f"{settings.kalshi_api}/markets",
            params={"limit": 100, "status": "open"},
            timeout=8.0,
        )
        r.raise_for_status()
        data = r.json()
        markets = data.get("markets") or []
        target_terms = set(_tokenize(question))
        if not target_terms:
            return None
        best_overlap = 0
        best_yes: Optional[float] = None
        for m in markets:
            title = " ".join(filter(None, [
                m.get("title"), m.get("yes_sub_title"), m.get("rules_primary"),
            ]))
            terms = set(_tokenize(title))
            overlap = len(target_terms & terms)
            if overlap > best_overlap and overlap >= 3:
                # Prefer mid (bid+ask)/2 over last-price for cleaner signal.
                bid_s = m.get("yes_bid_dollars") or "0"
                ask_s = m.get("yes_ask_dollars") or "0"
                try:
                    bid = float(bid_s)
                    ask = float(ask_s)
                except (TypeError, ValueError):
                    continue
                if bid > 0 and ask > 0:
                    best_overlap = overlap
                    best_yes = (bid + ask) / 2.0
        return best_yes
    except Exception as e:
        logger.debug(f"Kalshi lookup failed: {e}")
    return None


# ============================================================
# HELPERS
# ============================================================


_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "be", "will", "is", "are",
    "by", "at", "from", "with", "and", "or", "as", "this", "that", "it", "its",
    "do", "does", "did", "have", "has", "had", "was", "were", "been", "than",
    "but", "if", "any", "all", "before", "after",
}


def _tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 2]


def _short_search_terms(text: str, n: int = 5) -> str:
    return " ".join(_tokenize(text)[:n])


def _blend(probs: Iterable[float]) -> float:
    """Geometric blend of probabilities in log-odds space (stable around 0/1)."""
    vals = [p for p in probs if p is not None and 0.0 < p < 1.0]
    if not vals:
        return 0.5
    logits = [math.log(p / (1.0 - p)) for p in vals]
    avg = sum(logits) / len(logits)
    return 1.0 / (1.0 + math.exp(-avg))


# ============================================================
# PUBLIC API
# ============================================================


async def compute_edge(
    *,
    market_id: str,
    question: str,
    yes_price: float,
    spread: float,
    volume_24h: float,
    liquidity: float,
    hours_to_close: float,
    client: Optional[httpx.AsyncClient] = None,
) -> EdgeResult:
    """Compute the model probability and edge for a single Polymarket market.

    Honesty rule: if NO oracle returns a probability, we set
    `model_probability = yes_price`, `edge = 0`, `confidence = 0`.
    The Kelly sizer will then refuse to trade. This is intentional — without
    independent evidence, fabricating edge is how amateur bots lose money.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient()

    try:
        oracle_probs: list[float] = []
        sources: list[str] = []

        for name, fetcher in (
            ("kalshi",    _fetch_kalshi_match),
            ("metaculus", _fetch_metaculus_match),
        ):
            p = await fetcher(question, client)
            if p is not None:
                oracle_probs.append(p)
                sources.append(name)

        if oracle_probs:
            model_p = _blend(oracle_probs)
            # Confidence grows with the number of agreeing sources, dampened
            # by spread. One source: 0.65 baseline. Two sources: +0.25 bonus.
            agreement_bonus = 0.25 * (len(oracle_probs) - 1)
            confidence = max(0.0, min(1.0, 0.65 + agreement_bonus - 2.0 * spread))
            edge = model_p - yes_price
        else:
            # Honest fallback: zero edge, zero confidence. Kelly will skip it.
            sources.append("no-oracle")
            model_p = yes_price
            edge = 0.0
            confidence = 0.0

        abs_edge = abs(edge)
        side = "YES" if edge > 0 else "NO"

        # Composite ranking score: edge * liquidity_score * time_weight.
        # Multiplied by confidence so "no-oracle" markets score 0 and never
        # crowd out real opportunities at the top of the ranking.
        liquidity_score = math.log1p(liquidity) / math.log1p(1_000_000.0)
        time_weight = 1.0 - math.exp(-hours_to_close / 48.0)
        score = abs_edge * liquidity_score * time_weight * confidence

        return EdgeResult(
            market_id=market_id,
            market_price=yes_price,
            model_probability=model_p,
            edge=edge,
            abs_edge=abs_edge,
            confidence=confidence,
            side=side,
            sources=sources,
            score=score,
        )
    finally:
        if owns_client:
            await client.aclose()
