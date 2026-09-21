"""Regression tests for dashboard JSON + open positions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

from dashboard_writer import (
    DashboardWriter,
    aggregate_closed_positions,
    dashboard_row_derived_from_moomoo_sync,
    moomoo_position_records_to_dashboard_opens,
    normalize_and_validate_open_positions,
    tradier_position_records_to_dashboard_opens,
)


def test_aggregate_closed_positions_one_row_per_close():
    trades = [
        {"ledger_group_id": "g1", "ledger_leg": "OPEN", "pnl": 0, "include_in_session_pnl": False},
        {"ledger_group_id": "g1", "ledger_leg": "TRIM", "pnl_leg": 10, "pnl": 0, "include_in_session_pnl": False},
        {
            "ledger_group_id": "g1",
            "ledger_leg": "CLOSE",
            "pnl_position_total": 40.0,
            "pnl": 40.0,
            "include_in_session_pnl": True,
        },
        {"ledger_group_id": "g2", "ledger_leg": "OPEN", "pnl": 0, "include_in_session_pnl": False},
        {"ledger_group_id": "g2", "ledger_leg": "CLOSE", "pnl_position_total": -5, "pnl": -5, "include_in_session_pnl": True},
    ]
    pos = aggregate_closed_positions(trades)
    assert len(pos) == 2
    assert pos[0]["pnl"] + pos[1]["pnl"] == 35.0  # 40 + (-5), order may vary
    pnls = sorted(p["pnl"] for p in pos)
    assert pnls == [-5.0, 40.0]


def test_aggregate_closed_positions_sums_legs_when_close_totals_missing():
    trades = [
        {"ledger_group_id": "x", "ledger_leg": "TRIM", "pnl_leg": 15, "include_in_session_pnl": False},
        {
            "ledger_group_id": "x",
            "ledger_leg": "CLOSE",
            "pnl_leg": 25,
            "include_in_session_pnl": True,
        },
    ]
    pos = aggregate_closed_positions(trades)
    assert len(pos) == 1
    assert pos[0]["pnl"] == 40.0


def test_aggregate_closed_positions_includes_legacy_single_row():
    trades = [{"date": "2026-01-02", "symbol": "SPY", "pnl": 12.5, "include_in_session_pnl": True}]
    pos = aggregate_closed_positions(trades)
    assert len(pos) == 1
    assert pos[0]["pnl"] == 12.5


def test_aggregate_closed_positions_sums_multiple_fifo_slice_closes_same_entry_key():
    """Reconcile rows for partial exits share trade_group_key; P&L must sum all slices."""
    same_open = {
        "date": "2026-05-08",
        "symbol": "QQQ",
        "direction": "CALL",
        "entry_time": "09:48:30",
        "entry_price": 1.96,
    }
    trades = [
        {
            **same_open,
            "exit_time": "11:35:39",
            "ledger_leg": "CLOSE",
            "contracts": 10,
            "pnl": 2050.0,
            "pnl_position_total": 2050.0,
            "pnl_leg": 2050.0,
            "include_in_session_pnl": True,
            "notes": "moomoo_paper_fifo",
        },
        {
            **same_open,
            "exit_time": "11:36:03",
            "ledger_leg": "CLOSE",
            "contracts": 5,
            "pnl": 1030.0,
            "pnl_position_total": 1030.0,
            "pnl_leg": 1030.0,
            "include_in_session_pnl": True,
            "notes": "moomoo_paper_fifo",
        },
        {
            **same_open,
            "exit_time": "13:51:45",
            "ledger_leg": "CLOSE",
            "contracts": 5,
            "pnl": 1705.0,
            "pnl_position_total": 1705.0,
            "pnl_leg": 1705.0,
            "include_in_session_pnl": True,
            "notes": "moomoo_paper_fifo",
        },
    ]
    pos = aggregate_closed_positions(trades)
    assert len(pos) == 1
    assert pos[0]["pnl"] == 4785.0


def _minimal_daily(date: str) -> dict:
    return {
        "date": date,
        "total_trades": 0,
        "winners": 0,
        "losers": 0,
        "win_rate": 0.0,
        "net_pnl": 0.0,
        "gross_win": 0.0,
        "gross_loss": 0.0,
        "capital": 0.0,
        "daily_return": 0.0,
        "proven_edge": 0.0,
    }


def test_append_session_replaces_stale_open_positions(tmp_path):
    data_file = tmp_path / "trade_data.json"
    live_html = tmp_path / "live_dashboard.html"
    main_html = tmp_path / "fabio_live_dashboard.html"
    data_file.write_text(
        json.dumps(
            {
                "trades": [],
                "daily": [],
                "open_positions": [
                    {"symbol": "SPY", "contracts": 99, "date": "2099-01-01"}
                ],
            }
        ),
        encoding="utf-8",
    )

    with (
        patch("dashboard_writer.DATA_FILE", str(data_file)),
        patch("dashboard_writer.DASH_LOCAL", str(live_html)),
        patch("dashboard_writer.DASH_MAIN", str(main_html)),
    ):
        w = DashboardWriter()
        w.append_session([], _minimal_daily("2026-05-07"), [])

    saved = json.loads(data_file.read_text(encoding="utf-8"))
    assert saved["open_positions"] == []


def test_append_session_preserves_moomoo_fifo_group_and_merges_daily(tmp_path):
    """Broker OPEN legs may omit Moomoo tags; CLOSE tags carry backfill markers."""
    data_file = tmp_path / "trade_data.json"
    live_html = tmp_path / "live_dashboard.html"
    main_html = tmp_path / "fabio_live_dashboard.html"
    today = "2026-05-07"
    fifo_open = {
        "date": today,
        "symbol": "SPY",
        "ledger_group_id": "mg1",
        "ledger_leg": "OPEN",
        "pnl": 0,
        "include_in_session_pnl": False,
    }
    fifo_close = {
        "date": today,
        "symbol": "SPY",
        "ledger_group_id": "mg1",
        "ledger_leg": "CLOSE",
        "pnl_position_total": 42.0,
        "pnl": 42.0,
        "include_in_session_pnl": True,
        "exit_reason": "moomoo fill backfill",
    }
    ephemeral_same_day = {
        "date": today,
        "symbol": "QQQ",
        "pnl": 99.0,
        "include_in_session_pnl": True,
    }
    legacy_other_day = {"date": "2026-01-02", "symbol": "X", "pnl": 5.0, "include_in_session_pnl": True}
    data_file.write_text(
        json.dumps(
            {
                "trades": [legacy_other_day, fifo_open, fifo_close, ephemeral_same_day],
                "daily": [],
                "open_positions": [],
            }
        ),
        encoding="utf-8",
    )
    newer_bot_flat = [{"date": today, "symbol": "DIA", "pnl": 3.0, "include_in_session_pnl": True}]
    daily_in = dict(_minimal_daily(today))
    daily_in.update(
        {
            "total_trades": 1,
            "winners": 1,
            "losers": 0,
            "win_rate": 100.0,
            "net_pnl": 3.0,
            "gross_win": 3.0,
            "gross_loss": 0.0,
            "proven_edge": 3.0,
        }
    )
    with (
        patch("dashboard_writer.DATA_FILE", str(data_file)),
        patch("dashboard_writer.DASH_LOCAL", str(live_html)),
        patch("dashboard_writer.DASH_MAIN", str(main_html)),
    ):
        w = DashboardWriter()
        w.append_session(newer_bot_flat, daily_in, [])

    saved = json.loads(data_file.read_text(encoding="utf-8"))
    symbols_today = {t["symbol"] for t in saved["trades"] if t.get("date") == today}
    assert symbols_today == {"SPY", "DIA"}
    assert ephemeral_same_day["symbol"] not in symbols_today
    spy_same_day = [t for t in saved["trades"] if t.get("date") == today and t.get("symbol") == "SPY"]
    assert len(spy_same_day) == 2
    assert {t.get("ledger_leg") for t in spy_same_day} == {"OPEN", "CLOSE"}
    summary = next(d for d in saved["daily"] if d["date"] == today)
    assert summary["total_trades"] == 2
    assert summary["winners"] == 2 and summary["losers"] == 0
    assert summary["net_pnl"] == 45.0


def test_append_session_explicit_opens_persisted(tmp_path):
    data_file = tmp_path / "trade_data.json"
    live_html = tmp_path / "live_dashboard.html"
    main_html = tmp_path / "fabio_live_dashboard.html"
    data_file.write_text(
        json.dumps({"trades": [], "daily": [], "open_positions": []}),
        encoding="utf-8",
    )

    opens = [
        {
            "date": "2026-05-07",
            "symbol": "QQQ",
            "direction": "CALL",
            "entry_time": "—",
            "entry_price": 1.0,
            "contracts": 1,
            "vix": 0.0,
            "or_atr_pct": 0.0,
            "notes": "broker code=US.QQQ251219C00400000",
        }
    ]
    with (
        patch("dashboard_writer.DATA_FILE", str(data_file)),
        patch("dashboard_writer.DASH_LOCAL", str(live_html)),
        patch("dashboard_writer.DASH_MAIN", str(main_html)),
    ):
        w = DashboardWriter()
        w.append_session([], _minimal_daily("2026-05-07"), opens)

    saved = json.loads(data_file.read_text(encoding="utf-8"))
    assert saved["open_positions"] == opens


def test_moomoo_position_records_to_dashboard_opens_call_and_put():
    call_row = {
        "code": "US.SPY251219C00600000",
        "qty": 2,
        "cost_price": 1.55,
    }
    put_row = {
        "code": "US.QQQ251219P00400000",
        "qty": 3,
        "average_cost": 0.88,
    }
    out = moomoo_position_records_to_dashboard_opens(
        [call_row, put_row],
        as_of_date="2026-05-07",
    )
    assert len(out) == 2
    assert out[0]["symbol"] == "SPY"
    assert out[0]["direction"] == "CALL"
    assert out[0]["contracts"] == 2
    assert out[0]["entry_price"] == 1.55
    assert out[1]["symbol"] == "QQQ"
    assert out[1]["direction"] == "PUT"
    assert out[1]["contracts"] == 3


def test_moomoo_position_records_skips_non_positive_qty():
    out = moomoo_position_records_to_dashboard_opens(
        [{"code": "US.SPY251219C00600000", "qty": 0}]
    )
    assert out == []


def test_moomoo_position_records_falls_back_to_can_sell_qty():
    """Moomoo sometimes reports qty 0 while can_sell_qty > 0; treat like reconcile."""
    out = moomoo_position_records_to_dashboard_opens(
        [
            {
                "code": "US.SPY251219C00600000",
                "qty": 0,
                "can_sell_qty": 4,
                "cost_price": 1.1,
            }
        ]
    )
    assert len(out) == 1
    assert out[0]["contracts"] == 4


def test_moomoo_position_records_skips_stock_holdings():
    """Underlying stock rows are not options; do not map to dashboard opens."""
    out = moomoo_position_records_to_dashboard_opens(
        [{"code": "US.NVDA", "qty": 100, "cost_price": 212.0}]
    )
    assert out == []


def test_normalize_and_validate_open_positions():
    fifo_row = {
        "symbol": "SPY",
        "contracts": 2,
        "notes": "moomoo_paper_fifo",
        "date": "2026-05-07",
    }
    bad_notes = {
        "symbol": "SPY",
        "contracts": 1,
        "notes": "random",
    }
    legacy = {"symbol": "SPY", "contracts": 1, "exit_reason": "OPEN"}
    cleaned, dropped = normalize_and_validate_open_positions(
        [fifo_row, bad_notes, legacy, "not-a-dict"]
    )
    assert dropped == 3
    assert cleaned == [fifo_row]


def test_normalize_keeps_four_book_sources_and_preserves_moomoo_fifo():
    fifo = {"symbol": "SPY", "contracts": 2, "notes": "moomoo_paper_fifo"}
    tradier = {"symbol": "QQQ", "contracts": 1, "notes": "source=tradier_paper"}
    mr = {"symbol": "NOK", "contracts": 1, "notes": "source=mr"}
    mr_t = {"symbol": "IBM", "contracts": 1, "notes": "source=mr_tradier"}
    cleaned, dropped = normalize_and_validate_open_positions(
        [fifo, tradier, mr, mr_t, {"symbol": "X", "contracts": 1, "notes": "random"}]
    )
    assert dropped == 1
    assert [r["notes"] for r in cleaned] == [
        "moomoo_paper_fifo",
        "source=tradier_paper",
        "source=mr",
        "source=mr_tradier",
    ]
    assert dashboard_row_derived_from_moomoo_sync(fifo) is True
    assert dashboard_row_derived_from_moomoo_sync(tradier) is False
    assert dashboard_row_derived_from_moomoo_sync(mr_t) is False


def test_tradier_position_records_not_tagged_moomoo_fifo():
    out = tradier_position_records_to_dashboard_opens(
        [{"symbol": "SPY261016C00500000", "quantity": 2, "cost_basis": 1.25}]
    )
    assert len(out) == 1
    assert out[0]["symbol"] == "SPY"
    assert out[0]["notes"] == "source=tradier_paper"
    assert "moomoo_paper_fifo" not in out[0]["notes"]


def test_init_strips_legacy_trade_shaped_opens(tmp_path):
    """Rows copied from trade-entry logs (exit_reason field) are not broker opens."""
    data_file = tmp_path / "trade_data.json"
    live_html = tmp_path / "live_dashboard.html"
    main_html = tmp_path / "fabio_live_dashboard.html"
    data_file.write_text(
        json.dumps(
            {
                "trades": [],
                "daily": [],
                "open_positions": [
                    {"symbol": "SPY", "exit_reason": "OPEN", "contracts": 5},
                    {
                        "symbol": "QQQ",
                        "contracts": 1,
                        "notes": "broker code=US.x",
                        "date": "2026-05-07",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with (
        patch("dashboard_writer.DATA_FILE", str(data_file)),
        patch("dashboard_writer.DASH_LOCAL", str(live_html)),
        patch("dashboard_writer.DASH_MAIN", str(main_html)),
    ):
        DashboardWriter()

    saved = json.loads(data_file.read_text(encoding="utf-8"))
    assert len(saved["open_positions"]) == 1
    assert saved["open_positions"][0]["symbol"] == "QQQ"


def test_load_normalizes_missing_open_positions_key(tmp_path):
    data_file = tmp_path / "trade_data.json"
    live_html = tmp_path / "live_dashboard.html"
    main_html = tmp_path / "fabio_live_dashboard.html"
    data_file.write_text(
        json.dumps({"trades": [], "daily": []}),
        encoding="utf-8",
    )
    with (
        patch("dashboard_writer.DATA_FILE", str(data_file)),
        patch("dashboard_writer.DASH_LOCAL", str(live_html)),
        patch("dashboard_writer.DASH_MAIN", str(main_html)),
    ):
        w = DashboardWriter()
        assert w._data.get("open_positions") == []
    assert not live_html.exists()  # empty load does not rewrite HTML


_TRACKED_LIVE = Path(__file__).resolve().parents[1] / "live_dashboard.html"
_WRITER_SRC = Path(__file__).resolve().parents[1] / "dashboard_writer.py"
_PUSH_DASH = Path(__file__).resolve().parents[2] / "portal" / "push_dashboard.sh"


def test_writer_source_has_no_embedded_template():
    src = _WRITER_SRC.read_text(encoding="utf-8")
    assert "id=\"opsView\"" not in src
    assert "id='opsView'" not in src
    assert "_TEMPLATE" not in src
    assert "live_dashboard_template.html" in src


def test_write_html_uses_file_template_preserves_ops_and_data(tmp_path):
    tracked_before = _TRACKED_LIVE.read_bytes() if _TRACKED_LIVE.is_file() else b""
    data_file = tmp_path / "trade_data.json"
    live_html = tmp_path / "live_dashboard.html"
    main_html = tmp_path / "fabio_live_dashboard.html"
    trades = [
        {
            "date": "2026-09-21",
            "symbol": "SPY",
            "direction": "CALL",
            "pnl": 12.0,
            "include_in_session_pnl": True,
            "notes": "moomoo_paper_fifo",
        }
    ]
    opens = [
        {
            "date": "2026-09-21",
            "symbol": "QQQ",
            "direction": "CALL",
            "entry_time": "—",
            "entry_price": 1.0,
            "contracts": 1,
            "vix": 0.0,
            "or_atr_pct": 0.0,
            "notes": "broker code=US.QQQ251219C00400000",
        }
    ]
    data_file.write_text(
        json.dumps({"trades": trades, "daily": [_minimal_daily("2026-09-21")], "open_positions": opens}),
        encoding="utf-8",
    )
    with (
        patch("dashboard_writer.DATA_FILE", str(data_file)),
        patch("dashboard_writer.DASH_LOCAL", str(live_html)),
        patch("dashboard_writer.DASH_MAIN", str(main_html)),
        patch("dashboard_writer.HEALTH_JSONL_FILE", str(tmp_path / "missing_health.jsonl")),
    ):
        w = DashboardWriter()
        w._write_html()

    html = live_html.read_text(encoding="utf-8")
    assert 'id="opsView"' in html
    assert 'id="botStatusPill"' in html
    assert "__DATA_JSON__" not in html
    assert "__STORY_LINK_HTML__" not in html
    assert "fabio_scrollytelling.html" in html
    assert "python3 -m http.server" in html
    assert "fabio serve" not in html
    assert "Four paper books · $10k each · paper only" in html
    for bid in ("orb-moomoo", "orb-tradier", "mr-moomoo", "mr-tradier"):
        assert f'data-book-id="{bid}"' in html
    data_line = next(ln for ln in html.splitlines() if ln.startswith("const DATA = "))
    raw = data_line[len("const DATA = ") :]
    if raw.endswith(";"):
        raw = raw[:-1]
    payload = json.loads(raw)
    assert payload["trades"][0]["symbol"] == "SPY"
    assert payload["open_positions"][0]["symbol"] == "QQQ"
    assert payload.get("beta_identity") is not None
    assert "dashboard_generated_at_utc" in payload
    assert _TRACKED_LIVE.read_bytes() == tracked_before


def test_push_dashboard_sh_does_not_auto_commit_html():
    text = _PUSH_DASH.read_text(encoding="utf-8")
    assert not re.search(r"^\s*git add\b", text, re.M)
    assert not re.search(r"^\s*git commit\b", text, re.M)
    assert not re.search(r"^\s*git push\b", text, re.M)
    assert "python3 -m http.server" in text
    assert "live_dashboard.html" in text
    assert "paused" in text.lower() or "PAUSED" in text
