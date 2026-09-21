"""
dashboard_writer.py — Generates fabio_live_dashboard.html from accumulated trade data.

The bot calls DashboardWriter.append_session() at EOD each day with a broker
snapshot for open_positions so the HTML table does not retain stale rows from
prior reconcile runs.
Data is persisted in ``backend/trade_data.json`` so history builds up across sessions.
Aggregated KPIs (win rate, net P&L, charts) use closed positions — one outcome
per CLOSE leg / round-trip — not raw ledger rows. Sheets Daily Summary uses the
same aggregation (aggregate_closed_positions).

Output files (dashboard / state under ``Fabio_bot/``):
  backend/trade_data.json   ← persistent data store
  frontend/live_dashboard.html  ← local HTML beside repo layout
  frontend/fabio_live_dashboard.html  ← same build, stable filename for publish scripts
"""

import json
import os
import re
import datetime
from typing import Any

from pathlib import Path

from manual_position_omissions import is_omitted_dashboard_close_trade

try:
    from fabio_live.paper_books import is_allowed_open_position_notes as _book_notes_ok
except ImportError:  # pragma: no cover - dashboard-only hosts
    def _book_notes_ok(notes: str) -> bool:
        n = str(notes or "").strip()
        if n.startswith("broker code=") or n == "moomoo_paper_fifo":
            return True
        return False


def is_allowed_open_position_notes(notes: str) -> bool:
    """Open-row notes: preserve ``moomoo_paper_fifo`` plus other paper-book tags."""
    return _book_notes_ok(notes)

_FABIO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE   = str(_FABIO_ROOT / "backend" / "trade_data.json")
DASH_LOCAL  = str(_FABIO_ROOT / "frontend" / "live_dashboard.html")
DASH_MAIN   = str(_FABIO_ROOT / "frontend" / "fabio_live_dashboard.html")

# OS option symbol token after market prefix (e.g. SPY + expiry + C/P + strike).
_OPTION_CODE_CORE_RE = re.compile(r"([A-Z]+)\d{6}([CP])\d+")


def dashboard_row_derived_from_moomoo_sync(t: Any) -> bool:
    """
    True if this persisted dashboard trade originates from broker history / reconcile
    (canonical Moomoo pipeline), vs bot-authored ledger-only rows appended at EOD.
    """
    if not isinstance(t, dict):
        return False
    notes_l = str(t.get("notes", "") or "").lower()
    reason = str(t.get("exit_reason", "") or "").strip().lower()
    if reason == "moomoo fill backfill":
        return True
    if reason == "reconciled fill close":
        return True
    if "source=moomoo_paper" in notes_l:
        return True
    if notes_l.strip() == "moomoo_paper_fifo":
        return True
    return False


def _is_manually_omitted_dashboard_trade(t: Any) -> bool:
    return is_omitted_dashboard_close_trade(t)


def normalize_and_validate_open_positions(opens: list[Any]) -> tuple[list[dict], int]:
    """
    Keep only rows that look like broker or FIFO snapshots (not trade-log dicts).

    Allowed `notes`: ``broker code=...`` (EOD Moomoo snapshot), ``moomoo_paper_fifo``
    (reconciled Moomoo FIFO — preserved), or other paper-book tags
    (``tradier_paper_fifo``, ``source=tradier_paper``, ``source=mr``,
    ``source=mr_tradier``, …). Drops non-dicts, rows with ``exit_reason``, invalid
    symbol/contracts, or unrecognized ``notes``.
    """
    if not opens:
        return [], 0
    kept: list[dict] = []
    dropped = 0
    for item in opens:
        if not isinstance(item, dict):
            dropped += 1
            continue
        if "exit_reason" in item:
            dropped += 1
            continue
        sym = str(item.get("symbol", "")).strip()
        try:
            ct = int(float(item.get("contracts", 0) or 0))
        except (TypeError, ValueError):
            ct = 0
        if ct <= 0 or not sym:
            dropped += 1
            continue
        notes = str(item.get("notes", "")).strip()
        if not is_allowed_open_position_notes(notes):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def trade_group_key(t: dict) -> str:
    """Stable bucket key for ledger rows belonging to one position (matches dashboard JS)."""
    gid = str(t.get("ledger_group_id") or "").strip()
    if gid:
        return f"gid:{gid}"
    ep = t.get("entry_price")
    ep_s = str(ep) if ep is not None and ep != "" else ""
    parts = [
        str(t.get("date") or ""),
        str(t.get("symbol") or ""),
        str(t.get("direction") or ""),
        str(t.get("entry_time") or ""),
        ep_s,
    ]
    return "fb:" + "|".join(parts)


def _position_closed_net_pnl(legs_sorted: list[dict], close_leg: dict) -> float:
    """CLOSE row totals first; if absent, sum non-OPEN ``pnl_leg`` (trim slices)."""

    def _f(v: Any) -> float | None:
        if v is None:
            return None
        if isinstance(v, str) and not str(v).strip():
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    close_legs_only = [
        x
        for x in legs_sorted
        if str(x.get("ledger_leg") or "CLOSE").upper() == "CLOSE"
    ]
    # FIFO reconcile emits one dashboard row per matched sell slice; they share a
    # synthetic open key (same entry_time / entry_price). Prefer summing slice P&L;
    # using only the last row's pnl_position_total undercounts (e.g. QQQ 3 exits).
    if len(close_legs_only) > 1:
        total_slices = 0.0
        for x in close_legs_only:
            pl = _f(x.get("pnl"))
            if pl is not None:
                total_slices += pl
                continue
            leg = _f(x.get("pnl_leg"))
            total_slices += leg if leg is not None else 0.0
        return round(total_slices, 2)

    pt = _f(close_leg.get("pnl_position_total"))
    if pt is not None:
        return round(pt, 2)
    pv = _f(close_leg.get("pnl"))
    if pv is not None:
        return round(pv, 2)
    leg_sum = 0.0
    for x in legs_sorted:
        if str(x.get("ledger_leg") or "").upper() == "OPEN":
            continue
        pl = _f(x.get("pnl_leg"))
        leg_sum += pl if pl is not None else 0.0
    return round(leg_sum, 2)


def aggregate_closed_positions(trades: list) -> list[dict]:
    """
    One entry per *closed* position for KPIs, daily rollups, and win rate.

    P&L: ``pnl_position_total`` or ``pnl`` on the CLOSE row; if both missing,
    sums ``pnl_leg`` on non-OPEN rows (trims + final slice).

    OPEN/TRIM rows with ``include_in_session_pnl: false`` are included when
    their ``ledger_group_id`` matches any session-counted row so legs stay grouped.

    Groups with no CLOSE leg (still open) are skipped; legacy single-row trades
    count as one close.
    """
    from collections import defaultdict

    session_gids: set[str] = set()
    for t in trades:
        if not isinstance(t, dict):
            continue
        if t.get("include_in_session_pnl") is False:
            continue
        g = str(t.get("ledger_group_id") or "").strip()
        if g:
            session_gids.add(g)

    by_key: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if not isinstance(t, dict):
            continue
        if t.get("include_in_session_pnl") is False:
            g = str(t.get("ledger_group_id") or "").strip()
            if not g or g not in session_gids:
                continue
        by_key[trade_group_key(t)].append(t)

    def leg_order(row: dict) -> tuple:
        x = str(row.get("ledger_leg") or "CLOSE").upper()
        o = 0 if x == "OPEN" else 1 if x == "TRIM" else 2
        return (o, str(row.get("entry_time") or ""), str(row.get("exit_time") or ""))

    out: list[dict] = []
    for legs in by_key.values():
        legs_sorted = sorted(legs, key=leg_order)
        close_leg: dict | None = None
        for x in reversed(legs_sorted):
            if str(x.get("ledger_leg") or "CLOSE").upper() == "CLOSE":
                close_leg = x
                break
        if close_leg is None:
            if len(legs_sorted) == 1:
                close_leg = legs_sorted[0]
            else:
                continue
        pnl = _position_closed_net_pnl(legs_sorted, close_leg)
        open_leg = next(
            (
                x
                for x in legs_sorted
                if str(x.get("ledger_leg") or "").upper() == "OPEN"
            ),
            legs_sorted[0],
        )
        pos_date = str(close_leg.get("date") or open_leg.get("date") or "")
        out.append(
            {
                "pnl": round(pnl, 2),
                "symbol": open_leg.get("symbol"),
                "direction": open_leg.get("direction"),
                "date": pos_date,
                "legs": legs_sorted,
                "close_leg": close_leg,
                "open_leg": open_leg,
            }
        )
    return out


def _build_daily_from_dashboard_trades(trades: list[dict]) -> list[dict]:
    """Recompute dashboard daily aggregates from current closed positions."""
    from collections import defaultdict

    grouped: dict[str, list[dict]] = defaultdict(list)
    for p in aggregate_closed_positions(trades):
        d = str(p.get("date") or "").strip()
        if d:
            grouped[d].append(p)

    out: list[dict] = []
    for d in sorted(grouped.keys()):
        plist = grouped[d]
        pnls = [float(p.get("pnl") or 0.0) for p in plist]
        winners = [x for x in pnls if x > 0]
        losers = [x for x in pnls if x < 0]
        total = len(plist)
        net = round(sum(pnls), 2)
        wr = round((len(winners) / total) * 100, 1) if total else 0.0
        avg_win = (sum(winners) / len(winners)) if winners else 0.0
        avg_loss = (sum(losers) / len(losers)) if losers else 0.0
        wr_frac = (len(winners) / total) if total else 0.0
        out.append(
            {
                "date": d,
                "total_trades": total,
                "winners": len(winners),
                "losers": len(losers),
                "win_rate": wr,
                "net_pnl": net,
                "gross_win": round(sum(winners), 2),
                "gross_loss": round(sum(losers), 2),
                "capital": 0.0,
                "daily_return": 0.0,
                "proven_edge": round((avg_win * wr_frac) + (avg_loss * (1 - wr_frac)), 2),
            }
        )
    return out


def _build_buy_hold_overlay_from_daily(
    daily_rows: list[dict],
    *,
    symbols: tuple[str, ...] = ("SPY", "QQQ", "NVDA"),
    base_notional: float = 10_000.0,
) -> dict:
    """
    Build normalized buy-and-hold P&L overlays aligned to dashboard daily dates.
    Returns {"dates": [...], "base_notional": ..., "series": {SYM: [pnl,...]}}.
    If data fetch fails, returns an empty series payload.
    """
    dates = sorted(
        {
            str(d.get("date") or "").strip()
            for d in (daily_rows or [])
            if str(d.get("date") or "").strip()
        }
    )
    out = {"dates": dates, "base_notional": float(base_notional), "series": {}}
    if not dates:
        return out

    try:
        import pandas as pd
        import yfinance as yf
    except Exception:
        return out

    start = dates[0]
    # yfinance `end` is exclusive; include one extra day so the final date is covered.
    try:
        end_dt = datetime.date.fromisoformat(dates[-1]) + datetime.timedelta(days=1)
        end = end_dt.isoformat()
    except Exception:
        end = None

    for sym in symbols:
        try:
            hist = yf.Ticker(sym).history(
                start=start,
                end=end,
                interval="1d",
                auto_adjust=True,
            )
            if hist is None or hist.empty:
                continue
            close = hist.get("Close")
            if close is None or close.empty:
                continue
            close = pd.to_numeric(close, errors="coerce").dropna()
            if close.empty:
                continue
            close.index = pd.to_datetime(close.index).tz_localize(None).normalize()

            # Align to dashboard dates using last available close <= date.
            idx = pd.to_datetime(dates, errors="coerce").normalize()
            aligned = close.reindex(idx, method="ffill")
            aligned = aligned.dropna()
            if aligned.empty:
                continue

            first = float(aligned.iloc[0])
            if first <= 0:
                continue
            pnl = (((aligned / first) - 1.0) * float(base_notional)).round(2)
            # Map back to all dates, preserving order and filling missing with None.
            pnl_by_date = {
                d.strftime("%Y-%m-%d"): float(v)
                for d, v in zip(aligned.index.to_pydatetime(), pnl.tolist())
            }
            out["series"][sym] = [pnl_by_date.get(d) for d in dates]
        except Exception:
            continue

    return out


