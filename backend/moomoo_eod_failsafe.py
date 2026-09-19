#!/usr/bin/env python3
"""
Post–EOD-close fail-safe for Moomoo OpenAPI (OpenD).

If anything is still open after the primary bot's scheduled flatten and near the end of the
regular session, this script pulls *live* positions from the broker (entire account, not a
strategy session), then submits market orders to flatten matching rows.

**Non-interference:** Schedule this job **after** the primary bot's EOD flatten (legacy ~3:45 PM ET
on full sessions) and **after** the fail-safe eligibility window — typically ~10 minutes before
official **XNYS session close** (``FABIO_FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES``, default ``10``).
On early-close days the window moves with the exchange calendar (e.g. ~12:50 PM ET before a 1:00 PM
close). Use ``--require-after-et`` so accidental daytime runs do not fire; it compares America/New_York
time to ``session_close_et`` from ``exchange_calendars`` minus the fail-safe offset (unless
``--legacy-fixed-cutoff-et`` is set).

Prerequisites:
  - OpenD running and logged in (same machine or reachable host/port).
  - pip install -r backend/requirements-moomoo.txt (from Fabio_bot root)
  - For live: MOOMOO_TRADE_PASSWORD (or --password) to unlock trading once per run.

Default scope is US-style *listed options* only (OCC-style symbol with yymmdd + C/P).
Use ``--scope all`` only if you intend to liquidate stocks and everything else with can_sell_qty > 0.

This closes in the market only (sell longs / buy shorts). It does not exercise options.

**Paper pin:** ``--trd-env`` defaults to ``MOOMOO_TRADE_ENV``, else legacy ``MOOMOO_TRD_ENV``,
else **SIMULATE** (never silently REAL). REAL also requires ``FABIO_ALLOW_REAL_TRADING=1``.

**Idempotency:** By default, re-queries positions (``refresh_cache=True``) before each
``place_order``. Each order gets a traceable ``remark`` (``eod_fs_<run>_<seq>[_code]``).
Logs lines are UTC ISO timestamps. Use ``--no-refresh-per-order`` only if you accept
stale qty between sibling closes.

Use ``--log-format jsonl`` for append-only JSON Lines (``component``, ``event``, ``symbol``,
``decision``, ``reason_code``, ``latency_ms``) for jq or log tooling.

**API discipline:** Prefer default ``--scope options`` for emergency flatten; use ``--scope all``
only when intentional. Space orders with ``--sleep-between-orders`` (or env
``MOOMOO_SLEEP_BETWEEN_ORDERS``). Per-order position refresh trades extra reads for safety;
``--no-refresh-per-order`` reduces API load if you accept stale qty between closes.

**Exit codes:**

- ``0`` — Success: empty book, nothing closable, dry-run only, or live run finished with **no**
  ``place_order`` failures.
- ``1`` — Error: missing dependency, invalid ``--require-after-et`` when ``zoneinfo`` unavailable,
  missing live password, ``unlock_trade`` failed, initial ``position_list_query`` failed, or
  unknown ``--security-firm``.
- ``2`` — Reserved: **invalid CLI** (Python ``argparse`` convention on unknown flags / bad values).
- ``3`` — Live run finished but **at least one** ``place_order`` returned non-OK; inspect logs / JSONL.
- ``4`` — Aborted: ``--require-after-et`` and current ET is outside the allowed window (XNYS
  calendar fail-safe slot or legacy weekday cutoff).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    from moomoo import (
        OpenSecTradeContext,
        OrderType,
        RET_OK,
        SecurityFirm,
        TrdEnv,
        TrdMarket,
        TrdSide,
    )
except ImportError:
    OpenSecTradeContext = None  # type: ignore

from paper_pin import enforce_paper_trading_pin, resolve_moomoo_trd_env_name

COMPONENT = "moomoo_eod_failsafe"
_LOG_CFG = {"format": "human"}

# US OCC-style option: underlying + 6-digit expiry + C or P + strike digits
_US_OPTION_CODE = re.compile(r"^US\.[A-Z.]{1,10}\d{6}[CP]\d{3,12}$", re.IGNORECASE)


def _looks_like_us_listed_option(code: str) -> bool:
    s = (code or "").strip()
    return bool(_US_OPTION_CODE.match(s))


def _closing_side(position_side: str):
    ps = (position_side or "").upper()
    if ps == "SHORT":
        return TrdSide.BUY
    return TrdSide.SELL


def _parse_security_firm(name: str):
    n = (name or "").strip().upper()
    mapping = {
        "FUTUINC": SecurityFirm.FUTUINC,
        "FUTUSECURITIES": SecurityFirm.FUTUSECURITIES,
        "FUTUSG": SecurityFirm.FUTUSG,
        "FUTUAU": SecurityFirm.FUTUAU,
        "FUTUCA": SecurityFirm.FUTUCA,
        "FUTUMY": SecurityFirm.FUTUMY,
        "FUTUJP": SecurityFirm.FUTUJP,
    }
    if n not in mapping:
        raise SystemExit(
            f"Unknown security firm {name!r}. Use one of: {', '.join(sorted(mapping))}"
        )
    return mapping[n]


def _log(
    msg: str,
    *,
    err: bool = False,
    event: str | None = None,
    symbol: str | None = None,
    decision: str | None = None,
    reason_code: str | None = None,
    latency_ms: float | None = None,
    **extra,
) -> None:
    if _LOG_CFG.get("format") == "jsonl":
        obj: dict = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "component": COMPONENT,
            "event": event or "info",
            "message": msg,
        }
        if symbol is not None:
            obj["symbol"] = symbol
        if decision is not None:
            obj["decision"] = decision
        if reason_code is not None:
            obj["reason_code"] = reason_code
        if latency_ms is not None:
            obj["latency_ms"] = round(latency_ms, 3)
        obj["level"] = "error" if err else "info"
        if extra:
            obj["extra"] = extra
        line = json.dumps(obj, default=str, ensure_ascii=False) + "\n"
        sys.stdout.write(line)
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {msg}\n"
    (sys.stderr if err else sys.stdout).write(line)


def _extract_closable_rows(pos, scope: str) -> list[tuple]:
    rows: list[tuple] = []
    if pos is None or getattr(pos, "empty", True):
        return rows
    for _, row in pos.iterrows():
        code = str(row.get("code", "")).strip()
        if scope == "options" and not _looks_like_us_listed_option(code):
            continue
        can_sell = float(row.get("can_sell_qty", 0) or 0)
        if can_sell <= 0:
            continue
        rows.append((code, can_sell, str(row.get("position_side", "LONG"))))
    return rows


def _order_remark(run_id: str, seq: int, code: str, max_bytes: int = 64) -> str:
    base = f"eod_fs_{run_id}_{seq:03d}_{code}"
    if len(base.encode("utf-8")) <= max_bytes:
        return base
    short = f"eod_fs_{run_id}_{seq:03d}"
    enc = short.encode("utf-8")
    if len(enc) <= max_bytes:
        return short
    return enc[:max_bytes].decode("utf-8", errors="ignore")


def _us_weekday_after_cutoff(now_et: datetime, hour: int, minute: int) -> bool:
    if now_et.weekday() >= 5:
        return False
    cutoff = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now_et >= cutoff


def _load_env_file() -> None:
    """Match live bot env resolution so MOOMOO_TRADE_ENV from .env is visible."""
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


def default_trd_env_choice() -> str:
    """CLI default: SIMULATE unless MOOMOO_TRADE_ENV / legacy MOOMOO_TRD_ENV is set."""
    return resolve_moomoo_trd_env_name()


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser. --trd-env default is resolved at call time from env."""
    parser = argparse.ArgumentParser(description="Moomoo broker fail-safe: flatten open positions.")
    parser.add_argument("--host", default=os.environ.get("MOOMOO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOOMOO_PORT", "11111")))
    parser.add_argument(
        "--security-firm",
        default=os.environ.get("MOOMOO_SECURITY_FIRM", "FUTUINC"),
        help="Moomoo entity, e.g. FUTUINC (US), FUTUSECURITIES (HK), …",
    )
    parser.add_argument(
        "--trd-env",
        choices=("REAL", "SIMULATE"),
        default=default_trd_env_choice(),
        help=(
            "Moomoo trade environment. Default: %(default)s "
            "(MOOMOO_TRADE_ENV, else legacy MOOMOO_TRD_ENV, else SIMULATE). "
            "REAL requires FABIO_ALLOW_REAL_TRADING=1; paper-only otherwise."
        ),
    )
    parser.add_argument(
        "--acc-id",
        type=int,
        default=int(os.environ["MOOMOO_ACC_ID"]) if os.environ.get("MOOMOO_ACC_ID") else 0,
        help="0 = default account from OpenD",
    )
    parser.add_argument(
        "--scope",
        choices=("options", "all"),
        default="options",
        help="options: US listed-options only (safer default); all: all closable positions (explicit widen)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions only; no unlock and no orders",
    )
    parser.add_argument(
        "--require-after-et",
        action="store_true",
        help="Abort unless US/Eastern weekday and local ET clock is after --cutoff-et (fail-safe guard)",
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
    parser.add_argument(
        "--password",
        default=os.environ.get("MOOMOO_TRADE_PASSWORD", ""),
        help="Trading password for unlock_trade (or set MOOMOO_TRADE_PASSWORD)",
    )
    parser.add_argument(
        "--sleep-between-orders",
        type=float,
        default=float(os.environ.get("MOOMOO_SLEEP_BETWEEN_ORDERS", "0.35")),
        help="Seconds between place_order calls (broker/API pacing; env MOOMOO_SLEEP_BETWEEN_ORDERS)",
    )
    parser.add_argument(
        "--no-refresh-per-order",
        action="store_true",
        help="Single position_list_query before all closes (fewer API reads; weaker vs partial fills)",
    )
    parser.add_argument(
        "--log-format",
        choices=("human", "jsonl"),
        default=os.environ.get("MOOMOO_LOG_FORMAT", "human"),
        help="human: timestamped text; jsonl: one JSON object per line on stdout",
    )
    return parser


def _xnys_failsafe_cutoff_ok(now_et: datetime) -> tuple[bool, str]:
    """After session_close - FAILSAFE_MINUTES on NYSE trading days; else explains abort."""
    try:
        from datetime import timedelta

        from fabio_live.constants import FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES
        from fabio_live.us_equity_calendar import get_nyse_session_schedule_et
    except ImportError as e:
        return False, f"xnys_calendar_unavailable:{e}"

    day = now_et.date()
    sched = get_nyse_session_schedule_et(day)
    if sched is None:
        return False, "nyse_not_a_session_day"
    mins = max(0, int(FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES))
    cutoff = sched.session_close_et - timedelta(minutes=mins)
    if now_et >= cutoff:
        return True, "ok"
    return False, f"before_failsafe_cutoff want_>={cutoff.isoformat()}"


def main() -> int:
    _load_env_file()
    parser = build_arg_parser()
    args = parser.parse_args()
    _LOG_CFG["format"] = args.log_format
    enforce_paper_trading_pin(args.trd_env)

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None  # type: ignore

    if args.require_after_et:
        if ZoneInfo is None:
            print("ERROR: zoneinfo not available; use Python 3.9+.", file=sys.stderr)
            return 1
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
                return 4
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
                        return 4
                else:
                    print(
                        f"Abort: --require-after-et ({detail}) "
                        f"now_et={now_et.isoformat()}.",
                        file=sys.stderr,
                    )
                    return 4

    if OpenSecTradeContext is None:
        print(
            "ERROR: moomoo package not installed. pip install -r backend/requirements-moomoo.txt",
            file=sys.stderr,
        )
        return 1

    trd_env = TrdEnv.REAL if args.trd_env == "REAL" else TrdEnv.SIMULATE
    sec_firm = _parse_security_firm(args.security_firm)

    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.NONE,
        host=args.host,
        port=args.port,
        security_firm=sec_firm,
    )

    try:
        if trd_env == TrdEnv.REAL and not args.dry_run:
            if not args.password:
                print(
                    "ERROR: live trading needs --password or MOOMOO_TRADE_PASSWORD for unlock_trade.",
                    file=sys.stderr,
                )
                return 1
            ret, msg = trd_ctx.unlock_trade(password=args.password)
            if ret != RET_OK:
                print(f"unlock_trade failed: {msg}", file=sys.stderr)
                return 1

        t0 = time.perf_counter()
        ret, pos = trd_ctx.position_list_query(
            trd_env=trd_env,
            acc_id=args.acc_id,
            refresh_cache=True,
        )
        query_ms = (time.perf_counter() - t0) * 1000
        if ret != RET_OK:
            _log(
                f"position_list_query failed: {pos}",
                err=True,
                event="position_query_error",
                reason_code="initial_query_failed",
                latency_ms=query_ms,
                extra={"broker_msg": str(pos)},
            )
            return 1

        if pos is None or getattr(pos, "empty", True):
            _log(
                "No rows returned from broker (empty book).",
                event="book_empty",
                latency_ms=query_ms,
            )
            return 0

        rows = _extract_closable_rows(pos, args.scope)

        if not rows:
            _log(
                "No closable positions after filters (refresh_cache=True).",
                event="no_closable",
                latency_ms=query_ms,
                extra={"scope": args.scope},
            )
            return 0

        _log(
            f"Found {len(rows)} position(s) to flatten ({args.scope}, trd_env={args.trd_env}):",
            event="closable_list",
            decision="flatten",
            latency_ms=query_ms,
            extra={"count": len(rows), "scope": args.scope, "trd_env": args.trd_env},
        )
        for code, qty, side in rows:
            _log(
                f"  {code} qty={qty} position_side={side} -> {_closing_side(side)}",
                event="closable_row",
                symbol=code,
                decision="flatten",
                extra={
                    "qty": qty,
                    "position_side": side,
                    "trd_side": str(_closing_side(side)),
                },
            )

        if args.dry_run:
            _log("Dry run: no orders sent.", event="dry_run", decision="noop")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"_{os.getpid()}"
        order_seq = 0
        place_order_failures = 0

        if args.no_refresh_per_order:
            closable_iter = list(rows)
        else:
            closable_iter = [(c, None, None) for c in [r[0] for r in rows]]

        for item in closable_iter:
            if args.no_refresh_per_order:
                code, qty, pos_side = item
            else:
                code = item[0]
                tq0 = time.perf_counter()
                ret, pos = trd_ctx.position_list_query(
                    trd_env=trd_env,
                    acc_id=args.acc_id,
                    refresh_cache=True,
                )
                per_q_ms = (time.perf_counter() - tq0) * 1000
                if ret != RET_OK:
                    _log(
                        f"position_list_query failed before {code}: {pos}",
                        err=True,
                        event="position_query_error",
                        symbol=code,
                        reason_code="refresh_before_close_failed",
                        latency_ms=per_q_ms,
                        extra={"broker_msg": str(pos)},
                    )
                    continue
                fresh = _extract_closable_rows(pos, args.scope)
                match = next((x for x in fresh if x[0] == code), None)
                if not match:
                    _log(
                        f"skip code={code} reason=no_closable_qty_after_refresh "
                        "(already flat or filtered)",
                        event="skip_close",
                        symbol=code,
                        decision="flatten",
                        reason_code="no_closable_after_refresh",
                        latency_ms=per_q_ms,
                    )
                    continue
                code, qty, pos_side = match

            qty_int = int(qty)
            if qty_int <= 0:
                _log(
                    f"skip code={code} reason=qty_zero",
                    event="skip_close",
                    symbol=code,
                    decision="flatten",
                    reason_code="qty_zero",
                )
                continue

            side = _closing_side(pos_side)
            order_seq += 1
            remark = _order_remark(run_id, order_seq, code)
            _log(
                f"place_order begin code={code} trd_side={side} qty={qty_int} remark={remark!r}",
                event="place_order_begin",
                symbol=code,
                decision="flatten",
                extra={
                    "qty": qty_int,
                    "trd_side": str(side),
                    "remark": remark,
                    "order_seq": order_seq,
                },
            )
            tpo0 = time.perf_counter()
            ret, data = trd_ctx.place_order(
                price=0,
                qty=qty_int,
                code=code,
                trd_side=side,
                order_type=OrderType.MARKET,
                trd_env=trd_env,
                acc_id=args.acc_id,
                remark=remark,
            )
            po_ms = (time.perf_counter() - tpo0) * 1000
            if ret != RET_OK:
                _log(
                    f"place_order end code={code} FAILED ret={ret} data={data!r}",
                    err=True,
                    event="place_order_end",
                    symbol=code,
                    decision="flatten",
                    reason_code="place_order_failed",
                    latency_ms=po_ms,
                    extra={"ret": ret, "data": str(data)},
                )
                place_order_failures += 1
            else:
                _log(
                    f"place_order end code={code} OK ret={ret} data={data!r}",
                    event="place_order_end",
                    symbol=code,
                    decision="flatten",
                    reason_code="ok",
                    latency_ms=po_ms,
                    extra={"ret": ret, "data": str(data)},
                )
            time.sleep(max(0.0, args.sleep_between_orders))

        _log("Done.", event="run_complete", decision="flatten")
        if place_order_failures:
            _log(
                f"Exiting with code 3: {place_order_failures} place_order failure(s)",
                event="run_partial_failure",
                err=True,
                reason_code="place_order_failed",
                extra={"place_order_failures": place_order_failures},
            )
            return 3
        return 0
    finally:
        trd_ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
