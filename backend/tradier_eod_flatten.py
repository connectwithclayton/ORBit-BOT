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

Usage:
    PYTHONPATH=backend:frontend python3 backend/tradier_eod_flatten.py --dry-run
    PYTHONPATH=backend:frontend python3 backend/tradier_eod_flatten.py --scope options
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
    TRADIER_BOOK_IDS,
    default_flatten_book_id,
    flatten_symbol_filters,
    load_all_ledgers,
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
    return parser


def _log(
    msg: str,
    *,
    err: bool = False,
    event: str | None = None,
    symbol: str | None = None,
    decision: str | None = None,
    reason_code: str | None = None,
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

    enforce_tradier_paper_pin(args.env)  # fail-fast; client constructor pins again

    if client is None:
        from brokers.tradier.client import TradierPaperClient

        if args.account_id:
            os.environ["TRADIER_ACCOUNT_ID"] = args.account_id
        try:
            client = TradierPaperClient.from_env(env_override=args.env)
        except Exception as exc:
            _log(f"ERROR: {exc}", err=True, event="client_init_failed", reason_code="init")
            return 1

    book_id = getattr(args, "book", None) or BOOK_ORB_TRADIER
    only, exclude = flatten_symbol_filters(book_id, "tradier", load_all_ledgers())
    _log(
        f"Tradier flatten start book={book_id} env={args.env} "
        f"scope={args.scope} dry_run={args.dry_run}",
        event="run_start",
        extra={
            "book": book_id,
            "env": args.env,
            "scope": args.scope,
            "dry_run": args.dry_run,
        },
    )
    try:
        flatten_kwargs = dict(
            scope=args.scope,
            dry_run=args.dry_run,
            sleep_fn=time.sleep,
            sleep_between_orders=max(0.0, float(args.sleep_between_orders)),
        )
        try:
            summary = client.flatten_open_positions(
                **flatten_kwargs,
                only_symbols=only,
                exclude_symbols=exclude,
            )
        except TypeError:
            summary = client.flatten_open_positions(**flatten_kwargs)
    except Exception as exc:
        _log(f"ERROR: flatten failed: {exc}", err=True, event="flatten_error")
        return 1

    for row in summary.get("results") or []:
        _log(
            f"  {row.get('symbol')} qty={row.get('quantity')} side={row.get('side')} "
            f"status={row.get('status')}",
            event="flatten_row",
            symbol=str(row.get("symbol") or ""),
            decision="flatten",
            reason_code=str(row.get("status") or ""),
        )

    failures = int(summary.get("failures") or 0)
    planned = int(summary.get("planned") or 0)
    if args.dry_run:
        _log(
            f"Dry run: {planned} row(s) planned; no orders sent.",
            event="dry_run",
            decision="noop",
        )
        return 0
    if planned == 0:
        _log("No closable positions after filters.", event="no_closable")
        return 0
    if failures:
        _log(
            f"Exiting with code 3: {failures} place_order failure(s)",
            err=True,
            event="run_partial_failure",
            reason_code="place_order_failed",
            extra={"place_order_failures": failures},
        )
        return 3
    _log("Done.", event="run_complete", decision="flatten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
