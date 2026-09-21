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
    load_all_ledgers,
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
    polluted = RiskCircuitBreaker()
    polluted.set_portfolio_open(1_000_000.0)
    init_book_circuit(polluted)
    assert polluted.portfolio_at_open == PAPER_BOOK_STARTING_BALANCE == 10_000.0


def test_registry_apply_starting_balances_includes_orb_moomoo(tmp_path):
    """Captain B: ORB-Moomoo CB denom is $10k, same as the other three books."""
    reg = PaperBookRegistry()
    bound = []
    for spec in ALL_PAPER_BOOKS:
        cb = RiskCircuitBreaker()
        cb.set_portfolio_open(1_000_000.0)
        rt = reg.bind(
            spec.book_id,
            order_mgr=SimpleNamespace(positions={}),
            cb=cb,
            ledger_dir=tmp_path,
        )
        bound.append(rt)
    assert {rt.spec.book_id for rt in bound} == {b.book_id for b in ALL_PAPER_BOOKS}
    for rt in bound:
        assert rt.cb.portfolio_at_open == PAPER_BOOK_STARTING_BALANCE
        rt.cb.set_portfolio_open(50_000.0)
    reg.apply_starting_balances()
    for rt in reg.all_runtimes():
        assert rt.spec.starting_balance == PAPER_BOOK_STARTING_BALANCE == 10_000.0
        assert rt.cb.portfolio_at_open == 10_000.0
        assert rt.modeled_equity() == 10_000.0


def _stub_initialize_day_bot(tmp_path, *, prefetched: bool, opend_equity: float):
    from fabio_live.bot import ORBBot

    orb_cb = RiskCircuitBreaker()
    orb_cb.set_portfolio_open(opend_equity)
    reg = PaperBookRegistry()
    for spec in ALL_PAPER_BOOKS:
        cb = orb_cb if spec.book_id == BOOK_ORB_MOOMOO else RiskCircuitBreaker()
        if spec.book_id != BOOK_ORB_MOOMOO:
            cb.set_portfolio_open(opend_equity)
        reg.bind(
            spec.book_id,
            order_mgr=SimpleNamespace(positions={}),
            cb=cb,
            ledger_dir=tmp_path,
        )
    bot = ORBBot.__new__(ORBBot)
    bot._prefetched = prefetched
    bot._prefetch_vix = 18.0
    bot._prefetch_portfolio = opend_equity
    bot._prefetch_daily = {}
    bot.cb = orb_cb
    bot._paper_books = reg
    bot._capital_at_open = 0.0
    bot.quote_ctx = object()
    bot.trade_ctx = object()
    bot.regimes = {}
    bot.sheets = SimpleNamespace(is_connected=lambda: False)
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot._enqueue_intraday_dashboard_refresh = lambda: None
    bot._now_market = lambda: __import__("datetime").datetime(
        2026, 9, 21, 9, 31, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")
    )
    return bot, reg


def test_initialize_day_all_four_cbs_use_10k_not_opend(monkeypatch, tmp_path):
    monkeypatch.setattr("fabio_live.bot.SYMBOLS", [])
    monkeypatch.setattr("fabio_live.bot.get_vix", lambda *_a, **_k: 18.0)
    monkeypatch.setattr(
        "fabio_live.bot.get_portfolio_value", lambda *_a, **_k: 1_000_000.0
    )
    bot, reg = _stub_initialize_day_bot(tmp_path, prefetched=False, opend_equity=1_000_000.0)
    bot.initialize_day()
    assert bot.cb.portfolio_at_open == PAPER_BOOK_STARTING_BALANCE == 10_000.0
    assert bot._capital_at_open == 10_000.0
    ids = {rt.spec.book_id for rt in reg.all_runtimes()}
    assert ids == {
        BOOK_ORB_MOOMOO,
        BOOK_ORB_TRADIER,
        BOOK_MR_MOOMOO,
        BOOK_MR_TRADIER,
    }
    for rt in reg.all_runtimes():
        assert rt.cb.portfolio_at_open == 10_000.0, rt.spec.book_id
        assert rt.modeled_equity() == 10_000.0, rt.spec.book_id
    orb = reg.get(BOOK_ORB_MOOMOO)
    assert orb is not None and orb.cb is bot.cb
    assert orb.cb.daily_loss_pct == 0.0
    orb.cb.record_result(-500.0)
    assert orb.cb.daily_loss_pct == pytest.approx(-500.0 / 10_000.0)


