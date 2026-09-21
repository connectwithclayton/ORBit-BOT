"""Slice 4: MR shelf fixtures → CB + sizing → Moomoo paper (flag-gated)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from moomoo import OrderStatus, OrderType, TrdSide

from fabio_live.circuit import RiskCircuitBreaker
from fabio_live.constants import (
    CB_DAILY_LOSS_PCT,
    CB_MAX_OPEN_POS,
    CB_MAX_TRADES,
    STRATEGY_CAPITAL,
)
from fabio_live.mr_paper import (
    MR_ALLOW_ORB_SYMBOLS_ENV,
    MR_PAPER_ENABLED_ENV,
    SKIP_COMBINED_DAILY_LOSS,
    SKIP_DISABLED,
    SKIP_EQUITY_OPTIONS_ONLY,
    SKIP_MULTI_LEG,
    SKIP_NOT_MR,
    SKIP_NOT_OPEN,
    SKIP_ORB_SYMBOL,
    SKIP_REAL_REFUSED,
    SKIP_UNDERLYING_OCCUPIED,
    DurableMrCursor,
    MrPaperExecutor,
    combined_daily_loss_blocks,
    mr_paper_enabled,
    sizing_risk_pct,
)
from fabio_live.orders import OrderManager, build_option_code
from fabio_live.signals import SignalEngine
from signal_intake.gmail_stub import (
    LiveGmailPollerDisabled,
    live_gmail_poller_not_implemented,
)
from signal_intake.ids import IdempotencyStore
from signal_intake.models import ACTION_BUY, ACTION_EXIT, DECISION_SKIP, SOURCE_MR
from signal_intake.parse import parse_payload
from paper_pin import LiveFundsRefused
from signal_intake.replay import SHELF_DIR, load_fixture

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _shelf(name: str) -> dict:
    return load_fixture(SHELF_DIR / name)


def _parse_shelf(name: str, store: IdempotencyStore | None = None):
    return parse_payload(_shelf(name), store=store)


class _FakeQuote:
    def __init__(self, ask: float = 1.0):
        self.ask = ask

    def get_option_expiration_date(self, code):
        return 0, pd.DataFrame({"date": ["2026-10-16"]})

    def get_option_chain(self, code, index_option_type, start, end, option_type):
        return 0, pd.DataFrame(
            {"strike_price": [500.0], "code": ["US.SPY261016C00500000"]}
        )

    def get_market_snapshot(self, codes):
        return 0, pd.DataFrame(
            {"ask_price": [self.ask], "bid_price": [self.ask], "last_price": [self.ask]}
        )


class _FakeTrade:
    def __init__(self):
        self.orders = []
        self._n = 0

    def place_order(self, price, qty, code, trd_side, order_type, trd_env, time_in_force):
        self._n += 1
        oid = f"M{self._n}"
        self.orders.append(
            {
                "order_id": oid,
                "price": price,
                "qty": qty,
                "code": code,
                "trd_side": trd_side,
                "order_type": order_type,
                "trd_env": trd_env,
            }
        )
        return 0, pd.DataFrame({"order_id": [oid]})

    def order_list_query(self, order_id=None, trd_env=None):
        if order_id:
            return 0, pd.DataFrame(
                {"order_status": [OrderStatus.FILLED_ALL], "dealt_qty": [1]}
            )
        return 0, pd.DataFrame()

    def modify_order(self, modify_order_op, order_id, qty, price, **kwargs):
        return 0, None


def _executor(*, enabled=True, options_only=True, ask=0.79, orb_cb=None, cb=None):
    trade = _FakeTrade()
    quote = _FakeQuote(ask=ask)
    mgr = OrderManager(trade, quote, trd_env="SIMULATE")
    circuit = cb or RiskCircuitBreaker()
    circuit.set_portfolio_open(STRATEGY_CAPITAL)
    ex = MrPaperExecutor(
        mgr,
        circuit,
        orb_cb=orb_cb,
        paper_only=True,
        options_only=options_only,
        modeled_book=STRATEGY_CAPITAL,
        enabled=enabled,
    )
    return ex, mgr, trade, circuit


def test_shelf_nok_ibm_run_entries_accepted():
    store = IdempotencyStore()
    nok = _parse_shelf("buy_nok_calls.json", store)
    ibm = _parse_shelf("buy_ibm_calls.json", store)
    run = _parse_shelf("buy_run_calls.json", store)
    assert nok.accepted and ibm.accepted and run.accepted
    assert nok.action == ACTION_BUY and nok.direction == "CALL"
    assert nok.symbol == "NOK" and nok.strike == 10 and nok.expiry == "2026-10-16"
    assert nok.premium == pytest.approx(0.79)
    assert ibm.symbol == "IBM" and ibm.strike == 250 and ibm.expiry == "2026-10-16"
    assert ibm.premium == pytest.approx(6.38)
    assert run.symbol == "RUN" and run.strike == 9 and run.expiry == "2026-10-16"
    assert run.premium == pytest.approx(0.64)
    assert nok.contract_key == "NOK|2026-10-16|10|CALL"
    assert ibm.contract_key == "IBM|2026-10-16|250|CALL"
    assert run.source == SOURCE_MR


def test_shelf_tlt_call_vertical_is_multi_leg_skip():
    intent = _parse_shelf("buy_tlt_call_vertical.json")
    assert intent.accepted is False
    assert intent.skip == "multi-leg"
    assert intent.decision == DECISION_SKIP
    assert intent.direction == ""


def test_shelf_ibm_lock_in_is_exit_same_contract():
    store = IdempotencyStore()
    buy = _parse_shelf("buy_ibm_calls.json", store)
    lock = _parse_shelf("lock_ibm_call.json", store)
    assert buy.accepted is True
    assert lock.accepted is True
    assert lock.action == ACTION_EXIT
    assert lock.symbol == "IBM"
    assert lock.direction == "CALL"
    assert lock.expiry == "2026-10-16"
    assert lock.strike == 250
    assert lock.contract_key == buy.contract_key
    assert lock.skip is None


def test_buy_vs_trade_log_dedupes_same_symbol_expiry_strike():
    store = IdempotencyStore()
    buy = parse_payload(_shelf("buy_nok_calls.json"), store=store)
    assert buy.accepted is True
    log = parse_payload(
        {
            "id": "trade_log_nok_calls",
            "channel": "email",
            "kind": "entry_single_call",
            "subject": "RRP - RRP Trade Log - NOK",
            "date": "2026-09-16T15:10:00Z",
            "body": (
                "RRP Trade Log - NOK\n\n"
                "Buy the NOK October 16 $10 Call for $0.79.\n"
            ),
        },
        store=store,
    )
    assert log.accepted is False
    assert log.skip == "duplicate"
    assert log.contract_key == buy.contract_key


def test_flag_off_does_not_place_orders(monkeypatch):
    monkeypatch.delenv(MR_PAPER_ENABLED_ENV, raising=False)
    assert mr_paper_enabled() is False
    ex, mgr, trade, _cb = _executor(enabled=False)
    out = ex.consider(_parse_shelf("buy_nok_calls.json"), portfolio_val=10_000)
    assert out["skip"] == SKIP_DISABLED
    assert out["placed"] is False
    assert trade.orders == []
    assert mgr.open_count() == 0


def test_shelf_entries_execute_on_moomoo_paper_when_flag_on(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    store = IdempotencyStore()
    ex, mgr, trade, _cb = _executor(enabled=True, ask=0.79)
    for name in ("buy_nok_calls.json", "buy_ibm_calls.json", "buy_run_calls.json"):
        out = ex.consider(_parse_shelf(name, store), portfolio_val=10_000)
        assert out["status"] == "entered", name
        assert out["placed"] is True
        assert out["source"] == SOURCE_MR
    assert mgr.open_count() == 3
    assert mgr.has_position("NOK") and mgr.has_position("IBM") and mgr.has_position("RUN")
    codes = {o["code"] for o in trade.orders}
    assert "US.NOK261016C00010000" in codes
    assert "US.IBM261016C00250000" in codes
    assert "US.RUN261016C00009000" in codes
    assert all(o["trd_side"] == TrdSide.BUY for o in trade.orders)
    assert all(o["trd_env"] == "SIMULATE" for o in trade.orders)


def test_tlt_vertical_does_not_place(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, _cb = _executor(enabled=True)
    out = ex.consider(_parse_shelf("buy_tlt_call_vertical.json"), portfolio_val=10_000)
    assert out["placed"] is False
    assert out["skip"] == SKIP_MULTI_LEG
    assert trade.orders == []
    assert mgr.open_count() == 0


def test_ibm_lock_in_market_closes_when_modeled(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    store = IdempotencyStore()
    ex, mgr, trade, cb = _executor(enabled=True, ask=6.38)
    entered = ex.consider(_parse_shelf("buy_ibm_calls.json", store), portfolio_val=10_000)
    assert entered["placed"] is True
    buys = len(trade.orders)
    out = ex.consider(_parse_shelf("lock_ibm_call.json", store), portfolio_val=10_000)
    assert out["status"] == "exited"
    assert out["action"] == ACTION_EXIT
    assert out["reason"] == "MR_LOCK_IN"
    assert mgr.has_position("IBM") is False
    sells = [o for o in trade.orders[buys:] if o["trd_side"] == TrdSide.SELL]
    assert len(sells) == 1
    assert sells[0]["order_type"] == OrderType.MARKET
    assert cb.trade_count == 1


def test_lock_in_without_open_position_skips(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    ex, _mgr, trade, _cb = _executor(enabled=True)
    out = ex.consider(_parse_shelf("lock_ibm_call.json"), portfolio_val=10_000)
    assert out["skip"] == SKIP_NOT_OPEN
    assert trade.orders == []


def test_options_only_blocks_equity_share_buys(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    from signal_intake.replay import FIXTURE_DIR

    equity = parse_payload(load_fixture(FIXTURE_DIR / "03_site_aapl_equity.json"))
    assert equity.accepted is True
    assert equity.direction == "EQUITY"
    ex, mgr, trade, _cb = _executor(enabled=True, options_only=True)
    out = ex.consider(equity, portfolio_val=10_000)
    assert out["skip"] == SKIP_EQUITY_OPTIONS_ONLY
    assert trade.orders == []
    assert mgr.open_count() == 0


def test_cb_same_limits_max_open_and_daily_loss(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    store = IdempotencyStore()
    ex, mgr, _trade, cb = _executor(enabled=True, ask=0.79)
    for name in ("buy_nok_calls.json", "buy_ibm_calls.json", "buy_run_calls.json"):
        assert ex.consider(_parse_shelf(name, store), portfolio_val=10_000)["placed"]
    assert mgr.open_count() == CB_MAX_OPEN_POS
    extra = parse_payload(
        {
            "id": "buy_extra_amd",
            "channel": "email",
            "subject": "RRP - Buy AMD Calls",
            "date": "2026-09-16T16:00:00Z",
            "body": "Buy the October 16th AMD $150 Call for $1.00.\n",
        },
        store=store,
    )
    blocked = ex.consider(extra, portfolio_val=10_000)
    assert blocked["placed"] is False
    assert blocked["skip"] == "circuit_breaker"
    assert "Max open" in blocked["detail"]

    cb2 = RiskCircuitBreaker()
    cb2.set_portfolio_open(STRATEGY_CAPITAL)
    cb2.record_result(-STRATEGY_CAPITAL * CB_DAILY_LOSS_PCT)
    ex2, _mgr2, trade2, _ = _executor(enabled=True, cb=cb2)
    out = ex2.consider(_parse_shelf("buy_nok_calls.json"), portfolio_val=10_000)
    assert out["placed"] is False
    assert "Daily loss" in out["detail"]
    assert trade2.orders == []


def test_cb_max_completed_trades_and_half_size_after_three_losses(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    cb = RiskCircuitBreaker()
    cb.set_portfolio_open(STRATEGY_CAPITAL)
    for _ in range(CB_MAX_TRADES):
        cb.record_result(-1.0)
    assert cb.size_modifier() == 0.5
    assert cb.can_enter(0)[0] is False
    assert "Daily trade cap" in cb.can_enter(0)[1]
    fresh = RiskCircuitBreaker()
    fresh.loss_streak = 3
    assert sizing_risk_pct(fresh) == pytest.approx(0.05)


def test_partitioned_cbs_combined_daily_loss_on_modeled_10k(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    orb = RiskCircuitBreaker()
    orb.set_portfolio_open(STRATEGY_CAPITAL)
    orb.record_result(-150.0)  # 1.5% of 10k
    mr = RiskCircuitBreaker()
    mr.set_portfolio_open(STRATEGY_CAPITAL)
    mr.record_result(-60.0)  # would be 0.6% more → combined 2.1%
    blocked, reason = combined_daily_loss_blocks(
        [orb, mr], modeled_book=STRATEGY_CAPITAL
    )
    assert blocked is True
    assert "Combined daily loss" in reason
    ex, _mgr, trade, _ = _executor(enabled=True, cb=mr, orb_cb=orb, ask=0.79)
    out = ex.consider(_parse_shelf("buy_nok_calls.json"), portfolio_val=10_000)
    assert out["placed"] is False
    assert out["skip"] == SKIP_COMBINED_DAILY_LOSS
    assert trade.orders == []


def test_eod_leftover_mr_flatten_is_market_sell_not_exercise(monkeypatch):
    from fabio_live.bot import ORBBot

    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, cb = _executor(enabled=True, ask=0.79)
    ex.consider(_parse_shelf("buy_nok_calls.json"), portfolio_val=10_000)
    assert mgr.has_position("NOK")
    bot = ORBBot.__new__(ORBBot)
    bot.signals = {}
    bot.order_mgr = mgr
    bot.cb = cb
    bot._mr_executor = ex
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (0, pd.DataFrame())
    )
    bot.eod_close_all()
    assert mgr.has_position("NOK") is False
    sells = [o for o in trade.orders if o["trd_side"] == TrdSide.SELL]
    assert sells
    assert all(o["order_type"] == OrderType.MARKET for o in sells)
    src = (BACKEND_ROOT / "fabio_live" / "mr_paper.py").read_text()
    assert "exercise(" not in src.lower()
    orders_src = (BACKEND_ROOT / "fabio_live" / "orders.py").read_text()
    assert "exercise(" not in orders_src.lower()


def test_orb_breakout_path_unchanged_on_spy():
    from tests.conftest import daily_bullish_atr, intraday_5m_or_known, minimal_settings
    from backtest.fabio.regime import DayRegime, OpeningRangeStyle
    from fabio_live.regime import MarketRegime

    cfg = minimal_settings()
    day = "2025-06-02"
    daily = daily_bullish_atr(end_day=day)
    or_high, or_low = 402.0, 398.0
    df = intraday_5m_or_known(day=day, or_high=or_high, or_low=or_low).copy()
    m1000 = (df.index.hour == 10) & (df.index.minute == 0)
    m1005 = (df.index.hour == 10) & (df.index.minute == 5)
    df.loc[m1000, ["Open", "High", "Low", "Close"]] = [402.4, 403.4, 402.3, 403.0]
    df.loc[m1005, ["Open", "High", "Low", "Close"]] = [403.0, 403.3, 402.9, 403.1]
    live_5m = pd.DataFrame(
        {
            "time_key": df.index,
            "open": df["Open"].to_numpy(),
            "high": df["High"].to_numpy(),
            "low": df["Low"].to_numpy(),
            "close": df["Close"].to_numpy(),
            "volume": 1,
        }
    )
    # Live SignalEngine needs a MarketRegime; build via a thin stub matching OR.
    regime = SimpleNamespace(
        or_high=or_high,
        or_low=or_low,
        retest_required=False,
        atr=2.0,
        bullish_trend=True,
    )
    ts_cut = pd.Timestamp(f"{day} 10:05", tz="America/New_York")
    window = live_5m[live_5m["time_key"] <= ts_cut]
    engine = SignalEngine(regime)
    assert engine.check_breakout(window) == "CALL"
    sig_src = (BACKEND_ROOT / "fabio_live" / "signals.py").read_text()
    assert "signal_intake" not in sig_src
    assert "mr_paper" not in sig_src
    assert cfg.symbols == ["SPY", "QQQ", "NVDA"]
    _ = (DayRegime, OpeningRangeStyle, MarketRegime, daily)


def test_default_bot_signal_loop_skips_mr_when_flag_off():
    from fabio_live.bot import ORBBot

    bot = ORBBot.__new__(ORBBot)
    bot._mr_executor = None
    bot._mr_queue = [{"id": "should-not-run"}]
    bot._drain_mr_paper(allow_entries=True)


def test_four_paper_books_isolated_not_shared_om():
    """Slice 5: four books exist; MR module still does not import Tradier HTTP."""
    src = (BACKEND_ROOT / "fabio_live" / "mr_paper.py").read_text()
    bot_src = (BACKEND_ROOT / "fabio_live" / "bot.py").read_text()
    assert "import tradier" not in src
    assert "from tradier" not in src
    assert "brokers.tradier" not in src
    assert "TradierPaperClient" not in src
    assert "TradierPaperClient" not in bot_src
    assert "isolated_place_order_client" not in bot_src
    assert "FABIO_BROKER" not in src
    assert "FABIO_BROKER" not in bot_src
    from fabio_live.paper_books import ALL_PAPER_BOOKS, PAPER_BOOK_STARTING_BALANCE

    ids = {b.book_id for b in ALL_PAPER_BOOKS}
    assert ids == {"orb-moomoo", "orb-tradier", "mr-moomoo", "mr-tradier"}
    assert {b.starting_balance for b in ALL_PAPER_BOOKS} == {PAPER_BOOK_STARTING_BALANCE}
    from fabio_live.paper_books import init_book_circuit

    for spec in ALL_PAPER_BOOKS:
        cb = init_book_circuit()
        assert cb.portfolio_at_open == PAPER_BOOK_STARTING_BALANCE == 10_000.0


def test_real_trd_env_refused(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    trade = _FakeTrade()
    mgr = OrderManager(trade, _FakeQuote(), trd_env="REAL")
    with pytest.raises(LiveFundsRefused):
        MrPaperExecutor(
            mgr,
            RiskCircuitBreaker(),
            paper_only=True,
            enabled=True,
        )
    # Bypass ctor pin to assert consider() still refuses.
    monkeypatch.setattr(
        "fabio_live.mr_paper.enforce_paper_trading_pin", lambda *_a, **_k: None
    )
    ex = MrPaperExecutor(
        mgr,
        RiskCircuitBreaker(),
        paper_only=True,
        enabled=True,
    )
    out = ex.consider(_parse_shelf("buy_nok_calls.json"), portfolio_val=10_000)
    assert out["skip"] == SKIP_REAL_REFUSED
    assert out["placed"] is False
    assert trade.orders == []


def test_gmail_poller_is_stub():
    with pytest.raises(LiveGmailPollerDisabled):
        live_gmail_poller_not_implemented()


def test_shelf_fixtures_have_no_captain_pii():
    banned = (
        "@gmail.com",
        "@yahoo.com",
        "@outlook.com",
        "imap.",
        "BEGIN ",
        "password",
        "api_key",
        "TELEGRAM_BOT_TOKEN",
        "MOOMOO_",
        "TRADIER",
    )
    for path in sorted(SHELF_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        for needle in banned:
            assert needle.lower() not in lower, f"{path.name} contains {needle!r}"
        assert "example.invalid" in text or path.name == "index.json"


def test_build_option_code_matches_moomoo_occ():
    assert build_option_code("IBM", "2026-10-16", "CALL", 250) == "US.IBM261016C00250000"
    assert build_option_code("NOK", "2026-10-16", "CALL", 10) == "US.NOK261016C00010000"
    assert build_option_code("RUN", "2026-10-16", "PUT", 9) == "US.RUN261016P00009000"


def test_atm_enter_still_used_by_orb_not_mr_strike(monkeypatch):
    """ORB OrderManager.enter still resolves ATM via chain (SPY path)."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    mgr = OrderManager(_FakeTrade(), _FakeQuote(ask=1.0), trd_env="SIMULATE")
    mgr.enter("SPY", "CALL", 500.0, 0.01, 10_000.0)
    assert mgr.has_position("SPY")
    assert mgr.positions["SPY"]["code"] == "US.SPY261016C00500000"
    assert mgr.positions["SPY"].get("source") != "mr"


