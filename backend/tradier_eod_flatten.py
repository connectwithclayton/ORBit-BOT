#!/usr/bin/env python3
"""
Tradier PAPER flatten — separate from Moomoo ``moomoo_eod_failsafe.py``.

Market-closes open positions on **one** Tradier paper book (``--book``,
default ``orb-tradier``). Never calls Moomoo OpenD. Never flattens the
MR-Tradier book unless ``--book mr-tradier``. Never exercises options.
Default env is **paper** (sandbox).

Live ``api.tradier.com`` / ``--env live`` is refused unless
``FABIO_ALLOW_REAL_TRADING=1`` (same allow flag as the Moomoo paper pin).
Does not read ``MOOMOO_TRADE_ENV``.

``--require-after-et`` mirrors Moomoo: abort (exit 4, sidecar
``aborted_window``) unless the XNYS fail-safe window has opened, so a 10:00
launchd fire cannot flatten. Scheduled jobs always pass this flag.

Usage:
    PYTHONPATH=backend:frontend python3 backend/tradier_eod_flatten.py --dry-run --book orb-tradier
    PYTHONPATH=backend:frontend python3 backend/tradier_eod_flatten.py --scope options --book orb-tradier --require-after-et
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from fabio_live.paper_books import (
    BOOK_ORB_TRADIER,
    BROKER_TRADIER,
    REASON_ABORTED_WINDOW,
    REASON_ERROR,
    REASON_FLATTEN,
    REASON_NO_CLOSABLE,
    TRADIER_BOOK_IDS,
    default_flatten_book_id,
    flatten_symbol_filters,
    ledger_directory,
    load_all_ledgers,
    utc_now_iso,
    write_failsafe_last_flatten,
)
from paper_pin import ALLOW_REAL_ENV, TRADIER_ENV_NAME, enforce_tradier_paper_pin

COMPONENT = "tradier_eod_flatten"
_LOG_CFG = {"format": "human"}


def _load_env_file() -> None:
    try:
        from dotenv import load_dotenv

        from fabio_bot_paths import fabio_bot_root
    except ImportError:
        return
    override = os.getenv("FABIO_ENV_FILE", "").strip()
    if override:
        load_dotenv(override)
    else:
        load_dotenv(fabio_bot_root() / ".env")


def default_tradier_env_choice() -> str:
    """CLI default is always paper — never silently live, even if TRADIER_ENV=live."""
    return "paper"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Tradier paper flatten: market-close one Tradier paper book. "
            "Separate from moomoo_eod_failsafe.py. Does not exercise options. "
            "Does not flatten Moomoo or the other Tradier strategy book."
        )
    )
    parser.add_argument(
        "--book",
        choices=TRADIER_BOOK_IDS,
        default=default_flatten_book_id("tradier"),
        help=(
            "Which Tradier paper book to flatten (default: %(default)s). "
            "orb-tradier and mr-tradier are separate; this never calls Moomoo."
        ),
    )
    parser.add_argument(
        "--env",
        choices=("paper", "live"),
        default=default_tradier_env_choice(),
        help=(
            "Tradier environment. Default: %(default)s (sandbox). "
            f"live requires {ALLOW_REAL_ENV}=1; paper-only otherwise. "
            f"Independent of MOOMOO_TRADE_ENV (optional {TRADIER_ENV_NAME}=paper)."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("options", "all"),
        default="options",
        help="options: OCC listed-options only (safer default); all: equities too",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned closes only; no place_order",
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="Override TRADIER_ACCOUNT_ID",
    )
    parser.add_argument(
        "--sleep-between-orders",
        type=float,
        default=float(os.environ.get("TRADIER_SLEEP_BETWEEN_ORDERS", "0.35")),
        help="Seconds between place_order calls",
    )
    parser.add_argument(
        "--log-format",
        choices=("human", "jsonl"),
        default=os.environ.get("TRADIER_LOG_FORMAT", "human"),
        help="human: timestamped text; jsonl: one JSON object per line on stdout",
    )
    parser.add_argument(
        "--require-after-et",
        action="store_true",
        help="Abort unless US/Eastern weekday and local ET clock is after fail-safe cutoff",
    )
    parser.add_argument(
        "--cutoff-et-hour",
        type=int,
        default=15,
        help="Hour on US/Eastern clock for --require-after-et (default 15 = 3 PM)",
    )
    parser.add_argument(
        "--cutoff-et-minute",
        type=int,
        default=45,
        help="Minute for legacy fixed cutoff (--legacy-fixed-cutoff-et only)",
    )
    parser.add_argument(
        "--legacy-fixed-cutoff-et",
        action="store_true",
        help=(
            "Use Mon–Fri + fixed --cutoff-et-hour/minute for --require-after-et "
            "(no XNYS calendar). Default is calendar-based fail-safe window."
        ),
    )
    return parser


def _us_weekday_after_cutoff(now_et: datetime, hour: int, minute: int) -> bool:
    from fabio_live.calendar_gate import us_weekday_after_cutoff

    return us_weekday_after_cutoff(now_et, hour, minute)


def _xnys_failsafe_cutoff_ok(now_et: datetime) -> tuple[bool, str]:
    from fabio_live.calendar_gate import xnys_failsafe_cutoff_ok

    return xnys_failsafe_cutoff_ok(now_et)


def _log(
    msg: str,
    *,
    err: bool = False,
    event: str | None = None,
    symbol: str | None = None,
    decision: str | None = None,
    reason_code: str | None = None,
    book_id: str | None = None,
    **extra: Any,
) -> None:
    if _LOG_CFG.get("format") == "jsonl":
        obj: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "component": COMPONENT,
            "event": event or "info",
            "message": msg,
            "level": "error" if err else "info",
        }
        if symbol is not None:
            obj["symbol"] = symbol
        if decision is not None:
            obj["decision"] = decision
        if reason_code is not None:
            obj["reason_code"] = reason_code
        if book_id is not None:
            obj["book_id"] = book_id
        if extra:
            obj["extra"] = extra
        sys.stdout.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {msg}\n"
    (sys.stderr if err else sys.stdout).write(line)


def main(argv: list[str] | None = None, *, client=None) -> int:
    _load_env_file()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _LOG_CFG["format"] = args.log_format
    book_id = getattr(args, "book", None) or BOOK_ORB_TRADIER
    started = utc_now_iso()
    dry_run = bool(args.dry_run)
    sidecar_dir = ledger_directory()
    state: dict[str, Any] = {
        "reason": REASON_ERROR,
        "exit_code": 1,
        "planned": 0,
        "failures": 0,
        "selected_codes_count": 0,
    }

    def _finish(
        code: int,
        *,
        reason: str,
        planned: int | None = None,
        failures: int | None = None,
        selected: int | None = None,
    ) -> int:
        state["reason"] = reason
        state["exit_code"] = int(code)
        if planned is not None:
            state["planned"] = int(planned)
        if failures is not None:
            state["failures"] = int(failures)
        if selected is not None:
            state["selected_codes_count"] = int(selected)
        return code

    try:
        enforce_tradier_paper_pin(args.env)  # fail-fast; client constructor pins again

        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            ZoneInfo = None  # type: ignore

        if args.require_after_et:
            if ZoneInfo is None:
                print("ERROR: zoneinfo not available; use Python 3.9+.", file=sys.stderr)
                return _finish(1, reason=REASON_ERROR)
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if args.legacy_fixed_cutoff_et:
                ok = _us_weekday_after_cutoff(
                    now_et, args.cutoff_et_hour, args.cutoff_et_minute
                )
                if not ok:
                    print(
                        f"Abort: --require-after-et legacy cutoff "
                        f"now_et={now_et.isoformat()} "
                        f"not weekday after "
                        f"{args.cutoff_et_hour:02d}:{args.cutoff_et_minute:02d} ET.",
                        file=sys.stderr,
                    )
                    return _finish(4, reason=REASON_ABORTED_WINDOW)
            else:
                ok, detail = _xnys_failsafe_cutoff_ok(now_et)
                if not ok:
                    if detail.startswith("xnys_calendar_unavailable"):
                        ok2 = _us_weekday_after_cutoff(
                            now_et, args.cutoff_et_hour, args.cutoff_et_minute
                        )
                        if ok2:
                            print(
                                "WARN: XNYS calendar unavailable; using legacy "
                                f"{args.cutoff_et_hour:02d}:{args.cutoff_et_minute:02d} ET cutoff.",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"Abort: --require-after-et {detail}; legacy cutoff also blocked.",
                                file=sys.stderr,
                            )
                            return _finish(4, reason=REASON_ABORTED_WINDOW)
                    else:
                        print(
                            f"Abort: --require-after-et ({detail}) "
                            f"now_et={now_et.isoformat()}.",
                            file=sys.stderr,
                        )
                        return _finish(4, reason=REASON_ABORTED_WINDOW)

        if client is None:
            from brokers.tradier.client import TradierPaperClient

            if args.account_id:
                os.environ["TRADIER_ACCOUNT_ID"] = args.account_id
            try:
                client = TradierPaperClient.from_env(env_override=args.env)
            except Exception as exc:
                _log(
                    f"ERROR: {exc}",
                    err=True,
                    event="client_init_failed",
                    reason_code="init",
                    book_id=book_id,
                    extra={"book": book_id, "book_id": book_id},
                )
                return _finish(1, reason=REASON_ERROR)

        only, exclude = flatten_symbol_filters(
            book_id, BROKER_TRADIER, load_all_ledgers()
        )
        _log(
            f"Tradier flatten start book={book_id} env={args.env} "
            f"scope={args.scope} dry_run={args.dry_run}",
            event="run_start",
            book_id=book_id,
            extra={
                "book": book_id,
                "book_id": book_id,
                "env": args.env,
                "scope": args.scope,
                "dry_run": args.dry_run,
            },
        )
        try:
            summary = client.flatten_open_positions(
                scope=args.scope,
                dry_run=args.dry_run,
                sleep_fn=time.sleep,
                sleep_between_orders=max(0.0, float(args.sleep_between_orders)),
                only_symbols=only,
                exclude_symbols=exclude,
            )
        except TypeError as exc:
            _log(
                f"ERROR: flatten client rejected book filters; refuse unfiltered "
                f"account flatten: {exc}",
                err=True,
                event="flatten_filters_required",
                reason_code="book_filters_required",
                book_id=book_id,
                extra={"book": book_id, "book_id": book_id},
            )
            return _finish(1, reason=REASON_ERROR)
        except Exception as exc:
            _log(
                f"ERROR: flatten failed: {exc}",
                err=True,
                event="flatten_error",
                book_id=book_id,
                extra={"book": book_id, "book_id": book_id},
            )
            return _finish(1, reason=REASON_ERROR)

        for row in summary.get("results") or []:
            _log(
                f"  {row.get('symbol')} qty={row.get('quantity')} side={row.get('side')} "
                f"status={row.get('status')}",
                event="flatten_row",
                symbol=str(row.get("symbol") or ""),
                decision="flatten",
                reason_code=str(row.get("status") or ""),
                book_id=book_id,
            )

        failures = int(summary.get("failures") or 0)
        planned = int(summary.get("planned") or 0)
        selected_n = planned
        if planned == 0:
            _log(
                "No closable positions after filters.",
                event="no_closable",
                book_id=book_id,
                extra={"book": book_id, "book_id": book_id},
            )
            if args.dry_run:
                _log(
                    f"Dry run: {planned} row(s) planned; no orders sent.",
                    event="dry_run",
                    decision="noop",
                    book_id=book_id,
                    extra={"book": book_id, "book_id": book_id},
                )
            return _finish(
                0,
                reason=REASON_NO_CLOSABLE,
                planned=0,
                failures=0,
                selected=0,
            )
        if args.dry_run:
            _log(
                f"Dry run: {planned} row(s) planned; no orders sent.",
                event="dry_run",
                decision="noop",
                book_id=book_id,
                extra={"book": book_id, "book_id": book_id},
            )
            return _finish(
                0,
                reason=REASON_FLATTEN,
                planned=planned,
                failures=0,
                selected=selected_n,
            )
        if failures:
            _log(
                f"Exiting with code 3: {failures} place_order failure(s)",
                err=True,
                event="run_partial_failure",
                reason_code="place_order_failed",
                book_id=book_id,
                extra={"place_order_failures": failures, "book": book_id, "book_id": book_id},
            )
            return _finish(
                3,
                reason=REASON_FLATTEN,
                planned=planned,
                failures=failures,
                selected=selected_n,
            )
        _log(
            "Done.",
            event="run_complete",
            decision="flatten",
            book_id=book_id,
            extra={"book": book_id, "book_id": book_id},
        )
        return _finish(
            0,
            reason=REASON_FLATTEN,
            planned=planned,
            failures=0,
            selected=selected_n,
        )
    except Exception:
        _finish(1, reason=REASON_ERROR)
        raise
    finally:
        write_failsafe_last_flatten(
            book_id=book_id,
            started_at_utc=started,
            finished_at_utc=utc_now_iso(),
            dry_run=dry_run,
            exit_code=int(state["exit_code"]),
            planned=int(state["planned"]),
            failures=int(state["failures"]),
            selected_codes_count=int(state["selected_codes_count"]),
            reason=str(state["reason"]),
            directory=sidecar_dir,
        )


if __name__ == "__main__":
    raise SystemExit(main())