def test_initialize_day_prefetch_opend_equity_does_not_become_cb_denom(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("fabio_live.bot.SYMBOLS", [])
    bot, reg = _stub_initialize_day_bot(
        tmp_path, prefetched=True, opend_equity=50_000.0
    )
    bot.initialize_day()
    assert bot.cb.portfolio_at_open == 10_000.0
    assert bot._capital_at_open == 10_000.0
    assert all(rt.cb.portfolio_at_open == 10_000.0 for rt in reg.all_runtimes())


def test_initialize_day_source_does_not_skip_orb_moomoo_cb():
    src = (BACKEND / "fabio_live" / "bot.py").read_text(encoding="utf-8")
    assert "self.cb.set_portfolio_open(PAPER_BOOK_STARTING_BALANCE)" in src
    assert "self._capital_at_open = PAPER_BOOK_STARTING_BALANCE" in src
    assert "books.apply_starting_balances()" in src
    assert "if rt.spec.book_id == BOOK_ORB_MOOMOO:" not in src
    assert "self.cb.set_portfolio_open(portfolio_val)" not in src


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
    assert orb.ledger.codes == set()
    orb_disk = PaperBookLedger(BOOK_ORB_MOOMOO, directory=tmp_path)
    assert orb_disk.load() is True
    assert orb_disk.codes == set()
    mr.flatten_tracked(reason="EOD")
    assert mr_mgr.closed == ["NOK"]
    assert mr.ledger.codes == set()


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


def test_mr_fills_update_moomoo_and_tradier_ledgers(tmp_path, monkeypatch):
    from fabio_live.bot import ORBBot
    from signal_intake.replay import SHELF_DIR, load_fixture
    from tests.test_mr_paper import _executor

    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, _trade, cb = _executor(enabled=True, ask=0.79)
    session = FakeSession([FakeResp(200, {"order": {"id": "1", "status": "ok"}})])
    t_mgr = TradierOrderManager(
        _client(session), source="mr_tradier", book_id=BOOK_MR_TRADIER
    )
    t_cb = init_book_circuit()
    t_ex = MrPaperExecutor(
        t_mgr,
        t_cb,
        enabled=True,
        modeled_book=10_000,
        source="mr_tradier",
        book_id=BOOK_MR_TRADIER,
    )
    reg = PaperBookRegistry()
    reg.bind(
        BOOK_ORB_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        ledger_dir=tmp_path,
    )
    reg.bind(BOOK_MR_MOOMOO, order_mgr=mgr, cb=cb, ledger_dir=tmp_path)
    reg.bind(BOOK_MR_TRADIER, order_mgr=t_mgr, cb=t_cb, ledger_dir=tmp_path)

    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._paper_books = reg
    bot._mr_executor = ex
    bot._mr_executors = [ex, t_ex]
    bot._mr_queue = [load_fixture(SHELF_DIR / "buy_nok_calls.json")]
    bot._mr_store = None
    bot._mr_cursor = None
    bot._drain_mr_paper(allow_entries=True)

    assert mgr.has_position("NOK")
    assert t_mgr.has_position("NOK")
    moomoo_ledger = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    tradier_ledger = PaperBookLedger(BOOK_MR_TRADIER, directory=tmp_path)
    assert moomoo_ledger.load() is True
    assert tradier_ledger.load() is True
    assert any("NOK" in c for c in moomoo_ledger.codes)
    assert any("NOK" in c for c in tradier_ledger.codes)

    bot._sync_mr_paper_book_ledgers()
    again = PaperBookLedger(BOOK_MR_TRADIER, directory=tmp_path)
    assert again.load() is True
    assert any("NOK" in c for c in again.codes)


def test_eod_persists_mr_tradier_ledger(tmp_path):
    from fabio_live.bot import ORBBot

    t_mgr = SimpleNamespace(
        positions={"NOK": {"code": "NOK261016C00010000", "source": "mr_tradier"}}
    )
    m_mgr = SimpleNamespace(
        positions={"IBM": {"code": "US.IBM261016C00250000", "source": "mr"}}
    )
    reg = PaperBookRegistry()
    reg.bind(BOOK_MR_MOOMOO, order_mgr=m_mgr, ledger_dir=tmp_path)
    reg.bind(BOOK_MR_TRADIER, order_mgr=t_mgr, ledger_dir=tmp_path)
    bot = ORBBot.__new__(ORBBot)
    bot._paper_books = reg
    bot._sync_mr_paper_book_ledgers()
    moomoo = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    tradier = PaperBookLedger(BOOK_MR_TRADIER, directory=tmp_path)
    assert moomoo.load() is True and tradier.load() is True
    assert "US.IBM261016C00250000" in moomoo.codes
    assert "NOK261016C00010000" in tradier.codes


def test_empty_mr_ledger_does_not_allow_sibling_flatten():
    """Empty own ledger fail-closes; never market-close the other strategy."""
    spy = "US.SPY261016C00500000"
    nok = "US.NOK261016C00010000"
    ledgers = {
        BOOK_ORB_MOOMOO: set(),
        BOOK_MR_MOOMOO: set(),
        BOOK_ORB_TRADIER: set(),
        BOOK_MR_TRADIER: set(),
    }
    account = [spy, nok]
    assert (
        select_flatten_codes(
            book_id=BOOK_ORB_MOOMOO,
            broker="moomoo",
            account_codes=account,
            ledgers=ledgers,
        )
        == []
    )
    assert (
        select_flatten_codes(
            book_id=BOOK_MR_MOOMOO,
            broker="moomoo",
            account_codes=account,
            ledgers=ledgers,
        )
        == []
    )
    only, exclude = flatten_symbol_filters(BOOK_ORB_MOOMOO, "moomoo", ledgers)
    assert only == set()
    assert only is not None
    only_mr, _ = flatten_symbol_filters(BOOK_MR_MOOMOO, "moomoo", ledgers)
    assert only_mr == set()

    ledgers_sib = {BOOK_ORB_MOOMOO: set(), BOOK_MR_MOOMOO: {nok}}
    orb = select_flatten_codes(
        book_id=BOOK_ORB_MOOMOO,
        broker="moomoo",
        account_codes=account,
        ledgers=ledgers_sib,
    )
    assert nok not in orb
    assert orb == []
    only_orb, exclude_orb = flatten_symbol_filters(
        BOOK_ORB_MOOMOO, "moomoo", ledgers_sib
    )
    assert only_orb == set()
    assert nok in exclude_orb

    t_spy, t_ibm = "SPY261016C00500000", "IBM261016C00250000"
    t_ledgers = {BOOK_ORB_TRADIER: set(), BOOK_MR_TRADIER: set()}
    assert (
        select_flatten_codes(
            book_id=BOOK_ORB_TRADIER,
            broker="tradier",
            account_codes=[t_spy, t_ibm],
            ledgers=t_ledgers,
        )
        == []
    )
    only_t, _ = flatten_symbol_filters(BOOK_ORB_TRADIER, "tradier", t_ledgers)
    assert only_t == set()


def test_tradier_typeerror_cannot_flatten_without_book_filters():
    from tradier_eod_flatten import main

    class ClientRejectsFilters:
        def __init__(self):
            self.unfiltered_calls = 0

        def flatten_open_positions(
            self,
            scope="options",
            dry_run=False,
            sleep_fn=None,
            sleep_between_orders=0.0,
        ):
            self.unfiltered_calls += 1
            return {
                "planned": 2,
                "failures": 0,
                "dry_run": True,
                "results": [
                    {"symbol": "SPY261016C00500000", "status": "ok"},
                    {"symbol": "IBM261016C00250000", "status": "ok"},
                ],
            }

    client = ClientRejectsFilters()
    code = main(["--dry-run", "--sleep-between-orders", "0"], client=client)
    assert code == 1
    assert client.unfiltered_calls == 0
    src = (BACKEND / "tradier_eod_flatten.py").read_text(encoding="utf-8")
    assert "flatten_open_positions(**flatten_kwargs)" not in src
    assert "book_filters_required" in src


def test_gitignore_keeps_health_snapshots_and_paper_book_ledgers():
    text = (BACKEND.parent / ".gitignore").read_text(encoding="utf-8")
    assert "bot_health_snapshots.jsonl" in text
    assert "backend/paper_book_ledgers/" in text


def _persist_moomoo_ledgers(tmp_path, *, spy: str, nok: str) -> None:
    orb = PaperBookLedger(BOOK_ORB_MOOMOO, directory=tmp_path)
    orb.add(spy)
    assert orb.save() is True
    mr = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    mr.add(nok)
    assert mr.save() is True


def test_restart_empty_om_does_not_erase_persisted_ledgers(tmp_path):
    spy = "US.SPY261016C00500000"
    nok = "US.NOK261016C00010000"
    _persist_moomoo_ledgers(tmp_path, spy=spy, nok=nok)

    reg = PaperBookRegistry()
    orb = reg.bind(
        BOOK_ORB_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        ledger_dir=tmp_path,
    )
    mr = reg.bind(
        BOOK_MR_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        ledger_dir=tmp_path,
    )
    assert spy in orb.ledger.codes
    assert nok in mr.ledger.codes
    assert orb.cb.portfolio_at_open == PAPER_BOOK_STARTING_BALANCE == 10_000.0
    assert mr.cb.portfolio_at_open == 10_000.0

    reg.sync_and_save()
    reg.sync_strategy_ledgers("mr")
    tables = load_all_ledgers(tmp_path)
    assert spy in tables[BOOK_ORB_MOOMOO]
    assert nok in tables[BOOK_MR_MOOMOO]

    from fabio_live.bot import ORBBot

    bot = ORBBot.__new__(ORBBot)
    bot._paper_books = reg
    bot._sync_mr_paper_book_ledgers()
    again = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    assert again.load() is True
    assert nok in again.codes
    orb_again = PaperBookLedger(BOOK_ORB_MOOMOO, directory=tmp_path)
    assert orb_again.load() is True
    assert spy in orb_again.codes


def test_save_after_empty_om_does_not_wipe_disk(tmp_path):
    spy = "US.SPY261016C00500000"
    nok = "US.NOK261016C00010000"
    _persist_moomoo_ledgers(tmp_path, spy=spy, nok=nok)
    ledger = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    assert ledger.load() is True
    ledger.replace_codes([])
    assert ledger.save() is False
    assert nok in ledger.codes
    disk = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    assert disk.load() is True
    assert nok in disk.codes

    empty = PaperBookLedger(BOOK_ORB_MOOMOO, directory=tmp_path)
    empty.replace_codes([])
    assert empty.save() is False
    orb_disk = PaperBookLedger(BOOK_ORB_MOOMOO, directory=tmp_path)
    assert orb_disk.load() is True
    assert spy in orb_disk.codes


def test_orphan_sweep_cannot_close_sibling_moomoo_after_restart(tmp_path):
    import pandas as pd
    from fabio_live.bot import ORBBot

    spy = "US.SPY261016C00500000"
    nok = "US.NOK261016C00010000"
    _persist_moomoo_ledgers(tmp_path, spy=spy, nok=nok)

    sold: list[str] = []
    orb_om = SimpleNamespace(
        trd_env="SIMULATE",
        positions={},
        _sell=lambda code, qty, label="": sold.append(code),
    )
    mr_om = SimpleNamespace(positions={})
    reg = PaperBookRegistry()
    reg.bind(BOOK_ORB_MOOMOO, order_mgr=orb_om, ledger_dir=tmp_path)
    reg.bind(BOOK_MR_MOOMOO, order_mgr=mr_om, ledger_dir=tmp_path)

    protected = reg.protected_codes_for_broker(
        "moomoo", except_book=BOOK_ORB_MOOMOO
    )
    assert nok in protected
    assert spy not in protected

    bot = ORBBot.__new__(ORBBot)
    bot.signals = {}
    bot._mr_executors = []
    bot._mr_executor = None
    bot._paper_books = reg
    bot.order_mgr = orb_om
    bot.ops = SimpleNamespace(alert=lambda *_a, **_k: None, log_alert=lambda *_a, **_k: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **_k: (
            0,
            pd.DataFrame(
                [
                    {"code": spy, "qty": 1, "unrealized_pl": 0.0},
                    {"code": nok, "qty": 1, "unrealized_pl": 0.0},
                ]
            ),
        )
    )
    bot.eod_close_all()
    assert nok not in sold
    disk = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    assert disk.load() is True
    assert nok in disk.codes
    failsafe = load_all_ledgers(tmp_path)
    assert nok in failsafe[BOOK_MR_MOOMOO]
    assert spy in failsafe[BOOK_ORB_MOOMOO]
    orb_only, exclude = flatten_symbol_filters(
        BOOK_ORB_MOOMOO, "moomoo", failsafe
    )
    assert nok in exclude
    assert nok not in orb_only


def test_sync_unions_om_fills_onto_persisted_codes(tmp_path):
    nok = "US.NOK261016C00010000"
    ibm = "US.IBM261016C00250000"
    existing = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    existing.add(nok)
    assert existing.save() is True
    om = SimpleNamespace(positions={"IBM": {"code": ibm}})
    rt = PaperBookRegistry().bind(
        BOOK_MR_MOOMOO, order_mgr=om, ledger_dir=tmp_path
    )
    assert nok in rt.ledger.codes
    assert ibm in rt.ledger.codes
    rt.ledger.save()
    disk = PaperBookLedger(BOOK_MR_MOOMOO, directory=tmp_path)
    assert disk.load() is True
    assert disk.codes == {nok, ibm}
