"""Slice 2: per-book last-flatten sidecars (fail-safe + primary bot)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from fabio_live.paper_books import (
    ALL_PAPER_BOOKS,
    BOOK_MR_MOOMOO,
    BOOK_MR_TRADIER,
    BOOK_ORB_MOOMOO,
    BOOK_ORB_TRADIER,
    BROKER_MOOMOO,
    BROKER_TRADIER,
    LAYER_FAILSAFE,
    LAYER_PRIMARY_BOT,
    LEDGER_DIR_ENV,
    PRIMARY_FLATTEN_SCRIPT,
    REASON_ABORTED_WINDOW,
    REASON_BOOK_EMPTY,
    REASON_ERROR,
    REASON_FLATTEN,
    REASON_NO_CLOSABLE,
    PaperBookLedger,
    PaperBookRegistry,
    flatten_sidecar_reason,
    last_flatten_path,
    read_last_flatten,
    write_failsafe_last_flatten,
    write_last_flatten,
    write_primary_last_flatten,
)
from moomoo_eod_failsafe import main as moomoo_main
from tests.test_tradier_paper import FakeResp, FakeSession, _client
from tradier_eod_flatten import main as tradier_main

SPY_MOOMOO = "US.SPY261016C00500000"
NOK_MOOMOO = "US.NOK261016C00010000"
SPY_TRADIER = "SPY261016C00500000"
IBM_TRADIER = "IBM261016C00250000"

REQUIRED_KEYS = {
    "book_id",
    "broker",
    "script",
    "started_at_utc",
    "finished_at_utc",
    "dry_run",
    "exit_code",
    "planned",
    "failures",
    "selected_codes_count",
    "reason",
    "layer",
}


def _assert_shape(data: dict, *, book_id: str, broker: str, layer: str) -> None:
    assert REQUIRED_KEYS <= set(data)
    assert data["book_id"] == book_id
    assert data["broker"] == broker
    assert data["layer"] == layer
    assert data["reason"] in {
        REASON_BOOK_EMPTY,
        REASON_NO_CLOSABLE,
        REASON_FLATTEN,
        REASON_ABORTED_WINDOW,
        REASON_ERROR,
    }


def _sidecar_names(directory) -> set[str]:
    return {p.name for p in directory.glob("*.last_flatten.json")}


class _FakePos:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        for i, row in enumerate(self._rows):
            yield i, row


class FakeMoomooCtx:
    def __init__(self, rows: list[dict] | None = None, *, query_ret: int = 0):
        self.rows = list(rows or [])
        self.query_ret = query_ret
        self.place_calls: list[dict] = []
        self.closed = False
        self.unlock_calls = 0

    def position_list_query(self, **_kwargs):
        return self.query_ret, _FakePos(self.rows)

    def place_order(self, **kwargs):
        self.place_calls.append(dict(kwargs))
        return 0, {"ok": True}

    def unlock_trade(self, password=""):
        self.unlock_calls += 1
        return 0, "ok"

    def close(self):
        self.closed = True


def _option_row(code: str, qty: float = 1.0) -> dict:
    return {"code": code, "can_sell_qty": qty, "position_side": "LONG"}


def _persist(tmp_path, book_id: str, *codes: str) -> None:
    led = PaperBookLedger(book_id, directory=tmp_path)
    for code in codes:
        led.add(code)
    assert led.save() is True


def test_flatten_sidecar_reason_enum():
    assert flatten_sidecar_reason(aborted=True) == REASON_ABORTED_WINDOW
    assert flatten_sidecar_reason(error=True) == REASON_ERROR
    assert flatten_sidecar_reason(broker_empty=True) == REASON_BOOK_EMPTY
    assert flatten_sidecar_reason(selected_codes_count=0) == REASON_NO_CLOSABLE
    assert flatten_sidecar_reason(selected_codes_count=2) == REASON_FLATTEN


def test_write_last_flatten_refuses_cross_broker(tmp_path):
    assert (
        write_last_flatten(
            book_id=BOOK_ORB_MOOMOO,
            broker=BROKER_TRADIER,
            script="moomoo_eod_failsafe.py",
            started_at_utc="2026-09-21T19:00:00Z",
            dry_run=True,
            exit_code=0,
            planned=0,
            failures=0,
            selected_codes_count=0,
            reason=REASON_NO_CLOSABLE,
            layer=LAYER_FAILSAFE,
            directory=tmp_path,
        )
        is None
    )
    assert (
        write_last_flatten(
            book_id=BOOK_ORB_TRADIER,
            broker=BROKER_MOOMOO,
            script="tradier_eod_flatten.py",
            started_at_utc="2026-09-21T19:00:00Z",
            dry_run=True,
            exit_code=0,
            planned=0,
            failures=0,
            selected_codes_count=0,
            reason=REASON_NO_CLOSABLE,
            layer=LAYER_FAILSAFE,
            directory=tmp_path,
        )
        is None
    )
    assert list(tmp_path.glob("*.last_flatten.json")) == []


def test_failsafe_write_is_book_isolated(tmp_path):
    path = write_failsafe_last_flatten(
        book_id=BOOK_MR_MOOMOO,
        started_at_utc="2026-09-21T19:00:00Z",
        dry_run=True,
        exit_code=0,
        planned=0,
        failures=0,
        selected_codes_count=0,
        reason=REASON_NO_CLOSABLE,
        directory=tmp_path,
    )
    assert path is not None
    assert path.name == "mr-moomoo.last_flatten.json"
    assert not (tmp_path / "orb-moomoo.last_flatten.json").exists()
    for spec in ALL_PAPER_BOOKS:
        if spec.book_id == BOOK_MR_MOOMOO:
            continue
        assert not last_flatten_path(spec.book_id, tmp_path).exists()
    data = read_last_flatten(BOOK_MR_MOOMOO, tmp_path)
    assert data is not None
    _assert_shape(data, book_id=BOOK_MR_MOOMOO, broker=BROKER_MOOMOO, layer=LAYER_FAILSAFE)
    assert data["script"] == "moomoo_eod_failsafe.py"
    assert data["dry_run"] is True


def test_moomoo_failsafe_book_flag_never_writes_sibling_or_tradier(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    _persist(tmp_path, BOOK_ORB_MOOMOO, SPY_MOOMOO)
    _persist(tmp_path, BOOK_MR_MOOMOO, NOK_MOOMOO)
    ctx = FakeMoomooCtx(
        [_option_row(SPY_MOOMOO), _option_row(NOK_MOOMOO)],
    )
    code = moomoo_main(
        ["--book", "mr-moomoo", "--dry-run", "--sleep-between-orders", "0"],
        trd_ctx=ctx,
    )
    assert code == 0
    assert ctx.place_calls == []
    names = _sidecar_names(tmp_path)
    assert names == {"mr-moomoo.last_flatten.json"}
    data = read_last_flatten(BOOK_MR_MOOMOO, tmp_path)
    assert data is not None
    _assert_shape(data, book_id=BOOK_MR_MOOMOO, broker=BROKER_MOOMOO, layer=LAYER_FAILSAFE)
    assert data["dry_run"] is True
    assert data["reason"] == REASON_FLATTEN
    assert data["selected_codes_count"] == 1
    assert data["planned"] == 1
    assert data["exit_code"] == 0
    assert read_last_flatten(BOOK_ORB_MOOMOO, tmp_path) is None
    assert read_last_flatten(BOOK_ORB_TRADIER, tmp_path) is None
    assert read_last_flatten(BOOK_MR_TRADIER, tmp_path) is None


def test_moomoo_failsafe_empty_ledger_is_visible_no_closable(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    ctx = FakeMoomooCtx([_option_row(SPY_MOOMOO), _option_row(NOK_MOOMOO)])
    code = moomoo_main(
        ["--book", "orb-moomoo", "--dry-run", "--log-format", "jsonl"],
        trd_ctx=ctx,
    )
    assert code == 0
    assert ctx.place_calls == []
    data = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert data is not None
    _assert_shape(data, book_id=BOOK_ORB_MOOMOO, broker=BROKER_MOOMOO, layer=LAYER_FAILSAFE)
    assert data["selected_codes_count"] == 0
    assert data["planned"] == 0
    assert data["reason"] == REASON_NO_CLOSABLE
    assert data["exit_code"] == 0
    assert data["dry_run"] is True
    assert _sidecar_names(tmp_path) == {"orb-moomoo.last_flatten.json"}
    assert read_last_flatten(BOOK_MR_MOOMOO, tmp_path) is None


def test_moomoo_failsafe_broker_empty_book(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    _persist(tmp_path, BOOK_ORB_MOOMOO, SPY_MOOMOO)
    ctx = FakeMoomooCtx([])
    code = moomoo_main(["--book", "orb-moomoo", "--dry-run"], trd_ctx=ctx)
    assert code == 0
    data = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert data is not None
    assert data["reason"] == REASON_BOOK_EMPTY
    assert data["selected_codes_count"] == 0
    assert data["exit_code"] == 0
    assert ctx.place_calls == []


def test_moomoo_failsafe_dry_run_writes_flatten_and_places_nothing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    _persist(tmp_path, BOOK_ORB_MOOMOO, SPY_MOOMOO)
    ctx = FakeMoomooCtx([_option_row(SPY_MOOMOO)])
    code = moomoo_main(
        ["--book", "orb-moomoo", "--dry-run", "--no-refresh-per-order"],
        trd_ctx=ctx,
    )
    assert code == 0
    assert ctx.place_calls == []
    data = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert data is not None
    assert data["dry_run"] is True
    assert data["reason"] == REASON_FLATTEN
    assert data["selected_codes_count"] == 1
    assert data["exit_code"] == 0


def test_moomoo_failsafe_jsonl_includes_book_id(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    ctx = FakeMoomooCtx([_option_row(SPY_MOOMOO)])
    code = moomoo_main(
        ["--book", "mr-moomoo", "--dry-run", "--log-format", "jsonl"],
        trd_ctx=ctx,
    )
    assert code == 0
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    no_closable = [e for e in events if e.get("event") == "no_closable"]
    assert no_closable
    assert all(e.get("book_id") == BOOK_MR_MOOMOO for e in no_closable)


def test_moomoo_failsafe_aborted_window_writes_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        "moomoo_eod_failsafe._xnys_failsafe_cutoff_ok",
        lambda *_a, **_k: (False, "before_failsafe_cutoff want_>=x"),
    )
    ctx = FakeMoomooCtx([_option_row(SPY_MOOMOO)])
    code = moomoo_main(
        ["--require-after-et", "--book", "orb-moomoo", "--dry-run"],
        trd_ctx=ctx,
    )
    assert code == 4
    assert ctx.place_calls == []
    data = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert data is not None
    assert data["reason"] == REASON_ABORTED_WINDOW
    assert data["exit_code"] == 4
    assert data["dry_run"] is True
    assert read_last_flatten(BOOK_ORB_TRADIER, tmp_path) is None


def test_tradier_failsafe_never_writes_moomoo_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    _persist(tmp_path, BOOK_ORB_TRADIER, SPY_TRADIER)
    _persist(tmp_path, BOOK_MR_TRADIER, IBM_TRADIER)
    session = FakeSession(
        [
            FakeResp(
                200,
                {
                    "positions": {
                        "position": [
                            {"symbol": SPY_TRADIER, "quantity": "1"},
                            {"symbol": IBM_TRADIER, "quantity": "2"},
                        ]
                    }
                },
            )
        ]
    )
    client = _client(session)
    code = tradier_main(
        ["--book", "mr-tradier", "--dry-run", "--sleep-between-orders", "0"],
        client=client,
    )
    assert code == 0
    posts = [c for c in session.calls if c["method"] == "POST"]
    assert posts == []
    names = _sidecar_names(tmp_path)
    assert names == {"mr-tradier.last_flatten.json"}
    data = read_last_flatten(BOOK_MR_TRADIER, tmp_path)
    assert data is not None
    _assert_shape(
        data, book_id=BOOK_MR_TRADIER, broker=BROKER_TRADIER, layer=LAYER_FAILSAFE
    )
    assert data["script"] == "tradier_eod_flatten.py"
    assert data["dry_run"] is True
    assert data["reason"] == REASON_FLATTEN
    assert data["selected_codes_count"] == 1
    assert data["exit_code"] == 0
    assert read_last_flatten(BOOK_ORB_TRADIER, tmp_path) is None
    assert read_last_flatten(BOOK_ORB_MOOMOO, tmp_path) is None
    assert read_last_flatten(BOOK_MR_MOOMOO, tmp_path) is None


def test_tradier_empty_ledger_visible_no_closable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    session = FakeSession(
        [
            FakeResp(
                200,
                {
                    "positions": {
                        "position": [
                            {"symbol": SPY_TRADIER, "quantity": "1"},
                            {"symbol": IBM_TRADIER, "quantity": "1"},
                        ]
                    }
                },
            )
        ]
    )
    client = _client(session)
    code = tradier_main(
        [
            "--book",
            "orb-tradier",
            "--dry-run",
            "--log-format",
            "jsonl",
            "--sleep-between-orders",
            "0",
        ],
        client=client,
    )
    assert code == 0
    assert [c["method"] for c in session.calls] == ["GET"]
    data = read_last_flatten(BOOK_ORB_TRADIER, tmp_path)
    assert data is not None
    _assert_shape(
        data, book_id=BOOK_ORB_TRADIER, broker=BROKER_TRADIER, layer=LAYER_FAILSAFE
    )
    assert data["selected_codes_count"] == 0
    assert data["planned"] == 0
    assert data["reason"] == REASON_NO_CLOSABLE
    assert data["exit_code"] == 0
    assert data["dry_run"] is True
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    no_closable = [e for e in events if e.get("event") == "no_closable"]
    assert no_closable
    assert all(e.get("book_id") == BOOK_ORB_TRADIER for e in no_closable)
    assert read_last_flatten(BOOK_ORB_MOOMOO, tmp_path) is None


def test_tradier_run_complete_jsonl_includes_book_id(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    _persist(tmp_path, BOOK_ORB_TRADIER, SPY_TRADIER)
    session = FakeSession(
        [
            FakeResp(
                200,
                {
                    "positions": {
                        "position": {"symbol": SPY_TRADIER, "quantity": "1"}
                    }
                },
            ),
            FakeResp(200, {"order": {"id": "1", "status": "ok"}}),
        ]
    )
    client = _client(session)
    code = tradier_main(
        ["--book", "orb-tradier", "--log-format", "jsonl", "--sleep-between-orders", "0"],
        client=client,
    )
    assert code == 0
    data = read_last_flatten(BOOK_ORB_TRADIER, tmp_path)
    assert data is not None
    assert data["dry_run"] is False
    assert data["reason"] == REASON_FLATTEN
    assert data["selected_codes_count"] == 1
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    done = [e for e in events if e.get("event") == "run_complete"]
    assert done
    assert all(e.get("book_id") == BOOK_ORB_TRADIER for e in done)


def test_primary_eod_writes_layer_primary_bot_per_book(tmp_path):
    from fabio_live.bot import ORBBot

    class _Mgr:
        def __init__(self, symbols, *, prefix="US."):
            self.positions = {
                s: {
                    "code": f"{prefix}{s}261016C00010000",
                    "remaining_qty": 1,
                    "direction": "CALL",
                    "source": "mr",
                }
                for s in symbols
            }
            self.closed = []
            self.trd_env = "SIMULATE"

        def exit_result(self, symbol, reason=""):
            self.closed.append(symbol)
            self.positions.pop(symbol, None)
            return {"success": True, "pnl": 1.0, "symbol": symbol, "reason": reason}

        def _sell(self, code, qty, label=""):
            raise AssertionError("orphan sweep should not run in this test")

    orb_mgr = _Mgr(["SPY"])
    mr_mgr = _Mgr(["NOK"])
    t_mgr = _Mgr(["QQQ"], prefix="")
    t_mgr.positions["QQQ"]["source"] = "tradier_paper"
    t_mgr.positions["QQQ"]["code"] = "QQQ261016C00400000"
    reg = PaperBookRegistry()
    reg.bind(BOOK_ORB_MOOMOO, order_mgr=orb_mgr, ledger_dir=tmp_path)
    mr_rt = reg.bind(BOOK_MR_MOOMOO, order_mgr=mr_mgr, ledger_dir=tmp_path)
    reg.bind(BOOK_ORB_TRADIER, order_mgr=t_mgr, ledger_dir=tmp_path)
    mr_ex = SimpleNamespace(
        book_id=BOOK_MR_MOOMOO,
        order_mgr=mr_mgr,
        owns=lambda _s: True,
        record_close=lambda _pnl: None,
        release=lambda _s: None,
        cb=mr_rt.cb,
    )
    bot = ORBBot.__new__(ORBBot)
    bot.signals = {"SPY": "CALL"}
    bot.order_mgr = orb_mgr
    bot.cb = SimpleNamespace(record_result=lambda *_a, **_k: None)
    bot._log_exit = lambda *_a, **_k: None
    bot._mr_executors = [mr_ex]
    bot._mr_executor = mr_ex
    bot._paper_books = reg
    bot.ops = SimpleNamespace(alert=lambda *_a, **_k: None, log_alert=lambda *_a, **_k: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **_k: (0, pd.DataFrame())
    )
    bot.eod_close_all()

    orb = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    mr = read_last_flatten(BOOK_MR_MOOMOO, tmp_path)
    tradier = read_last_flatten(BOOK_ORB_TRADIER, tmp_path)
    assert orb is not None and mr is not None and tradier is not None
    _assert_shape(orb, book_id=BOOK_ORB_MOOMOO, broker=BROKER_MOOMOO, layer=LAYER_PRIMARY_BOT)
    _assert_shape(mr, book_id=BOOK_MR_MOOMOO, broker=BROKER_MOOMOO, layer=LAYER_PRIMARY_BOT)
    _assert_shape(
        tradier, book_id=BOOK_ORB_TRADIER, broker=BROKER_TRADIER, layer=LAYER_PRIMARY_BOT
    )
    assert orb["script"] == PRIMARY_FLATTEN_SCRIPT
    assert orb["reason"] == REASON_FLATTEN
    assert orb["selected_codes_count"] == 1
    assert mr["reason"] == REASON_FLATTEN
    assert tradier["reason"] == REASON_FLATTEN
    assert read_last_flatten(BOOK_MR_TRADIER, tmp_path) is None
    assert orb_mgr.closed == ["SPY"]
    assert mr_mgr.closed == ["NOK"]
    assert t_mgr.closed == ["QQQ"]


def test_primary_does_not_write_unbound_tradier_from_moomoo_eod(tmp_path):
    from fabio_live.bot import ORBBot

    orb_om = SimpleNamespace(
        trd_env="SIMULATE",
        positions={},
        exit_result=lambda *_a, **_k: {"success": True, "pnl": 0},
        _sell=lambda *_a, **_k: None,
    )
    reg = PaperBookRegistry()
    reg.bind(BOOK_ORB_MOOMOO, order_mgr=orb_om, ledger_dir=tmp_path)
    bot = ORBBot.__new__(ORBBot)
    bot.signals = {}
    bot.order_mgr = orb_om
    bot.cb = SimpleNamespace(record_result=lambda *_a, **_k: None)
    bot._mr_executors = []
    bot._mr_executor = None
    bot._paper_books = reg
    bot.ops = SimpleNamespace(alert=lambda *_a, **_k: None, log_alert=lambda *_a, **_k: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **_k: (0, pd.DataFrame())
    )
    bot.eod_close_all()
    orb = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert orb is not None
    assert orb["layer"] == LAYER_PRIMARY_BOT
    assert orb["reason"] == REASON_NO_CLOSABLE
    assert orb["selected_codes_count"] == 0
    assert orb["exit_code"] == 0
    assert read_last_flatten(BOOK_ORB_TRADIER, tmp_path) is None
    assert read_last_flatten(BOOK_MR_MOOMOO, tmp_path) is None


def test_write_primary_vs_failsafe_layers(tmp_path):
    write_primary_last_flatten(
        book_id=BOOK_ORB_MOOMOO,
        started_at_utc="2026-09-21T19:40:00Z",
        planned=1,
        failures=0,
        selected_codes_count=1,
        reason=REASON_FLATTEN,
        directory=tmp_path,
    )
    primary = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert primary is not None
    assert primary["layer"] == LAYER_PRIMARY_BOT
    write_failsafe_last_flatten(
        book_id=BOOK_ORB_MOOMOO,
        started_at_utc="2026-09-21T19:50:00Z",
        dry_run=False,
        exit_code=0,
        planned=0,
        failures=0,
        selected_codes_count=0,
        reason=REASON_NO_CLOSABLE,
        directory=tmp_path,
    )
    latest = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert latest is not None
    assert latest["layer"] == LAYER_FAILSAFE
    assert latest["reason"] == REASON_NO_CLOSABLE
