from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from fabio_live.bot import (
    ORBBot,
    PAUSE_REASON_RECONCILE_AUTO_ADOPT_NONE,
    PAUSE_REASON_RECONCILE_EXCEPTION,
    PAUSE_REASON_RECONCILE_MANUAL_REQUIRED,
    PAUSE_REASON_RECONCILE_QUERY_FAILED,
    PAUSE_REASON_VIX_UNAVAILABLE,
)
from fabio_live.market_data import get_vix
from fabio_live.orders import OrderManager
from reconcile_moomoo_to_sheets import _reconcile_fifo
from sheets_logger import SheetsLogger
import verify_phase2_reliability as gate


class _QuoteCtxForExit:
    def get_market_snapshot(self, codes):
        return 0, pd.DataFrame({"ask_price": [1.0], "bid_price": [1.1], "last_price": [1.1]})


class _TradeCtxExitOk:
    def place_order(self, **kwargs):
        return 0, pd.DataFrame({"order_id": ["S1"]})


class _TradeCtxExitFail:
    def place_order(self, **kwargs):
        return 1, "fail"


def test_order_manager_exit_result_success_and_failure():
    ok_mgr = OrderManager(_TradeCtxExitOk(), _QuoteCtxForExit(), trd_env=None)
    ok_mgr.positions["SPY"] = {
        "direction": "CALL",
        "code": "US.SPYC",
        "original_qty": 2,
        "remaining_qty": 2,
        "entry_option_price": 1.0,
        "trim_level": 0,
        "realized_trim_pnl": 0.0,
    }
    ok = ok_mgr.exit_result("SPY", reason="test")
    assert ok["success"] is True
    assert ok["qty_final_leg"] == 2
    assert ok["pnl_final_leg"] == pytest.approx(20.0)
    assert ok["pnl"] == pytest.approx(20.0)
    assert "SPY" not in ok_mgr.positions

    fail_mgr = OrderManager(_TradeCtxExitFail(), _QuoteCtxForExit(), trd_env=None)
    fail_mgr.positions["QQQ"] = {
        "direction": "PUT",
        "code": "US.QQQP",
        "original_qty": 1,
        "remaining_qty": 1,
        "entry_option_price": 1.0,
        "trim_level": 0,
        "realized_trim_pnl": 0.0,
    }
    fail = fail_mgr.exit_result("QQQ", reason="test")
    assert fail["success"] is False
    assert fail["error"] == "sell_failed"
    assert "QQQ" in fail_mgr.positions


def test_options_only_safety_blocks_non_option_entry(monkeypatch):
    class _TradeCtx:
        def __init__(self):
            self.calls = 0

        def place_order(self, **kwargs):
            self.calls += 1
            return 0, pd.DataFrame({"order_id": ["B1"]})

    class _QuoteCtx:
        pass

    mgr = OrderManager(_TradeCtx(), _QuoteCtx(), trd_env=None)
    monkeypatch.setattr(mgr, "_option_code", lambda *args, **kwargs: "US.SPY")
    mgr.enter(
        symbol="SPY",
        direction="CALL",
        price=500.0,
        risk_pct=0.01,
        portfolio_val=100_000.0,
    )
    assert mgr.ctx.calls == 0


def test_verify_gate_pass_and_fail_paths(monkeypatch, tmp_path: Path):
    snap = tmp_path / "snap.jsonl"
    payload = {
        "ts": "2099-01-01T09:30:00-05:00",
        "ops": {
            "queue_depth": 1,
            "queue_max": 500,
            "thread_alive": True,
            "errors": 0,
            "dropped_critical": 0,
            "dropped_noncritical": 0,
        },
        "data_health": {"SPY_5m": {"state": "OK"}},
    }
    snap.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        gate,
        "_parse_args",
        lambda: type(
            "Args",
            (),
            {
                "snapshot_path": str(snap),
                "max_age_min": 999999.0,
                "allow_stale_data": False,
                "max_queue_ratio": 0.95,
                "sync_audit_jsonl": "",
                "sync_audit_max_age_min": -1.0,
            },
        )(),
    )
    assert gate.main() == 0

    bad_payload = dict(payload)
    bad_payload["ops"] = dict(payload["ops"])
    bad_payload["ops"]["dropped_critical"] = 1
    snap.write_text(json.dumps(bad_payload) + "\n", encoding="utf-8")
    assert gate.main() == 1


