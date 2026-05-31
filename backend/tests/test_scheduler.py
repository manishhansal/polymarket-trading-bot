"""Regression tests for the APScheduler job graph + scan_and_trade.

History: on 2026-05-30 *two* bugs combined to cause silent no-trading:

  1. `scan_and_trade` was added with `next_run_time=None`, which puts
     APScheduler jobs in a paused state. The job never fired at all.

  2. After fix #1, the job ran but the "All oracles unavailable" alert
     never fired in the *exact* case it was meant to handle. The
     detector lived inside `if opportunities:`, but when every market
     comes back oracleless, `compute_edge` returns edge=0 and those
     candidates are filtered out before reaching the opportunity list.
     Result: scheduler had `opportunities=[]`, skipped the detector,
     and the user saw zero alerts + zero trades with no explanation.

These tests guard against both.

APScheduler 3.x semantics worth knowing for these tests:
    - `add_job(next_run_time=undefined)`  (the default)
        → Job is added WITHOUT the next_run_time attribute. It is computed
          from the trigger when the scheduler starts. This is the normal,
          correct case.
    - `add_job(next_run_time=<datetime>)`
        → Job is added WITH next_run_time set to that datetime. Used to
          fire the job immediately on boot. Also correct.
    - `add_job(next_run_time=None)`
        → Job is added WITH next_run_time = None. The scheduler treats
          this as PAUSED and never fires the job. THIS IS THE BUG.
"""

from __future__ import annotations

_SENTINEL = object()


def _next_run_attr(job):
    """Return the job's next_run_time, or _SENTINEL if the attribute was
    deliberately not set (the trigger will compute it on scheduler start)."""
    return getattr(job, "next_run_time", _SENTINEL)


def test_no_job_is_added_in_paused_state():
    """Every job built by build_scheduler() must be schedulable.

    A job whose next_run_time is explicitly None is considered PAUSED by
    APScheduler and will never fire on its own — this is exactly the
    silent-no-trades bug from 2026-05-30.
    """
    from backend.scheduler import build_scheduler

    sched = build_scheduler()
    jobs = list(sched._pending_jobs)
    assert jobs, "scheduler has no pending jobs"

    paused = [
        job.id for job, _, _ in jobs if _next_run_attr(job) is None
    ]
    assert not paused, (
        f"These jobs were added with next_run_time=None and will be paused "
        f"forever: {paused}. APScheduler will silently never fire them."
    )


def test_expected_jobs_are_registered():
    """Sanity check that every job in the design is actually wired up."""
    from backend.scheduler import build_scheduler

    sched = build_scheduler()
    ids = {job.id for job, _, _ in sched._pending_jobs}
    assert {"scan_and_trade", "refresh_marks", "write_snapshot", "poll_wallet"} <= ids


def test_scan_job_first_run_is_immediate_or_within_interval():
    """The scan job's first-run time must be at-or-before now + interval.

    Catches the regression in a second way: not just 'is it paused', but
    'will it actually fire soon'. Our fix sets next_run_time = now so the
    first scan happens on boot rather than after a full interval.
    """
    from datetime import datetime, timedelta, timezone

    from backend.config import get_settings
    from backend.scheduler import build_scheduler

    sched = build_scheduler()
    scan_job = next(
        (job for job, _, _ in sched._pending_jobs if job.id == "scan_and_trade"),
        None,
    )
    assert scan_job is not None

    next_run = _next_run_attr(scan_job)
    if next_run is _SENTINEL:
        # Acceptable: trigger will compute it at scheduler.start() time.
        # Means the job is NOT paused, which is what we care about.
        return

    assert next_run is not None, "scan_and_trade is paused (next_run_time=None)"
    interval = get_settings().scan_interval_seconds
    latest_acceptable = datetime.now(timezone.utc) + timedelta(seconds=interval + 5)
    assert next_run <= latest_acceptable, (
        f"scan_and_trade scheduled too far out: {next_run}"
    )


# ============================================================
# scan_and_trade: oracle-unavailable alert flow
# ============================================================
#
# These regression-guard the second bug: when every market returns
# no-oracle, edge=0 → markets get filtered before they reach
# `opportunities`. The detector therefore CANNOT base its decision on
# the qualified opportunity list — it has to look at evaluated_count
# vs oracleless_count from the new ScanResult.


import pytest


def _scan_result_factory(*, evaluated, oracleless, opportunities=()):
    """Build a backend.scanner.ScanResult without hitting the network."""
    from backend.scanner import ScanResult
    return ScanResult(
        opportunities=list(opportunities),
        evaluated_count=evaluated,
        oracleless_count=oracleless,
    )