def moomoo_position_records_to_dashboard_opens(
    records: list[Any],
    as_of_date: str | None = None,
) -> list[dict]:
    """
    Build dashboard `open_positions` dicts from Moomoo `position_list_query` rows
    (use only rows with qty > 0). Shape matches reconciled open inventory entries.
    """
    today = as_of_date or datetime.date.today().isoformat()
    out: list[dict] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code", "")).strip()
        try:
            qty = int(float(row.get("qty", 0) or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty < 0:
            qty = abs(qty)
        if qty <= 0:
            try:
                csq = int(float(row.get("can_sell_qty", 0) or 0))
            except (TypeError, ValueError):
                csq = 0
            if csq > 0:
                qty = csq
        if qty <= 0 or not code:
            continue

        raw = code.split(".")[-1]
        m = _OPTION_CODE_CORE_RE.match(raw)
        if not m:
            continue

        symbol = m.group(1)
        direction = "CALL" if m.group(2) == "C" else "PUT"

        ep = 0.0
        for k in ("cost_price", "average_cost", "avg_price", "nominal_price"):
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                try:
                    ep = float(v)
                    break
                except (TypeError, ValueError):
                    continue

        out.append(
            {
                "date": today,
                "symbol": symbol,
                "direction": direction,
                "entry_time": "—",
                "entry_price": round(ep, 4),
                "contracts": qty,
                "vix": 0.0,
                "or_atr_pct": 0.0,
                "notes": f"broker code={code}",
            }
        )
    return out


def tradier_position_records_to_dashboard_opens(
    records: list[Any],
    as_of_date: str | None = None,
    *,
    source: str = "tradier_paper",
) -> list[dict]:
    """Dashboard opens from Tradier positions. Never tagged ``moomoo_paper_fifo``."""
    today = as_of_date or datetime.date.today().isoformat()
    tag = str(source or "tradier_paper").strip() or "tradier_paper"
    notes = tag if tag in ("tradier_paper_fifo", "mr_tradier_paper_fifo") else f"source={tag}"
    out: list[dict] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        code = str(row.get("symbol") or row.get("option_symbol") or row.get("code") or "").strip()
        try:
            qty = int(float(row.get("quantity") or row.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0 or not code:
            continue
        raw = code.split(".")[-1]
        m = _OPTION_CODE_CORE_RE.match(raw.upper())
        if not m:
            continue
        symbol = m.group(1)
        direction = "CALL" if m.group(2) == "C" else "PUT"
        ep = 0.0
        for k in ("cost_basis", "average_cost", "cost_price", "avg_price"):
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                try:
                    ep = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        out.append(
            {
                "date": today,
                "symbol": symbol,
                "direction": direction,
                "entry_time": "—",
                "entry_price": round(ep, 4),
                "contracts": qty,
                "vix": 0.0,
                "or_atr_pct": 0.0,
                "notes": notes,
                "source": tag,
            }
        )
    return out


# ── HTML template — __DATA_JSON__ is replaced at write time ──────────────────
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0d0d0d">
<title>ORBit Bot — Live Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root {
  --bg:      #0d0d0d;
  --surface: #161616;
  --border:  #252525;
  --text:    #e8e8e8;
  --muted:   #777;
  --green:   #00e676;
  --red:     #ff5252;
  --blue:    #4fc3f7;
  --purple:  #bc8cff;
  --yellow:  #ffd740;
  --orange:  #ff9100;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px; min-height: 100vh;
  min-height: 100dvh;
  padding-bottom: max(60px, env(safe-area-inset-bottom));
  -webkit-text-size-adjust: 100%;
  overscroll-behavior-y: contain;
}
.table-scroll {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior-x: contain;
}
@media (prefers-reduced-motion: reduce) {
  .collapse-btn .chev { transition: none; }
}

/* ── Header ── */
header {
  background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 20px max(32px, env(safe-area-inset-left)) 20px max(32px, env(safe-area-inset-right));
  display: flex; align-items: center; gap: 24px;
}
.header-title { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
.header-sub   { color: var(--muted); font-size: 13px; }
.header-right { margin-left: auto; text-align: right; }
.header-updated { font-size: 12px; color: var(--muted); }
.header-equity-note { max-width: 420px; text-align: right; }
.header-top-row { display: inline-flex; align-items: center; gap: 8px; }
.title-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

@media (max-width: 768px) {
  .header-equity-note { max-width: none; text-align: left; }
}
.badge {
  display: inline-block; padding: 2px 9px; border-radius: 4px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.4px;
  background: rgba(0,230,118,0.12); color: var(--green);
  border: 1px solid rgba(0,230,118,0.25);
}
.badge-beta {
  background: rgba(255, 215, 64, 0.10);
  color: #ffd740;
  border-color: rgba(255, 215, 64, 0.35);
}
.header-btn {
  display: inline-block; padding: 2px 9px; border-radius: 4px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase;
  background: rgba(79,195,247,0.12); color: var(--blue);
  border: 1px solid rgba(79,195,247,0.30); cursor: pointer;
}
.header-btn:hover {
  background: rgba(79,195,247,0.18);
  border-color: rgba(79,195,247,0.55);
}

/* ── Rules modal ── */
.modal-backdrop {
  position: fixed; inset: 0; z-index: 1200;
  background: rgba(0,0,0,0.62);
  display: none; align-items: center; justify-content: center;
  padding: 16px;
}
.modal-backdrop.is-open { display: flex; }
.modal-card {
  width: min(980px, 100%); max-height: 88vh; overflow: auto;
  border: 1px solid var(--border); border-radius: 12px;
  background: var(--surface);
  box-shadow: 0 16px 48px rgba(0,0,0,0.45);
}
.modal-head {
  position: sticky; top: 0; z-index: 2;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 14px 16px; border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, #1a1a1a 0%, #161616 100%);
}
.modal-title {
  font-size: 12px; font-weight: 700; letter-spacing: 0.5px;
  text-transform: uppercase; color: var(--muted);
}
.modal-close {
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg); color: var(--text);
  font-size: 12px; font-weight: 700; padding: 6px 10px; cursor: pointer;
}
.modal-close:hover { border-color: #555; }
.modal-body { padding: 14px 16px 18px; font-size: 13px; line-height: 1.45; color: var(--text); }
.rules-note { color: var(--muted); margin-bottom: 8px; }
.rules-section { margin-top: 12px; }
.rules-section h4 {
  margin: 0 0 6px; font-size: 12px; color: var(--blue);
  text-transform: uppercase; letter-spacing: 0.45px;
}
.rules-section ul { margin: 0; padding-left: 18px; }
.rules-section li { margin: 4px 0; }

/* ── Page wrapper ── */
.page {
  max-width: 1400px; margin: 0 auto;
  padding: 28px max(28px, env(safe-area-inset-left)) 0 max(28px, env(safe-area-inset-right));
}

/* ── Section headings ── */
.dashboard-section-head { margin-bottom: 16px; }
.dashboard-section-head .section-title {
  font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--text);
}
.dashboard-section-head .section-caption {
  font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.35;
}

/* Trading view: same surface + controls language as table-card / filters */
.trading-view-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 22px;
  margin-bottom: 14px;
}
.trading-view-panel > .tv-toolbar-row {
  margin-bottom: 14px;
}
.tv-toolbar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.tv-toolbar-row:focus-visible {
  outline: none;
}
.tv-toolbar-group {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.tv-toolbar-sep {
  width: 1px;
  height: 26px;
  background: var(--border);
  flex-shrink: 0;
  align-self: center;
}
.tv-btn {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.2;
  cursor: pointer;
  font-family: inherit;
  touch-action: manipulation;
  -webkit-tap-highlight-color: rgba(79, 195, 247, 0.15);
}
.tv-btn:hover { border-color: #555; color: var(--text); }
.tv-btn.tv-btn-active {
  background: rgba(79, 195, 247, 0.12);
  border-color: rgba(79, 195, 247, 0.45);
  color: var(--blue);
}
@media (pointer: coarse) {
  .tv-btn {
    padding: 8px 14px;
    font-size: 13px;
    min-height: 40px;
  }
  .tv-toolbar-sep { height: 32px; }
}
.tv-chart-wrap {
  position: relative;
  width: 100%;
  border-radius: 8px;
  overflow: hidden;
  background: #131722;
  border: 1px solid var(--border);
  min-height: 400px;
  height: min(760px, 72vh);
  max-height: 85vh;
  height: min(760px, 72dvh);
  max-height: 85dvh;
}
.tv-chart-wrap iframe {
  display: block;
  width: 100%;
  height: 100%;
  min-height: 380px;
  border: none;
}
.tv-multi-wrap {
  display: none;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  width: 100%;
  height: 100%;
  min-height: 380px;
  padding: 8px;
}
.tv-multi-wrap iframe {
  width: 100%;
  height: 100%;
  min-height: 360px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #131722;
}
@media (max-width: 980px) {
  .tv-multi-wrap { grid-template-columns: 1fr; }
  .tv-multi-wrap iframe { min-height: 300px; }
}
.performance-analytics-panel {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 20px 8px;
  margin: 18px 0 14px;
  background: rgba(255,255,255,0.02);
}
.perf-head-row {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 12px;
}
.collapse-btn {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  touch-action: manipulation;
  -webkit-tap-highlight-color: rgba(79,195,247,0.15);
  flex-shrink: 0;
}
.collapse-btn:hover { border-color: #555; }
.collapse-btn .chev { display: inline-block; margin-right: 6px; transition: transform 180ms ease; }
.collapse-btn[data-collapsed="1"] .chev { transform: rotate(-90deg); }
.perf-collapsed .perf-body { display: none; }

/* ── KPI Cards ── */
.kpi-grid {
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 14px;
  margin-top: 14px;
  margin-bottom: 24px;
}
.kpi-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 20px;
}
.kpi-label { font-size: 11px; color: var(--muted); text-transform: uppercase;
             letter-spacing: 0.6px; margin-bottom: 8px; }
.kpi-value { font-size: 26px; font-weight: 700; line-height: 1; }
.kpi-sub   { font-size: 11px; color: var(--muted); margin-top: 5px; }
.green { color: var(--green); }
.red   { color: var(--red); }
.blue  { color: var(--blue); }
.purple{ color: var(--purple); }
.yellow{ color: var(--yellow); }

/* ── Chart rows ── */
.charts-row {
  display: grid; gap: 14px; margin-bottom: 14px;
}
.charts-row.row-2-1 { grid-template-columns: 2fr 1fr; }
.charts-row.row-2-equal { grid-template-columns: 1fr 1fr; }
.charts-row.row-3   { grid-template-columns: 1fr 1fr 1fr; }

.chart-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 20px 22px;
}
.chart-title {
  font-size: 13px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 16px;
}
.chart-canvas { position: relative; width: 100%; }

/* ── Trades Table ── */
.table-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 22px; margin-top: 14px;
}
.table-header {
  display: flex; align-items: center; gap: 16px; margin-bottom: 16px;
  flex-wrap: wrap;
}
.table-header h3 { font-size: 13px; font-weight: 600; color: var(--muted);
                   text-transform: uppercase; letter-spacing: 0.5px; }
.filters { display: flex; gap: 8px; margin-left: auto; flex-wrap: wrap; align-items: center; }
.filter-select {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  background-color: rgba(255, 255, 255, 0.045);
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'%3E%3Cpath d='M2 3.5L5 7l3-3.5' fill='none' stroke='%23999' stroke-width='1.25' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 11px center;
  background-size: 10px 10px;
  color: var(--text);
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 8px;
  padding: 7px 32px 7px 12px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
  line-height: 1.35;
  cursor: pointer;
  outline: none;
  min-height: 38px;
  min-width: 0;
  max-width: 100%;
  touch-action: manipulation;
  -webkit-tap-highlight-color: rgba(79, 195, 247, 0.15);
  transition: border-color 0.15s ease, background-color 0.15s ease, box-shadow 0.15s ease;
}
.filter-select:hover {
  background-color: rgba(255, 255, 255, 0.07);
  border-color: rgba(255, 255, 255, 0.16);
}
.filter-select:focus-visible {
  border-color: rgba(79, 195, 247, 0.55);
  box-shadow: 0 0 0 2px rgba(79, 195, 247, 0.18);
}
@media (pointer: coarse) {
  .filter-select {
    min-height: 44px;
    padding-top: 9px;
    padding-bottom: 9px;
  }
}

table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead th {
  background: var(--bg); color: var(--muted); font-weight: 600;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px;
  padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border);
  cursor: pointer; user-select: none; white-space: nowrap;
}
thead th:hover { color: var(--text); }
thead th .sort-arrow { font-size: 10px; margin-left: 4px; opacity: 0.4; }
thead th.sorted .sort-arrow { opacity: 1; color: var(--blue); }
tbody tr { border-bottom: 1px solid var(--border); }
tbody tr:hover { background: rgba(255,255,255,0.03); }
tbody td { padding: 9px 12px; vertical-align: middle; }
.pnl-pos { color: var(--green); font-weight: 600; }
.pnl-neg { color: var(--red); font-weight: 600; }
.dir-call { color: var(--blue); font-weight: 600; font-size: 11px; }
.dir-put  { color: var(--purple); font-weight: 600; font-size: 11px; }
.trend-with    { background: rgba(0,230,118,0.12); color: var(--green);
                 padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.trend-counter { background: rgba(255,145,0,0.12); color: var(--orange);
                 padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.trend-bull    { background: rgba(79,195,247,0.14); color: var(--blue);
                 padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 700; }
.trend-bear    { background: rgba(188,140,255,0.14); color: var(--purple);
                 padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 700; }
.day-green  { color: var(--green); font-size: 11px; }
.day-yellow { color: var(--yellow); font-size: 11px; }
.day-red    { color: var(--red); font-size: 11px; }
.empty-state {
  text-align: center; padding: 40px; color: var(--muted); font-size: 13px;
}
.row-count { font-size: 12px; color: var(--muted); margin-left: 8px; }
.open-tag {
  color: var(--yellow);
  font-size: 11px;
  font-weight: 600;
}
.leg-open  { color: var(--blue); font-weight: 600; font-size: 11px; }
.leg-trim  { color: var(--yellow); font-weight: 600; font-size: 11px; }
.leg-close { color: var(--green); font-weight: 600; font-size: 11px; }
.side-buy  { color: var(--green); font-weight: 600; font-size: 11px; }
.side-sell { color: var(--orange); font-weight: 600; font-size: 11px; }

/* Ledger rows: BUY vs SELL tint + leg pills */
#tradesTable tbody tr.ledger-row-buy {
  background: rgba(0, 230, 118, 0.055);
  box-shadow: inset 4px 0 0 rgba(0, 230, 118, 0.42);
}
#tradesTable tbody tr.ledger-row-sell {
  background: rgba(255, 145, 0, 0.055);
  box-shadow: inset 4px 0 0 rgba(255, 145, 0, 0.42);
}
#tradesTable tbody tr.ledger-row-buy td,
#tradesTable tbody tr.ledger-row-sell td {
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.leg-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.leg-badge-open  { background: rgba(79,195,247,0.18); color: var(--blue); border: 1px solid rgba(79,195,247,0.38); }
.leg-badge-trim  { background: rgba(255,215,64,0.16); color: var(--yellow); border: 1px solid rgba(255,215,64,0.42); }
.leg-badge-close { background: rgba(0,230,118,0.12); color: var(--green); border: 1px solid rgba(0,230,118,0.3); }
.side-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.03em;
}
.side-badge-buy  { background: rgba(0,230,118,0.14); color: var(--green); border: 1px solid rgba(0,230,118,0.35); }
.side-badge-sell { background: rgba(255,145,0,0.14); color: var(--orange); border: 1px solid rgba(255,145,0,0.38); }
.pos-pnl-total-hint {
  display: block;
  font-size: 10px;
  color: var(--muted);
  font-weight: 500;
  margin-top: 2px;
  letter-spacing: 0.02em;
}

/* Grouped position cards (default trade log view) */
.trades-grouped-wrap {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.trade-group-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.trade-group-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 20px;
  padding: 14px 18px;
  background: rgba(255,255,255,0.02);
  border-bottom: 1px solid var(--border);
}
.trade-group-head h4 {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin: 0;
}
.trade-group-head .meta {
  font-size: 12px;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
}
.trade-group-head .pos-pnl-total {
  margin-left: auto;
  font-size: 15px;
  font-weight: 700;
}
.trade-group-legs {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.trade-group-legs th {
  text-align: left;
  padding: 8px 14px;
  color: var(--muted);
  font-weight: 600;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.trade-group-legs td {
  padding: 10px 14px;
  border-bottom: 1px solid rgba(255,255,255,0.05);
  vertical-align: middle;
}
.trade-group-legs tr.ledger-row-buy td:first-child {
  box-shadow: inset 3px 0 0 rgba(0,230,118,0.45);
}
.trade-group-legs tr.ledger-row-sell td:first-child {
  box-shadow: inset 3px 0 0 rgba(255,145,0,0.45);
}
.trade-group-legs tr:last-child td {
  border-bottom: none;
}

.trade-day-section {
  margin-bottom: 22px;
}
.trade-day-section:last-child {
  margin-bottom: 0;
}
.trade-day-head-bar {
  display: flex;
  align-items: center;
  width: 100%;
  box-sizing: border-box;
  background: var(--bg);
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.4px;
}
.trade-day-title {
  display: inline-flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.trade-day-title strong {
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.trade-day-net {
  margin-left: auto;
  flex-shrink: 0;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  letter-spacing: 0.02em;
  text-transform: none;
}
.trade-day-head-bar .muted {
  color: var(--muted);
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.trade-day-more-btn {
  display: inline-block;
  margin: 8px 4px 0;
  padding: 8px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--blue);
  background: rgba(79,195,247,0.08);
  border: 1px solid rgba(79,195,247,0.35);
  border-radius: 8px;
  cursor: pointer;
}
.trade-day-more-btn:hover {
  background: rgba(79,195,247,0.14);
}
.trade-day-extra.is-hidden {
  display: none;
}
.leg-rel {
  min-width: 72px;
  vertical-align: middle;
}
.leg-rel-track {
  height: 6px;
  border-radius: 3px;
  background: rgba(255,255,255,0.06);
  overflow: hidden;
  max-width: 88px;
}
.leg-rel-fill {
  height: 100%;
  border-radius: 3px;
  min-width: 2px;
}
.leg-rel-fill.leg-rel-pos {
  background: linear-gradient(90deg, rgba(0,230,118,0.25), rgba(0,230,118,0.85));
}
.leg-rel-fill.leg-rel-neg {
  background: linear-gradient(90deg, rgba(255,82,82,0.25), rgba(255,82,82,0.85));
}

@media (max-width: 900px) {
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
  .charts-row.row-2-1 { grid-template-columns: 1fr; }
  .charts-row.row-2-equal { grid-template-columns: 1fr; }
  .charts-row.row-3   { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  header {
    flex-direction: column;
    align-items: flex-start;
    gap: 16px;
    padding: 16px max(16px, env(safe-area-inset-left)) 16px max(16px, env(safe-area-inset-right));
  }
  .header-title { font-size: clamp(17px, 4.5vw, 20px); }
  .header-right {
    margin-left: 0;
    text-align: left;
    width: 100%;
  }
  .page {
    padding: 16px max(14px, env(safe-area-inset-left)) 0 max(14px, env(safe-area-inset-right));
  }
  .trading-view-panel {
    padding: 16px 14px;
  }
  .trading-view-panel > .tv-toolbar-row {
    justify-content: flex-start;
    margin-bottom: 12px;
  }
  .performance-analytics-panel {
    padding: 14px 14px 6px;
    border-radius: 10px;
  }
  .tv-chart-wrap {
    min-height: 340px;
    height: min(620px, 62vh);
    height: min(620px, 62dvh);
  }
  .chart-card { padding: 14px 16px; }
}

@media (max-width: 600px) {
  body { font-size: 13px; }
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
    margin-bottom: 18px;
  }
  .kpi-card { padding: 14px 12px; }
  .kpi-value { font-size: clamp(1.125rem, 6vw, 1.625rem); }
  .table-card { padding: 14px 12px; border-radius: 10px; }
  .table-header {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
  }
  .filters {
    margin-left: 0;
    width: 100%;
    gap: 8px;
    flex-direction: column;
    align-items: stretch;
  }
  .filter-select { width: 100%; }
  .trade-group-head {
    flex-direction: column;
    align-items: flex-start;
  }
  .trade-group-head .pos-pnl-total { margin-left: 0; }
  thead th,
  tbody td { padding-left: 8px; padding-right: 8px; font-size: 12px; }
  /* Shorter Chart.js wrapper heights vs desktop inline heights */
  .chart-card .chart-canvas { height: 176px !important; }
  .trade-day-more-btn { min-height: 44px; padding: 10px 16px; }
}

@media (max-width: 400px) {
  .kpi-grid { grid-template-columns: 1fr; }
  .tv-toolbar-row {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }
  .tv-toolbar-sep {
    display: none;
  }
  .tv-toolbar-group {
    justify-content: flex-start;
    width: 100%;
  }
}
/* Older cached builds may still ship #betaGitMeta; keep hidden before/without JS. */
#betaGitMeta { display: none !important; }
</style>
</head>
<body>

<header>
  <div>
    <div class="title-row">
      <div class="header-title">ORBit Bot <span style="color:var(--muted);font-weight:400">— Live Dashboard</span></div>
      <button type="button" class="header-btn" id="rulesBtn">Ruleset</button>
    </div>
    <div class="header-sub" style="margin-top:4px">SPY · QQQ · NVDA &nbsp;|&nbsp; 0-1DTE Options &nbsp;|&nbsp; ORB Strategy &nbsp;|&nbsp; Initial Capital $10,000</div>
  </div>
    <div class="header-right">
    <div class="header-top-row" style="margin-bottom:6px">
      <span class="badge badge-beta" id="betaBadge" title="Beta channel — see beta_manifest.json in repo">BETA</span>
      <span class="badge">PAPER TRADING</span>
    </div>
    <div class="header-updated">Last updated: <span id="lastUpdated">—</span></div>
  </div>
</header>

<div class="page">

  <!-- KPI Cards -->
  <div class="kpi-grid" role="region" aria-label="Key stats">
    <div class="kpi-card">
      <div class="kpi-label">Total Trades</div>
      <div class="kpi-value blue" id="kpiTrades">—</div>
      <div class="kpi-sub" id="kpiDays">— sessions</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Win Rate</div>
      <div class="kpi-value" id="kpiWinRate">—</div>
      <div class="kpi-sub" id="kpiWL">— W / — L</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Net P&amp;L</div>
      <div class="kpi-value" id="kpiNetPnl">—</div>
      <div class="kpi-sub" id="kpiNetSub">all time</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Proven Edge</div>
      <div class="kpi-value" id="kpiEdge">—</div>
      <div class="kpi-sub">per trade expectancy</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Win</div>
      <div class="kpi-value green" id="kpiAvgWin">—</div>
      <div class="kpi-sub" id="kpiAvgWinSub">— trades</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Loss</div>
      <div class="kpi-value red" id="kpiAvgLoss">—</div>
      <div class="kpi-sub" id="kpiAvgLossSub">— trades</div>
    </div>
  </div>

  <div class="trading-view-panel" id="tradingViewPanel">
    <div class="tv-toolbar-row" role="toolbar" aria-label="TradingView chart — watchlist and tool bar (search symbols in the chart)">
      <div class="tv-toolbar-group" role="group" aria-label="Ticker">
        <button type="button" class="tv-btn tv-btn-active" data-tv-symbol="SPY">SPY</button>
        <button type="button" class="tv-btn" data-tv-symbol="QQQ">QQQ</button>
        <button type="button" class="tv-btn" data-tv-symbol="NVDA">NVDA</button>
        <button type="button" class="tv-btn" data-tv-symbol="ALL">ALL</button>
      </div>
      <span class="tv-toolbar-sep" aria-hidden="true"></span>
      <button type="button" class="tv-btn" id="tvDrawBarBtn"
        aria-pressed="false"
        title="The free TradingView embed cannot shrink the toolbar—only hide it. Off = wider chart.">Tool bar</button>
      <span class="tv-toolbar-sep" aria-hidden="true"></span>
      <button type="button" class="tv-btn" id="tvOpenPositionsBtn"
        title="Jump to symbols that currently have open positions.">Open positions</button>
      <span class="tv-toolbar-sep" aria-hidden="true"></span>
      <button type="button" class="tv-btn" id="tvFullscreenBtn"
        aria-pressed="false"
        title="Toggle full screen for the TradingView panel.">Full screen</button>
    </div>

    <div class="tv-chart-wrap">
      <iframe id="tradingViewFrame" title="TradingView chart" src="about:blank" loading="lazy"></iframe>
      <div class="tv-multi-wrap" id="tradingViewMultiWrap" aria-label="TradingView multi-chart view">
        <iframe id="tradingViewFrameSPY" title="TradingView SPY chart" src="about:blank" loading="lazy"></iframe>
        <iframe id="tradingViewFrameQQQ" title="TradingView QQQ chart" src="about:blank" loading="lazy"></iframe>
        <iframe id="tradingViewFrameNVDA" title="TradingView NVDA chart" src="about:blank" loading="lazy"></iframe>
      </div>
    </div>
  </div><!-- /trading-view-panel -->

  <!-- Open Positions -->
  <div class="table-card">
    <div class="table-header">
      <h3>Open positions <span class="row-count" id="openRowCount"></span></h3>
    </div>
    <div class="table-scroll">
      <table id="openPositionsTable">
        <thead>
          <tr>
            <th>Date</th>
            <th>Symbol</th>
            <th>Dir</th>
            <th>Entry</th>
            <th>Entry $</th>
            <th>Contracts</th>
            <th>VIX</th>
            <th>OR/ATR</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="openTableBody"></tbody>
      </table>
    </div>
  </div>

  <div class="performance-analytics-panel" id="performanceAnalyticsPanel">
    <section class="dashboard-section-head" aria-label="Performance analytics">
      <div class="perf-head-row">
        <div class="section-title">Performance analytics</div>
        <button type="button" class="collapse-btn" id="perfAnalyticsToggle" aria-controls="perfAnalyticsBody">
          <span class="chev" aria-hidden="true">▾</span>
          <span class="label">Collapse</span>
        </button>
      </div>
    </section>

  <div class="perf-body" id="perfAnalyticsBody">
  <!-- Row 1: Equity Curve + Daily P&L -->
  <div class="charts-row row-2-1">
    <div class="chart-card">
      <div class="chart-title">Equity Curve — Cumulative P&amp;L ($)</div>
      <div class="chart-canvas" style="height:220px">
        <canvas id="equityChart"></canvas>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Daily P&amp;L ($)</div>
      <div class="chart-canvas" style="height:220px">
        <canvas id="dailyChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Row 2: Symbol / Direction / Exit Reason -->
  <div class="charts-row row-3">
    <div class="chart-card">
      <div class="chart-title">Symbol Performance ($)</div>
      <div class="chart-canvas" style="height:190px">
        <canvas id="symbolChart"></canvas>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Direction Breakdown</div>
      <div class="chart-canvas" style="height:190px">
        <canvas id="directionChart"></canvas>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Exit Reasons</div>
      <div class="chart-canvas" style="height:190px">
        <canvas id="exitChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Row 3: VIX + win rate (ORB trades with-trend only; no counter-trend split chart) -->
  <div class="charts-row row-2-equal">
    <div class="chart-card">
      <div class="chart-title">VIX Regime Distribution</div>
      <div class="chart-canvas" style="height:160px">
        <canvas id="vixChart"></canvas>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Win Rate by Symbol (%)</div>
      <div class="chart-canvas" style="height:160px">
        <canvas id="winRateSymbolChart"></canvas>
      </div>
    </div>
  </div>
  </div><!-- /perf-body -->
  </div><!-- /performance-analytics-panel -->

  <!-- Filled Trades Table -->
  <div class="table-card">
    <div class="table-header">
      <div>
        <h3>All Trades <span class="row-count" id="rowCount"></span></h3>
      </div>
      <div class="filters">
        <select class="filter-select" id="tradeViewMode" onchange="renderTable()" title="Layout">
          <option value="grouped" selected>By position</option>
          <option value="flat">Flat table</option>
        </select>
        <select class="filter-select" id="filterSymbol" onchange="renderTable()">
          <option value="">All Symbols</option>
          <option>SPY</option><option>QQQ</option><option>NVDA</option>
        </select>
        <select class="filter-select" id="filterDir" onchange="renderTable()">
          <option value="">All Directions</option>
          <option>CALL</option><option>PUT</option>
        </select>
        <select class="filter-select" id="filterTrend" onchange="renderTable()">
          <option value="">All Trends</option>
          <option value="BULL">Bull</option>
          <option value="BEAR">Bear</option>
        </select>
        <select class="filter-select" id="filterExit" onchange="renderTable()">
          <option value="">All Exits</option>
          <option value="OR midpoint">OR midpoint</option>
          <option value="EMA crossover">EMA crossover</option>
          <option value="EOD close">EOD close</option>
          <option value="Force close">Force close</option>
          <option value="Profit lock close">Profit lock close</option>
          <option value="Profit trim">Profit trim</option>
        </select>
        <select class="filter-select" id="filterResult" onchange="renderTable()">
          <option value="">Win &amp; Loss</option>
          <option value="win">Winners Only</option>
          <option value="loss">Losers Only</option>
        </select>
      </div>
    </div>
    <div id="tradesGroupedMount" class="trades-grouped-wrap"></div>
    <div id="tradesFlatMount" class="table-scroll" style="display:none">
      <table id="tradesTable">
        <thead>
          <tr>
            <th onclick="sortTable('date')">Date <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('symbol')">Symbol <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('direction')">Dir <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('ledger_leg')">Leg <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('qty_leg')">Qty <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('qty_after')">Rem <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('pnl_leg')">Leg P&amp;L <span class="sort-arrow">↕</span></th>
            <th>Entry</th>
            <th onclick="sortTable('entry_price')">Entry $ <span class="sort-arrow">↕</span></th>
            <th>Exit $</th>
            <th>Exit</th>
            <th onclick="sortTable('pnl')">Pos P&amp;L <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('return_pct')">Return % <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('exit_reason')">Exit Reason <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('vix')">VIX <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('or_atr_pct')">OR/ATR <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('trend')">Trend <span class="sort-arrow">↕</span></th>
            <th onclick="sortTable('day_color')">Day <span class="sort-arrow">↕</span></th>
          </tr>
        </thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>
  </div>

</div><!-- /page -->

<div class="modal-backdrop" id="rulesModal" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="rulesModalTitle">
    <div class="modal-head">
      <div class="modal-title" id="rulesModalTitle">Fabio ORB Ruleset (Educational)</div>
      <button type="button" class="modal-close" id="rulesCloseBtn">Close</button>
    </div>
    <div class="modal-body">
      <div class="rules-note">
        Source of truth: <code>backend/backtest/Fabio_orb_backtest.py</code> (BacktestMode.RESEARCH), <code>backend/backtest/fabio/settings.py</code>, <code>backend/backtest/fabio/engine.py</code>, <code>backend/backtest/fabio/signals.py</code>, <code>backend/backtest/fabio/reporting.py</code>. Educational only - not advice.
      </div>
      <div class="rules-section">
        <h4>Universe, Data, Instrument Model</h4>
        <ul>
          <li>Universe: SPY, QQQ, NVDA.</li>
          <li>RTH assumptions: ET session, 5m signal bars, exits prefer 3m closes when available.</li>
          <li>VIX input: prior daily <code>^VIX</code> close from yfinance (fallback 18 if missing).</li>
          <li>Options model: Black-Scholes, OPTION_DTE=1, IV_BASE=0.20, slippage/commission modeled.</li>
        </ul>
      </div>
      <div class="rules-section">
        <h4>Risk and Circuit Breakers</h4>
        <ul>
          <li>VIX risk tiers: &lt;14 not tradeable; 14-16 half risk; 16-20 normal; 20-28 aggressive; above 28 half.</li>
          <li>Risk cap <code>RISK_PCT_MAX=10%</code>.</li>
          <li>Halt new entries if day P&amp;L ≤ -2% day-start capital.</li>
          <li>Max 3 exits/day across symbols; 3-loss streak halves risk; max 3 simultaneous positions.</li>
          <li>Sizing: <code>risk_dollars / (entry_opt_px * 100)</code>, minimum 1 contract.</li>
        </ul>
      </div>
      <div class="rules-section">
        <h4>Opening Range and Day Filters</h4>
        <ul>
          <li>OR = high/low from 09:30-09:44 ET on 5m bars.</li>
          <li>OR/ATR(14): skip below 8%; 0.75x for 8-15%; 1.0x for 15-60%; 0.75x above 60%.</li>
          <li>Gap filter: skip if gap ≥ 3.0%; if 1.5-3.0%, require OR boundary retest.</li>
          <li>Daily trend filter: bullish if EMA10&gt;EMA20 and close&gt;EMA50; counter-trend disabled.</li>
        </ul>
      </div>
      <div class="rules-section">
        <h4>Entry Rules (Research)</h4>
        <ul>
          <li>Scan window: 09:45-14:00 ET on closed 5m bars.</li>
          <li>CALL: two consecutive closes above OR high. PUT: two consecutive closes below OR low.</li>
          <li>Entry gate: VIX ≥ 16.1.</li>
          <li>When 20&lt;VIX≤28, CALL entries are skipped (PUT allowed).</li>
        </ul>
      </div>
      <div class="rules-section">
        <h4>Exit Rules</h4>
        <ul>
          <li>Strategy exit (unless profit-locked): two closes past OR midpoint or EMA10/EMA20 cross against trade.</li>
          <li>Profit lock at 1.2x entry premium; trims and hard stop still active.</li>
          <li>Hard stop: 2x daily ATR adverse move from entry stock price.</li>
          <li>Trim ladder: 2x, 4x, 8x multiples; trim 50% of remaining contracts.</li>
          <li>
            Primary bot EOD flatten: FABIO_EOD_CLOSE_BEFORE_SESSION_MINUTES (default 15) before
            official XNYS session close; early-close days follow the exchange calendar.
          </li>
        </ul>
      </div>
      <div class="rules-section">
        <h4>Reported Portfolio Stats</h4>
        <ul>
          <li>Core: count, wins/losses, win rate, avg win/loss, profit factor, expectancy, total P&amp;L/return, max drawdown, Sharpe, final capital.</li>
          <li>Slices: by exit reason, direction, trend, symbol.</li>
          <li>Artifacts: <code>Fabio_backtest_trades.csv</code>, <code>Fabio_backtest_equity.csv</code>, <code>Fabio_backtest_report.png</code>.</li>
        </ul>
      </div>
    </div>
  </div>
</div>

<script>
// ── Data ──────────────────────────────────────────────────────────────────────
const DATA = __DATA_JSON__;
const trades = DATA.trades || [];
const openPositions = DATA.open_positions || [];
const daily  = (DATA.daily || []).slice().sort((a,b) => a.date.localeCompare(b.date));

(function initBetaBadge() {
  var legacyMeta = document.getElementById('betaGitMeta');
  if (legacyMeta) {
    legacyMeta.textContent = '';
    legacyMeta.remove();
  }
  var badge = document.getElementById('betaBadge');
  if (badge) badge.textContent = 'BETA';
})();

(function initRulesModal() {
  const btn = document.getElementById('rulesBtn');
  const modal = document.getElementById('rulesModal');
  const closeBtn = document.getElementById('rulesCloseBtn');
  if (!btn || !modal || !closeBtn) return;

  function openModal() {
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
  }
  function closeModal() {
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
  }
  btn.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal.classList.contains('is-open')) closeModal();
  });
})();

(function initTradingViewIframe() {
  var frame = document.getElementById('tradingViewFrame');
  var multiWrap = document.getElementById('tradingViewMultiWrap');
  var frameSPY = document.getElementById('tradingViewFrameSPY');
  var frameQQQ = document.getElementById('tradingViewFrameQQQ');
  var frameNVDA = document.getElementById('tradingViewFrameNVDA');
  if (!frame) return;
  var TV_WATCHLIST = { SPY: 'AMEX:SPY', QQQ: 'NASDAQ:QQQ', NVDA: 'NASDAQ:NVDA' };
  var state = {
    tvSymbol: TV_WATCHLIST.SPY,
    interval: '5',
    drawBarVisible: false,
    openPosCycleIdx: 0,
    multiAll: false
  };
  function openPositionSymbols() {
    var seen = {};
    var out = [];
    (openPositions || []).forEach(function (p) {
      var qty = Number(p && p.contracts != null ? p.contracts : 0);
      if (!(qty > 0)) return;
      var sym = String((p && p.symbol) || '').toUpperCase().trim();
      if (!TV_WATCHLIST[sym] || seen[sym]) return;
      seen[sym] = true;
      out.push(sym);
    });
    return out;
  }
  function syncPresetHighlights() {
    var tv = state.tvSymbol;
    document.querySelectorAll('[data-tv-symbol]').forEach(function (b) {
      var key = String(b.getAttribute('data-tv-symbol') || '').toUpperCase();
      if (key === 'ALL') {
        b.classList.toggle('tv-btn-active', state.multiAll);
      } else {
        b.classList.toggle('tv-btn-active', !state.multiAll && !!(key && TV_WATCHLIST[key] === tv));
      }
    });
  }
  function tvUrlForSymbol(symbolFull) {
    var sym = encodeURIComponent(symbolFull);
    return (
      'https://www.tradingview-widget.com/embed-widget/advanced-chart/'
      + '?autosize=true&symbol=' + sym
      + '&interval=' + encodeURIComponent(state.interval)
      + '&timezone=' + encodeURIComponent('America/New_York')
      + '&theme=dark&style=1&locale=en'
      + '&hide_top_toolbar=false'
      /* TV embed evaluates +hide_side_toolbar; strings "true"/"false" are NaN (ignored). Use 1/0. */
      + '&hide_side_toolbar=' + (state.drawBarVisible ? '0' : '1')
      + '&hide_legend=false'
      + '&save_image=false&calendar=false&allow_symbol_change=true'
    );
  }
  function tvUrl() {
    return tvUrlForSymbol(state.tvSymbol);
  }
  function reload() {
    if (state.multiAll) {
      frame.style.display = 'none';
      if (multiWrap) {
        multiWrap.style.display = 'grid';
      }
      if (frameSPY) frameSPY.src = tvUrlForSymbol(TV_WATCHLIST.SPY);
      if (frameQQQ) frameQQQ.src = tvUrlForSymbol(TV_WATCHLIST.QQQ);
      if (frameNVDA) frameNVDA.src = tvUrlForSymbol(TV_WATCHLIST.NVDA);
      return;
    }
    if (multiWrap) multiWrap.style.display = 'none';
    frame.style.display = 'block';
    frame.src = tvUrl();
  }
  document.querySelectorAll('[data-tv-symbol]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var key = String(btn.getAttribute('data-tv-symbol') || 'SPY').toUpperCase();
      if (key === 'ALL') {
        state.multiAll = true;
      } else {
        state.multiAll = false;
        state.tvSymbol = TV_WATCHLIST[key] || TV_WATCHLIST.SPY;
      }
      syncPresetHighlights();
      reload();
    });
  });
  // Timeframe buttons removed; keep default interval via state.interval.
  var drawBarBtn = document.getElementById('tvDrawBarBtn');
  if (drawBarBtn) {
    // Ensure initial UI state matches default (toolbar hidden).
    drawBarBtn.classList.toggle('tv-btn-active', state.drawBarVisible);
    drawBarBtn.setAttribute('aria-pressed', state.drawBarVisible ? 'true' : 'false');
    drawBarBtn.addEventListener('click', function () {
      state.drawBarVisible = !state.drawBarVisible;
      drawBarBtn.classList.toggle('tv-btn-active', state.drawBarVisible);
      drawBarBtn.setAttribute('aria-pressed', state.drawBarVisible ? 'true' : 'false');
      reload();
    });
  }
  var panel = document.getElementById('tradingViewPanel');
  var fullscreenBtn = document.getElementById('tvFullscreenBtn');
  var openPosBtn = document.getElementById('tvOpenPositionsBtn');
  if (panel && fullscreenBtn) {
    function syncFullscreenButton() {
      var isFs = document.fullscreenElement === panel;
      fullscreenBtn.classList.toggle('tv-btn-active', isFs);
      fullscreenBtn.setAttribute('aria-pressed', isFs ? 'true' : 'false');
      fullscreenBtn.textContent = isFs ? 'Exit full screen' : 'Full screen';
    }
    fullscreenBtn.addEventListener('click', function () {
      if (document.fullscreenElement === panel) {
        if (document.exitFullscreen) document.exitFullscreen();
        return;
      }
      if (panel.requestFullscreen) panel.requestFullscreen();
    });
    document.addEventListener('fullscreenchange', syncFullscreenButton);
    syncFullscreenButton();
  }
  if (openPosBtn) {
    openPosBtn.addEventListener('click', function () {
      var syms = openPositionSymbols();
      if (!syms.length) {
        openPosBtn.classList.remove('tv-btn-active');
        openPosBtn.textContent = 'Open positions';
        return;
      }
      var idx = state.openPosCycleIdx % syms.length;
      var sym = syms[idx];
      state.openPosCycleIdx = (idx + 1) % syms.length;
      state.multiAll = false;
      state.tvSymbol = TV_WATCHLIST[sym] || TV_WATCHLIST.SPY;
      syncPresetHighlights();
      reload();
      openPosBtn.classList.add('tv-btn-active');
      openPosBtn.textContent = syms.length > 1 ? ('Open: ' + sym + ' (' + state.openPosCycleIdx + '/' + syms.length + ')') : ('Open: ' + sym);
    });
    // Prime button state on load.
    var initialSyms = openPositionSymbols();
    openPosBtn.disabled = !initialSyms.length;
    if (!initialSyms.length) {
      openPosBtn.textContent = 'Open positions';
      openPosBtn.title = 'No open positions currently.';
    }
  }
  syncPresetHighlights();
  function scheduleTvInitialLoad() {
    function go() { reload(); }
    if (typeof window.requestIdleCallback === 'function') {
      window.requestIdleCallback(go, { timeout: 1800 });
    } else {
      setTimeout(go, 16);
    }
  }
  scheduleTvInitialLoad();
})();

