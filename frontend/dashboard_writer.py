"""
dashboard_writer.py — Generates local dashboard HTML from accumulated trade data.

The bot calls DashboardWriter.append_session() at EOD each day with a broker
snapshot for open_positions so the HTML table does not retain stale rows from
prior reconcile runs.
Data is persisted in ``backend/trade_data.json`` so history builds up across sessions.
Aggregated KPIs (win rate, net P&L, charts) use closed positions — one outcome
per CLOSE leg / round-trip — not raw ledger rows. Sheets Daily Summary uses the
same aggregation (aggregate_closed_positions).

HTML shell is ``frontend/templates/live_dashboard_template.html`` (Ops tab +
status pill). Regenerated ``frontend/live_dashboard.html`` is a **local** write
for operators — do not commit it until four-book UI is ready. Pages /
orbit.clayj.app stay unwired. ``file://`` will not poll live JSON; serve with
``python3 -m http.server`` from ``frontend/``.

Output files:
  backend/trade_data.json            ← persistent data store (gitignored)
  frontend/live_dashboard.html       ← local HTML (tracked snapshot frozen; do not commit regen)
  frontend/fabio_live_dashboard.html ← same build, gitignored
  frontend/bot_live_status.json      ← gitignored live pill/ops snapshot
  frontend/bot_ops_feed.json         ← gitignored ops event feed
"""

import json
import os
import re
import datetime
from typing import Any

from pathlib import Path

from manual_position_omissions import is_omitted_dashboard_close_trade

from paper_book_dashboard import (
    PAPER_DESK_HEADER,
    annotate_open_positions_with_book,
    dashboard_books_payload,
    is_allowed_open_position_notes,
    live_status_books_payload,
    read_latest_health_snapshot,
)

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


# Single HTML source: frontend/templates/live_dashboard_template.html
# Regenerated live_dashboard.html is local-only until four-book UI is ready.
# Do not commit it; portal/push_dashboard.sh no longer auto-commits tracked HTML.
TEMPLATE_PATH = str(_FABIO_ROOT / "frontend" / "templates" / "live_dashboard_template.html")
LIVE_STATUS_FILE = str(_FABIO_ROOT / "frontend" / "bot_live_status.json")
OPS_FEED_FILE = str(_FABIO_ROOT / "frontend" / "bot_ops_feed.json")
try:
    from fabio_live.constants import HEALTH_SNAPSHOT_PATH as _HEALTH_JSONL
except ImportError:  # pragma: no cover - dashboard-only hosts
    _HEALTH_JSONL = str(_FABIO_ROOT / "bot_health_snapshots.jsonl")
HEALTH_JSONL_FILE = _HEALTH_JSONL
STORY_LINK_HTML = (
    '<a class="header-btn" href="fabio_scrollytelling.html" '
    'title="Scroll-driven operating picture">Story</a>'
)
OPS_FEED_MAX_EVENTS = 300


def load_live_dashboard_template(path: str | None = None) -> str:
    """Load the on-disk dashboard shell (Ops tab + status pill)."""
    pth = Path(path or TEMPLATE_PATH)
    text = pth.read_text(encoding="utf-8")
    if "__DATA_JSON__" not in text:
        raise RuntimeError(f"Dashboard template missing __DATA_JSON__ placeholder: {pth}")
    return text


def render_live_dashboard_html(template: str, data_json: str) -> str:
    """Fill story-link and DATA placeholders. Calendar is JSON in DATA, not a HTML token."""
    html = template.replace("__STORY_LINK_HTML__", STORY_LINK_HTML)
    return html.replace("__DATA_JSON__", data_json)


def _write_json_atomic(path: str, payload: dict) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    tmp.replace(dest)


