from __future__ import annotations

import datetime

import pytest

from fabio_live.constants import EOD_CLOSE_BEFORE_SESSION_MINUTES
from fabio_live.us_equity_calendar import (
    get_nyse_session_schedule_et,
    is_nyse_trading_day,
)

from fabio_live.calendar_gate import (
    should_run_failsafe,
    should_run_sync_audit_eod,
    should_run_sync_audit_intraday,
    should_start_bot,
    xnys_failsafe_cutoff_ok,
)


def test_christmas_not_trading_day():
    xmas = datetime.date(2024, 12, 25)
    assert is_nyse_trading_day(xmas) is False


def test_defaults_primary_flatten_fifteen_minutes_like_legacy_full_day():
    assert EOD_CLOSE_BEFORE_SESSION_MINUTES == 15
    d = datetime.date(2025, 10, 1)
    sched = get_nyse_session_schedule_et(d)
    assert sched is not None
    assert sched.eod_force_flatten_et.strftime("%H:%M") == "15:45"


def test_black_friday_2024_early_close_session_bounds():
    d = datetime.date(2024, 11, 29)
    assert is_nyse_trading_day(d) is True
    sched = get_nyse_session_schedule_et(
        d,
        eod_before_close_minutes=10,
        summary_after_close_minutes=1,
        signal_end_buffer_before_close_minutes=5,
        opening_range_duration_minutes=15,
        signal_end_hour=14,
    )
    assert sched is not None
    assert sched.session_open_et.hour == 9 and sched.session_open_et.minute == 30
    assert sched.session_close_et.hour == 13 and sched.session_close_et.minute == 0
    assert sched.eod_force_flatten_et.strftime("%H:%M") == "12:50"
    assert sched.effective_signal_end_et <= sched.session_close_et
    assert sched.or_end_et == sched.session_open_et + datetime.timedelta(minutes=15)


def test_regular_day_2025_has_sixteen_hundred_close():
    d = datetime.date(2025, 10, 1)
    sched = get_nyse_session_schedule_et(d)
    assert sched is not None
    assert sched.session_close_et.hour == 16 and sched.session_close_et.minute == 0


def test_calendar_gate_eod_blocked_before_close(monkeypatch):
    tz = pytest.importorskip("zoneinfo").ZoneInfo("America/New_York")
    monkeypatch.setattr(
        "fabio_live.calendar_gate._nyse_today",
        lambda: datetime.date(2025, 10, 1),
    )

    proceed, reason = should_run_sync_audit_eod(
        datetime.datetime(2025, 10, 1, 14, 0, 0, tzinfo=tz)
    )
    assert proceed is False
    assert reason == "session_not_closed_yet"


def test_calendar_gate_intraday_mid_session():
    tz = pytest.importorskip("zoneinfo").ZoneInfo("America/New_York")
    d = datetime.date(2025, 10, 1)
    assert is_nyse_trading_day(d) is True
    now = datetime.datetime(2025, 10, 1, 12, 0, 0, tzinfo=tz)
    assert should_run_sync_audit_intraday(now) is True


def test_should_start_bot_regular_vs_holiday():
    tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    assert should_start_bot(datetime.datetime(2024, 12, 25, 10, 0, 0, tzinfo=tz)) is False
    assert should_start_bot(datetime.datetime(2025, 10, 1, 10, 0, 0, tzinfo=tz)) is True


def test_should_run_failsafe_matches_session_day_not_cutoff():
    tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    holiday_morning = datetime.datetime(2024, 12, 25, 10, 0, 0, tzinfo=tz)
    session_morning = datetime.datetime(2025, 10, 1, 10, 0, 0, tzinfo=tz)
    assert should_run_failsafe(holiday_morning) is False
    assert should_run_failsafe(session_morning) is True
    assert should_run_failsafe(session_morning) == should_start_bot(session_morning)


def test_xnys_failsafe_cutoff_holiday_and_before_window():
    tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    ok, detail = xnys_failsafe_cutoff_ok(
        datetime.datetime(2024, 12, 25, 15, 50, 0, tzinfo=tz)
    )
    assert ok is False
    assert detail == "nyse_not_a_session_day"
    ok, detail = xnys_failsafe_cutoff_ok(
        datetime.datetime(2025, 10, 1, 10, 0, 0, tzinfo=tz)
    )
    assert ok is False
    assert "before_failsafe_cutoff" in detail
    ok, detail = xnys_failsafe_cutoff_ok(
        datetime.datetime(2025, 10, 1, 15, 50, 0, tzinfo=tz)
    )
    assert ok is True
    assert detail == "ok"


def test_xnys_failsafe_cutoff_maps_lazy_exchange_calendars_import_error(monkeypatch):
    """Missing exchange_calendars at _xnys_calendar() must not hard-fail require-after-et."""

    def missing_xcals():
        raise ImportError("No module named 'exchange_calendars'")

    monkeypatch.setattr(
        "fabio_live.us_equity_calendar._xnys_calendar",
        missing_xcals,
    )
    tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    ok, detail = xnys_failsafe_cutoff_ok(
        datetime.datetime(2025, 10, 1, 15, 50, 0, tzinfo=tz)
    )
    assert ok is False
    assert detail.startswith("xnys_calendar_unavailable:")
    assert "exchange_calendars" in detail