function isSessionTrade(t) {
  return t.include_in_session_pnl !== false;
}

function isStrategyExitAttribution(closeLeg) {
  if (!closeLeg) return false;
  const source = String(closeLeg.reason_source || '').trim().toLowerCase();
  if (source) return source === 'strategy';
  const code = String(closeLeg.exit_reason_code || '').trim().toUpperCase();
  if (code) return code !== 'RECONCILED_CLOSE';
  const reason = String(closeLeg.exit_reason || '').trim().toLowerCase();
  return reason !== 'reconciled fill close' && reason !== 'moomoo fill backfill';
}

function tradeGroupKey(t) {
  const gid = String(t.ledger_group_id || '').trim();
  if (gid) return 'gid:' + gid;
  const ep = t.entry_price != null ? String(t.entry_price) : '';
  return 'fb:' + [t.date, t.symbol, t.direction, t.entry_time, ep].join('|');
}

function legOrder(ledgerLeg) {
  const x = ledgerLeg || 'CLOSE';
  if (x === 'OPEN') return 0;
  if (x === 'TRIM') return 1;
  return 2;
}

function groupLatestSortKey(legs) {
  let best = '';
  for (const t of legs) {
    const d = String(t.date || '');
    const tail = String(t.exit_time || t.entry_time || '');
    const cand = d + ' ' + tail;
    if (cand > best) best = cand;
  }
  return best;
}

