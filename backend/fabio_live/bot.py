"""Main scheduling loop for the Fabio ORB live bot."""

from __future__ import annotations

import datetime
import json
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import telegram_bot as tg
from dashboard_writer import (
    DashboardWriter,
    aggregate_closed_positions,
    moomoo_position_records_to_dashboard_opens,
)
from exit_reasons import (
    REASON_SOURCE_STRATEGY,
    canonical_exit_reason,
)
from moomoo import KLType, OpenQuoteContext, OpenSecTradeContext, SecurityFirm, TrdEnv, TrdMarket
from sheets_logger import SheetsLogger

from config import MOOMOO_HOST, MOOMOO_PORT, modeled_equity_annotation_suffix
from paper_pin import enforce_paper_trading_pin
from fabio_live.async_ops import AsyncOpsWorker
from fabio_live.circuit import RiskCircuitBreaker
from fabio_live.constants import (
    AUTO_ADOPT_OPEN_POSITIONS,
    ATR_HARD_STOP_MULT,
    CB_MAX_TRADES,
    ENTRY_SIGNAL_MAX_AGE_MIN,
    ENTRY_STALE_BLOCK_SEC,
    HEALTH_SNAPSHOT_INTERVAL_SEC,
    HEALTH_SNAPSHOT_PATH,
    HEALTH_SNAPSHOT_RETENTION_DAYS,
    MAIN_LOOP_SLEEP_ACTIVE_SEC,
    MARKET_TIMEZONE,
    OPS_ALERT_COOLDOWN_SEC,
    OPS_DASHBOARD_OPEN_REFRESH_THROTTLE_SEC,
    OPS_DASHBOARD_REFRESH_THROTTLE_SEC,
    OPS_QUEUE_WARN_THRESHOLD,
    PAPER_TRADING,
    PROFIT_LOCK_MULTIPLE,
    RESEARCH_RISK_CAP_MULTIPLIER,
    RISK_PCT_MAX,
    STRATEGY_CAPITAL,
    SYMBOLS,
    VIX_AGGRESSIVE_MAX,
    VIX_HALF_MAX,
    VIX_NORMAL_MAX,
)
from fabio_live.market_data import (
    candle_age_seconds,
    get_candles,
    get_candles_fresh,
    get_portfolio_value,
    get_vix,
)
from fabio_live.orders import OrderManager
from fabio_live.regime import MarketRegime
from fabio_live.signals import SignalEngine
from fabio_live.us_equity_calendar import get_session_schedule_for_now_et

# Stable pause reason codes for startup / feed gates (logged + surfaced in status).
PAUSE_REASON_RECONCILE_QUERY_FAILED = "reconcile_query_failed"
PAUSE_REASON_RECONCILE_MANUAL_REQUIRED = "reconcile_manual_required"
PAUSE_REASON_RECONCILE_AUTO_ADOPT_NONE = "reconcile_auto_adopt_none"
PAUSE_REASON_RECONCILE_EXCEPTION = "reconcile_exception"
PAUSE_REASON_VIX_UNAVAILABLE = "vix_unavailable"
PAUSE_REASON_MANUAL_OPERATOR = "manual_operator"


def _default_position_parity_state() -> dict[str, Any]:
    return {
        "ok": True,
        "parity_ok": True,
        "query_ok": False,
        "query_ret": None,
        "query_error": "init",
        "drift_count": 0,
        "drifts": [],
        "broker_codes": {},
        "tracked_codes": {},
        "checked_at_ts": "",
    }


def _broker_fabio_option_qty_map(
    pos_df: pd.DataFrame | None, fabio_underlyings_upper: set[str]
) -> dict[str, int]:
    """Aggregate qty>0 option rows whose underlying is in the Fabio universe."""
    out: dict[str, int] = {}
    if pos_df is None or pos_df.empty:
        return out
    for _, row in pos_df.iterrows():
        try:
            q = int(float(row.get("qty", 0) or 0))
        except (TypeError, ValueError):
            q = 0
        if q <= 0:
            continue
        code = str(row.get("code", "") or "")
        sym, _ = ORBBot._parse_option_code(code)
        if sym and sym.upper() in fabio_underlyings_upper:
            out[code] = out.get(code, 0) + q
    return out


