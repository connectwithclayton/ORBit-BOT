"""CLI gates for launchd jobs: NYSE calendar, sync-audit skips, paper flatten.

Exit codes:
  0 — proceed with the guarded action (or stamp succeeded).
  1 — skip cleanly (cron/launcher should exit 0 and not fail).
  2 — unexpected error (launcher may surface non-zero).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from fabio_bot_paths import fabio_bot_root

from fabio_live.constants import MARKET_TIMEZONE
from fabio_live.us_equity_calendar import (
    get_nyse_session_schedule_et,
    is_nyse_trading_day,
)

_STAMP_FILENAME = ".sync_audit_calendar_eod_date"


def _nyse_today() -> datetime.date:
    tz = ZoneInfo(MARKET_TIMEZONE)
    return datetime.datetime.now(tz).date()


def _stamp_path() -> Path:
    return fabio_bot_root() / _STAMP_FILENAME


def stamp_sync_audit_eod_date(day: datetime.date | None = None) -> None:
    """Persist last calendar EOD audit date (Et date string)."""
    d = day or _nyse_today()
    _stamp_path().write_text(f"{d.isoformat()}\n", encoding="utf-8")


def _read_stamp_date() -> datetime.date | None:
    p = _stamp_path()
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip().split("\n")[0].strip()
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None


def _as_et(now_et: datetime.datetime | None = None) -> datetime.datetime:
    tz = ZoneInfo(MARKET_TIMEZONE)
    if now_et is None:
        return datetime.datetime.now(tz)
    if now_et.tzinfo is None:
        return now_et.replace(tzinfo=tz)
    return now_et.astimezone(tz)


def should_start_bot(now_et: datetime.datetime | None = None) -> bool:
    """True if orb_bot_fabio should start (today is an NYSE session day)."""
    today = _as_et(now_et).date()
    if not is_nyse_trading_day(today):
        return False
    return True


def should_run_failsafe(now_et: datetime.datetime | None = None) -> bool:
    """True if a paper flatten job should invoke the broker script today.

    Holidays / weekends skip in the wrapper (exit 0). The after-cutoff window
    is **not** applied here: ``--require-after-et`` on the flatten scripts
    writes ``aborted_window`` (exit 4) so a weekday 10:00 fire cannot flatten.
    """
    return should_start_bot(now_et)


def us_weekday_after_cutoff(
    now_et: datetime.datetime, hour: int, minute: int
) -> bool:
    """Legacy Mon–Fri + fixed ET clock cutoff (no XNYS calendar)."""
    now_et = _as_et(now_et)
    if now_et.weekday() >= 5:
        return False
    cutoff = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now_et >= cutoff


def xnys_failsafe_cutoff_ok(
    now_et: datetime.datetime,
) -> tuple[bool, str]:
    """After session_close - FAILSAFE_MINUTES on NYSE trading days; else explains abort.

    ``exchange_calendars`` is imported lazily inside ``_xnys_calendar``. Catch
    ImportError on that call path (not only the calendar_gate import) so
    ``--require-after-et`` can fall back to the legacy Mon–Fri cutoff.
    """
    now_et = _as_et(now_et)
    try:
        from datetime import timedelta

        from fabio_live.constants import FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES
        from fabio_live.us_equity_calendar import get_nyse_session_schedule_et

        day = now_et.date()
        sched = get_nyse_session_schedule_et(day)
    except ImportError as e:
        return False, f"xnys_calendar_unavailable:{e}"

    if sched is None:
        return False, "nyse_not_a_session_day"
    mins = max(0, int(FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES))
    cutoff = sched.session_close_et - timedelta(minutes=mins)
    if now_et >= cutoff:
        return True, "ok"
    return False, f"before_failsafe_cutoff want_>={cutoff.isoformat()}"


def should_run_sync_audit_intraday(now_et: datetime.datetime | None = None) -> bool:
    tz = ZoneInfo(MARKET_TIMEZONE)
    if now_et is None:
        now_et = datetime.datetime.now(tz)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=tz)
    else:
        now_et = now_et.astimezone(tz)

    day = now_et.date()
    if not is_nyse_trading_day(day):
        return False
    sched = get_nyse_session_schedule_et(day)
    if sched is None:
        return False
    return sched.session_open_et <= now_et <= sched.session_close_et


def should_run_sync_audit_eod(now_et: datetime.datetime | None = None) -> tuple[bool, str]:
    tz = ZoneInfo(MARKET_TIMEZONE)
    if now_et is None:
        now_et = datetime.datetime.now(tz)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=tz)
    else:
        now_et = now_et.astimezone(tz)

    day = now_et.date()
    if not is_nyse_trading_day(day):
        return False, "not_an_nyse_trading_day"

    sched = get_nyse_session_schedule_et(day)
    if sched is None:
        return False, "no_session_schedule"

    if now_et < sched.session_close_et:
        return False, "session_not_closed_yet"

    stamp = _read_stamp_date()
    if stamp == day:
        return False, "eod_audit_already_completed_today"

    return True, "ok"


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="NYSE calendar launcher gates.")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser(
        "should-start-bot",
        help="Exit 0 if orb_bot should start today; 1 if NYSE closed.",
    )
    b.add_argument("--json", action="store_true", help="Print reason JSON to stdout.")

    i = sub.add_parser(
        "should-run-sync-audit-intraday",
        help="Exit 0 inside regular session window; else 1.",
    )
    i.add_argument("--json", action="store_true")

    e = sub.add_parser(
        "should-run-sync-audit-eod",
        help="Exit 0 if post session close EOD audit should run; else 1.",
    )
    e.add_argument("--json", action="store_true")

    sub.add_parser(
        "stamp-sync-audit-eod",
        help="Record calendar EOD audit complete for Et today.",
    )

    f = sub.add_parser(
        "should-run-failsafe",
        help="Exit 0 on NYSE session days; 1 on holidays/weekends (flatten wrapper skip).",
    )
    f.add_argument("--json", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "should-start-bot":
        ok = should_start_bot()
        payload = {"proceed": ok, "reason": "nyse_closed" if not ok else "trading_day"}
        if getattr(args, "json", False):
            print(json.dumps(payload, separators=(",", ":")))
        return 0 if ok else 1

    if args.cmd == "should-run-sync-audit-intraday":
        ok = should_run_sync_audit_intraday()
        payload = {"proceed": ok, "reason": "outside_session_or_closed_day"}
        if getattr(args, "json", False):
            print(json.dumps(payload, separators=(",", ":")))
        return 0 if ok else 1

    if args.cmd == "should-run-sync-audit-eod":
        ok, reason = should_run_sync_audit_eod()
        payload = {"proceed": ok, "reason": reason}
        if getattr(args, "json", False):
            print(json.dumps(payload, separators=(",", ":")))
        return 0 if ok else 1

    if args.cmd == "stamp-sync-audit-eod":
        stamp_sync_audit_eod_date()
        return 0

    if args.cmd == "should-run-failsafe":
        ok = should_run_failsafe()
        payload = {"proceed": ok, "reason": "nyse_closed" if not ok else "trading_day"}
        if getattr(args, "json", False):
            print(json.dumps(payload, separators=(",", ":")))
        return 0 if ok else 1

    return 2


def main() -> int:
    try:
        return _main(sys.argv[1:])
    except Exception as e:
        print(f"calendar_gate error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