function sortLegsWithinGroup(legs) {
  return legs.slice().sort((a, b) => {
    const ra = legOrder(a.ledger_leg), rb = legOrder(b.ledger_leg);
    if (ra !== rb) return ra - rb;
    const ta = String(a.entry_time || '') + String(a.exit_time || '');
    const tb = String(b.entry_time || '') + String(b.exit_time || '');
    return ta.localeCompare(tb);
  });
}

function resolvePositionPnl(sorted, closeLeg) {
  const parseNum = v => {
    if (v == null) return null;
    if (typeof v === 'string' && v.trim() === '') return null;
    const n = Number(v);
    return Number.isNaN(n) ? null : n;
  };
  const closes = sorted.filter(t => (t.ledger_leg || 'CLOSE') === 'CLOSE');
  if (closes.length > 1) {
    let s = 0;
    for (const t of closes) {
      const p = parseNum(t.pnl);
      if (p != null) s += p;
      else s += Number(t.pnl_leg || 0);
    }
    return Math.round(s * 100) / 100;
  }
  const pt = parseNum(closeLeg.pnl_position_total);
  if (pt != null) return Math.round(pt * 100) / 100;
  const pv = parseNum(closeLeg.pnl);
  if (pv != null) return Math.round(pv * 100) / 100;
  let s = 0;
  for (const t of sorted) {
    if ((t.ledger_leg || 'CLOSE') === 'OPEN') continue;
    s += Number(t.pnl_leg || 0);
  }
  return Math.round(s * 100) / 100;
}

