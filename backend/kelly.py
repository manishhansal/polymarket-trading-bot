"""Kelly Criterion position sizing + compounding-stage manager.

The Kelly formula for a binary bet with payout odds `b`:

    f* = (b * m - (1 - m)) / b

where `m` is win probability and `b` is net-odds (payout / stake).

For a Polymarket YES contract bought at price `p ∈ (0, 1)` that pays $1.00
on a win and $0 on a loss:

    net odds  b   = (1 - p) / p
    Kelly f*      = (m - p) / (1 - p)        ← canonical closed form

(Derivation: maximise m·log(1 + f·b) + (1-m)·log(1-f) over f, then simplify.)

We always apply a fractional Kelly multiplier for safety — full Kelly is the
mathematically optimal long-run growth rate but has eye-watering 50% drawdown
variance. Fractional Kelly is the practical choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import get_active_stage, get_settings


@dataclass(frozen=True)
class KellySizing:
    """Result of a position-sizing calculation."""

    raw_kelly: float          # Full Kelly fraction (can be negative)
    fractional_kelly: float   # After stage multiplier
    capped_fraction: float    # After max-position cap
    stake_usd: float          # Dollar amount to bet
    shares: float             # Shares (stake / price)
    stage_name: str
    reject_reason: Optional[str] = None

    @property
    def should_trade(self) -> bool:
        return self.reject_reason is None and self.stake_usd > 0


def kelly_fraction(model_prob: float, market_price: float) -> float:
    """Compute the raw (full) Kelly fraction for a YES-side buy at `market_price`.

    Returns 0.0 when there is no edge (don't bet) and never returns negative
    (we don't shortsell in this design — flipping to NO is handled upstream).
    """
    if not 0.0 < market_price < 1.0:
        return 0.0
    if not 0.0 < model_prob < 1.0:
        return 0.0

    # Canonical Kelly for a binary contract bought at price `market_price`.
    f = (model_prob - market_price) / (1.0 - market_price)
    return max(f, 0.0)


def size_position(
    *,
    bankroll: float,
    model_prob: float,
    market_price: float,
    confidence: float,
    edge: float,
    open_position_count: int,
) -> KellySizing:
    """Compute the dollar stake for a candidate trade.

    Args:
        bankroll:             Current total bankroll (cash + open positions).
        model_prob:           Our model's win probability for the YES outcome.
        market_price:         Current YES market price ∈ (0, 1).
        confidence:           Confidence score in `model_prob` ∈ [0, 1].
        edge:                 model_prob - market_price (absolute).
        open_position_count:  How many open positions we already hold.

    Returns:
        KellySizing with `should_trade` False and a `reject_reason` when any
        gate fails.
    """
    settings = get_settings()
    stage = get_active_stage(bankroll)

    raw = kelly_fraction(model_prob, market_price)

    # Apply BOTH the stage multiplier AND the global Kelly fraction.
    # Stage multiplier dominates when more aggressive; global fraction
    # dominates when more conservative. Use the smaller (safer) of the two.
    effective_multiplier = min(stage["kelly_fraction"], settings.kelly_fraction)
    fractional = raw * effective_multiplier

    # Confidence-weighting: low confidence shrinks the stake linearly.
    fractional *= max(confidence, 0.0)

    # Hard cap at max position fraction of bankroll.
    capped = min(fractional, settings.max_position_fraction)

    stake = round(capped * bankroll, 4)

    # --- Gates ---
    if bankroll <= 0:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason="Bankroll is zero or negative")

    if edge < stage["min_edge"]:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason=f"Edge {edge:.2%} below stage minimum {stage['min_edge']:.2%}")

    if edge < settings.min_edge_threshold:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason=f"Edge {edge:.2%} below global minimum {settings.min_edge_threshold:.2%}")

    if confidence < settings.min_confidence:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason=f"Confidence {confidence:.2f} below threshold {settings.min_confidence:.2f}")

    if open_position_count >= stage["max_positions"]:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason=f"Already at stage max positions ({stage['max_positions']})")

    if open_position_count >= settings.max_concurrent_bets:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason=f"Already at global max positions ({settings.max_concurrent_bets})")

    if raw <= 0:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason="No Kelly edge")

    if stake < settings.min_bet_size_usd:
        return KellySizing(raw, fractional, capped, 0.0, 0.0, stage["name"],
                           reject_reason=f"Stake ${stake:.2f} below minimum ${settings.min_bet_size_usd:.2f}")

    shares = round(stake / market_price, 4) if market_price > 0 else 0.0

    return KellySizing(
        raw_kelly=raw,
        fractional_kelly=fractional,
        capped_fraction=capped,
        stake_usd=stake,
        shares=shares,
        stage_name=stage["name"],
    )


def drawdown_pct(peak: float, current: float) -> float:
    """Return drawdown as a positive fraction (0.30 means down 30% from peak)."""
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - current) / peak)


def circuit_breaker_tripped(peak: float, current: float) -> bool:
    """True if drawdown exceeds the configured circuit-breaker threshold."""
    settings = get_settings()
    return drawdown_pct(peak, current) >= settings.drawdown_circuit_breaker
