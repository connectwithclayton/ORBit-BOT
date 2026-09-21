"""Slice 5: four isolated paper books (ORB/MR × Moomoo/Tradier)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from brokers.tradier.client import TradierPaperClient
from brokers.tradier.orders import TradierOrderManager, tradier_occ
from fabio_live.circuit import RiskCircuitBreaker
from fabio_live.execution import ExecutionPort
from fabio_live.mr_paper import MrPaperExecutor, SKIP_MULTI_LEG
from fabio_live.paper_books import (
    ALL_PAPER_BOOKS,
    BOOK_MR_MOOMOO,
    BOOK_MR_TRADIER,
    BOOK_ORB_MOOMOO,
    BOOK_ORB_TRADIER,
    FIFO_NOTES_ORB_MOOMOO,
    PAPER_BOOK_STARTING_BALANCE,
    PaperBookLedger,
    PaperBookRegistry,
    flatten_entry_for_book,
    flatten_scripts_are_isolated,
    flatten_symbol_filters,
    init_book_circuit,
    is_allowed_open_position_notes,
    modeled_book_equity,
    select_flatten_codes,
    try_bind_tradier_books,
)
from paper_pin import ALLOW_REAL_ENV, LiveFundsRefused
from signal_intake.parse import parse_payload
from tests.test_tradier_paper import FakeResp, FakeSession

BACKEND = Path(__file__).resolve().parents[1]


def _client(session: FakeSession) -> TradierPaperClient:
    return TradierPaperClient(
        access_token="test-token",
        account_id="VA000",
        env="paper",
        session=session,
    )


def test_four_books_modeled_with_10k_each():
    assert len(ALL_PAPER_BOOKS) == 4
    ids = [b.book_id for b in ALL_PAPER_BOOKS]
    assert ids == [
        BOOK_ORB_MOOMOO,
        BOOK_ORB_TRADIER,
        BOOK_MR_MOOMOO,
        BOOK_MR_TRADIER,
    ]
    for spec in ALL_PAPER_BOOKS:
        assert spec.starting_balance == PAPER_BOOK_STARTING_BALANCE == 10_000.0
        cb = init_book_circuit()
        assert cb.portfolio_at_open == 10_000.0
        assert modeled_book_equity(cb) == 10_000.0


def test_sources_are_distinct_and_preserve_moomoo_fifo():
    sources = [b.source for b in ALL_PAPER_BOOKS]
    assert len(set(sources)) == 4
    fifo = {b.book_id: b.fifo_notes for b in ALL_PAPER_BOOKS}
    assert fifo[BOOK_ORB_MOOMOO] == FIFO_NOTES_ORB_MOOMOO == "moomoo_paper_fifo"
    assert is_allowed_open_position_notes("moomoo_paper_fifo")
    assert is_allowed_open_position_notes("source=tradier_paper")
    assert is_allowed_open_position_notes("source=mr")
    assert is_allowed_open_position_notes("source=mr_tradier")
    assert is_allowed_open_position_notes("broker code=US.SPY261016C00500000")
    assert not is_allowed_open_position_notes("random")


def test_flatten_entries_never_cross_brokers():
    assert flatten_entry_for_book(BOOK_ORB_MOOMOO) == "moomoo_eod_failsafe.py"
    assert flatten_entry_for_book(BOOK_MR_MOOMOO) == "moomoo_eod_failsafe.py"
    assert flatten_entry_for_book(BOOK_ORB_TRADIER) == "tradier_eod_flatten.py"
    assert flatten_entry_for_book(BOOK_MR_TRADIER) == "tradier_eod_flatten.py"
    assert flatten_scripts_are_isolated(BOOK_ORB_MOOMOO, BOOK_ORB_TRADIER)
    assert flatten_scripts_are_isolated(BOOK_MR_MOOMOO, BOOK_MR_TRADIER)
    assert flatten_scripts_are_isolated(BOOK_ORB_MOOMOO, BOOK_MR_MOOMOO)


def test_select_flatten_codes_does_not_close_the_other_book():
    ledgers = {
        BOOK_ORB_MOOMOO: {"US.SPY261016C00500000"},
        BOOK_MR_MOOMOO: {"US.NOK261016C00010000"},
        BOOK_ORB_TRADIER: {"SPY261016C00500000"},
        BOOK_MR_TRADIER: {"IBM261016C00250000"},
    }
    account = [
        "US.SPY261016C00500000",
        "US.NOK261016C00010000",
        "US.QQQ261016P00400000",
    ]
    orb = select_flatten_codes(
        book_id=BOOK_ORB_MOOMOO,
        broker="moomoo",
        account_codes=account,
        ledgers=ledgers,
    )
    mr = select_flatten_codes(
        book_id=BOOK_MR_MOOMOO,
        broker="moomoo",
        account_codes=account,
        ledgers=ledgers,
    )
    assert orb == ["US.SPY261016C00500000"]
    assert mr == ["US.NOK261016C00010000"]
    with pytest.raises(ValueError, match="cannot flatten"):
        select_flatten_codes(
            book_id=BOOK_ORB_TRADIER,
            broker="moomoo",
            account_codes=account,
            ledgers=ledgers,
        )


def test_mr_book_without_ledger_does_not_sweep_firm():
    ledgers = {BOOK_ORB_MOOMOO: {"US.SPY261016C00500000"}, BOOK_MR_MOOMOO: set()}
    mr = select_flatten_codes(
        book_id=BOOK_MR_MOOMOO,
        broker="moomoo",
        account_codes=["US.SPY261016C00500000", "US.NOK261016C00010000"],
        ledgers=ledgers,
    )
    assert mr == []
    only, exclude = flatten_symbol_filters(BOOK_MR_MOOMOO, "moomoo", ledgers)
    assert only == set()
    assert "US.SPY261016C00500000" in exclude


def test_registry_refuses_shared_order_manager(tmp_path):
    shared = SimpleNamespace(positions={})
    reg = PaperBookRegistry()
    reg.bind(BOOK_ORB_MOOMOO, order_mgr=shared, ledger_dir=tmp_path)
    with pytest.raises(ValueError, match="already bound"):
        reg.bind(BOOK_MR_MOOMOO, order_mgr=shared, ledger_dir=tmp_path)


def test_circuits_are_independent_across_books(tmp_path):
    orb_om = SimpleNamespace(positions={})
    mr_om = SimpleNamespace(positions={})
    reg = PaperBookRegistry()
    orb = reg.bind(BOOK_ORB_MOOMOO, order_mgr=orb_om, ledger_dir=tmp_path)
    mr = reg.bind(BOOK_MR_MOOMOO, order_mgr=mr_om, ledger_dir=tmp_path)
    assert orb.cb is not mr.cb
    orb.cb.record_result(-500.0)
    allowed, _ = mr.cb.can_enter(0)
    assert allowed is True
    assert orb.cb.realized_pnl == -500.0
    assert mr.cb.realized_pnl == 0.0
    assert orb.modeled_equity() == 9_500.0
    assert mr.modeled_equity() == 10_000.0


def test_flatten_tracked_only_closes_that_book(tmp_path):
    class _Mgr:
        def __init__(self, symbols):
            self.positions = {
                s: {"code": f"US.{s}261016C00010000", "remaining_qty": 1} for s in symbols
            }
            self.closed = []

        def exit_result(self, symbol, reason=""):
            self.closed.append(symbol)
            self.positions.pop(symbol, None)
            return {"success": True, "pnl": 1.0, "symbol": symbol, "reason": reason}

    orb_mgr = _Mgr(["SPY"])
    mr_mgr = _Mgr(["NOK"])
    reg = PaperBookRegistry()
    orb = reg.bind(BOOK_ORB_MOOMOO, order_mgr=orb_mgr, ledger_dir=tmp_path)
    mr = reg.bind(BOOK_MR_MOOMOO, order_mgr=mr_mgr, ledger_dir=tmp_path)
    orb.flatten_tracked(reason="EOD")
    assert orb_mgr.closed == ["SPY"]
    assert mr_mgr.closed == []
    assert "NOK" in mr_mgr.positions
    mr.flatten_tracked(reason="EOD")
    assert mr_mgr.closed == ["NOK"]


def test_tradier_order_manager_is_execution_port_and_mocked_http():
    occ = tradier_occ("NOK", "2026-10-16", "CALL", 10)
    session = FakeSession(
        [
            FakeResp(200, {"order": {"id": "1", "status": "ok"}}),
            FakeResp(200, {"order": {"id": "2", "status": "ok"}}),
        ]
    )
    mgr = TradierOrderManager(
        _client(session), source="mr_tradier", book_id=BOOK_MR_TRADIER
    )
    assert isinstance(mgr, ExecutionPort)
    mgr.enter_option_contract(
        "NOK",
        "CALL",
        strike=10,
        expiry="2026-10-16",
        premium=0.79,
        risk_pct=0.10,
        portfolio_val=10_000,
        source="mr_tradier",
    )
    assert mgr.has_position("NOK")
    assert mgr.positions["NOK"]["source"] == "mr_tradier"
    assert mgr.positions["NOK"]["code"] == occ
    assert session.calls[0]["data"]["side"] == "buy_to_open"
    out = mgr.exit_result("NOK", reason="EOD")
    assert out["success"] is True
    assert session.calls[1]["data"]["side"] == "sell_to_close"
    assert mgr.open_count() == 0


def test_tradier_order_manager_refuses_live_without_allow(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    session = FakeSession()
    with pytest.raises(LiveFundsRefused):
        TradierOrderManager(
            TradierPaperClient(
                access_token="test-token",
                account_id="VA000",
                env="live",
                session=session,
            )
        )


def test_try_bind_tradier_books_separate_oms(monkeypatch, tmp_path):
    monkeypatch.setenv("FABIO_TRADIER_PAPER_BOOKS", "1")
    session = FakeSession()
    client = _client(session)
    reg = PaperBookRegistry()
    bound = try_bind_tradier_books(
        reg, mr_paper=True, client=client, ledger_dir=tmp_path
    )
    assert bound == [BOOK_ORB_TRADIER, BOOK_MR_TRADIER]
    orb = reg.get(BOOK_ORB_TRADIER)
    mr = reg.get(BOOK_MR_TRADIER)
    assert orb is not None and mr is not None
    assert orb.order_mgr is not mr.order_mgr
    assert orb.cb is not mr.cb
    assert orb.order_mgr.positions is not mr.order_mgr.positions


def test_mr_tradier_book_skips_multi_leg():
    session = FakeSession()
    mgr = TradierOrderManager(_client(session), source="mr_tradier", book_id=BOOK_MR_TRADIER)
    cb = init_book_circuit()
    ex = MrPaperExecutor(
        mgr,
        cb,
        enabled=True,
        modeled_book=10_000,
        source="mr_tradier",
        book_id=BOOK_MR_TRADIER,
    )
    from signal_intake.replay import SHELF_DIR, load_fixture

    intent = parse_payload(load_fixture(SHELF_DIR / "buy_tlt_call_vertical.json"))
    out = ex.consider(intent, portfolio_val=10_000)
    assert out["placed"] is False
    assert out["skip"] == SKIP_MULTI_LEG
    assert session.calls == []


def test_ledger_roundtrip(tmp_path):
    ledger = PaperBookLedger(BOOK_ORB_TRADIER, directory=tmp_path)
    ledger.add("SPY261016C00500000")
    assert ledger.save() is True
    loaded = PaperBookLedger(BOOK_ORB_TRADIER, directory=tmp_path)
    assert loaded.load() is True
    assert loaded.codes == {"SPY261016C00500000"}


def test_moomoo_failsafe_default_book_is_orb_moomoo():
    from moomoo_eod_failsafe import build_arg_parser

    args = build_arg_parser().parse_args([])
    assert args.book == BOOK_ORB_MOOMOO
    help_text = build_arg_parser().format_help()
    assert "orb-moomoo" in help_text
    assert "mr-moomoo" in help_text
    assert "tradier" not in help_text.lower() or "never calls Tradier" in help_text


def test_tradier_flatten_default_book_is_orb_tradier():
    from tradier_eod_flatten import build_arg_parser

    args = build_arg_parser().parse_args([])
    assert args.book == BOOK_ORB_TRADIER
    help_text = build_arg_parser().format_help()
    assert "mr-tradier" in help_text
    assert "Moomoo" in help_text


def test_tradier_flatten_filters_other_book_codes():
    session = FakeSession(
        [
            FakeResp(
                200,
                {
                    "positions": {
                        "position": [
                            {"symbol": "SPY261016C00500000", "quantity": "1"},
                            {"symbol": "IBM261016C00250000", "quantity": "2"},
                        ]
                    }
                },
            ),
            FakeResp(200, {"order": {"id": "1", "status": "ok"}}),
        ]
    )
    client = _client(session)
    summary = client.flatten_open_positions(
        scope="options",
        only_symbols={"SPY261016C00500000"},
        exclude_symbols={"IBM261016C00250000"},
    )
    assert summary["planned"] == 1
    assert summary["results"][0]["symbol"] == "SPY261016C00500000"
    assert session.calls[1]["data"]["option_symbol"] == "SPY261016C00500000"


def test_tradier_orders_module_does_not_import_moomoo():
    path = BACKEND / "brokers" / "tradier" / "orders.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"moomoo", "futu"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden
    src = path.read_text(encoding="utf-8")
    assert "OpenSecTradeContext" not in src
    assert "exercise" not in src.lower() or "Never exercises" in src or "never exercise" in src.lower()


def test_orb_bot_still_defaults_moomoo_contexts():
    src = (BACKEND / "fabio_live" / "bot.py").read_text(encoding="utf-8")
    assert "self.quote_ctx = OpenQuoteContext(host=MOOMOO_HOST, port=MOOMOO_PORT)" in src
    assert "self.order_mgr = OrderManager(self.trade_ctx, self.quote_ctx, trd_env)" in src
    assert "TradierPaperClient" not in src
    assert "FABIO_BROKER" not in src