function sessionClosedPositions() {
  const sessionGids = new Set();
  for (const t of trades) {
    if (!isSessionTrade(t)) continue;
    const g = String(t.ledger_group_id || '').trim();
    if (g) sessionGids.add(g);
  }
  const rows = trades.filter(t => {
    if (isSessionTrade(t)) return true;
    const g = String(t.ledger_group_id || '').trim();
    return g && sessionGids.has(g);
  });
  const byKey = new Map();
  for (const t of rows) {
    const k = tradeGroupKey(t);
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k).push(t);
  }
  const out = [];
  for (const legs of byKey.values()) {
    const sorted = sortLegsWithinGroup(legs);
    let closeLeg = null;
    for (let i = sorted.length - 1; i >= 0; i--) {
      if ((sorted[i].ledger_leg || 'CLOSE') === 'CLOSE') {
        closeLeg = sorted[i];
        break;
      }
    }
    if (!closeLeg && sorted.length === 1) closeLeg = sorted[0];
    if (!closeLeg) continue;
    if (!isSessionTrade(closeLeg)) continue;
    const posPnl = resolvePositionPnl(sorted, closeLeg);
    const openLeg = sorted.find(l => (l.ledger_leg || '') === 'OPEN') || sorted[0];
    const day = closeLeg.date || openLeg.date || '';
    out.push({ legs: sorted, posPnl, closeLeg, openLeg, day });
  }
  return out;
}

/** Prefer persisted `daily` rows; otherwise derive daily net from closed positions (keeps charts alive). */
function dailySeriesForCharts() {
  if (daily.length) return daily;
  const positions = sessionClosedPositions();
  if (!positions.length) return [];
  const byD = new Map();
  for (const p of positions) {
    const d = String(p.day || '').trim();
    if (!d) continue;
    byD.set(d, (byD.get(d) || 0) + p.posPnl);
  }
  return Array.from(byD.entries())
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, net]) => ({ date, net_pnl: parseFloat(Number(net).toFixed(2)) }));
}

function legPnlScaleForGroup(legs) {
  let m = 0;
  for (const t of legs) {
    const leg = t.ledger_leg || 'CLOSE';
    if (leg === 'OPEN') continue;
    m = Math.max(m, Math.abs(Number(t.pnl_leg || 0)));
  }
  return m > 0 ? m : 1e-6;
}

function trendBucket(t) {
  const raw = String(t && t.trend ? t.trend : '').trim().toUpperCase();
  if (raw === 'BULL' || raw === 'BEAR') return raw;
  if (raw === 'WITH' || raw === 'COUNTER' || raw === 'RECON' || !raw) {
    const dir = String(t && t.direction ? t.direction : '').trim().toUpperCase();
    if (dir === 'CALL') return 'BULL';
    if (dir === 'PUT') return 'BEAR';
  }
  return raw;
}

function filterTradeRows(sym, dir, trend, exitR, result) {
  let rows = trades.filter(t => {
    if (sym    && t.symbol      !== sym)    return false;
    if (dir    && t.direction   !== dir)    return false;
    if (trend  && trendBucket(t) !== trend) return false;
    return true;
  });
  if (!exitR && result !== 'win' && result !== 'loss') return rows;

  const byKey = new Map();
  for (const t of rows) {
    const k = tradeGroupKey(t);
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k).push(t);
  }
  const keep = new Set();
  for (const [k, legs] of byKey.entries()) {
    const sorted = sortLegsWithinGroup(legs);
    let closeLeg = null;
    for (let i = sorted.length - 1; i >= 0; i--) {
      if ((sorted[i].ledger_leg || 'CLOSE') === 'CLOSE') {
        closeLeg = sorted[i];
        break;
      }
    }
    if (!closeLeg && sorted.length === 1) closeLeg = sorted[0];
    if (exitR && (!closeLeg || closeLeg.exit_reason !== exitR)) continue;
    if (result === 'win' || result === 'loss') {
      if (!closeLeg || !isSessionTrade(closeLeg)) continue;
      const EPS = 1e-6;
      const posPnl = resolvePositionPnl(sorted, closeLeg);
      if (result === 'win' && posPnl <= EPS) continue;
      if (result === 'loss' && posPnl >= -EPS) continue;
    }
    keep.add(k);
  }
  return rows.filter(t => keep.has(tradeGroupKey(t)));
}

function toggleDayExtra(btn) {
  const id = btn.getAttribute('data-target');
  const n = parseInt(btn.getAttribute('data-n'), 10) || 0;
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('is-hidden');
  btn.textContent = el.classList.contains('is-hidden')
    ? ('Show ' + n + ' more positions')
    : ('Hide extra positions');
}

// ── Chart defaults ────────────────────────────────────────────────────────────
Chart.defaults.color           = '#777';
Chart.defaults.borderColor     = '#252525';
Chart.defaults.font.family     = '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
Chart.defaults.font.size       = 11;
Chart.defaults.plugins.legend.labels.boxWidth  = 10;
Chart.defaults.plugins.legend.labels.padding   = 14;