def live_status_from_health(
    snapshot: dict,
    *,
    pid: int,
    running: bool,
) -> dict:
    """Map a health-snapshot dict onto the live-status JSON the Ops pill polls."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    bot_state = snap.get("bot_state") if isinstance(snap.get("bot_state"), dict) else {}
    ops = snap.get("ops") if isinstance(snap.get("ops"), dict) else {}
    circuit = snap.get("circuit") if isinstance(snap.get("circuit"), dict) else {}
    checked = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    book_payload = live_status_books_payload(snap)
    return {
        "running": bool(running),
        "paused": bool(bot_state.get("paused")),
        "pid": int(pid),
        "source": "health_snapshot",
        "checked_at_utc": checked,
        "ops_snapshot": {
            "paused": bool(bot_state.get("paused")),
            "signals_open": list(bot_state.get("signals_open") or []),
            "circuit": circuit,
            "loop_cadence_mode": ops.get("loop_cadence_mode"),
            "loop_sleep_sec": ops.get("loop_sleep_sec"),
            "ops_queue_depth": ops.get("queue_depth"),
            "ops_queue_max": ops.get("queue_max"),
        },
        "health": snap,
        "books": book_payload["books"],
        "health_ts": book_payload["health_ts"],
    }


def write_bot_live_status(payload: dict, path: str | None = None) -> None:
    _write_json_atomic(path or LIVE_STATUS_FILE, payload)


def health_ops_feed_event(snapshot: dict) -> dict:
    snap = snapshot if isinstance(snapshot, dict) else {}
    ops = snap.get("ops") if isinstance(snap.get("ops"), dict) else {}
    pp = snap.get("position_parity") if isinstance(snap.get("position_parity"), dict) else {}
    detail = (
        f"queue={ops.get('queue_depth', 0)}/{ops.get('queue_max', 0)} "
        f"errors={ops.get('errors', 0)} "
        f"drops={ops.get('dropped_noncritical', 0)}/{ops.get('dropped_critical', 0)} "
        f"dash={ops.get('dashboard_intraday_refresh_enqueued', 0)}/"
        f"{ops.get('dashboard_intraday_refresh_requests', 0)} "
        f"open={ops.get('dashboard_open_refresh_enqueued', 0)}/"
        f"{ops.get('dashboard_open_refresh_requests', 0)} "
        f"cadence={ops.get('loop_cadence_mode', 'idle')}@"
        f"{float(ops.get('loop_sleep_sec') or 0):.0f}s"
        f" parity_ok={pp.get('parity_ok')} query_ok={pp.get('query_ok')}"
        f" drift_count={pp.get('drift_count')}"
    )
    ts = snap.get("ts") or datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )
    return {
        "ts": ts,
        "level": "info",
        "category": "SYSTEM",
        "symbol": "",
        "title": "Health Snapshot",
        "detail": detail,
        "meta": {"alert_type": "HEALTH_SNAPSHOT"},
    }


def append_health_to_ops_feed(
    snapshot: dict,
    path: str | None = None,
    max_events: int = OPS_FEED_MAX_EVENTS,
) -> None:
    dest = Path(path or OPS_FEED_FILE)
    events: list[dict] = []
    if dest.is_file():
        try:
            raw = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("events"), list):
                events = [e for e in raw["events"] if isinstance(e, dict)]
        except (OSError, json.JSONDecodeError, TypeError):
            events = []
    events.append(health_ops_feed_event(snapshot))
    cap = max(1, int(max_events))
    events = events[-cap:]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    _write_json_atomic(
        str(dest),
        {"version": 1, "updated_at_utc": now, "events": events},
    )


class DashboardWriter:
    """
    Manages the persistent trade_data.json store and regenerates
    local live_dashboard.html after each trading session.

    Writes are local-only. Do not commit regenerated HTML (SHIP-009).
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
        """Inject DATA into the on-disk template and write both local HTML files."""
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
        health_snap = read_latest_health_snapshot(HEALTH_JSONL_FILE)
        books = dashboard_books_payload(health_snap)
        health_ts = None
        if isinstance(health_snap, dict):
            health_ts = health_snap.get("ts")
        html_payload = {
            **self._data,
            "open_positions": annotate_open_positions_with_book(
                self._data.get("open_positions")
            ),
            "books": books,
            "health_ts": health_ts,
            "desk_header": PAPER_DESK_HEADER,
            "equity_modeled_subtitle": _eq_sub,
            "buy_hold_overlay": buy_hold_overlay,
            "beta_identity": _beta,
            "dashboard_generated_at_utc": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"),
        }
        data_json = json.dumps(html_payload, default=str)
        try:
            template = load_live_dashboard_template()
            html = render_live_dashboard_html(template, data_json)
        except Exception as e:
            print(f"[Dashboard] Template render failed: {e}")
            return

        for path in (DASH_LOCAL, DASH_MAIN):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(html)
            except Exception as e:
                print(f"[Dashboard] Write failed ({path}): {e}")

        print(f"[Dashboard] ✅ Dashboard written → {DASH_LOCAL} | {DASH_MAIN}")