def test_get_vix_returns_none_on_fetch_failure(monkeypatch):
    class _Ticker:
        def history(self, **kwargs):
            raise RuntimeError("network down")

    class _YF:
        @staticmethod
        def Ticker(_sym):
            return _Ticker()

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _YF)
    assert get_vix(None) is None


def test_startup_reconcile_pauses_on_orphan_positions(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", False)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None)
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (
            0,
            pd.DataFrame({"code": ["US.SPYC"], "qty": [2]}),
        )
    )
    bot._startup_reconcile_positions()
    assert bot.paused is True
    assert bot._pause_reason_code == PAUSE_REASON_RECONCILE_MANUAL_REQUIRED
    assert len(bot._startup_unreconciled_positions) == 1


def test_startup_reconcile_auto_adopts_valid_positions(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None, positions={})
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (
            0,
            pd.DataFrame(
                {
                    "code": ["US.SPY260507C00735000"],
                    "qty": [2],
                    "cost_price": [1.25],
                }
            ),
        )
    )
    bot._startup_reconcile_positions()
    assert bot.paused is False
    assert bot._startup_unreconciled_positions == []
    assert "SPY" in bot.order_mgr.positions
    assert bot.signals["SPY"] == "CALL"
    assert "SPY" in bot.exit_tfs
    assert bot.order_mgr.positions["SPY"]["entry_option_price"] == 1.25
    assert bot.order_mgr.positions["SPY"].get("source") != "mr"


def test_startup_reconcile_partial_adoption_keeps_running(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None, positions={})
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (
            0,
            pd.DataFrame(
                {
                    "code": ["US.SPY260507C00735000", "US.BADCODE"],
                    "qty": [2, 1],
                    "cost_price": [1.25, 0.9],
                }
            ),
        )
    )
    bot._startup_reconcile_positions()
    assert bot.paused is False
    assert len(bot._startup_unreconciled_positions) == 1
    assert "SPY" in bot.order_mgr.positions


def test_startup_reconcile_auto_adopt_failure_pauses(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None, positions={})
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (
            0,
            pd.DataFrame(
                {
                    "code": ["US.BADCODE"],
                    "qty": [2],
                    "cost_price": [0.0],
                }
            ),
        )
    )
    bot._startup_reconcile_positions()
    assert bot.paused is True
    assert bot._pause_reason_code == PAUSE_REASON_RECONCILE_AUTO_ADOPT_NONE
    assert len(bot._startup_unreconciled_positions) == 1


def test_startup_reconcile_query_failure_pauses(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._pause_reason_code = ""
    bot._pause_reason_hint = ""
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None, positions={})
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (-1, pd.DataFrame()),
    )
    bot._startup_reconcile_positions()
    assert bot.paused is True
    assert bot._pause_reason_code == PAUSE_REASON_RECONCILE_QUERY_FAILED


def test_startup_reconcile_exception_pauses(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._pause_reason_code = ""
    bot._pause_reason_hint = ""
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None, positions={})
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)

    def boom(**kwargs):
        raise RuntimeError("boom")

    bot.trade_ctx = SimpleNamespace(position_list_query=boom)
    bot._startup_reconcile_positions()
    assert bot.paused is True
    assert bot._pause_reason_code == PAUSE_REASON_RECONCILE_EXCEPTION


def test_startup_reconcile_no_pause_when_pos_df_none(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._pause_reason_code = ""
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None, positions={})
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(position_list_query=lambda **kwargs: (0, None))
    bot._startup_reconcile_positions()
    assert bot.paused is False
    assert bot._pause_reason_code == ""


def test_startup_reconcile_no_pause_when_only_zero_qty_rows(monkeypatch):
    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._pause_reason_code = ""
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = SimpleNamespace(trd_env=None, positions={})
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (
            0,
            pd.DataFrame({"code": ["US.SPY260507C00735000"], "qty": [0], "cost_price": [1.0]}),
        )
    )
    bot._startup_reconcile_positions()
    assert bot.paused is False