const GRID = { color: 'rgba(255,255,255,0.05)', drawTicks: false };
const TICK = { color: '#555', padding: 8 };

function fmtPnl(v) {
  const sign = v >= 0 ? '+' : '';
  return sign + '$' + v.toFixed(2);
}
function fmtPct(v) {
  const sign = v >= 0 ? '+' : '';
  return sign + v.toFixed(1) + '%';
}

// ── KPI Cards ─────────────────────────────────────────────────────────────────
(function buildKpis() {
  const positions = sessionClosedPositions();
  const n       = positions.length;
  const EPS = 1e-6;
  const winners = positions.filter(p => p.posPnl > EPS);
  const losers  = positions.filter(p => p.posPnl < -EPS);
  const breakeven = positions.filter(p => Math.abs(p.posPnl) <= EPS).length;
  const netPnl  = positions.reduce((s, p) => s + p.posPnl, 0);
  const avgWin  = winners.length ? winners.reduce((s,p)=>s+p.posPnl,0)/winners.length : 0;
  const avgLoss = losers.length  ? losers.reduce((s,p)=>s+p.posPnl,0)/losers.length   : 0;
  const wr      = n ? winners.length / n : 0;
  const edge    = (avgWin * wr) + (avgLoss * (1 - wr));

  function set(id, val) { document.getElementById(id).textContent = val; }
  function cls(id, cls) { document.getElementById(id).className = 'kpi-value ' + cls; }

  set('kpiTrades', n);
  set('kpiDays',   daily.length + (daily.length === 1 ? ' session' : ' sessions'));
  set('kpiWinRate', n ? (wr * 100).toFixed(1) + '%' : '—');
  cls('kpiWinRate', wr >= 0.5 ? 'kpi-value green' : 'kpi-value red');
  document.getElementById('kpiWL').innerHTML = winners.length + ' W / ' + losers.length + ' L'
    + (breakeven ? ' / <span style="color:var(--muted)">' + breakeven + ' BE</span>' : '')
    + ' <span style="color:var(--muted);font-weight:500;font-size:11px">(' + n + ' closed)</span>';
  set('kpiNetPnl', n ? fmtPnl(netPnl) : '—');
  cls('kpiNetPnl', netPnl >= 0 ? 'kpi-value green' : 'kpi-value red');
  set('kpiEdge', n ? fmtPnl(edge) : '—');
  cls('kpiEdge', edge >= 0 ? 'kpi-value green' : 'kpi-value red');
  set('kpiAvgWin',  winners.length ? fmtPnl(avgWin) : '—');
  set('kpiAvgWinSub', winners.length + ' positions');
  set('kpiAvgLoss', losers.length ? fmtPnl(avgLoss) : '—');
  set('kpiAvgLossSub', losers.length + ' positions');

  const dc = dailySeriesForCharts();
  const lastDaily = daily.length ? daily[daily.length - 1] : (dc.length ? dc[dc.length - 1] : null);
  const sessionDate = lastDaily ? lastDaily.date : new Date().toISOString().slice(0, 10);
  const genIso = DATA.dashboard_generated_at_utc;
  function formatEasternFromIso(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return '';
    try {
      var f = new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hourCycle: 'h23',
        timeZoneName: 'short',
      });
      var parts = f.formatToParts(d);
      function g(t) {
        var p = parts.find(function (x) { return x.type === t; });
        return p ? p.value : '';
      }
      return g('year') + '-' + g('month') + '-' + g('day') + ' ' + g('hour') + ':' + g('minute') + ':' + g('second') + ' ' + g('timeZoneName');
    } catch (e) {
      return '';
    }
  }
  const et = formatEasternFromIso(genIso);
  const ts = et || sessionDate;
  document.getElementById('lastUpdated').textContent = ts;
})();

// ── Performance charts (lazy: skipped when panel starts collapsed on mobile) ─
function resizePerfCharts() {
  const ids = ['equityChart','dailyChart','symbolChart','directionChart','exitChart','vixChart','winRateSymbolChart'];
  ids.forEach(function (id) {
    var el = document.getElementById(id);
    if (!el || !window.Chart) return;
    var c = Chart.getChart(el);
    if (c && typeof c.resize === 'function') c.resize();
  });
}

function initAnalyticsCharts() {
  if (window.__fabioPerfChartsDone) return;
  window.__fabioPerfChartsDone = true;
  var compactTicks = (window.matchMedia && window.matchMedia('(max-width: 600px)').matches);

  // ── Equity Curve ──────────────────────────────────────────────────────────
  {
  const dc = dailySeriesForCharts();
  if (dc.length) {
  let cum = 0;
  const labels = dc.map(d => d.date);
  const values = dc.map(d => { cum += (d.net_pnl || 0); return parseFloat(cum.toFixed(2)); });
  const isPos  = values[values.length - 1] >= 0;
  const datasets = [{
    label: 'Strategy Cumulative P&L ($)',
    data: values,
    borderColor: isPos ? '#00e676' : '#ff5252',
    backgroundColor: isPos ? 'rgba(0,230,118,0.07)' : 'rgba(255,82,82,0.07)',
    fill: true, tension: 0.35, pointRadius: 3,
    pointBackgroundColor: isPos ? '#00e676' : '#ff5252',
    borderWidth: 2,
  }];

  const bh = DATA.buy_hold_overlay || {};
  const bhDates = Array.isArray(bh.dates) ? bh.dates : [];
  const bhSeries = (bh && bh.series) ? bh.series : {};
  const bhPalette = { SPY: '#4fc3f7', QQQ: '#bc8cff', NVDA: '#ffd740' };
  const bhSymbols = ['SPY', 'QQQ', 'NVDA'];
  const hasSameTimeline = bhDates.length === labels.length && bhDates.every((d, i) => d === labels[i]);
  if (hasSameTimeline) {
    bhSymbols.forEach(sym => {
      const arr = Array.isArray(bhSeries[sym]) ? bhSeries[sym] : null;
      if (!arr || !arr.length) return;
      datasets.push({
        label: sym + ' Buy & Hold P&L ($)',
        data: arr,
        borderColor: bhPalette[sym] || '#aaa',
        backgroundColor: 'rgba(0,0,0,0)',
        borderDash: [6, 4],
        fill: false,
        tension: 0.25,
        pointRadius: 0,
        borderWidth: 1.8,
      });
    });
  }

  new Chart(document.getElementById('equityChart'), {
    type: 'line',
    data: {
      labels,
      datasets
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: true, position: 'bottom' }, tooltip: {
        callbacks: { label: ctx => ' ' + fmtPnl(ctx.raw) }
      }},
      scales: {
        x: { grid: GRID, ticks: { ...TICK, maxTicksLimit: compactTicks ? 5 : 8 } },
        y: { grid: GRID, ticks: { ...TICK, callback: v => '$' + v } }
      }
    }
  });
  }
  }

  // ── Daily P&L Bars ────────────────────────────────────────────────────────────
  {
  const dc = dailySeriesForCharts();
  if (dc.length) {
  const labels = dc.map(d => d.date.slice(5));  // MM-DD
  const values = dc.map(d => d.net_pnl || 0);
  const colors = values.map(v => v >= 0 ? 'rgba(0,230,118,0.75)' : 'rgba(255,82,82,0.75)');

  new Chart(document.getElementById('dailyChart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label: 'Daily P&L ($)', data: values, backgroundColor: colors, borderRadius: 3 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: ctx => ' ' + fmtPnl(ctx.raw) }
      }},
      scales: {
        x: { grid: GRID, ticks: { ...TICK, maxRotation: compactTicks ? 45 : 0 } },
        y: { grid: GRID, ticks: { ...TICK, callback: v => '$' + v } }
      }
    }
  });
  }
  }

  // ── Symbol Performance ────────────────────────────────────────────────────────
  const positions = sessionClosedPositions();
  const syms = ['SPY', 'QQQ', 'NVDA'];
  const pnls = syms.map(s => {
    const t = positions.filter(x => x.openLeg && x.openLeg.symbol === s);
    return t.length ? parseFloat(t.reduce((a,b)=>a+b.posPnl,0).toFixed(2)) : 0;
  });
  const colorsSym = pnls.map(v => v >= 0 ? 'rgba(0,230,118,0.75)' : 'rgba(255,82,82,0.75)');

  new Chart(document.getElementById('symbolChart'), {
    type: 'bar',
    data: { labels: syms, datasets: [{ data: pnls, backgroundColor: colorsSym, borderRadius: 4 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: ctx => ' ' + fmtPnl(ctx.raw) }
      }},
      scales: {
        x: { grid: GRID, ticks: { ...TICK, callback: v => '$' + v } },
        y: { grid: { display: false }, ticks: TICK }
      }
    }
  });

  // ── Direction Donut ───────────────────────────────────────────────────────────
  const calls = positions.filter(p => p.openLeg && p.openLeg.direction === 'CALL').length;
  const puts  = positions.filter(p => p.openLeg && p.openLeg.direction === 'PUT').length;
  if (calls || puts) {
  new Chart(document.getElementById('directionChart'), {
    type: 'doughnut',
    data: {
      labels: ['CALL', 'PUT'],
      datasets: [{ data: [calls, puts],
        backgroundColor: ['rgba(79,195,247,0.8)', 'rgba(188,140,255,0.8)'],
        borderColor: '#161616', borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } }
    }
  });
  }

  // ── Exit Reason Donut ─────────────────────────────────────────────────────────
  const reasons = {};
  sessionClosedPositions().filter(p => isStrategyExitAttribution(p.closeLeg)).forEach(p => {
    const r = (p.closeLeg && p.closeLeg.exit_reason) ? p.closeLeg.exit_reason : '—';
    reasons[r] = (reasons[r] || 0) + 1;
  });
  const exitLabels = Object.keys(reasons);
  const exitValues = exitLabels.map(k => reasons[k]);
  if (exitLabels.length) {
  const paletteEx = ['rgba(255,215,64,0.8)','rgba(0,230,118,0.8)','rgba(255,82,82,0.8)','rgba(79,195,247,0.8)'];

  new Chart(document.getElementById('exitChart'), {
    type: 'doughnut',
    data: {
      labels: exitLabels,
      datasets: [{ data: exitValues,
        backgroundColor: exitLabels.map((_,i)=>paletteEx[i%paletteEx.length]),
        borderColor: '#161616', borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } }
    }
  });
  }

  // ── VIX Regime Donut ──────────────────────────────────────────────────────────
  const regimes = {};
  sessionClosedPositions().filter(p => isStrategyExitAttribution(p.closeLeg)).forEach(p => {
    const raw = (p.closeLeg && p.closeLeg.vix_regime) ? p.closeLeg.vix_regime : 'Unknown';
    const r = String(raw).split('(')[0].trim();
    regimes[r] = (regimes[r] || 0) + 1;
  });
  const vixLabels = Object.keys(regimes);
  const vixValues = vixLabels.map(k => regimes[k]);
  if (vixLabels.length) {
  const paletteVx = ['rgba(0,230,118,0.8)','rgba(255,215,64,0.8)','rgba(255,145,0,0.8)','rgba(255,82,82,0.8)'];

  new Chart(document.getElementById('vixChart'), {
    type: 'doughnut',
    data: {
      labels: vixLabels,
      datasets: [{ data: vixValues,
        backgroundColor: vixLabels.map((_,i)=>paletteVx[i%paletteVx.length]),
        borderColor: '#161616', borderWidth: 2 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } }
    }
  });
  }

  // ── Win Rate by Symbol ────────────────────────────────────────────────────────
  const wrs = syms.map(s => {
    const t = positions.filter(x => x.openLeg && x.openLeg.symbol === s);
    if (!t.length) return 0;
    return parseFloat((t.filter(x => x.posPnl > 1e-6).length / t.length * 100).toFixed(1));
  });

  new Chart(document.getElementById('winRateSymbolChart'), {
    type: 'bar',
    data: {
      labels: syms,
      datasets: [{
        data: wrs,
        backgroundColor: wrs.map(v => v >= 50 ? 'rgba(0,230,118,0.75)' : 'rgba(255,82,82,0.75)'),
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: ctx => ' ' + ctx.raw + '%' }
      }},
      scales: {
        x: { grid: { display: false }, ticks: TICK },
        y: { grid: GRID, min: 0, max: 100,
             ticks: { ...TICK, callback: v => v + '%' } }
      }
    }
  });
}