def _spy_lock_payload() -> dict:
    return {
        "id": "lock_spy_call",
        "channel": "email",
        "kind": "exit_take_profit",
        "subject": "RRP - Lock in SPY Call For Gain Of 10%",
        "date": "2026-09-16T16:00:00Z",
        "body": (
            "Lock in SPY Call For Gain Of 10%\n\n"
            "Last Wednesday we bought the October 16th SPY $500 Call for $1.00.\n"
        ),
    }


def _spy_buy_payload() -> dict:
    return {
        "id": "buy_spy_calls",
        "channel": "email",
        "subject": "RRP - Buy SPY Calls",
        "date": "2026-09-16T15:00:00Z",
        "body": "Buy the October 16th SPY $500 Call for $1.00.\n",
    }


def test_lock_in_does_not_flatten_unmarked_orb_spy_leg(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, _cb = _executor(enabled=True, ask=1.0)
    mgr.enter("SPY", "CALL", 500.0, 0.01, 10_000.0)
    orb_code = mgr.positions["SPY"]["code"]
    buys = len(trade.orders)
    lock = parse_payload(_spy_lock_payload())
    assert lock.accepted is True
    assert lock.action == ACTION_EXIT
    assert lock.symbol == "SPY"
    out = ex.consider(lock, portfolio_val=10_000)
    assert out["placed"] is False
    assert out["skip"] == SKIP_NOT_MR
    assert mgr.has_position("SPY") is True
    assert mgr.positions["SPY"]["code"] == orb_code
    assert mgr.positions["SPY"].get("source") != "mr"
    sells = [o for o in trade.orders[buys:] if o["trd_side"] == TrdSide.SELL]
    assert sells == []


def test_enter_option_contract_does_not_overwrite_orb_spy(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    mgr = OrderManager(_FakeTrade(), _FakeQuote(ask=1.0), trd_env="SIMULATE")
    mgr.enter("SPY", "CALL", 500.0, 0.01, 10_000.0)
    orb = dict(mgr.positions["SPY"])
    mgr.enter_option_contract(
        "SPY",
        "CALL",
        strike=500.0,
        expiry="2026-10-16",
        premium=1.0,
        risk_pct=0.01,
        portfolio_val=10_000.0,
        source="mr",
    )
    assert mgr.positions["SPY"] == orb
    assert mgr.positions["SPY"].get("source") != "mr"


def test_allow_orb_symbols_still_refuses_occupied_spy(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setenv(MR_ALLOW_ORB_SYMBOLS_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, _cb = _executor(enabled=True, ask=1.0)
    mgr.enter("SPY", "CALL", 500.0, 0.01, 10_000.0)
    before = dict(mgr.positions["SPY"])
    n_orders = len(trade.orders)
    out = ex.consider(parse_payload(_spy_buy_payload()), portfolio_val=10_000)
    assert out["placed"] is False
    assert out["skip"] == SKIP_UNDERLYING_OCCUPIED
    assert mgr.positions["SPY"] == before
    assert mgr.positions["SPY"].get("source") != "mr"
    assert len(trade.orders) == n_orders


def test_mr_entry_refuses_when_underlying_already_tracked(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, _cb = _executor(enabled=True, ask=0.79)
    mgr.positions["NOK"] = {
        "direction": "CALL",
        "code": "US.NOK261016C00010000",
        "original_qty": 2,
        "remaining_qty": 2,
        "entry_option_price": 0.50,
        "trim_level": 0,
        "realized_trim_pnl": 0.0,
    }
    before = dict(mgr.positions["NOK"])
    n_orders = len(trade.orders)
    out = ex.consider(_parse_shelf("buy_nok_calls.json"), portfolio_val=10_000)
    assert out["placed"] is False
    assert out["skip"] == SKIP_UNDERLYING_OCCUPIED
    assert mgr.positions["NOK"] == before
    assert mgr.positions["NOK"].get("source") != "mr"
    assert len(trade.orders) == n_orders


def test_mr_entry_denied_on_orb_symbols_by_default(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.delenv(MR_ALLOW_ORB_SYMBOLS_ENV, raising=False)
    ex, mgr, trade, _cb = _executor(enabled=True)
    for ticker in ("SPY", "QQQ", "NVDA"):
        intent = parse_payload(
            {
                "id": f"buy_{ticker.lower()}_calls",
                "channel": "email",
                "subject": f"RRP - Buy {ticker} Calls",
                "date": "2026-09-16T15:00:00Z",
                "body": f"Buy the October 16th {ticker} $100 Call for $1.00.\n",
            }
        )
        assert intent.accepted is True
        out = ex.consider(intent, portfolio_val=10_000)
        assert out["placed"] is False, ticker
        assert out["skip"] == SKIP_ORB_SYMBOL, ticker
        assert mgr.has_position(ticker) is False
    assert trade.orders == []


def test_mr_allow_orb_symbols_override_can_enter_spy_if_free(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setenv(MR_ALLOW_ORB_SYMBOLS_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, _cb = _executor(enabled=True, ask=1.0)
    out = ex.consider(parse_payload(_spy_buy_payload()), portfolio_val=10_000)
    assert out["placed"] is True
    assert mgr.has_position("SPY")
    assert mgr.positions["SPY"].get("source") == SOURCE_MR
    assert trade.orders


def test_lock_in_spy_allowed_only_when_source_is_mr(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setenv(MR_ALLOW_ORB_SYMBOLS_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, _cb = _executor(enabled=True, ask=1.0)
    assert ex.consider(parse_payload(_spy_buy_payload()), portfolio_val=10_000)["placed"]
    buys = len(trade.orders)
    out = ex.consider(parse_payload(_spy_lock_payload()), portfolio_val=10_000)
    assert out["status"] == "exited"
    assert mgr.has_position("SPY") is False
    sells = [o for o in trade.orders[buys:] if o["trd_side"] == TrdSide.SELL]
    assert len(sells) == 1
    assert sells[0]["order_type"] == OrderType.MARKET


def test_eod_leftover_skips_unmarked_orb_legs(monkeypatch):
    from fabio_live.bot import ORBBot

    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, cb = _executor(enabled=True, ask=0.79)
    mgr.enter("SPY", "CALL", 500.0, 0.01, 10_000.0)
    ex.consider(_parse_shelf("buy_nok_calls.json"), portfolio_val=10_000)
    assert mgr.has_position("SPY") and mgr.has_position("NOK")
    bot = ORBBot.__new__(ORBBot)
    bot.signals = {}
    bot.order_mgr = mgr
    bot.cb = cb
    bot._mr_executor = ex
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (0, pd.DataFrame())
    )
    bot.eod_close_all()
    assert mgr.has_position("SPY") is True
    assert mgr.positions["SPY"].get("source") != "mr"
    assert mgr.has_position("NOK") is False


def test_durable_cursor_prevents_re_drain_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    queue = tmp_path / "mr.jsonl"
    queue.write_text("", encoding="utf-8")
    store = IdempotencyStore()
    cursor = DurableMrCursor(str(queue), store=store)
    assert cursor.load_or_create() is True
    assert cursor.path.is_file()
    payload = _shelf("buy_nok_calls.json")
    queue.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    ex, mgr, trade, _cb = _executor(enabled=True, ask=0.79)
    rows = cursor.next_rows()
    assert rows and rows[0][0] is not None
    intent = parse_payload(rows[0][0], store=store)
    out = ex.consider(intent, portfolio_val=10_000)
    assert out["placed"] is True
    assert cursor.commit_offset(rows[0][1]) is True
    n_orders = len(trade.orders)

    store2 = IdempotencyStore()
    cursor2 = DurableMrCursor(str(queue), store=store2)
    assert cursor2.load_or_create() is True
    assert cursor2.offset == cursor.offset
    assert "mr:buy_nok_calls" in store2._seen
    rows2 = cursor2.next_rows()
    assert rows2 == []
    assert len(trade.orders) == n_orders
    assert mgr.open_count() == 1


def test_retained_jsonl_without_cursor_is_refused(tmp_path):
    queue = tmp_path / "retained.jsonl"
    queue.write_text(json.dumps(_shelf("buy_nok_calls.json")) + "\n", encoding="utf-8")
    cursor = DurableMrCursor(str(queue))
    assert cursor.load_or_create() is False
    assert cursor.refused is True
    assert cursor.next_rows() is None
    assert cursor.save() is False
    assert not cursor.path.is_file()


def _write_cursor(queue: Path, offset, *, seen=None, contracts=None) -> Path:
    path = Path(str(queue) + ".cursor.json")
    path.write_text(
        json.dumps(
            {
                "offset": offset,
                "seen": list(seen or []),
                "contracts": list(contracts or []),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_stale_high_cursor_offset_refuses_truncated_queue(tmp_path):
    queue = tmp_path / "mr.jsonl"
    bulky = (json.dumps(_shelf("buy_nok_calls.json")) + "\n") * 4
    queue.write_text(bulky, encoding="utf-8")
    high = queue.stat().st_size
    _write_cursor(queue, high, seen=["mr:buy_nok_calls"])
    queue.write_text(json.dumps(_shelf("buy_ibm_calls.json")) + "\n", encoding="utf-8")
    assert queue.stat().st_size < high

    store = IdempotencyStore()
    cursor = DurableMrCursor(str(queue), store=store)
    assert cursor.load_or_create() is False
    assert cursor.refused is True
    assert "beyond queue size" in cursor.refuse_reason
    assert cursor.next_rows() is None
    assert store._seen == set()


def test_next_rows_refuses_stale_high_offset_without_rewind(tmp_path):
    queue = tmp_path / "mr.jsonl"
    queue.write_text("", encoding="utf-8")
    cursor = DurableMrCursor(str(queue))
    assert cursor.load_or_create() is True
    line = json.dumps(_shelf("buy_nok_calls.json")) + "\n"
    queue.write_text(line * 2, encoding="utf-8")
    rows = cursor.next_rows()
    assert rows and len(rows) == 2
    assert cursor.commit_offset(rows[-1][1]) is True
    queue.write_text(json.dumps(_shelf("buy_ibm_calls.json")) + "\n", encoding="utf-8")
    assert cursor.offset > queue.stat().st_size
    assert cursor.next_rows() is None
    assert cursor.refused is True
    assert "beyond queue size" in cursor.refuse_reason


def test_corrupt_cursor_offset_is_refused(tmp_path):
    queue = tmp_path / "mr.jsonl"
    queue.write_text(json.dumps(_shelf("buy_nok_calls.json")) + "\n", encoding="utf-8")
    for bad in ("not-an-int", [1], {"n": 0}, None):
        _write_cursor(queue, bad)
        cursor = DurableMrCursor(str(queue))
        assert cursor.load_or_create() is False, bad
        assert cursor.refused is True, bad
        assert "corrupt" in cursor.refuse_reason, bad
        assert cursor.next_rows() is None
        assert cursor.save() is False


def test_negative_cursor_offset_is_refused(tmp_path):
    queue = tmp_path / "mr.jsonl"
    queue.write_text(json.dumps(_shelf("buy_nok_calls.json")) + "\n", encoding="utf-8")
    _write_cursor(queue, -5)
    cursor = DurableMrCursor(str(queue))
    assert cursor.load_or_create() is False
    assert cursor.refused is True
    assert "negative" in cursor.refuse_reason
    assert cursor.next_rows() is None


def test_bot_drain_refuses_truncated_queue_with_stale_cursor(tmp_path, monkeypatch):
    queue = tmp_path / "mr.jsonl"
    bulky = (json.dumps(_shelf("buy_nok_calls.json")) + "\n") * 4
    queue.write_text(bulky, encoding="utf-8")
    _write_cursor(queue, queue.stat().st_size, seen=["mr:buy_nok_calls"])
    queue.write_text(json.dumps(_shelf("buy_ibm_calls.json")) + "\n", encoding="utf-8")
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setenv("FABIO_MR_QUEUE_PATH", str(queue))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "fabio_live.bot.get_portfolio_value", lambda *_a, **_k: 10_000.0
    )
    ex, mgr, trade, cb = _executor(enabled=True, ask=6.38)
    bot = _drain_bot(mgr, cb, paused=False)
    bot._drain_mr_paper(allow_entries=True)
    assert bot._mr_cursor.refused is True
    assert "beyond queue size" in bot._mr_cursor.refuse_reason
    assert trade.orders == []
    assert mgr.open_count() == 0


def test_bot_restart_jsonl_drain_does_not_duplicate(tmp_path, monkeypatch):
    from fabio_live.bot import ORBBot

    queue = tmp_path / "mr.jsonl"
    queue.write_text("", encoding="utf-8")
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setenv("FABIO_MR_QUEUE_PATH", str(queue))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "fabio_live.bot.get_portfolio_value", lambda *_a, **_k: 10_000.0
    )

    def _bot(mgr, cb):
        bot = ORBBot.__new__(ORBBot)
        bot.order_mgr = mgr
        bot.cb = cb
        bot.ops = SimpleNamespace(
            log_decision=lambda *a, **k: None, alert=lambda *a: None
        )
        bot.trade_ctx = SimpleNamespace()
        bot._mr_queue = []
        bot._init_mr_paper_executor()
        return bot

    ex1, mgr1, trade1, cb1 = _executor(enabled=True, ask=0.79)
    bot1 = _bot(mgr1, cb1)
    queue.write_text(json.dumps(_shelf("buy_nok_calls.json")) + "\n", encoding="utf-8")
    bot1._drain_mr_paper(allow_entries=True)
    assert mgr1.has_position("NOK")
    assert trade1.orders
    n = len(trade1.orders)

    ex2, mgr2, trade2, cb2 = _executor(enabled=True, ask=0.79)
    bot2 = _bot(mgr2, cb2)
    bot2._drain_mr_paper(allow_entries=True)
    assert trade2.orders == []
    assert mgr2.open_count() == 0
    assert len(trade1.orders) == n


def _drain_bot(mgr, cb, *, paused=False):
    from fabio_live.bot import ORBBot

    bot = ORBBot.__new__(ORBBot)
    bot.paused = paused
    bot.order_mgr = mgr
    bot.cb = cb
    bot.ops = SimpleNamespace(
        log_decision=lambda *a, **k: None, alert=lambda *a: None
    )
    bot.trade_ctx = SimpleNamespace()
    bot._mr_queue = []
    bot._init_mr_paper_executor()
    return bot


def test_drain_does_not_write_cursor_when_refused(tmp_path, monkeypatch):
    """In-memory payloads must not mint offset=0 beside retained JSONL."""
    queue = tmp_path / "retained.jsonl"
    queue.write_text(json.dumps(_shelf("buy_nok_calls.json")) + "\n", encoding="utf-8")
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setenv("FABIO_MR_QUEUE_PATH", str(queue))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "fabio_live.bot.get_portfolio_value", lambda *_a, **_k: 10_000.0
    )
    cursor_path = Path(str(queue) + ".cursor.json")
    assert not cursor_path.is_file()

    ex, mgr, trade, cb = _executor(enabled=True, ask=0.79)
    bot = _drain_bot(mgr, cb, paused=False)
    assert bot._mr_cursor is not None
    assert bot._mr_cursor.refused is True
    bot._mr_queue = [
        {
            "id": "memory_only",
            "channel": "email",
            "subject": "not a trade",
            "body": "",
        }
    ]
    bot._drain_mr_paper(allow_entries=True)
    assert not cursor_path.is_file(), "refused drain must not create cursor"
    assert mgr.open_count() == 0
    assert trade.orders == []

    ex2, mgr2, trade2, cb2 = _executor(enabled=True, ask=0.79)
    bot2 = _drain_bot(mgr2, cb2, paused=False)
    bot2._drain_mr_paper(allow_entries=True)
    assert bot2._mr_cursor.refused is True
    assert not cursor_path.is_file()
    assert trade2.orders == []
    assert mgr2.open_count() == 0


def test_drain_skipped_while_bot_paused(tmp_path, monkeypatch):
    queue = tmp_path / "mr.jsonl"
    queue.write_text("", encoding="utf-8")
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setenv("FABIO_MR_QUEUE_PATH", str(queue))
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "fabio_live.bot.get_portfolio_value", lambda *_a, **_k: 10_000.0
    )

    ex, mgr, trade, cb = _executor(enabled=True, ask=0.79)
    bot = _drain_bot(mgr, cb, paused=True)
    queue.write_text(json.dumps(_shelf("buy_nok_calls.json")) + "\n", encoding="utf-8")
    queued = [{"id": "paused_memory", "channel": "email", "body": ""}]
    bot._mr_queue = list(queued)
    bot._drain_mr_paper(allow_entries=True)
    assert mgr.open_count() == 0
    assert trade.orders == []
    assert bot._mr_queue == queued
    assert int(bot._mr_cursor.offset) == 0

    bot.paused = False
    bot._drain_mr_paper(allow_entries=True)
    assert mgr.has_position("NOK")
    assert trade.orders
    assert bot._mr_queue == []


def _reconcile_bot(monkeypatch, mgr, rows, *, cb=None, mr_ex=None):
    from fabio_live.bot import ORBBot

    monkeypatch.setattr("fabio_live.bot.AUTO_ADOPT_OPEN_POSITIONS", True)
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot._pause_reason_code = ""
    bot._pause_reason_hint = ""
    bot._startup_unreconciled_positions = []
    bot.signals = {}
    bot.exit_tfs = {}
    bot.regimes = {}
    bot._trade_entries = {}
    bot._tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    bot.order_mgr = mgr
    bot.cb = cb or RiskCircuitBreaker()
    bot._mr_executor = mr_ex
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    df = pd.DataFrame(rows)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (0, df)
    )
    bot._startup_reconcile_positions()
    return bot


def test_auto_adopt_non_orb_tags_source_mr_and_skips_orb_signals(monkeypatch):
    """Mid-session restart: NOK/IBM adopted as MR; SPY stays unmarked ORB."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, _trade, cb = _executor(enabled=True, ask=0.79)
    bot = _reconcile_bot(
        monkeypatch,
        mgr,
        [
            {
                "code": "US.NOK261016C00010000",
                "qty": 2,
                "cost_price": 0.79,
            },
            {
                "code": "US.IBM261016C00250000",
                "qty": 1,
                "cost_price": 6.38,
            },
            {
                "code": "US.SPY260507C00735000",
                "qty": 2,
                "cost_price": 1.25,
            },
        ],
        cb=cb,
        mr_ex=ex,
    )
    assert bot.paused is False
    assert mgr.positions["NOK"].get("source") == SOURCE_MR
    assert mgr.positions["IBM"].get("source") == SOURCE_MR
    assert mgr.positions["NOK"]["strike"] == pytest.approx(10)
    assert mgr.positions["NOK"]["expiry"] == "2026-10-16"
    assert mgr.positions["IBM"]["strike"] == pytest.approx(250)
    assert mgr.positions["IBM"]["expiry"] == "2026-10-16"
    assert "NOK" not in bot.signals
    assert "IBM" not in bot.signals
    assert "NOK" not in bot.exit_tfs
    assert "IBM" not in bot.exit_tfs
    assert "NOK" not in bot._trade_entries
    assert bot.signals["SPY"] == "CALL"
    assert "SPY" in bot.exit_tfs
    assert mgr.positions["SPY"].get("source") != "mr"


def test_auto_adopt_non_orb_run_exit_loop_no_regimes_keyerror(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, _trade, cb = _executor(enabled=True, ask=0.79)
    bot = _reconcile_bot(
        monkeypatch,
        mgr,
        [{"code": "US.NOK261016C00010000", "qty": 2, "cost_price": 0.79}],
        cb=cb,
        mr_ex=ex,
    )
    assert "NOK" not in bot.signals
    assert "NOK" not in bot.regimes
    bot.quote_ctx = SimpleNamespace()
    bot.run_exit_loop()
    assert mgr.has_position("NOK")
    assert mgr.positions["NOK"].get("source") == SOURCE_MR


def test_auto_adopt_non_orb_lock_in_closes_after_restart(monkeypatch):
    monkeypatch.setenv(MR_PAPER_ENABLED_ENV, "1")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, cb = _executor(enabled=True, ask=6.38)
    bot = _reconcile_bot(
        monkeypatch,
        mgr,
        [{"code": "US.IBM261016C00250000", "qty": 1, "cost_price": 6.38}],
        cb=cb,
        mr_ex=ex,
    )
    assert mgr.has_position("IBM")
    assert "IBM" not in bot.signals
    buys = len(trade.orders)
    out = ex.consider(_parse_shelf("lock_ibm_call.json"), portfolio_val=10_000)
    assert out["status"] == "exited"
    assert out["reason"] == "MR_LOCK_IN"
    assert mgr.has_position("IBM") is False
    sells = [o for o in trade.orders[buys:] if o["trd_side"] == TrdSide.SELL]
    assert len(sells) == 1
    assert sells[0]["order_type"] == OrderType.MARKET


def test_auto_adopt_non_orb_eod_leftover_closes_not_orb_spy(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ex, mgr, trade, cb = _executor(enabled=True, ask=0.79)
    bot = _reconcile_bot(
        monkeypatch,
        mgr,
        [{"code": "US.NOK261016C00010000", "qty": 2, "cost_price": 0.79}],
        cb=cb,
        mr_ex=ex,
    )
    mgr.enter("SPY", "CALL", 500.0, 0.01, 10_000.0)
    assert mgr.has_position("NOK") and mgr.has_position("SPY")
    assert "NOK" not in bot.signals
    bot.ops = SimpleNamespace(alert=lambda *_: None, log_alert=lambda *_: None)
    bot.trade_ctx = SimpleNamespace(
        position_list_query=lambda **kwargs: (0, pd.DataFrame())
    )
    bot.eod_close_all()
    assert mgr.has_position("NOK") is False
    assert mgr.has_position("SPY") is True
    assert mgr.positions["SPY"].get("source") != "mr"
    sells = [o for o in trade.orders if o["trd_side"] == TrdSide.SELL]
    assert sells
    assert all(o["order_type"] == OrderType.MARKET for o in sells)