def test_initialize_day_pauses_with_vix_unavailable(monkeypatch):
    import datetime
    from zoneinfo import ZoneInfo

    bot = ORBBot.__new__(ORBBot)
    bot._prefetched = False
    bot.paused = False
    bot._pause_reason_code = ""
    bot._pause_reason_hint = ""
    bot.regimes = {}
    bot.quote_ctx = object()
    bot.trade_ctx = object()
    bot.sheets = SimpleNamespace(is_connected=lambda: False)
    bot.cb = SimpleNamespace(set_portfolio_open=lambda *_: None)

    def _now():
        return datetime.datetime(2026, 5, 8, 9, 31, tzinfo=ZoneInfo("America/New_York"))

    bot._now_market = _now

    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    monkeypatch.setattr("fabio_live.bot.get_vix", lambda _q: None)
    bot.initialize_day()
    assert bot.paused is True
    assert bot._pause_reason_code == PAUSE_REASON_VIX_UNAVAILABLE


def test_hydrate_circuit_from_sheets_today_applies_snapshot():
    bot = ORBBot.__new__(ORBBot)
    bot.cb = SimpleNamespace(trade_count=0, realized_pnl=0.0, loss_streak=0)
    bot.sheets = SimpleNamespace(
        is_connected=lambda: True,
        get_today_circuit_snapshot=lambda: {
            "trade_count": 3,
            "realized_pnl": 450.0,
            "loss_streak": 1,
        },
    )
    bot._hydrate_circuit_from_sheets_today()
    assert bot.cb.trade_count == 3
    assert bot.cb.realized_pnl == 450.0
    assert bot.cb.loss_streak == 1


def test_hydrate_circuit_from_sheets_today_no_snapshot_no_change():
    bot = ORBBot.__new__(ORBBot)
    bot.cb = SimpleNamespace(trade_count=0, realized_pnl=0.0, loss_streak=0)
    bot.sheets = SimpleNamespace(
        is_connected=lambda: True,
        get_today_circuit_snapshot=lambda: None,
    )
    bot._hydrate_circuit_from_sheets_today()
    assert bot.cb.trade_count == 0
    assert bot.cb.realized_pnl == 0.0
    assert bot.cb.loss_streak == 0


def test_reconcile_fifo_full_close_and_flat_open_inventory():
    records = [
        {
            "fill_id": "1",
            "time": "2026-05-07 09:45:00",
            "code": "US.SPY260507C00735000",
            "symbol": "SPY",
            "direction": "CALL",
            "trd_side": "BUY",
            "qty": 2,
            "avg_price": 1.0,
            "realized_pnl": 0.0,
        },
        {
            "fill_id": "2",
            "time": "2026-05-07 10:00:00",
            "code": "US.SPY260507C00735000",
            "symbol": "SPY",
            "direction": "CALL",
            "trd_side": "SELL",
            "qty": 2,
            "avg_price": 1.2,
            "realized_pnl": 40.0,
        },
    ]
    closed, open_rows = _reconcile_fifo(records)
    assert len(closed) == 1
    assert open_rows == []
    assert float(closed[0][10]) == 40.0


def test_reconcile_fifo_partial_close_keeps_open_lot():
    records = [
        {
            "fill_id": "1",
            "time": "2026-05-07 09:45:00",
            "code": "US.SPY260507C00735000",
            "symbol": "SPY",
            "direction": "CALL",
            "trd_side": "BUY",
            "qty": 3,
            "avg_price": 1.0,
            "realized_pnl": 0.0,
        },
        {
            "fill_id": "2",
            "time": "2026-05-07 10:00:00",
            "code": "US.SPY260507C00735000",
            "symbol": "SPY",
            "direction": "CALL",
            "trd_side": "SELL",
            "qty": 1,
            "avg_price": 1.3,
            "realized_pnl": 30.0,
        },
    ]
    closed, open_rows = _reconcile_fifo(records)
    assert len(closed) == 1
    assert len(open_rows) == 1
    assert int(open_rows[0][5]) == 2