// ── Performance Analytics collapse ─────────────────────────────────────────────
(function initPerfCollapse() {
  const panel = document.getElementById('performanceAnalyticsPanel');
  const btn = document.getElementById('perfAnalyticsToggle');
  const body = document.getElementById('perfAnalyticsBody');
  if (!panel || !btn || !body) return;

  const STORAGE_KEY = 'fabio_perf_analytics_collapsed';
  function setCollapsed(collapsed) {
    panel.classList.toggle('perf-collapsed', !!collapsed);
    btn.setAttribute('data-collapsed', collapsed ? '1' : '0');
    btn.querySelector('.label').textContent = collapsed ? 'Expand' : 'Collapse';
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    try { localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0'); } catch (e) {}
    if (!collapsed) {
      requestAnimationFrame(function () { resizePerfCharts(); });
    }
  }

  let initialCollapsed = false;
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === '1') initialCollapsed = true;
    else if (v === null && window.matchMedia && window.matchMedia('(max-width: 768px)').matches) initialCollapsed = true;
  } catch (e) {}
  setCollapsed(initialCollapsed);

  if (!initialCollapsed) {
    initAnalyticsCharts();
    requestAnimationFrame(function () { resizePerfCharts(); });
  }

  btn.addEventListener('click', function () {
    const wasCollapsed = panel.classList.contains('perf-collapsed');
    setCollapsed(!wasCollapsed);
    if (wasCollapsed) {
      initAnalyticsCharts();
      requestAnimationFrame(function () { resizePerfCharts(); });
    }
  });

  var resizeTimer;
  window.addEventListener('resize', function () {
    if (panel.classList.contains('perf-collapsed') || !window.__fabioPerfChartsDone) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { resizePerfCharts(); }, 120);
  });
})();

// ── Trades Table ──────────────────────────────────────────────────────────────
let _sortCol = 'date', _sortAsc = false;

function renderOpenPositions() {
  const filtered = (openPositions || []).filter(function (p) {
    const qty = Number(p && p.contracts != null ? p.contracts : 0);
    return qty > 0;
  });
  const rows = filtered.slice().sort((a, b) => {
    const da = String(a.date || '');
    const db = String(b.date || '');
    if (da !== db) return db.localeCompare(da);
    return String(b.entry_time || '').localeCompare(String(a.entry_time || ''));
  });
  document.getElementById('openRowCount').textContent = '(' + rows.length + ')';
  const tbody = document.getElementById('openTableBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">No open positions.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(t => {
    const dirCls = t.direction === 'CALL' ? 'dir-call' : 'dir-put';
    return `<tr>
      <td>${t.date || '—'}</td>
      <td><strong>${t.symbol || '—'}</strong></td>
      <td><span class="${dirCls}">${t.direction || '—'}</span></td>
      <td style="color:var(--muted)">${t.entry_time || '—'}</td>
      <td>$${(t.entry_price||0).toFixed(2)}</td>
      <td>${t.contracts || 0}</td>
      <td>${(t.vix||0).toFixed(1)}</td>
      <td>${(t.or_atr_pct||0).toFixed(0)}%</td>
      <td><span class="open-tag">OPEN</span></td>
    </tr>`;
  }).join('');
}

function sortTable(col) {
  const modeEl = document.getElementById('tradeViewMode');
  if (modeEl && modeEl.value !== 'flat') return;
  if (_sortCol === col) { _sortAsc = !_sortAsc; }
  else { _sortCol = col; _sortAsc = false; }
  document.querySelectorAll('#tradesTable thead th').forEach(th => th.classList.remove('sorted'));
  const idx = ['date','symbol','direction','ledger_leg','qty_leg','qty_after','pnl_leg','','entry_price','','','pnl','return_pct','exit_reason','vix','or_atr_pct','trend','day_color'];
  const thArr = Array.from(document.querySelectorAll('#tradesTable thead th'));
  const matchIdx = idx.indexOf(col);
  if (matchIdx >= 0 && thArr[matchIdx]) {
    thArr[matchIdx].classList.add('sorted');
    thArr[matchIdx].querySelector('.sort-arrow').textContent = _sortAsc ? '↑' : '↓';
  }
  renderTable();
}

const TRADE_GROUP_LEGS_THEAD = '<thead><tr>'
  + '<th>Date</th><th>Symbol</th><th>Dir</th><th>Leg</th><th>Qty</th><th>Rem</th><th>Leg P&amp;L</th>'
  + '<th title="Bar vs largest |leg P&amp;L| in this position">∑ leg</th>'
  + '<th>Entry</th><th>Entry $</th><th>Exit $</th><th>Exit</th><th>Pos P&amp;L</th><th>Return %</th><th>Exit Reason</th><th>VIX</th><th>OR/ATR</th><th>Trend</th><th>Day</th>'
  + '</tr></thead>';

function rowHtmlForTrade(t, opts) {
  opts = opts || {};
  const leg = t.ledger_leg || 'CLOSE';
  const side = t.ledger_side || (leg === 'OPEN' ? 'BUY' : 'SELL');
  const rowSideClass = side === 'BUY' ? 'ledger-row-buy' : 'ledger-row-sell';
  let legBadgeHtml = '<span class="leg-badge leg-badge-close">Close</span>';
  if (leg === 'OPEN') legBadgeHtml = '<span class="leg-badge leg-badge-open">Open</span>';
  else if (leg === 'TRIM') legBadgeHtml = '<span class="leg-badge leg-badge-trim">Trim</span>';
  const qLeg = (t.qty_leg != null && t.qty_leg !== '') ? t.qty_leg : (t.contracts != null ? t.contracts : '—');
  const qAfter = (t.qty_after != null && t.qty_after !== '') ? t.qty_after : '—';
  let legPnlHtml = '—';
  let relCell = '';
  if (leg !== 'OPEN') {
    const pl = Number(t.pnl_leg || 0);
    const lpCls = pl >= 0 ? 'pnl-pos' : 'pnl-neg';
    legPnlHtml = '<span class="' + lpCls + '">' + fmtPnl(pl) + '</span>';
    if (opts.grouped && opts.legScale) {
      const pct = Math.min(100, (Math.abs(pl) / opts.legScale) * 100);
      const fCls = pl >= 0 ? 'leg-rel-pos' : 'leg-rel-neg';
      relCell = '<td class="leg-rel"><div class="leg-rel-track"><div class="leg-rel-fill ' + fCls + '" style="width:' + pct + '%"></div></div></td>';
    }
  }
  if (opts.grouped && !relCell) {
    relCell = '<td class="leg-rel"><span style="color:var(--muted)">—</span></td>';
  }
  const pnlCls  = t.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
  const dirCls  = t.direction === 'CALL' ? 'dir-call' : 'dir-put';
  const trendVal = trendBucket(t);
  const trendHtml = trendVal === 'BULL'
    ? '<span class="trend-bull">BULL</span>'
    : trendVal === 'BEAR'
      ? '<span class="trend-bear">BEAR</span>'
      : t.trend === 'WITH'
    ? '<span class="trend-with">WITH</span>'
    : t.trend === 'COUNTER'
      ? '<span class="trend-counter">COUNTER</span>'
      : t.trend || '—';
  const dayHtml = t.day_color === 'GREEN'
    ? '<span class="day-green">● GREEN</span>'
    : t.day_color === 'YELLOW'
      ? '<span class="day-yellow">● YELLOW</span>'
      : t.day_color === 'RED'
        ? '<span class="day-red">● RED</span>'
        : '—';
  const retPct = (isSessionTrade(t) && t.return_pct !== undefined && t.return_pct !== null)
    ? '<span class="' + pnlCls + '">' + fmtPct(t.return_pct) + '</span>'
    : '—';

  const posTotalVal = (t.pnl_position_total != null && t.pnl_position_total !== '')
    ? Number(t.pnl_position_total)
    : Number(t.pnl || 0);
  const posPnlHint = (leg === 'CLOSE' && isSessionTrade(t))
    ? '<span class="pos-pnl-total-hint">Position total (all legs)</span>'
    : '';
  const posPnlInner = isSessionTrade(t)
    ? '<div>' + fmtPnl(posTotalVal) + '</div>' + posPnlHint
    : '—';
  const posPnlCell = isSessionTrade(t)
    ? '<td class="' + pnlCls + '">' + posPnlInner + '</td>'
    : '<td style="color:var(--muted)">—</td>';

  return `<tr class="${rowSideClass}">
    <td>${t.date || '—'}</td>
    <td><strong>${ t.symbol || '—'}</strong></td>
    <td><span class="${dirCls}">${t.direction || '—'}</span></td>
    <td>${legBadgeHtml}</td>
    <td>${qLeg}</td>
    <td style="color:var(--muted)">${qAfter}</td>
    <td>${legPnlHtml}</td>
    ${relCell}
    <td style="color:var(--muted)">${t.entry_time || '—'}</td>
    <td>$${(t.entry_price||0).toFixed(2)}</td>
    <td>$${(t.exit_price||0).toFixed(2)}</td>
    <td style="color:var(--muted)">${t.exit_time || '—'}</td>
    ${posPnlCell}
    <td>${retPct}</td>
    <td style="color:var(--muted)">${t.exit_reason || '—'}</td>
    <td>${(t.vix||0).toFixed(1)}</td>
    <td>${(t.or_atr_pct||0).toFixed(0)}%</td>
    <td>${trendHtml}</td>
    <td>${dayHtml}</td>
  </tr>`;
}

function renderTable() {
  const sym    = document.getElementById('filterSymbol').value;
  const dir    = document.getElementById('filterDir').value;
  const trend  = document.getElementById('filterTrend').value;
  const exitR  = document.getElementById('filterExit').value;
  const result = document.getElementById('filterResult').value;
  const viewMode = (document.getElementById('tradeViewMode') || {}).value || 'grouped';
  const groupedMount = document.getElementById('tradesGroupedMount');
  const flatMount = document.getElementById('tradesFlatMount');

  const rows = filterTradeRows(sym, dir, trend, exitR, result);
  const tbody = document.getElementById('tableBody');

  function renderOneGroupCard({ legs }) {
    const legScale = legPnlScaleForGroup(legs);
    const openLeg = legs.find(l => (l.ledger_leg || 'CLOSE') === 'OPEN') || legs[0];
    const closeLeg = [...legs].reverse().find(l => (l.ledger_leg || 'CLOSE') === 'CLOSE') || legs[legs.length - 1];
    const sym0 = openLeg.symbol || '—';
    const ddir = openLeg.direction || '—';
    const dirCls = ddir === 'CALL' ? 'dir-call' : 'dir-put';
    const sess = closeLeg && isSessionTrade(closeLeg);
    const sortedForPnl = sortLegsWithinGroup(legs);
    const posTotalVal = sess ? resolvePositionPnl(sortedForPnl, closeLeg) : 0;
    const pnlCls = posTotalVal >= 0 ? 'pnl-pos' : 'pnl-neg';
    const pnlBlock = closeLeg && sess
      ? '<span class="pos-pnl-total ' + pnlCls + '">' + fmtPnl(posTotalVal) + '</span>'
      : '<span class="pos-pnl-total" style="color:var(--muted);font-weight:500">—</span>';
    const exitMeta = closeLeg && closeLeg.exit_reason
      ? '<span>' + closeLeg.exit_reason + '</span>'
      : '';
    const dateMeta = (openLeg.date || '—') + (openLeg.entry_time ? ' · ' + openLeg.entry_time : '');
    return '<div class="trade-group-card">'
      + '<div class="trade-group-head">'
      + '<h4><strong>' + sym0 + '</strong> <span class="' + dirCls + '">' + ddir + '</span></h4>'
      + '<div class="meta"><span>' + dateMeta + '</span>' + (exitMeta ? ' · ' + exitMeta : '') + '</div>'
      + pnlBlock
      + '</div>'
      + '<div class="table-scroll"><table class="trade-group-legs">' + TRADE_GROUP_LEGS_THEAD
      + '<tbody>' + legs.map(t => rowHtmlForTrade(t, { grouped: true, legScale })).join('') + '</tbody></table></div>'
      + '</div>';
  }

  function positionNetForGroup(g) {
    const legs = g.legs;
    const closeLeg = [...legs].reverse().find(l => (l.ledger_leg || 'CLOSE') === 'CLOSE') || legs[legs.length - 1];
    const sess = closeLeg && isSessionTrade(closeLeg);
    const sortedForPnl = sortLegsWithinGroup(legs);
    return sess ? resolvePositionPnl(sortedForPnl, closeLeg) : 0;
  }

  if (viewMode === 'flat') {
    if (groupedMount) { groupedMount.innerHTML = ''; groupedMount.style.display = 'none'; }
    if (flatMount) flatMount.style.display = 'block';

    const sorted = rows.slice().sort((a, b) => {
      let va = a[_sortCol] ?? '', vb = b[_sortCol] ?? '';
      if (typeof va === 'number') return _sortAsc ? va - vb : vb - va;
      return _sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });

    document.getElementById('rowCount').textContent = '(' + sorted.length + ' leg rows · ' + trades.length + ' stored)';

    if (!sorted.length) {
      tbody.innerHTML = '<tr><td colspan="18" class="empty-state">No trades match the selected filters.</td></tr>';
      return;
    }
    tbody.innerHTML = sorted.map(t => rowHtmlForTrade(t)).join('');
    return;
  }

  if (flatMount) flatMount.style.display = 'none';
  if (groupedMount) groupedMount.style.display = '';

  tbody.innerHTML = '';

  if (!rows.length) {
    document.getElementById('rowCount').textContent = '(0 positions · ' + trades.length + ' total rows)';
    groupedMount.innerHTML = '<div class="empty-state" style="padding:24px">No trades match the selected filters.</div>';
    return;
  }

  const byKey = new Map();
  for (const t of rows) {
    const k = tradeGroupKey(t);
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k).push(t);
  }
  const groups = Array.from(byKey.entries()).map(([key, legs]) => ({ key, legs: sortLegsWithinGroup(legs) }));

  const byDay = new Map();
  for (const g of groups) {
    const openLeg = g.legs.find(l => (l.ledger_leg || 'CLOSE') === 'OPEN') || g.legs[0];
    const closeLeg = [...g.legs].reverse().find(l => (l.ledger_leg || 'CLOSE') === 'CLOSE') || g.legs[g.legs.length - 1];
    const day = String((closeLeg && closeLeg.date) || (openLeg && openLeg.date) || '');
    const dk = day || '—';
    if (!byDay.has(dk)) byDay.set(dk, []);
    byDay.get(dk).push(g);
  }
  const daysSorted = Array.from(byDay.keys()).sort((a, b) => {
    if (a === '—') return 1;
    if (b === '—') return -1;
    return b.localeCompare(a);
  });

  let legTotal = 0;
  for (const g of groups) legTotal += g.legs.length;

  document.getElementById('rowCount').textContent =
    '(' + groups.length + ' positions · ' + legTotal + ' legs · ' + trades.length + ' rows)';

  groupedMount.innerHTML = daysSorted.map(function (day) {
    const dayGroups = byDay.get(day).slice().sort((a, b) =>
      groupLatestSortKey(b.legs).localeCompare(groupLatestSortKey(a.legs)));
    let dayNet = 0;
    for (let gi = 0; gi < dayGroups.length; gi++) dayNet += positionNetForGroup(dayGroups[gi]);
    const dayNetCls = dayNet >= 0 ? 'pnl-pos' : 'pnl-neg';
    let html = '<div class="trade-day-section">'
      + '<div class="trade-day-head-bar" role="group" aria-label="' + day + ', ' + dayGroups.length + ' positions, day net ' + fmtPnl(dayNet) + '">'
      + '<span class="trade-day-title"><strong>' + day + '</strong>'
      + '<span class="muted">' + dayGroups.length + ' positions</span></span>'
      + '<span class="trade-day-net ' + dayNetCls + '">' + fmtPnl(dayNet) + '</span>'
      + '</div>'
      + '<div class="trade-day-body">';
    html += dayGroups.map(renderOneGroupCard).join('');
    html += '</div></div>';
    return html;
  }).join('');
}