def _tracked_option_qty_map(tracked_positions: dict[str, dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pos in tracked_positions.values():
        code = str(pos.get("code", "") or "")
        if not code:
            continue
        try:
            q = int(float(pos.get("remaining_qty", 0) or 0))
        except (TypeError, ValueError):
            q = 0
        if q <= 0:
            continue
        out[code] = q
    return out


def compute_position_parity_state(
    pos_df: pd.DataFrame | None,
    tracked_positions: dict[str, dict],
    fabio_symbols: set[str],
    *,
    query_ok: bool = True,
    query_ret: int | None = None,
    query_error: str = "",
) -> dict[str, Any]:
    """
    Compare Moomoo open option rows (Fabio universe) to OrderManager in-memory positions.

    ``ok`` / ``parity_ok`` are True when broker and tracked qty match for every involved code.
    """
    tz = ZoneInfo(MARKET_TIMEZONE)
    checked_at = datetime.datetime.now(tz).isoformat(timespec="seconds")
    fabio_u = {s.upper() for s in fabio_symbols}
    if not query_ok:
        return {
            "ok": False,
            "parity_ok": False,
            "query_ok": False,
            "query_ret": query_ret,
            "query_error": query_error or "query_failed",
            "drift_count": 0,
            "drifts": [],
            "broker_codes": {},
            "tracked_codes": _tracked_option_qty_map(tracked_positions),
            "checked_at_ts": checked_at,
        }
    broker_map = _broker_fabio_option_qty_map(pos_df, fabio_u)
    tracked_map = _tracked_option_qty_map(tracked_positions)
    all_codes = set(broker_map) | set(tracked_map)
    drifts = []
    for code in sorted(all_codes):
        bq = broker_map.get(code, 0)
        tq = tracked_map.get(code, 0)
        if bq != tq:
            drifts.append({"code": code, "broker_qty": bq, "tracked_qty": tq})
    parity_ok = len(drifts) == 0
    return {
        "ok": parity_ok,
        "parity_ok": parity_ok,
        "query_ok": True,
        "query_ret": int(query_ret if query_ret is not None else 0),
        "query_error": "",
        "drift_count": len(drifts),
        "drifts": drifts,
        "broker_codes": dict(sorted(broker_map.items())),
        "tracked_codes": dict(sorted(tracked_map.items())),
        "checked_at_ts": checked_at,
    }


class ORBBot:

    def __init__(self):
        trd_env = TrdEnv.SIMULATE if PAPER_TRADING else TrdEnv.REAL
        enforce_paper_trading_pin(trd_env)
        self.quote_ctx = OpenQuoteContext(host=MOOMOO_HOST, port=MOOMOO_PORT)
        self.trade_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=MOOMOO_HOST,
            port=MOOMOO_PORT,
            security_firm=SecurityFirm.FUTUINC,
        )
        self.trd_env = trd_env
        self.order_mgr = OrderManager(self.trade_ctx, self.quote_ctx, trd_env)
        self.cb = RiskCircuitBreaker()
        self.regimes = {}
        self.signals = {}
        self.exit_tfs = {}
        self.sheets = SheetsLogger()
        self.dashboard = DashboardWriter()
        self.ops = AsyncOpsWorker(self.sheets, self.dashboard)
        self._trades_today = []
        self._trade_entries = {}
        self._capital_at_open = 0.0
        self._cb_logged = set()
        self._prefetched = False
        self._prefetch_vix = None
        self._prefetch_portfolio = None
        self._prefetch_daily = {}
        self._ops_last_alert_ts = 0.0
        self._ops_prev_error_count = 0
        self._ops_prev_dropped_count = 0
        self._data_health = {}
        self._data_health_log_every_sec = 600
        self._health_snapshot_last_ts = 0.0
        self._position_parity_latest: dict[str, Any] = _default_position_parity_state()
        self._parity_alert_last_ts = 0.0
        self._loop_cadence_mode = "idle"
        self._loop_sleep_sec = 60.0
        self._tz = ZoneInfo(MARKET_TIMEZONE)
        self._startup_unreconciled_positions = []
        self.paused = False
        self.stopped = False
        self._pause_reason_code = ""
        self._pause_reason_hint = ""
        self._prune_snapshot_history()
        self._startup_reconcile_positions()
        if self.sheets.is_connected():
            self.ops.log_alert("INFO", "ORBit bot started", "")
        tg.start_listener(self, self._tg_stop)
        mode = "PAPER" if PAPER_TRADING else "LIVE"
        self.ops.alert(
            f"🤖 <b>Fabio ORB Bot started</b>\n"
            f"Mode: {mode} | Symbols: {', '.join(SYMBOLS)}\n"
            f"Risk cap: {RISK_PCT_MAX*100:.0f}% | Max trades/day: {CB_MAX_TRADES}\n"
            f"Waiting for 9:30 market open..."
        )

    def _tg_stop(self):
        self.eod_close_all()

    def _now_market(self) -> datetime.datetime:
        return datetime.datetime.now(self._tz)

    def _prune_snapshot_history(self):
        if HEALTH_SNAPSHOT_RETENTION_DAYS <= 0:
            return
        try:
            cutoff = self._now_market() - datetime.timedelta(
                days=HEALTH_SNAPSHOT_RETENTION_DAYS
            )
            kept = []
            with open(HEALTH_SNAPSHOT_PATH, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        ts = datetime.datetime.fromisoformat(str(obj.get("ts", "")))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=self._tz)
                        if ts >= cutoff:
                            kept.append(line)
                    except Exception:
                        continue
            with open(HEALTH_SNAPSHOT_PATH, "w", encoding="utf-8") as fh:
                for line in kept:
                    fh.write(line + "\n")
        except FileNotFoundError:
            return
        except Exception as e:
            print(f"  [HEALTH] Snapshot prune failed: {e}")

    @staticmethod
    def _as_float(value, default=0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _snapshot_trades_for_dashboard(self) -> list[dict]:
        return [dict(t) for t in list(getattr(self, "_trades_today", []) or [])]

    def _overlay_open_entry_fields_from_runtime(
        self,
        open_rows: list[dict],
    ) -> list[dict]:
        """
        Display-only enrichment of broker open rows with runtime entry metadata.
        Broker remains source-of-truth for open inventory/qty; overlay only fills
        entry_time, entry_price, vix, and or_atr_pct when runtime data is available.
        """
        if not open_rows:
            return []

        by_symbol: dict[str, dict] = {}
        for sym, entry in dict(getattr(self, "_trade_entries", {}) or {}).items():
            key = str(sym or "").strip().upper()
            if not key:
                continue
            by_symbol[key] = dict(entry or {})

        out: list[dict] = []
        for row in list(open_rows):
            r = dict(row or {})
            sym = str(r.get("symbol", "")).strip().upper()
            runtime = by_symbol.get(sym)
            if runtime:
                et = str(runtime.get("entry_time", "") or "").strip()[:8]
                ep = self._as_float(runtime.get("entry_price", 0.0), 0.0)
                vix = self._as_float(runtime.get("vix", 0.0), 0.0)
                or_atr_pct = self._as_float(runtime.get("or_atr_pct", 0.0), 0.0)
                if et:
                    r["entry_time"] = et
                if ep > 0:
                    r["entry_price"] = round(ep, 4)
                if vix > 0:
                    r["vix"] = round(vix, 4)
                if or_atr_pct > 0:
                    r["or_atr_pct"] = round(or_atr_pct, 4)
            out.append(r)
        return out

    def _fetch_broker_open_positions_for_dashboard(self) -> list[dict]:
        """
        Optional display-only broker open snapshot; runs via async worker hook.
        """
        try:
            ret, pos_df = self.trade_ctx.position_list_query(trd_env=self.order_mgr.trd_env)
            if ret != 0 or pos_df is None or pos_df.empty:
                return []
            # Effective qty matches reconcile/dashboard_writer (qty then can_sell_qty).
            broker_rows = moomoo_position_records_to_dashboard_opens(
                pos_df.to_dict("records"),
                as_of_date=datetime.date.today().isoformat(),
            )
            return self._overlay_open_entry_fields_from_runtime(broker_rows)
        except Exception as e:
            print(f"  ⚠  Intraday broker open snapshot failed: {e}")
            return []

    def _enqueue_intraday_dashboard_refresh(self):
        self.ops.refresh_dashboard_intraday(
            self._snapshot_trades_for_dashboard(),
            throttle_sec=OPS_DASHBOARD_REFRESH_THROTTLE_SEC,
        )
        self.ops.refresh_dashboard_broker_open_positions(
            self._fetch_broker_open_positions_for_dashboard,
            throttle_sec=OPS_DASHBOARD_OPEN_REFRESH_THROTTLE_SEC,
        )

    @staticmethod
    def _parse_option_code(code: str) -> tuple[str, str] | tuple[None, None]:
        text = str(code or "")
        if not text.startswith("US."):
            return None, None
        raw = text.split(".")[-1]
        m = re.match(r"([A-Z]+)\d{6}([CP])\d+", raw)
        if not m:
            return None, None
        symbol = m.group(1)
        direction = "CALL" if m.group(2) == "C" else "PUT"
        return symbol, direction

    def _symbol_label_from_reconcile_row(self, row: dict) -> str:
        code = str(row.get("code", ""))
        symbol, _ = self._parse_option_code(code)
        return symbol or (code[:20] + "…" if len(code) > 20 else code or "?")

    @staticmethod
    def _cost_fields_snapshot(row: dict) -> dict:
        return {
            "cost_price": row.get("cost_price"),
            "average_cost": row.get("average_cost"),
            "avg_price": row.get("avg_price"),
        }

    def _set_system_pause(self, reason_code: str, operator_hint: str) -> None:
        self.paused = True
        self._pause_reason_code = reason_code
        self._pause_reason_hint = operator_hint

    def clear_pause_diagnostics(self) -> None:
        """Clear operator-facing pause codes (call on /resume)."""
        self._pause_reason_code = ""
        self._pause_reason_hint = ""

    def set_operator_manual_pause(self) -> None:
        """Telegram /pause: pause without treating as startup reconcile."""
        self.paused = True
        self._pause_reason_code = PAUSE_REASON_MANUAL_OPERATOR
        self._pause_reason_hint = "Telegram /pause. Send /resume to enable new entries."

    def _log_startup_preflight(
        self,
        *,
        broker_open_positive_qty: int,
        adoptable_precheck: int | None = None,
        auto_adopt_on: bool,
        note: str = "",
    ) -> None:
        parts = [
            f"code=startup_preflight",
            f"broker_open_positive_qty={broker_open_positive_qty}",
            f"auto_adopt={int(bool(auto_adopt_on))}",
        ]
        if adoptable_precheck is not None:
            parts.append(f"adoptable_precheck={adoptable_precheck}")
            rest = broker_open_positive_qty - adoptable_precheck
            if broker_open_positive_qty >= 0:
                parts.append(f"non_adoptable_precheck={rest}")
        if note:
            parts.append(f"note={note}")
        payload = "STARTUP_PREFLIGHT " + " ".join(parts)
        try:
            self.ops.log_alert("STARTUP_PREFLIGHT", payload, "")
        except Exception as exc:
            print(f"  ⚠  Startup preflight log failed: {exc}")
        print(f"  [Startup] {payload}")

    def _startup_adopt_precheck(self, row: dict) -> tuple[bool, str]:
        code = str(row.get("code", ""))
        qty = int(self._as_float(row.get("qty", 0)))
        symbol, direction = self._parse_option_code(code)
        if qty <= 0:
            return False, f"{code or '?'} skipped: qty<=0"
        if not symbol or not direction:
            return False, f"{code or '?'} skipped: unsupported option code"
        if symbol in self.order_mgr.positions or symbol in self.signals:
            return False, f"{symbol} skipped: duplicate symbol already tracked"

        entry_opt = self._as_float(
            row.get("cost_price", row.get("average_cost", row.get("avg_price", 0.0))),
            default=0.0,
        )
        if entry_opt <= 0:
            return False, f"{symbol} skipped: missing/invalid cost basis"
        return True, ""

    def _adopt_startup_position(self, row: dict) -> tuple[bool, str]:
        ok, detail = self._startup_adopt_precheck(row)
        if not ok:
            return False, detail
        code = str(row.get("code", ""))
        qty = int(self._as_float(row.get("qty", 0)))
        symbol, direction = self._parse_option_code(code)
        entry_opt = self._as_float(
            row.get("cost_price", row.get("average_cost", row.get("avg_price", 0.0))),
            default=0.0,
        )

        self.order_mgr.positions[symbol] = {
            "direction": direction,
            "code": code,
            "original_qty": qty,
            "remaining_qty": qty,
            "entry_option_price": entry_opt,
            "trim_level": 0,
            "realized_trim_pnl": 0.0,
            "profit_lock_announced": False,
            # Broker snapshot generally does not include reliable stock-underlier entry.
            # Exit loop handles this as an unknown-basis position.
            "entry_stock_price": 0.0,
            "adopted_startup": True,
        }
        self.signals[symbol] = direction
        self.exit_tfs[symbol] = KLType.K_5M
        et = self._now_market().strftime("%H:%M:%S")
        gid = f"{datetime.date.today().isoformat()}:{symbol}:{et}"
        self._trade_entries[symbol] = {
            "direction": direction,
            "entry_time": et,
            "entry_price": entry_opt,
            "entry_stock_price": 0.0,
            "strike": code[-11:-3] if len(code) >= 11 else "",
            "expiry": code[-17:-11] if len(code) >= 17 else "",
            "contracts": qty,
            "vix": 0.0,
            "or_atr_pct": 0.0,
            "trend": "ADOPTED",
            "vix_regime": "ADOPTED",
            "day_color": "ADOPTED",
            "notes": "startup_auto_adopt",
            "ledger_group_id": gid,
        }
        self._append_ledger_open(symbol, direction, self._trade_entries[symbol])
        return True, f"{symbol} {direction} x{qty} adopted"

    def _startup_reconcile_positions(self):
        try:
            ret, pos_df = self.trade_ctx.position_list_query(trd_env=self.order_mgr.trd_env)
            if ret != 0:
                hint = (
                    "Verify broker/API connectivity and trd_env; retry when "
                    "position_list_query returns success."
                )
                msg = (
                    "[reconcile_query_failed] startup reconciliation failed: "
                    "cannot query broker positions"
                )
                self._set_system_pause(PAUSE_REASON_RECONCILE_QUERY_FAILED, hint)
                print(f"  ⚠  {msg}")
                self.ops.alert(f"⚠️ <b>FABIO paused</b>\n{msg}\n<i>{hint}</i>")
                self.ops.log_alert("RECONCILE", msg, "")
                self._log_startup_preflight(
                    broker_open_positive_qty=-1,
                    adoptable_precheck=None,
                    auto_adopt_on=AUTO_ADOPT_OPEN_POSITIONS,
                    note="position_list_query_nonzero_ret",
                )
                return

            if pos_df is None or pos_df.empty:
                self._log_startup_preflight(
                    broker_open_positive_qty=0,
                    adoptable_precheck=0,
                    auto_adopt_on=AUTO_ADOPT_OPEN_POSITIONS,
                    note="no_position_rows",
                )
                return

            orphans = pos_df[pos_df["qty"] > 0]
            if orphans.empty:
                self._log_startup_preflight(
                    broker_open_positive_qty=0,
                    adoptable_precheck=0,
                    auto_adopt_on=AUTO_ADOPT_OPEN_POSITIONS,
                    note="no_positive_qty_positions",
                )
                return

            orphan_rows = orphans.to_dict("records")
            adoptable_n = sum(1 for r in orphan_rows if self._startup_adopt_precheck(r)[0])
            self._log_startup_preflight(
                broker_open_positive_qty=len(orphan_rows),
                adoptable_precheck=adoptable_n,
                auto_adopt_on=AUTO_ADOPT_OPEN_POSITIONS,
                note="before_adopt_policy",
            )

            if not AUTO_ADOPT_OPEN_POSITIONS:
                self._startup_unreconciled_positions = orphan_rows
                hint = (
                    "Set FABIO_AUTO_ADOPT_OPEN_POSITIONS=1 after broker validation, "
                    "or reduce broker positions to zero before startup."
                )
                msg = (
                    "[reconcile_manual_required] startup reconciliation required: "
                    f"{len(orphan_rows)} broker position(s) found; entries blocked until manual clear"
                )
                self._set_system_pause(PAUSE_REASON_RECONCILE_MANUAL_REQUIRED, hint)
                print(f"  ⚠  {msg}")
                self.ops.alert(f"⚠️ <b>FABIO paused</b>\n{msg}\n<i>{hint}</i>")
                self.ops.log_alert("RECONCILE", msg, "")
                return

            adopted_msgs = []
            skipped_msgs = []
            unreconciled = []
            for row in orphan_rows:
                ok, detail = self._adopt_startup_position(row)
                if ok:
                    adopted_msgs.append(detail)
                else:
                    skipped_msgs.append(detail)
                    unreconciled.append(row)
            self._startup_unreconciled_positions = unreconciled
            if not adopted_msgs and unreconciled:
                hint = (
                    "Repair broker cost basis for listed contracts "
                    "(cost_price / average_cost / avg_price > 0), then restart;"
                    " or reconcile positions manually outside the bot."
                )
                cost_ctx = "; ".join(
                    json.dumps(
                        {
                            "sym": self._symbol_label_from_reconcile_row(row),
                            "qty": row.get("qty"),
                            **self._cost_fields_snapshot(row),
                        },
                        separators=(",", ":"),
                    )
                    for row in unreconciled[:5]
                )
                msg = (
                    "[reconcile_auto_adopt_none] startup auto-adopt failed: "
                    f"no positions could be adopted; {len(unreconciled)} position(s) "
                    "require manual reconcile"
                )
                full_log = (
                    f"{msg} | details={' | '.join(skipped_msgs[:5])}"
                    + (f" | cost_snapshot=[{cost_ctx}]" if cost_ctx else "")
                )
                print(f"  ⚠  {full_log}")
                self._set_system_pause(PAUSE_REASON_RECONCILE_AUTO_ADOPT_NONE, hint)
                self.ops.alert(
                    "⚠️ <b>FABIO paused</b>\n"
                    f"{msg}\n<i>{hint}</i>\n"
                    f"Details: {'; '.join(skipped_msgs[:5])}"
                )
                self.ops.log_alert("RECONCILE", full_log, "")
                return

            summary = (
                f"startup auto-adopt: adopted={len(adopted_msgs)} "
                f"skipped={len(skipped_msgs)} pending_manual={len(unreconciled)}"
            )
            print(f"  ✓ {summary}")
            if adopted_msgs:
                self.ops.alert(
                    "✅ <b>Startup auto-adopt complete</b>\n"
                    f"{summary}\n"
                    f"Adopted: {'; '.join(adopted_msgs[:5])}"
                )
            if skipped_msgs:
                self.ops.alert(
                    "⚠️ <b>Startup reconcile partial</b>\n"
                    f"Skipped: {'; '.join(skipped_msgs[:5])}"
                )
            self.ops.log_alert("RECONCILE", summary, "")
        except Exception as e:
            hint = "Inspect traceback in console; retry after fixing the underlying reconcile error."
            msg = f"[reconcile_exception] startup reconciliation error: {e}"
            print(f"  ⚠  {msg}")
            self._set_system_pause(PAUSE_REASON_RECONCILE_EXCEPTION, hint)
            self.ops.alert(f"⚠️ <b>FABIO paused</b>\n{msg}\n<i>{hint}</i>")
            self.ops.log_alert("RECONCILE", msg, "")

    def _hydrate_circuit_from_sheets_today(self):
        if not self.sheets.is_connected():
            return
        snap = self.sheets.get_today_circuit_snapshot()
        if not snap:
            return
        try:
            trade_count = int(snap.get("trade_count", 0))
            realized_pnl = float(snap.get("realized_pnl", 0.0))
            loss_streak = int(snap.get("loss_streak", 0))
            self.cb.trade_count = max(0, trade_count)
            self.cb.realized_pnl = realized_pnl
            self.cb.loss_streak = max(0, loss_streak)
            if trade_count > 0:
                print(
                    "  [Startup] Circuit hydrated from Sheets — "
                    f"trades={self.cb.trade_count}, pnl=${self.cb.realized_pnl:+.2f}, "
                    f"loss_streak={self.cb.loss_streak}"
                )
        except Exception as e:
            print(f"  [Startup] Circuit hydration skipped: {e}")

    def _check_ops_health(self):
        h = self.ops.health()
        queue_high = h["queue_depth"] >= OPS_QUEUE_WARN_THRESHOLD
        worker_down = not h["thread_alive"]
        new_errors = h["errors"] > self._ops_prev_error_count
        dropped_total = int(h.get("dropped_noncritical", 0)) + int(
            h.get("dropped_critical", 0)
        )
        new_drops = dropped_total > self._ops_prev_dropped_count

        if not (queue_high or worker_down or new_errors or new_drops):
            return

        now_ts = time.time()
        if now_ts - self._ops_last_alert_ts < OPS_ALERT_COOLDOWN_SEC:
            self._ops_prev_error_count = h["errors"]
            self._ops_prev_dropped_count = dropped_total
            return

        reasons = []
        if worker_down:
            reasons.append("worker DOWN")
        if queue_high:
            reasons.append(f"queue={h['queue_depth']} (>= {OPS_QUEUE_WARN_THRESHOLD})")
        if new_errors:
            reasons.append(f"errors={h['errors']}")
        if new_drops:
            reasons.append(
                f"drops={dropped_total} (noncritical={h.get('dropped_noncritical', 0)}, "
                f"critical={h.get('dropped_critical', 0)})"
            )
        reason_str = " | ".join(reasons)
        last_err = h["last_error"] if h["last_error"] else "none"

        msg = (
            "⚠️ <b>Async ops health warning</b>\n"
            f"{reason_str}\n"
            f"Last error: {last_err}"
        )
        print(f"  [OPS] {reason_str} | last_error={last_err}")
        self.ops.alert(msg)
        self.ops.log_alert("ASYNC_OPS", f"{reason_str} | last_error={last_err}", "")

        self._ops_last_alert_ts = now_ts
        self._ops_prev_error_count = h["errors"]
        self._ops_prev_dropped_count = dropped_total

    def _record_data_health(
        self,
        sym: str,
        tf_label: str,
        age_sec: float,
        stale_threshold_sec: float,
        source: str,
    ):
        """
        Track per-symbol feed health and emit low-noise logs/alerts.
        - periodic heartbeat every N seconds
        - transition alerts only when state flips (OK<->STALE)
        """
        now_ts = time.time()
        state = "STALE" if age_sec > stale_threshold_sec else "OK"
        key = (sym, tf_label)
        prev = self._data_health.get(key, {})
        prev_state = prev.get("state")
        prev_logged = float(prev.get("last_logged_ts", 0))

        should_log = (now_ts - prev_logged) >= self._data_health_log_every_sec
        state_changed = prev_state is not None and prev_state != state

        msg = (
            f"{tf_label} age={age_sec/60:.1f}m "
            f"(threshold={stale_threshold_sec/60:.1f}m) | src={source} | {state}"
        )

        if should_log:
            self.ops.log_decision(
                sym,
                None,
                "HEARTBEAT",
                msg,
                vix=self.regimes.get(sym).vix if sym in self.regimes else None,
                or_atr_pct=self.regimes.get(sym).or_atr_pct if sym in self.regimes else None,
                gap_pct=self.regimes.get(sym).gap_pct if sym in self.regimes else None,
                regime=self.regimes.get(sym).day_color if sym in self.regimes else None,
            )
            prev_logged = now_ts

        if state_changed:
            icon = "⚠️" if state == "STALE" else "✅"
            self.ops.alert(
                f"{icon} <b>Data feed {state}: {sym} {tf_label}</b>\n"
                f"{msg}"
            )
            self.ops.log_alert("DATA_FEED", f"{sym} {tf_label} {msg}", sym)

        self._data_health[key] = {
            "state": state,
            "last_logged_ts": prev_logged,
            "last_age_sec": age_sec,
            "source": source,
        }

    def status_summary(self) -> str:
        pos_lines = []
        for sym, direction in self.signals.items():
            pos = self.order_mgr.positions.get(sym, {})
            qty = pos.get("qty", pos.get("original_qty", 0))
            pos_lines.append(f"  {sym} {direction} ×{int(qty)}")
        pos_str = "\n".join(pos_lines) or "  None"

        data_lines = []
        for sym in SYMBOLS:
            tf_parts = []
            for tf in ("3m", "5m"):
                entry = self._data_health.get((sym, tf))
                if not entry:
                    continue
                age_sec = float(entry.get("last_age_sec", 0.0))
                state = entry.get("state", "?")
                tf_parts.append(f"{tf}:{state}@{age_sec/60:.1f}m")
            if tf_parts:
                data_lines.append(f"  {sym} {' | '.join(tf_parts)}")
        data_str = "\n".join(data_lines) or "  no snapshots yet"

        h = self.ops.health()
        last_err = h["last_error"] if h["last_error"] else "none"
        ur_labels = [
            self._symbol_label_from_reconcile_row(r) for r in self._startup_unreconciled_positions
        ]
        ur_line = ", ".join(dict.fromkeys(ur_labels)) if ur_labels else "none"

        if self.paused:
            paused_line = self._pause_reason_code or "paused (reason not set)"
            hint_line = self._pause_reason_hint or "—"
        else:
            paused_line = "none"
            hint_line = "—"
        return (
            f"Strategy: FABIO | {'PAUSED' if self.paused else 'ACTIVE'}\n"
            f"PauseReason: {paused_line}\n"
            f"PauseHint: {hint_line}\n"
            f"{self.cb.summary()}\n"
            f"Ops: queue={h['queue_depth']} | errors={h['errors']} | "
            f"worker={'UP' if h['thread_alive'] else 'DOWN'}\n"
            f"Ops last error: {last_err}\n"
            f"Data feeds:\n{data_str}\n"
            f"Tracked opens (strategy):\n{pos_str}\n"
            f"Startup reconcile pending rows: {len(self._startup_unreconciled_positions)} "
            f"({ur_line})\n"
            f"When paused by reconcile/VIX: fix root cause before relying on /resume alone."
        )

    def _refresh_position_parity(self) -> None:
        fabio = {s.upper() for s in SYMBOLS}
        try:
            ret, pos_df = self.trade_ctx.position_list_query(
                trd_env=self.order_mgr.trd_env
            )
            if ret != 0:
                state = compute_position_parity_state(
                    None,
                    self.order_mgr.positions,
                    fabio,
                    query_ok=False,
                    query_ret=ret,
                    query_error="position_list_query_nonzero_ret",
                )
            else:
                state = compute_position_parity_state(
                    pos_df,
                    self.order_mgr.positions,
                    fabio,
                )
            self._position_parity_latest = state
            self._maybe_alert_position_parity(state)
        except Exception as e:
            state = compute_position_parity_state(
                None,
                self.order_mgr.positions,
                fabio,
                query_ok=False,
                query_ret=None,
                query_error=str(e),
            )
            self._position_parity_latest = state
            self._maybe_alert_position_parity(state)

    def _maybe_alert_position_parity(self, state: dict[str, Any]) -> None:
        if bool(state.get("query_ok")) and bool(state.get("ok")):
            return
        compact = {
            "parity_ok": state.get("parity_ok"),
            "query_ok": state.get("query_ok"),
            "query_ret": state.get("query_ret"),
            "query_error": state.get("query_error"),
            "drift_count": state.get("drift_count"),
            "drifts": state.get("drifts"),
        }
        log_line = json.dumps(compact, separators=(",", ":"), default=str)
        if len(log_line) > 1800:
            log_line = log_line[:1800] + "…"
        self.ops.log_alert("POSITION_PARITY", log_line, "")
        now_ts = time.time()
        if now_ts - self._parity_alert_last_ts < OPS_ALERT_COOLDOWN_SEC:
            return
        self._parity_alert_last_ts = now_ts
        if not state.get("query_ok"):
            warn = (
                "⚠️ <b>POSITION_PARITY</b>\n"
                f"Broker query failed: ret={state.get('query_ret')} "
                f"| {state.get('query_error')}"
            )
        else:
            drift_preview = json.dumps(state.get("drifts"), separators=(",", ":"), default=str)
            if len(drift_preview) > 900:
                drift_preview = drift_preview[:900] + "…"
            warn = (
                "⚠️ <b>POSITION_PARITY drift</b>\n"
                "Moomoo open options (Fabio universe) disagree with tracked "
                f"<code>OrderManager</code> book. drift_count="
                f"{state.get('drift_count')}<pre>{drift_preview}</pre>"
            )
        print(f"  [POSITION_PARITY] {log_line[:240]}")
        self.ops.alert(warn)

    def _write_health_snapshot(self, snapshot: dict):
        try:
            with open(HEALTH_SNAPSHOT_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
        except Exception as e:
            print(f"  [HEALTH] Snapshot write failed: {e}")

    def _emit_health_snapshot(self, force: bool = False):
        now_ts = time.time()
        if (not force) and (
            now_ts - self._health_snapshot_last_ts < HEALTH_SNAPSHOT_INTERVAL_SEC
        ):
            return
        self._refresh_position_parity()
        ops_h = self.ops.health()
        snapshot = {
            "ts": self._now_market().isoformat(timespec="seconds"),
            "bot_state": {
                "paused": self.paused,
                "stopped": self.stopped,
                "pause_reason_code": self._pause_reason_code or "",
                "signals_open": list(self.signals.keys()),
            },
            "circuit": {
                "daily_loss_pct": round(self.cb.daily_loss_pct, 6),
                "trade_count": self.cb.trade_count,
                "loss_streak": self.cb.loss_streak,
            },
            "ops": {
                "queue_depth": int(ops_h.get("queue_depth", 0)),
                "queue_max": int(ops_h.get("queue_max", 0)),
                "errors": int(ops_h.get("errors", 0)),
                "thread_alive": bool(ops_h.get("thread_alive", False)),
                "dropped_noncritical": int(ops_h.get("dropped_noncritical", 0)),
                "dropped_critical": int(ops_h.get("dropped_critical", 0)),
                "coalesced_updates": int(ops_h.get("coalesced_updates", 0)),
                "inline_critical_fallbacks": int(
                    ops_h.get("inline_critical_fallbacks", 0)
                ),
                "dashboard_intraday_refresh_requests": int(
                    ops_h.get("dashboard_intraday_refresh_requests", 0)
                ),
                "dashboard_intraday_refresh_enqueued": int(
                    ops_h.get("dashboard_intraday_refresh_enqueued", 0)
                ),
                "dashboard_intraday_refresh_throttled": int(
                    ops_h.get("dashboard_intraday_refresh_throttled", 0)
                ),
                "dashboard_open_refresh_requests": int(
                    ops_h.get("dashboard_open_refresh_requests", 0)
                ),
                "dashboard_open_refresh_enqueued": int(
                    ops_h.get("dashboard_open_refresh_enqueued", 0)
                ),
                "dashboard_open_refresh_throttled": int(
                    ops_h.get("dashboard_open_refresh_throttled", 0)
                ),
                "loop_cadence_mode": str(getattr(self, "_loop_cadence_mode", "idle")),
                "loop_sleep_sec": float(getattr(self, "_loop_sleep_sec", 60.0)),
            },
            "data_health": {
                f"{sym}_{tf}": {
                    "state": v.get("state", "?"),
                    "age_sec": round(float(v.get("last_age_sec", 0.0)), 1),
                    "source": v.get("source", ""),
                }
                for (sym, tf), v in self._data_health.items()
            },
            "position_parity": dict(self._position_parity_latest),
        }
        self._write_health_snapshot(snapshot)
        pp = snapshot["position_parity"]
        pp_note = (
            f" parity_ok={pp.get('parity_ok')} query_ok={pp.get('query_ok')}"
            f" drift_count={pp.get('drift_count')}"
        )
        self.ops.log_alert(
            "HEALTH_SNAPSHOT",
            f"queue={snapshot['ops']['queue_depth']}/{snapshot['ops']['queue_max']} "
            f"errors={snapshot['ops']['errors']} "
            f"drops={snapshot['ops']['dropped_noncritical']}/{snapshot['ops']['dropped_critical']} "
            f"dash={snapshot['ops']['dashboard_intraday_refresh_enqueued']}/"
            f"{snapshot['ops']['dashboard_intraday_refresh_requests']} "
            f"open={snapshot['ops']['dashboard_open_refresh_enqueued']}/"
            f"{snapshot['ops']['dashboard_open_refresh_requests']} "
            f"cadence={snapshot['ops']['loop_cadence_mode']}@{snapshot['ops']['loop_sleep_sec']:.0f}s"
            f"{pp_note}",
            "",
        )
        self._health_snapshot_last_ts = now_ts

    def _compute_loop_sleep_sec(self) -> float:
        if self.signals:
            return max(1.0, float(MAIN_LOOP_SLEEP_ACTIVE_SEC))
        return 60.0

    def _update_loop_cadence_mode(self, sleep_sec: float) -> None:
        mode = "active" if self.signals else "idle"
        if mode != getattr(self, "_loop_cadence_mode", ""):
            print(f"  [Loop Cadence] {mode} mode — sleep {sleep_sec:.0f}s")
        self._loop_cadence_mode = mode
        self._loop_sleep_sec = float(sleep_sec)

    def prefetch_or_window(self):
        print("\n  [Pre-fetch] Fetching VIX + daily data during OR window...")
        try:
            self._prefetch_vix = get_vix(self.quote_ctx)
            if self._prefetch_vix is None:
                raise RuntimeError("VIX unavailable during prefetch")
            self._prefetch_portfolio = min(
                get_portfolio_value(self.trade_ctx),
                STRATEGY_CAPITAL * RESEARCH_RISK_CAP_MULTIPLIER,
            )
            self._prefetch_daily = {}
            for sym in SYMBOLS:
                self._prefetch_daily[sym] = get_candles(
                    self.quote_ctx, sym, KLType.K_DAY, 60
                )
            self._prefetched = True
            print(
                f"  [Pre-fetch] Done — VIX={self._prefetch_vix:.1f} | "
                f"Portfolio=${self._prefetch_portfolio:,.0f} | "
                f"Ready for 9:45 signal scan"
            )
        except Exception as e:
            print(f"  [Pre-fetch] Error: {e} — will retry at 9:45")
            self._prefetched = False

    def initialize_day(self):
        print("\n" + "=" * 60)
        print(f"  ORB BOT | FABIO STRATEGY | {self._now_market().strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"  Mode: {'PAPER' if PAPER_TRADING else 'LIVE'} | Risk cap: {RISK_PCT_MAX*100:.0f}% | No counter-trend")
        print("=" * 60)

        if self._prefetched:
            vix = self._prefetch_vix
            portfolio_val = self._prefetch_portfolio
            print(f"  VIX: {vix:.2f} | Portfolio: ${portfolio_val:,.0f}  [pre-fetched]")
        else:
            vix = get_vix(self.quote_ctx)
            if vix is None:
                hint = (
                    "Wait for VIX data; verify market-data path (yfinance). "
                    "Resume feed or restart bot after connectivity recovers."
                )
                self.ops.alert(
                    "⚠️ <b>VIX unavailable</b>\n"
                    "Day initialization blocked; entries paused until feed recovers.\n"
                    f"<i>{hint}</i>"
                )
                self.ops.log_alert(
                    "DATA_FEED",
                    "[vix_unavailable] VIX unavailable at day init",
                    "",
                )
                self._set_system_pause(PAUSE_REASON_VIX_UNAVAILABLE, hint)
                return
            portfolio_val = min(
                get_portfolio_value(self.trade_ctx),
                STRATEGY_CAPITAL * RESEARCH_RISK_CAP_MULTIPLIER,
            )
            print(f"  VIX: {vix:.2f} | Portfolio: ${portfolio_val:,.0f}")

        self.cb.set_portfolio_open(portfolio_val)
        self._capital_at_open = portfolio_val
        self._hydrate_circuit_from_sheets_today()

        for sym in SYMBOLS:
            if self._prefetched and sym in self._prefetch_daily:
                df_daily = self._prefetch_daily[sym]
            else:
                df_daily = get_candles(self.quote_ctx, sym, KLType.K_DAY, 60)
            df_5m_or = get_candles(self.quote_ctx, sym, KLType.K_5M, 40)
            regime = MarketRegime(sym, df_daily, df_5m_or, vix)
            self.regimes[sym] = regime
            print(f"\n  {regime.summary()}")
            if not regime.tradeable:
                print(f"  ⛔ {sym}: RED day — skipping")
                self.ops.log_decision(
                    sym,
                    None,
                    "SKIP",
                    f"RED day — VIX={vix:.1f} ({regime.vix_label}) | "
                    f"gap={regime.gap_pct:.1f}% | OR={regime.or_atr_pct:.0f}% ATR",
                    vix=vix,
                    or_atr_pct=regime.or_atr_pct,
                    gap_pct=regime.gap_pct,
                    regime=regime.day_color,
                )

        # Align dashboard open_positions with broker once per day init; otherwise
        # trade_data.json can keep stale reconcile/FIFO rows until the next ledger event.
        self._enqueue_intraday_dashboard_refresh()

    def run_signal_loop(self):
        print(f"\n  [Signal Loop] {self.cb.summary()}")
        if self._now_market().hour >= 13 and not hasattr(self, "_afternoon_vix_logged"):
            cached_vix = next(iter(self.regimes.values())).vix if self.regimes else "?"
            print(f"  [PM Session] VIX cached at {cached_vix} (morning snapshot)")
            self._afternoon_vix_logged = True

        for sym in SYMBOLS:
            try:
                self._process_signal(sym)
            except Exception as e:
                print(f"  ⚠  [{sym}] Signal loop error — skipping symbol: {e}")
                self.ops.alert(f"⚠️ <b>FABIO signal loop error [{sym}]</b>\n{e}")

    def _process_signal(self, sym: str):
        if sym not in self.regimes:
            print(f"  [{sym}] ⚪ No regime — not initialized")
            return
        regime = self.regimes[sym]
        if not regime.tradeable:
            print(
                f"  [{sym}] 🔴 RED day — skipping (VIX={regime.vix:.1f} "
                f"gap={regime.gap_pct:.1f}% OR={regime.or_atr_pct:.0f}%ATR)"
            )
            return
        if self.order_mgr.has_position(sym):
            print(f"  [{sym}] ⏳ Position open — holding")
            return

        if self.paused:
            print(f"  ⏸ [{sym}] Skipped — bot paused")
            return
        allowed, reason = self.cb.can_enter(self.order_mgr.open_count())
        if not allowed:
            print(f"  🚫 [{sym}] Entry blocked — {reason}")
            if reason not in self._cb_logged:
                self.ops.log_decision(
                    sym,
                    None,
                    "SKIP",
                    f"Circuit breaker: {reason}",
                    vix=regime.vix,
                    or_atr_pct=regime.or_atr_pct,
                    gap_pct=regime.gap_pct,
                    regime=regime.day_color,
                )
                self.ops.log_alert("CIRCUIT_BREAKER", reason, sym)
                self._cb_logged.add(reason)
            return

        df_5m = get_candles_fresh(
            self.quote_ctx,
            sym,
            KLType.K_5M,
            count=20,
            max_age_bars=2.0,
            retries=3,
        )
        age_5m = candle_age_seconds(df_5m)
        self._record_data_health(
            sym,
            "5m",
            age_sec=age_5m,
            stale_threshold_sec=10 * 60,
            source="signal_loop",
        )
        engine = SignalEngine(regime)
        direction = engine.check_breakout(df_5m)

        if not direction:
            or_high = regime.or_high
            or_low = regime.or_low
            last = df_5m["close"].iloc[-1] if not df_5m.empty else 0
            print(f"  [{sym}] 🟡 Scanning — price={last:.2f} OR[{or_low:.2f}–{or_high:.2f}] no breakout")
            return

        if age_5m > ENTRY_STALE_BLOCK_SEC:
            print(
                f"  ⛔ [{sym}] skipped — 5m feed stale ({age_5m/60:.1f}m > "
                f"{ENTRY_STALE_BLOCK_SEC/60:.1f}m hard limit)"
            )
            self.ops.log_decision(
                sym,
                None,
                "SKIP",
                f"Stale 5m feed ({age_5m/60:.1f}m)",
                vix=regime.vix,
                or_atr_pct=regime.or_atr_pct,
                gap_pct=regime.gap_pct,
                regime=regime.day_color,
            )
            return

        signal_candle_time = pd.Timestamp(df_5m["time_key"].iloc[-1])
        if signal_candle_time.tzinfo is None:
            signal_candle_time = signal_candle_time.tz_localize(self._tz)
        age_min = (self._now_market() - signal_candle_time).total_seconds() / 60
        if age_min > ENTRY_SIGNAL_MAX_AGE_MIN:
            print(
                f"  ⛔ [{sym}] {direction} skipped — signal {age_min:.0f} min old "
                f"(max {ENTRY_SIGNAL_MAX_AGE_MIN} min)"
            )
            return

        counter = engine.is_counter_trend(direction)

        if counter:
            print(f"  ⛔ [{sym}] {direction} skipped — counter-trend (Fabio)")
            return
        if regime.vix < (VIX_HALF_MAX + 0.1):
            print(f"  ⛔ [{sym}] skipped — VIX {regime.vix:.1f} below 16.1 threshold")
            return
        if direction == "CALL" and VIX_NORMAL_MAX < regime.vix <= VIX_AGGRESSIVE_MAX:
            print(
                f"  ⛔ [{sym}] CALL skipped — VIX {regime.vix:.1f} in 20-28 range (Fabio PUT-only zone)"
            )
            return

        cb_mod = self.cb.size_modifier()
        risk_mult = regime.risk_multiplier(counter_trend=False, cb_modifier=cb_mod)
        port_val = min(
            get_portfolio_value(self.trade_ctx),
            STRATEGY_CAPITAL * RESEARCH_RISK_CAP_MULTIPLIER,
        )
        last_price = df_5m["close"].iloc[-1]

        tags = []
        if cb_mod < 1.0:
            tags.append(f"⚠ SIZE×{cb_mod:.2f} (loss streak)")
        if regime.retest_required:
            tags.append("✓ RETEST CONFIRMED")
        tag_str = " | ".join(tags) if tags else "✓ WITH TREND"

        print(f"\n  [{sym}] {direction} | {tag_str} | Risk={risk_mult*100:.1f}%")
        self.ops.log_decision(
            sym,
            direction,
            "ENTER",
            f"{tag_str} | Risk={risk_mult*100:.1f}%",
            vix=regime.vix,
            or_atr_pct=regime.or_atr_pct,
            gap_pct=regime.gap_pct,
            regime=regime.day_color,
        )
        self.order_mgr.enter(sym, direction, last_price, risk_mult, port_val)

        if self.order_mgr.has_position(sym):
            self.signals[sym] = direction
            self.exit_tfs[sym] = engine.exit_timeframe(df_5m)
            tf_label = {KLType.K_3M: "3-min", KLType.K_5M: "5-min", KLType.K_15M: "15-min"}
            print(f"  Exit TF: {tf_label.get(self.exit_tfs[sym], '?')}")
            pos = self.order_mgr.positions.get(sym, {})
            entry_time = datetime.datetime.now().strftime("%H:%M:%S")
            trend_label = "COUNTER" if counter else "WITH"
            gid = f"{datetime.date.today().isoformat()}:{sym}:{entry_time}"
            self._trade_entries[sym] = {
                "direction": direction,
                "entry_time": entry_time,
                "entry_price": pos.get("entry_option_price", 0),
                "entry_stock_price": last_price,
                "strike": pos.get("code", "")[-11:-3],
                "expiry": pos.get("code", "")[-17:-11] if pos.get("code") else "",
                "contracts": pos.get("original_qty", 0),
                "vix": regime.vix,
                "or_atr_pct": regime.or_atr_pct,
                "trend": trend_label,
                "vix_regime": regime.vix_label,
                "day_color": regime.day_color,
                "ledger_group_id": gid,
            }
            pos["entry_stock_price"] = last_price
            entry_opt = pos.get("entry_option_price", 0)
            contracts = pos.get("original_qty", 0)
            self.ops.alert(
                f"{'🟢' if direction == 'CALL' else '🔴'} <b>FABIO ENTRY — {sym} {direction}</b>\n"
                f"Stock: ${last_price:.2f} | VIX: {regime.vix:.1f}\n"
                f"Option: {pos.get('code','?')} @ ${entry_opt:.2f}\n"
                f"Contracts: {int(contracts)} | Risk: {risk_mult*100:.1f}%\n"
                f"OR {regime.or_high:.2f} / {regime.or_low:.2f} | {tag_str}"
            )
            self.ops.log_trade_entry(
                sym,
                direction,
                entry_time,
                pos.get("entry_option_price", 0),
                self._trade_entries[sym]["strike"],
                self._trade_entries[sym]["expiry"],
                pos.get("original_qty", 0),
                regime.vix,
                regime.or_atr_pct,
                port_val,
                trend=trend_label,
                vix_regime=regime.vix_label,
                day_color=regime.day_color,
            )
            self._append_ledger_open(sym, direction, self._trade_entries[sym])
        else:
            self.ops.alert(
                f"⚠️ <b>FABIO no fill [{sym} {direction}]</b>\nCheck option chain availability in Moomoo."
            )

    def run_exit_loop(self):
        for sym in list(self.signals.keys()):
            if not self.order_mgr.has_position(sym):
                continue

            direction = self.signals[sym]
            regime = self.regimes[sym]
            pos = self.order_mgr.positions.get(sym, {})

            trim = self.order_mgr.check_profit_trim(sym)
            if trim.get("qty_sold", 0) > 0 and sym in self._trade_entries:
                self._append_ledger_trim(
                    sym,
                    direction,
                    self._trade_entries[sym],
                    int(trim["qty_sold"]),
                    float(trim["pnl_leg"]),
                    int(trim.get("remaining_after", 0)),
                )
            if trim.get("closed_fully"):
                self.cb.record_result(float(trim["position_total_pnl"]))
                self._finalize_position_close(
                    sym,
                    direction,
                    "Profit lock (trim flat)",
                    float(trim["position_total_pnl"]),
                    int(trim["qty_sold"]),
                    float(trim["pnl_leg"]),
                )
                del self.signals[sym]
                continue

            if not self.order_mgr.has_position(sym):
                continue

            df_3m = get_candles_fresh(
                self.quote_ctx,
                sym,
                KLType.K_3M,
                count=30,
                max_age_bars=2.0,
                retries=3,
            )
            if df_3m.empty:
                continue
            age_3m = candle_age_seconds(df_3m)
            self._record_data_health(
                sym,
                "3m",
                age_sec=age_3m,
                stale_threshold_sec=12 * 60,
                source="exit_loop",
            )
            if age_3m > 12 * 60:
                print(
                    f"  ⚠  [{sym}] 3m feed stale ({age_3m/60:.1f}m) — skipping exit check this cycle"
                )
                continue

            current_price = df_3m["close"].iloc[-1]
            entry_opt_px = pos.get("entry_option_price", 0)
            entry_stock = pos.get("entry_stock_price", df_3m["close"].iloc[0])
            entry_stock_valid = bool(entry_stock and entry_stock > 0)
            if entry_stock_valid:
                price_delta = current_price - entry_stock
                if direction == "PUT":
                    price_delta = -price_delta
                leverage = (entry_opt_px / entry_stock * 100) if entry_stock > 0 else 1
                curr_opt_est = entry_opt_px + (price_delta * leverage)
                profit_locked = (
                    curr_opt_est >= entry_opt_px * PROFIT_LOCK_MULTIPLE and entry_opt_px > 0
                )
            else:
                # Adopted positions may not have a reliable stock-underlier entry basis.
                # Keep management active, but skip price-delta-derived lock/ATR logic.
                profit_locked = False

            if profit_locked and not pos.get("profit_lock_announced"):
                pos["profit_lock_announced"] = True
                print(
                    f"  🔒 [{sym}] Profit locked (+{PROFIT_LOCK_MULTIPLE*100-100:.0f}%) — "
                    f"strategy exits off; 2×ATR still on"
                )

            atr_stop = regime.atr * ATR_HARD_STOP_MULT
            stock_move = (
                entry_stock - current_price
                if direction == "CALL"
                else current_price - entry_stock
            ) if entry_stock_valid else 0.0
            if stock_move >= atr_stop and entry_stock_valid:
                print(
                    f"\n  [{sym}] EXIT — 2×ATR hard stop "
                    f"(move=${stock_move:.2f} ≥ stop=${atr_stop:.2f})"
                )
                result = self.order_mgr.exit_result(sym, reason="ATR stop")
                if result.get("success") and sym in self.signals:
                    pnl = float(result.get("pnl", 0.0))
                    self.cb.record_result(pnl)
                    self._log_exit(sym, direction, "ATR stop", pnl, result)
                    del self.signals[sym]
                else:
                    self.ops.alert(
                        f"⚠️ <b>FABIO exit failed [{sym}]</b>\nReason=ATR stop | "
                        f"error={result.get('error', 'unknown')}"
                    )
                    self.ops.log_alert(
                        "EXIT_FAIL",
                        f"{sym} ATR stop exit failed: {result.get('error', 'unknown')}",
                        sym,
                    )
                continue

            if not profit_locked:
                or_mid = (regime.or_high + regime.or_low) / 2.0
                if len(df_3m) >= 2:
                    c1 = float(df_3m["close"].iloc[-2])
                    c2 = float(df_3m["close"].iloc[-1])
                    midpoint_breach = (
                        direction == "CALL" and c1 < or_mid and c2 < or_mid
                    ) or (direction == "PUT" and c1 > or_mid and c2 > or_mid)
                    if midpoint_breach:
                        print(
                            f"\n  [{sym}] EXIT — 2 consecutive closes inside OR midpoint ({or_mid:.2f})"
                        )
                        result = self.order_mgr.exit_result(sym, reason="OR midpoint")
                        if result.get("success") and sym in self.signals:
                            pnl = float(result.get("pnl", 0.0))
                            self.cb.record_result(pnl)
                            self._log_exit(sym, direction, "OR midpoint", pnl, result)
                            del self.signals[sym]
                        else:
                            self.ops.alert(
                                f"⚠️ <b>FABIO exit failed [{sym}]</b>\nReason=OR midpoint | "
                                f"error={result.get('error', 'unknown')}"
                            )
                            self.ops.log_alert(
                                "EXIT_FAIL",
                                f"{sym} OR midpoint exit failed: {result.get('error', 'unknown')}",
                                sym,
                            )
                        continue

            if not profit_locked and len(df_3m) >= 23:
                closes = df_3m["close"]
                e10 = closes.ewm(span=10, adjust=False).mean()
                e20 = closes.ewm(span=20, adjust=False).mean()
                prev_diff = float(e10.iloc[-2]) - float(e20.iloc[-2])
                curr_diff = float(e10.iloc[-1]) - float(e20.iloc[-1])
                ema_cross = (
                    direction == "CALL" and prev_diff > 0 and curr_diff <= 0
                ) or (direction == "PUT" and prev_diff < 0 and curr_diff >= 0)
                if ema_cross:
                    print(f"\n  [{sym}] EXIT — EMA 10/20 cross on 3-min")
                    result = self.order_mgr.exit_result(sym, reason="EMA 10/20 cross")
                    if result.get("success") and sym in self.signals:
                        pnl = float(result.get("pnl", 0.0))
                        self.cb.record_result(pnl)
                        self._log_exit(sym, direction, "EMA cross", pnl, result)
                        del self.signals[sym]
                    else:
                        self.ops.alert(
                            f"⚠️ <b>FABIO exit failed [{sym}]</b>\nReason=EMA cross | "
                            f"error={result.get('error', 'unknown')}"
                        )
                        self.ops.log_alert(
                            "EXIT_FAIL",
                            f"{sym} EMA cross exit failed: {result.get('error', 'unknown')}",
                            sym,
                        )

    def _append_ledger_open(self, sym: str, direction: str, entry: dict) -> None:
        buf = getattr(self, "_trades_today", None)
        if buf is None:
            return
        qty = int(entry.get("contracts", 0) or 0)
        if qty <= 0:
            return
        buf.append(
            {
                "date": datetime.date.today().isoformat(),
                "symbol": sym,
                "direction": direction,
                "ledger_leg": "OPEN",
                "ledger_side": "BUY",
                "qty_leg": qty,
                "qty_after": qty,
                "contracts": qty,
                "entry_time": entry.get("entry_time", ""),
                "entry_price": round(float(entry.get("entry_price", 0) or 0), 4),
                "exit_time": "",
                "exit_price": 0.0,
                "pnl": 0.0,
                "pnl_leg": 0.0,
                "return_pct": 0.0,
                "exit_reason": "—",
                "exit_reason_code": "OPEN",
                "reason_source": REASON_SOURCE_STRATEGY,
                "reason_detail": "open leg",
                "include_in_session_pnl": False,
                "ledger_group_id": entry.get("ledger_group_id", ""),
                "vix": float(entry.get("vix", 0) or 0),
                "or_atr_pct": float(entry.get("or_atr_pct", 0) or 0),
                "trend": entry.get("trend", "") or "",
                "vix_regime": entry.get("vix_regime", "") or "",
                "day_color": entry.get("day_color", "") or "",
            }
        )
        self._enqueue_intraday_dashboard_refresh()

    def _append_ledger_trim(
        self,
        sym: str,
        direction: str,
        entry: dict,
        qty_sold: int,
        pnl_leg: float,
        remaining_after: int,
    ) -> None:
        buf = getattr(self, "_trades_today", None)
        if buf is None:
            return
        exit_t = datetime.datetime.now().strftime("%H:%M:%S")
        er = canonical_exit_reason("Profit trim", source=REASON_SOURCE_STRATEGY)
        buf.append(
            {
                "date": datetime.date.today().isoformat(),
                "symbol": sym,
                "direction": direction,
                "ledger_leg": "TRIM",
                "ledger_side": "SELL",
                "qty_leg": qty_sold,
                "qty_after": remaining_after,
                "contracts": qty_sold,
                "entry_time": entry.get("entry_time", ""),
                "entry_price": round(float(entry.get("entry_price", 0) or 0), 4),
                "exit_time": exit_t,
                "exit_price": 0.0,
                "pnl": 0.0,
                "pnl_leg": round(float(pnl_leg), 2),
                "return_pct": 0.0,
                "exit_reason": er.label,
                "exit_reason_code": er.code,
                "reason_source": er.source,
                "reason_detail": er.detail,
                "include_in_session_pnl": False,
                "ledger_group_id": entry.get("ledger_group_id", ""),
                "vix": float(entry.get("vix", 0) or 0),
                "or_atr_pct": float(entry.get("or_atr_pct", 0) or 0),
                "trend": entry.get("trend", "") or "",
                "vix_regime": entry.get("vix_regime", "") or "",
                "day_color": entry.get("day_color", "") or "",
            }
        )
        self._enqueue_intraday_dashboard_refresh()

    def _finalize_position_close(
        self,
        sym: str,
        direction: str,
        reason: str,
        total_pnl: float,
        qty_final_leg: int,
        pnl_final_leg: float,
    ) -> None:
        entry = self._trade_entries.pop(sym, {})
        capital = get_portfolio_value(self.trade_ctx)
        er = canonical_exit_reason(reason, source=REASON_SOURCE_STRATEGY)
        ct_orig = int(entry.get("contracts", 0) or 0)
        self.ops.log_trade_exit(
            sym,
            direction,
            entry.get("entry_time", ""),
            entry.get("entry_price", 0),
            datetime.datetime.now().strftime("%H:%M:%S"),
            0,
            total_pnl,
            er.label,
            entry.get("vix", 0),
            entry.get("or_atr_pct", 0),
            entry.get("strike", ""),
            entry.get("expiry", ""),
            ct_orig,
            capital,
            trend=entry.get("trend", ""),
            vix_regime=entry.get("vix_regime", ""),
            day_color=entry.get("day_color", ""),
            exit_reason_code=er.code,
            reason_source=er.source,
            reason_detail=er.detail,
        )
        ep = float(entry.get("entry_price", 0) or 0)
        cost = ep * ct_orig * 100
        ret_pct = round((total_pnl / cost) * 100, 2) if cost else 0

        qf = int(qty_final_leg) if qty_final_leg > 0 else ct_orig

        buf = getattr(self, "_trades_today", None)
        if buf is not None:
            buf.append(
                {
                "date": datetime.date.today().isoformat(),
                "symbol": sym,
                "direction": direction,
                "ledger_leg": "CLOSE",
                "ledger_side": "SELL",
                "qty_leg": qf,
                "qty_after": 0,
                "contracts": qf,
                "entry_time": entry.get("entry_time", ""),
                "entry_price": round(ep, 4),
                "exit_time": datetime.datetime.now().strftime("%H:%M:%S"),
                "exit_price": 0.0,
                "pnl": round(float(total_pnl), 2),
                "pnl_position_total": round(float(total_pnl), 2),
                "pnl_leg": round(float(pnl_final_leg), 2),
                "return_pct": ret_pct,
                "exit_reason": er.label,
                "exit_reason_code": er.code,
                "reason_source": er.source,
                "reason_detail": er.detail,
                "include_in_session_pnl": True,
                "ledger_group_id": entry.get("ledger_group_id", ""),
                "vix": float(entry.get("vix", 0) or 0),
                "or_atr_pct": float(entry.get("or_atr_pct", 0) or 0),
                "trend": entry.get("trend", "") or "",
                "vix_regime": entry.get("vix_regime", "") or "",
                "day_color": entry.get("day_color", "") or "",
                }
            )
            self._enqueue_intraday_dashboard_refresh()
        emoji = "🟩" if total_pnl >= 0 else "🟥"
        self.ops.alert(
            f"{emoji} <b>EXIT — {sym} {direction}</b>\n"
            f"Reason: {er.label}\n"
            f"P&L: ${total_pnl:+,.0f} ({ret_pct:+.1f}%)\n"
            f"Final slice: {qf} contracts | Entry size: {ct_orig} @ ${ep:.2f}"
        )

    def _log_exit(
        self,
        sym: str,
        direction: str,
        reason: str,
        pnl: float,
        result: dict | None = None,
    ) -> None:
        res = result or {}
        qty_final = int(res.get("qty_final_leg", 0) or 0)
        pnl_final = float(res.get("pnl_final_leg", pnl) or 0.0)
        preview = self._trade_entries.get(sym, {})
        if qty_final <= 0:
            qty_final = int(preview.get("contracts", 0) or 0)
        if pnl_final == 0 and pnl:
            pnl_final = float(pnl)
        self._finalize_position_close(
            sym, direction, reason, float(pnl), qty_final, pnl_final
        )

    def eod_close_all(self):
        print("\n  [EOD] Closing all positions...")
        for sym in list(self.signals.keys()):
            direction = self.signals[sym]
            result = self.order_mgr.exit_result(sym, reason="EOD")
            if result.get("success"):
                pnl = float(result.get("pnl", 0.0))
                self.cb.record_result(pnl)
                self._log_exit(sym, direction, "EOD", pnl, result)
                self.signals.pop(sym, None)
            else:
                self.ops.alert(
                    f"⚠️ <b>EOD exit failed [{sym}]</b>\nerror={result.get('error', 'unknown')}"
                )
                self.ops.log_alert(
                    "EXIT_FAIL", f"{sym} EOD exit failed: {result.get('error', 'unknown')}", sym
                )

        print("\n  [EOD] Sweeping Moomoo account for any remaining open positions...")
        try:
            ret, pos_df = self.trade_ctx.position_list_query(
                trd_env=self.order_mgr.trd_env
            )
            if ret != 0:
                print(f"  ⚠  Account sweep failed: {pos_df}")
                self.ops.alert(
                    "⚠️ <b>EOD sweep failed</b> — could not query Moomoo positions. Check manually."
                )
            elif pos_df.empty:
                print("  ✓ Account sweep clean — no orphaned positions found.")
            else:
                orphans = pos_df[pos_df["qty"] > 0]
                if orphans.empty:
                    print("  ✓ Account sweep clean — no orphaned positions found.")
                else:
                    print(f"  ⚠  Found {len(orphans)} orphaned position(s) — closing now:")
                    self.ops.alert(
                        f"⚠️ <b>EOD sweep found {len(orphans)} orphaned position(s)</b> — closing now."
                    )
                    for _, row in orphans.iterrows():
                        code = row.get("code", "")
                        qty = int(row.get("qty", 0))
                        raw_pl = row.get("unrealized_pl", 0)
                        try:
                            pl = float(raw_pl)
                        except (TypeError, ValueError):
                            pl = 0.0
                        print(f"   → Closing orphan: {code} qty={qty} unreal P&L=${pl:+.2f}")
                        self.order_mgr._sell(code, qty, label="EOD sweep")
        except Exception as e:
            print(f"  ⚠  Account sweep error: {e}")
            self.ops.alert(f"⚠️ <b>EOD sweep error</b>: {e}")

    def _log_eod_summary(self):
        print("\n  [4:00 PM] Logging session summary to Sheets + Dashboard...")

        capital = get_portfolio_value(self.trade_ctx)

        t_all = self._trades_today
        t_session = [t for t in t_all if t.get("include_in_session_pnl", True)]
        positions = aggregate_closed_positions(t_session)
        winners = [p for p in positions if p["pnl"] > 0]
        losers = [p for p in positions if p["pnl"] < 0]
        net_pnl = sum(p["pnl"] for p in positions)
        gw = sum(p["pnl"] for p in winners)
        gl = sum(p["pnl"] for p in losers)
        wr = round(len(winners) / len(positions) * 100, 1) if positions else 0
        dret = round((net_pnl / self._capital_at_open) * 100, 2) if self._capital_at_open else 0
        avg_win = (gw / len(winners)) if winners else 0
        avg_loss = (gl / len(losers)) if losers else 0
        wrf = len(winners) / len(positions) if positions else 0
        edge = round((avg_win * wrf) + (avg_loss * (1 - wrf)), 2)

        daily_dict = {
            "date": datetime.date.today().isoformat(),
            "total_trades": len(positions),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": wr,
            "net_pnl": round(net_pnl, 2),
            "gross_win": round(gw, 2),
            "gross_loss": round(gl, 2),
            "capital": round(capital, 2),
            "daily_return": dret,
            "proven_edge": edge,
        }

        open_positions: list = []
        try:
            ret, pos_df = self.trade_ctx.position_list_query(
                trd_env=self.order_mgr.trd_env
            )
            if ret == 0 and pos_df is not None and not pos_df.empty:
                active = pos_df[pos_df["qty"] > 0]
                if not active.empty:
                    open_positions = moomoo_position_records_to_dashboard_opens(
                        active.to_dict("records"),
                        as_of_date=daily_dict["date"],
                    )
        except Exception as e:
            print(f"  ⚠  EOD open positions snapshot failed: {e}")

        self.ops.log_daily_summary(t_session, capital, self._capital_at_open)
        self.ops.append_dashboard_session(t_all, daily_dict, open_positions)

        _eq_ann = modeled_equity_annotation_suffix()
        self.ops.log_alert(
            "INFO",
            f"Session complete — {len(positions)} closed position(s) | Capital: ${capital:,.0f}"
            f"{_eq_ann}",
            "",
        )

        emoji = "🟩" if net_pnl >= 0 else "🟥"
        self.ops.alert(
            f"{emoji} <b>EOD Summary — Fabio ORB</b>\n"
            f"Closed: {len(t_session)} | W/L: {len(winners)}/{len(losers)} ({wr:.0f}%)\n"
            f"Net P&L: ${net_pnl:+,.0f} ({dret:+.2f}%)\n"
            f"Ledger rows: {len(t_all)} (includes opens/trims)\n"
            f"Capital: ${capital:,.0f}{_eq_ann}"
        )
        self._trades_today.clear()

    def run(self):
        now = self._now_market()
        sched = get_session_schedule_for_now_et(now)
        if sched is None:
            td = now.date().isoformat()
            print(
                f"  [Calendar] NYSE closed {td} ({MARKET_TIMEZONE}). "
                "Exiting without trading loop."
            )
            self._emit_health_snapshot(force=True)
            self.ops.stop(timeout=8.0)
            self.quote_ctx.close()
            self.trade_ctx.close()
            return

        or_open = sched.session_open_et
        or_close = sched.or_end_et
        signal_end = sched.effective_signal_end_et
        eod_close = sched.eod_force_flatten_et
        eod_log = sched.eod_summary_et
        market_close = sched.market_close_et
        _eod_closed = False
        _eod_logged = False

        o_s = or_open.strftime("%H:%M")
        c_s = market_close.strftime("%H:%M")
        sig_s = signal_end.strftime("%H:%M")
        eod_s = eod_close.strftime("%H:%M")
        log_s = eod_log.strftime("%H:%M")

        if now < or_open:
            print(f"  Waiting for market open ({o_s} {MARKET_TIMEZONE})...")
        elif now <= signal_end:
            print(
                f"  Market open — signal + exit loops "
                f"(entries through {sig_s}; session close {c_s})."
            )
        elif now < market_close:
            print(
                f"  Market open (PM management) — exit loop only "
                f"until ~{eod_s} flatten / {c_s} close."
            )
        else:
            print(
                f"  Post-close startup — EOD handling "
                f"(session closed {c_s}; summary from {log_s})."
            )

        while True:
            now = self._now_market()

            if self.stopped:
                print("\n  [TG] /stop received — shutting down.")
                break

            self._check_ops_health()
            self._emit_health_snapshot()

            if or_open <= now < or_close:
                if not self._prefetched:
                    self.prefetch_or_window()
                else:
                    time.sleep(15)
                continue

            if now < or_open:
                time.sleep(30)
                continue

            if not self.regimes:
                self.initialize_day()

            if or_close <= now <= signal_end:
                self.run_signal_loop()

            if self.signals:
                self.run_exit_loop()

            if now >= eod_close and not _eod_closed:
                self.eod_close_all()
                print(
                    f"\n  [EOD] Positions closed. Waiting until {log_s} "
                    f"{MARKET_TIMEZONE} to log summary..."
                )
                self.regimes.clear()
                self.exit_tfs.clear()
                self.cb.reset()
                self._cb_logged.clear()
                _eod_closed = True

            if now >= eod_log and not _eod_logged:
                self._log_eod_summary()
                print("\n  [EOD] Done. Resetting for next session.")
                _eod_logged = True
                break

            if now >= market_close and _eod_closed:
                if not _eod_logged:
                    self._log_eod_summary()
                break

            sleep_sec = self._compute_loop_sleep_sec()
            self._update_loop_cadence_mode(sleep_sec)
            time.sleep(sleep_sec)

        self._emit_health_snapshot(force=True)
        self.ops.stop(timeout=8.0)
        self.quote_ctx.close()
        self.trade_ctx.close()