def test_circuit_snapshot_prefers_reconciled_rows():
    rows = [
        ["2026-05-07 10:00:00", "2026-05-07", "US.A", "SPY", "CALL", "1", "", "", "", "", "50", "", ""],
        ["2026-05-07 11:00:00", "2026-05-07", "US.B", "QQQ", "PUT", "1", "", "", "", "", "-10", "", ""],
        ["2026-05-07 12:00:00", "2026-05-07", "US.C", "NVDA", "CALL", "1", "", "", "", "", "-5", "", ""],
    ]
    snap = SheetsLogger._compute_circuit_snapshot_from_recon_rows(rows, "2026-05-07")
    assert snap["trade_count"] == 3
    assert snap["realized_pnl"] == 35.0
    assert snap["loss_streak"] == 2


def test_stale_entry_data_hard_blocks_order(monkeypatch):
    entered = {"called": False}
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot.regimes = {
        "SPY": SimpleNamespace(
            tradeable=True,
            vix=18.0,
            gap_pct=0.2,
            or_atr_pct=20.0,
            or_high=101.0,
            or_low=99.0,
            day_color="GREEN",
            retest_required=False,
            vix_label="NORMAL",
            risk_multiplier=lambda **kwargs: 0.1,
        )
    }
    bot.order_mgr = SimpleNamespace(
        has_position=lambda sym: False,
        open_count=lambda: 0,
        enter=lambda *args, **kwargs: entered.__setitem__("called", True),
    )
    bot.cb = SimpleNamespace(can_enter=lambda n_open: (True, ""), size_modifier=lambda: 1.0)
    bot._cb_logged = set()
    bot.ops = SimpleNamespace(log_decision=lambda *args, **kwargs: None)
    bot._record_data_health = lambda *args, **kwargs: None
    bot.quote_ctx = None
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")

    old_ts = pd.Timestamp.now(tz=bot._tz) - pd.Timedelta(minutes=20)
    df = pd.DataFrame(
        {
            "time_key": [old_ts],
            "close": [100.0],
            "high": [100.1],
            "low": [99.9],
            "open": [100.0],
            "volume": [1],
        }
    )
    monkeypatch.setattr("fabio_live.bot.get_candles_fresh", lambda *args, **kwargs: df)
    monkeypatch.setattr("fabio_live.bot.candle_age_seconds", lambda _df: 20 * 60)

    class _Engine:
        def __init__(self, _regime):
            pass

        def check_breakout(self, _df):
            return "CALL"

        def is_counter_trend(self, _direction):
            return False

    monkeypatch.setattr("fabio_live.bot.SignalEngine", _Engine)
    bot._process_signal("SPY")
    assert entered["called"] is False


def test_eod_exit_failure_keeps_signal_tracked():
    bot = ORBBot.__new__(ORBBot)
    bot.signals = {"SPY": "CALL"}
    bot.order_mgr = SimpleNamespace(
        exit_result=lambda sym, reason="": {"success": False, "error": "sell_failed"},
        trd_env=None,
        _sell=lambda *args, **kwargs: True,
    )
    bot.cb = SimpleNamespace(record_result=lambda *_: None)
    bot._log_exit = lambda *args, **kwargs: None
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(position_list_query=lambda **kwargs: (0, pd.DataFrame()))
    bot.eod_close_all()
    assert "SPY" in bot.signals


def test_loop_cadence_uses_active_sleep_when_signals_open():
    bot = ORBBot.__new__(ORBBot)
    bot.signals = {}
    bot._loop_cadence_mode = "idle"
    bot._loop_sleep_sec = 60.0

    idle_sleep = bot._compute_loop_sleep_sec()
    assert idle_sleep == 60.0
    bot._update_loop_cadence_mode(idle_sleep)
    assert bot._loop_cadence_mode == "idle"
    assert bot._loop_sleep_sec == 60.0

    bot.signals = {"SPY": "CALL"}
    active_sleep = bot._compute_loop_sleep_sec()
    assert active_sleep <= 30.0
    assert active_sleep >= 1.0
    bot._update_loop_cadence_mode(active_sleep)
    assert bot._loop_cadence_mode == "active"
    assert bot._loop_sleep_sec == active_sleep