// Initial render — newest first by default
renderOpenPositions();
renderTable();
</script>
</body>
</html>
"""


class DashboardWriter:
    """
    Manages the persistent trade_data.json store and regenerates
    orb_live_dashboard.html after each trading session.
    """

    def __init__(self):
        self._data = self._load()
        n_strip = self._sanitize_open_positions_inplace()
        n_omit = self._sanitize_manually_omitted_trades_inplace()
        n_daily = self._rebuild_daily_from_trades_inplace()
        if n_strip:
            print(
                f"[Dashboard] Removed {n_strip} invalid open_positions row(s) "
                "(fails validation; not broker/FIFO snapshots)."
            )
        if n_omit:
            print(f"[Dashboard] Omitted {n_omit} manually-suppressed trade row(s).")
        if n_daily:
            print(f"[Dashboard] Rebuilt {n_daily} daily row(s) from current trade store.")
        if n_strip or n_omit or n_daily:
            self._save()
            self._write_html()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _sanitize_open_positions_inplace(self) -> int:
        """Return count of removed rows. Mutates `self._data['open_positions']`."""
        opens = self._data.get("open_positions", [])
        cleaned, dropped = normalize_and_validate_open_positions(opens)
        self._data["open_positions"] = cleaned
        return dropped

    def _sanitize_manually_omitted_trades_inplace(self) -> int:
        """Drop hardcoded one-off omitted positions from persisted dashboard trades."""
        trades = list(self._data.get("trades") or [])
        kept = [t for t in trades if not _is_manually_omitted_dashboard_trade(t)]
        self._data["trades"] = kept
        return len(trades) - len(kept)

    def _rebuild_daily_from_trades_inplace(self) -> int:
        """
        Recompute daily rows from current stored trades.
        Prevents stale daily aggregates from surviving after manual trade omissions.
        Returns number of daily rows after rebuild.
        """
        self._data["daily"] = _build_daily_from_dashboard_trades(
            list(self._data.get("trades") or [])
        )
        return len(self._data["daily"])

    def _load(self) -> dict:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("trades", [])
                    data.setdefault("daily", [])
                    data.setdefault("open_positions", [])
                    return data
            except Exception as e:
                print(f"[Dashboard] Could not read data file: {e}")
        return {"trades": [], "daily": [], "open_positions": []}

    def _save(self):
        with open(DATA_FILE, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    @staticmethod
    def _merge_daily_with_aggregate(day_trades: list, baseline: dict) -> dict:
        """Recompute closed-position KPIs from merged leg rows; keep capital/return from baseline."""
        out = dict(baseline)
        positions = aggregate_closed_positions(day_trades)
        if not positions:
            return out
        winners = [p for p in positions if p["pnl"] > 0]
        losers = [p for p in positions if p["pnl"] < 0]
        net_pnl = round(sum(p["pnl"] for p in positions), 2)
        gross_win = round(sum(p["pnl"] for p in winners), 2)
        gross_loss = round(sum(p["pnl"] for p in losers), 2)
        n = len(positions)
        win_rate = round(len(winners) / n * 100, 1) if n else 0.0
        avg_win = (gross_win / len(winners)) if winners else 0.0
        avg_loss = (gross_loss / len(losers)) if losers else 0.0
        wr = len(winners) / n if n else 0.0
        proven_edge = round((avg_win * wr) + (avg_loss * (1 - wr)), 2)
        out.update(
            {
                "total_trades": n,
                "winners": len(winners),
                "losers": len(losers),
                "win_rate": win_rate,
                "net_pnl": net_pnl,
                "gross_win": gross_win,
                "gross_loss": gross_loss,
                "proven_edge": proven_edge,
            }
        )
        return out

    # ── Public API ────────────────────────────────────────────────────────────

    def append_session(self, trades: list, daily: dict, open_positions: list | None = None):
        """
        Call at EOD each session.

        trades: list of trade dicts, each with keys:
          date, symbol, direction, entry_time, entry_price, exit_time,
          pnl, return_pct, exit_reason, contracts, vix, or_atr_pct,
          trend, vix_regime, day_color

        daily: dict with keys:
          date, total_trades, winners, losers, win_rate,
          net_pnl, gross_win, gross_loss, capital, daily_return, proven_edge

        open_positions: broker snapshot for the HTML "Open positions" table.
          Pass [] when flat. If omitted, defaults to [] (avoids stale rows from
          prior reconcile runs lingering in trade_data.json).
        """
        if open_positions is None:
            print(
                "[Dashboard] append_session: open_positions not passed; "
                "defaulting to [] (stale reconciled opens would otherwise persist)."
            )
            open_positions = []

        today = daily.get("date", datetime.date.today().isoformat())

        existing_trades = list(self._data.get("trades") or [])
        today_rows = [
            t
            for t in existing_trades
            if isinstance(t, dict) and str(t.get("date") or "") == str(today)
        ]
        # Preserve FIFO groups whose closes are broker-tagged OPEN legs often are not tagged.
        moomoo_group_ids = {
            str(t.get("ledger_group_id") or "").strip()
            for t in today_rows
            if dashboard_row_derived_from_moomoo_sync(t)
        }
        moomoo_group_ids.discard("")
        preserved_today_moomoo = [
            t
            for t in today_rows
            if dashboard_row_derived_from_moomoo_sync(t)
            or (
                str(t.get("ledger_group_id") or "").strip() in moomoo_group_ids
            )
        ]

        # Remove prior data for today (safe re-run intraday); keep broker/canonical fills.
        self._data["trades"] = [
            t for t in existing_trades if str(t.get("date") or "") != str(today)
        ]
        self._data["trades"].extend(preserved_today_moomoo)
        self._data["trades"].extend(trades)
        _ = self._sanitize_manually_omitted_trades_inplace()

        self._data["daily"] = [
            d for d in self._data.get("daily") or []
            if str(d.get("date") or "") != str(today)
        ]
        if daily:
            day_trades_all = [
                t
                for t in self._data["trades"]
                if str(t.get("date") or "") == str(today)
            ]
            self._data["daily"].append(
                self._merge_daily_with_aggregate(day_trades_all, dict(daily))
            )
        cleaned_opens, _ = normalize_and_validate_open_positions(open_positions)
        self._data["open_positions"] = cleaned_opens

        self._save()
        self._write_html()
        print(f"[Dashboard] Session saved: {len(trades)} trades | "
              f"Net P&L ${daily.get('net_pnl', 0):+.2f}")

    def refresh_intraday(self, trades: list, open_positions: list | None = None):
        """
        Lightweight intraday refresh for dashboard responsiveness.
        - Replaces today's bot-authored rows while preserving broker/reconcile rows.
        - Does not mutate historical days.
        - Leaves daily summary rows untouched (EOD append_session remains canonical).
        """
        today = datetime.date.today().isoformat()
        existing_trades = list(self._data.get("trades") or [])
        today_rows = [
            t
            for t in existing_trades
            if isinstance(t, dict) and str(t.get("date") or "") == str(today)
        ]
        moomoo_group_ids = {
            str(t.get("ledger_group_id") or "").strip()
            for t in today_rows
            if dashboard_row_derived_from_moomoo_sync(t)
        }
        moomoo_group_ids.discard("")
        preserved_today_moomoo = [
            t
            for t in today_rows
            if dashboard_row_derived_from_moomoo_sync(t)
            or (
                str(t.get("ledger_group_id") or "").strip() in moomoo_group_ids
            )
        ]
        self._data["trades"] = [
            t for t in existing_trades if str(t.get("date") or "") != str(today)
        ]
        self._data["trades"].extend(preserved_today_moomoo)
        self._data["trades"].extend(list(trades or []))
        _ = self._sanitize_manually_omitted_trades_inplace()
        if open_positions is not None:
            cleaned_opens, _ = normalize_and_validate_open_positions(open_positions)
            self._data["open_positions"] = cleaned_opens
        self._save()
        self._write_html()

    def refresh_intraday_open_positions(self, open_positions: list | None):
        """
        Display-only open positions refresh (throttled by caller).
        """
        cleaned_opens, _ = normalize_and_validate_open_positions(open_positions or [])
        self._data["open_positions"] = cleaned_opens
        self._save()
        self._write_html()

    def _write_html(self):
        """Inject data into the HTML template and write both output files."""
        try:
            from config import modeled_equity_dashboard_subtitle

            _eq_sub = modeled_equity_dashboard_subtitle()
        except ImportError:
            _eq_sub = None
        try:
            from fabio_beta_identity import beta_identity_payload

            _beta = beta_identity_payload()
        except ImportError:
            _beta = None
        buy_hold_overlay = _build_buy_hold_overlay_from_daily(
            list(self._data.get("daily") or [])
        )
        html_payload = {
            **self._data,
            "equity_modeled_subtitle": _eq_sub,
            "buy_hold_overlay": buy_hold_overlay,
            "beta_identity": _beta,
            "dashboard_generated_at_utc": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"),
        }
        data_json = json.dumps(html_payload, default=str)
        html      = _TEMPLATE.replace("__DATA_JSON__", data_json)

        for path in (DASH_LOCAL, DASH_MAIN):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(html)
            except Exception as e:
                print(f"[Dashboard] Write failed ({path}): {e}")

        print(f"[Dashboard] ✅ Dashboard written → {DASH_LOCAL} | {DASH_MAIN}")
