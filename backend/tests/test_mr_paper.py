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
    MR_PAPER_ENABLED_ENV,
    SKIP_COMBINED_DAILY_LOSS,
    SKIP_DISABLED,
    SKIP_EQUITY_OPTIONS_ONLY,
    SKIP_MULTI_LEG,
    SKIP_NOT_OPEN,
    SKIP_REAL_REFUSED,
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
            "id": "buy_extra_spy",
            "channel": "email",
            "subject": "RRP - Buy SPY Calls",
            "date": "2026-09-16T16:00:00Z",
            "body": "Buy the October 16th SPY $500 Call for $1.00.\n",
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


def test_no_tradier_dual_books_in_mr_paper():
    src = (BACKEND_ROOT / "fabio_live" / "mr_paper.py").read_text()
    bot_src = (BACKEND_ROOT / "fabio_live" / "bot.py").read_text()
    for blob in (src, bot_src):
        assert "import tradier" not in blob
        assert "from tradier" not in blob
        assert "brokers.tradier" not in blob
        assert "isolated_place_order_client" not in blob
        assert "TradierPaperClient" not in blob
    assert "FABIO_BROKER" not in src
    assert "FABIO_BROKER" not in bot_src


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