@pytest.mark.asyncio
async def test_all_oracles_down_alert_fires_with_zero_opportunities(
    fresh_db, monkeypatch
):
    """REGRESSION: previously the warning lived inside `if opportunities:`,
    so when ALL markets were oracleless the alert never fired — exactly
    the case where the user needs to know."""
    from backend import scheduler
    from backend.db import AlertLevel
    from backend.portfolio import recent_alerts

    async def fake_scan():
        return _scan_result_factory(evaluated=80, oracleless=80, opportunities=[])

    monkeypatch.setattr(scheduler, "scan", fake_scan)

    # Reset the one-shot flag so the alert is allowed to fire.
    from backend.db import set_kv
    set_kv("oracle_warning_emitted", "false")

    await scheduler.scan_and_trade()

    alerts = recent_alerts(limit=10)
    titles = [a.title for a in alerts]
    assert "All oracles unavailable" in titles, (
        f"Expected the silent-oracles warning to fire. Got: {titles}"
    )
    bad_alert = next(a for a in alerts if a.title == "All oracles unavailable")
    assert bad_alert.level == AlertLevel.WARNING


@pytest.mark.asyncio
async def test_all_oracles_down_alert_is_one_shot(fresh_db, monkeypatch):
    """The warning must not spam the feed every 60 seconds."""
    from backend import scheduler
    from backend.portfolio import recent_alerts
    from backend.db import set_kv

    async def fake_scan():
        return _scan_result_factory(evaluated=50, oracleless=50, opportunities=[])

    monkeypatch.setattr(scheduler, "scan", fake_scan)
    set_kv("oracle_warning_emitted", "false")

    await scheduler.scan_and_trade()
    await scheduler.scan_and_trade()
    await scheduler.scan_and_trade()

    alerts = recent_alerts(limit=20)
    occurrences = sum(1 for a in alerts if a.title == "All oracles unavailable")
    assert occurrences == 1, (
        f"Expected exactly one 'All oracles unavailable' alert across three "
        f"scans, got {occurrences}."
    )


@pytest.mark.asyncio
async def test_oracles_recovered_alert_fires_on_transition(fresh_db, monkeypatch):
    """When at least one oracle starts matching again, fire a success alert."""
    from backend import scheduler
    from backend.db import AlertLevel, set_kv
    from backend.portfolio import recent_alerts

    # Phase 1 — all down, warning fires.
    async def all_down():
        return _scan_result_factory(evaluated=10, oracleless=10, opportunities=[])

    monkeypatch.setattr(scheduler, "scan", all_down)
    set_kv("oracle_warning_emitted", "false")
    await scheduler.scan_and_trade()

    # Phase 2 — one matches, recovery alert should fire.
    async def one_matches():
        return _scan_result_factory(evaluated=10, oracleless=9, opportunities=[])

    monkeypatch.setattr(scheduler, "scan", one_matches)
    await scheduler.scan_and_trade()

    alerts = recent_alerts(limit=20)
    titles = [a.title for a in alerts]
    assert "Oracles recovered" in titles, (
        f"Expected recovery alert after partial oracle revival. Got: {titles}"
    )
    recovered = next(a for a in alerts if a.title == "Oracles recovered")
    assert recovered.level == AlertLevel.SUCCESS


@pytest.mark.asyncio
async def test_no_alert_when_some_markets_have_oracles(fresh_db, monkeypatch):
    """Healthy state: don't pollute the feed when most markets are matched."""
    from backend import scheduler
    from backend.portfolio import recent_alerts
    from backend.db import set_kv

    async def healthy():
        return _scan_result_factory(evaluated=80, oracleless=20, opportunities=[])

    monkeypatch.setattr(scheduler, "scan", healthy)
    set_kv("oracle_warning_emitted", "false")

    await scheduler.scan_and_trade()

    alerts = recent_alerts(limit=10)
    titles = {a.title for a in alerts}
    assert "All oracles unavailable" not in titles
    assert "Oracles recovered" not in titles


@pytest.mark.asyncio
async def test_no_alert_when_scanner_returned_zero_markets(fresh_db, monkeypatch):
    """If the Gamma API itself returned nothing, don't accuse the oracles."""
    from backend import scheduler
    from backend.portfolio import recent_alerts
    from backend.db import set_kv

    async def empty():
        return _scan_result_factory(evaluated=0, oracleless=0, opportunities=[])

    monkeypatch.setattr(scheduler, "scan", empty)
    set_kv("oracle_warning_emitted", "false")

    await scheduler.scan_and_trade()

    alerts = recent_alerts(limit=10)
    titles = {a.title for a in alerts}
    assert "All oracles unavailable" not in titles, (
        "Don't blame oracles when there were no markets to evaluate."
    )
