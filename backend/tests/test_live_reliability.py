from __future__ import annotations

import datetime

import pandas as pd
from moomoo import KLType, OrderStatus, TrdSide

from fabio_live.circuit import RiskCircuitBreaker
from fabio_live.constants import CB_DAILY_LOSS_PCT
from fabio_live.market_data import candle_age_seconds, get_candles_fresh
from fabio_live.orders import OrderManager


def test_circuit_breaker_blocks_after_daily_loss_limit():
    cb = RiskCircuitBreaker()
    cb.set_portfolio_open(100_000.0)
    cb.record_result(-100_000.0 * CB_DAILY_LOSS_PCT)
    allowed, reason = cb.can_enter(n_open=0)
    assert allowed is False
    assert "Daily loss" in reason


def test_circuit_breaker_trade_and_open_caps_match_backtest_language():
    cb = RiskCircuitBreaker()
    cb.set_portfolio_open(100_000.0)
    cb.trade_count = 2
    allowed, reason = cb.can_enter(n_open=1)
    assert allowed is True
    assert reason == ""

    cb.trade_count = 3
    allowed2, reason2 = cb.can_enter(n_open=0)
    assert allowed2 is False
    assert "Daily trade cap reached" in reason2

    cb.trade_count = 0
    allowed3, reason3 = cb.can_enter(n_open=3)
    assert allowed3 is False
    assert "Max open positions" in reason3


def test_get_candles_fresh_retries_then_returns_latest_if_stale(monkeypatch):
    calls = {"n": 0}

    def _stale_df(minutes_old: int) -> pd.DataFrame:
        ts = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(minutes=minutes_old)
        return pd.DataFrame(
            {
                "time_key": [ts],
                "open": [100.0],
                "close": [100.1],
                "high": [100.2],
                "low": [99.9],
                "volume": [10],
            }
        )

    def fake_get_candles(*args, **kwargs):
        calls["n"] += 1
        return _stale_df(30)

    monkeypatch.setattr("fabio_live.market_data.get_candles", fake_get_candles)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    df = get_candles_fresh(
        quote_ctx=None,
        symbol="SPY",
        ktype=KLType.K_5M,
        count=10,
        max_age_bars=2.0,
        retries=3,
    )
    assert len(df) == 1
    assert calls["n"] == 3
    assert candle_age_seconds(df) > 10 * 60


class _FakeQuoteCtx:
    def get_option_expiration_date(self, code):
        return 0, pd.DataFrame({"date": [datetime.date.today().isoformat()]})

    def get_option_chain(self, code, index_option_type, start, end, option_type):
        return 0, pd.DataFrame({"strike_price": [500.0], "code": ["US.SPY250507C00500000"]})

    def get_market_snapshot(self, codes):
        return 0, pd.DataFrame({"ask_price": [1.0], "bid_price": [1.0], "last_price": [1.0]})


class _FakeTradeCtx:
    def __init__(self):
        self._order_num = 0
        self._orders = {}

    def place_order(self, price, qty, code, trd_side, order_type, trd_env, time_in_force):
        self._order_num += 1
        order_id = f"O{self._order_num}"
        self._orders[order_id] = {"qty": qty, "attempt": self._order_num}
        return 0, pd.DataFrame({"order_id": [order_id]})

    def order_list_query(self, order_id, trd_env):
        if order_id == "O1":
            return 0, pd.DataFrame(
                {"order_status": [OrderStatus.FILLED_PART], "dealt_qty": [1]}
            )
        return 0, pd.DataFrame(
            {"order_status": [OrderStatus.FILLED_ALL], "dealt_qty": [1]}
        )

    def modify_order(self, modify_order_op, order_id, qty, price, **kwargs):
        return 0, None


def test_order_manager_partial_fill_retries_remaining_qty(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    mgr = OrderManager(_FakeTradeCtx(), _FakeQuoteCtx(), trd_env=None)
    mgr.enter(
        symbol="SPY",
        direction="CALL",
        price=500.0,
        risk_pct=0.01,  # enough for 2 contracts at $1 ask after risk-base cap
        portfolio_val=100_000.0,
    )
    assert mgr.has_position("SPY")
    assert mgr.positions["SPY"]["original_qty"] == 2


def test_order_manager_cancel_uses_modify_order_fallback(monkeypatch):
    class _ModifyOp:
        CANCEL = "CANCEL"

    class _Ctx:
        def __init__(self):
            self.calls = []

        def modify_order(self, modify_order_op, order_id, qty, price, **kwargs):
            self.calls.append(
                {
                    "modify_order_op": modify_order_op,
                    "order_id": order_id,
                    "qty": qty,
                    "price": price,
                    **kwargs,
                }
            )
            return 0, None

    monkeypatch.setattr("fabio_live.orders.ModifyOrderOp", _ModifyOp)
    mgr = OrderManager(_Ctx(), _FakeQuoteCtx(), trd_env="SIM")
    assert mgr._cancel("O123", "fallback") is True
    assert len(mgr.ctx.calls) == 1
    assert mgr.ctx.calls[0]["modify_order_op"] == _ModifyOp.CANCEL
    assert mgr.ctx.calls[0]["order_id"] == "O123"
    assert mgr.ctx.calls[0]["qty"] == 0 and mgr.ctx.calls[0]["price"] == 0


def test_order_manager_post_entry_sweep_cancels_only_working_buys(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)

    class _TradeCtxSweep:
        def __init__(self):
            self.cancelled = []

        def place_order(self, price, qty, code, trd_side, order_type, trd_env, time_in_force):
            return 0, pd.DataFrame({"order_id": ["E1"]})

        def order_list_query(self, order_id=None, trd_env=None):
            if order_id == "E1":
                return 0, pd.DataFrame(
                    {"order_status": [OrderStatus.FILLED_ALL], "dealt_qty": [1]}
                )
            return 0, pd.DataFrame(
                {
                    "order_id": ["W1", "W2", "W3"],
                    "code": [
                        "US.SPY250507C00500000",
                        "US.SPY250507C00500000",
                        "US.OTHER250507C00500000",
                    ],
                    "trd_side": [str(TrdSide.BUY), str(TrdSide.SELL), str(TrdSide.BUY)],
                    "order_status": [
                        OrderStatus.SUBMITTED,
                        OrderStatus.SUBMITTED,
                        OrderStatus.SUBMITTED,
                    ],
                }
            )

        def modify_order(self, modify_order_op, order_id, qty, price, **kwargs):
            self.cancelled.append(order_id)
            return 0, None

    mgr = OrderManager(_TradeCtxSweep(), _FakeQuoteCtx(), trd_env=None)
    mgr.enter(
        symbol="SPY",
        direction="CALL",
        price=500.0,
        risk_pct=0.005,
        portfolio_val=100_000.0,
    )
    assert mgr.has_position("SPY")
    assert "W1" in mgr.ctx.cancelled
    assert "W2" not in mgr.ctx.cancelled
    assert "W3" not in mgr.ctx.cancelled
