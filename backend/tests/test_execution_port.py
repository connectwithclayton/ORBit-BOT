"""ExecutionPort vs default Moomoo ORB wiring (FM-FABIO-SCOUT-001 slice 3)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from brokers.names import (
    BROKER_ENV,
    DEFAULT_BROKER,
    IsolatedBrokerNotAvailable,
    isolated_place_order_client,
    resolve_execution_broker,
)
from fabio_live.execution import ExecutionPort
from fabio_live.orders import OrderManager

BACKEND = Path(__file__).resolve().parents[1]


def test_order_manager_satisfies_execution_port():
    mgr = OrderManager(SimpleNamespace(), SimpleNamespace(), None)
    assert isinstance(mgr, ExecutionPort)
    assert mgr.open_count() == 0
    assert mgr.has_position("SPY") is False
    assert mgr.positions == {}


def test_execution_module_has_no_broker_sdk_imports():
    src = (BACKEND / "fabio_live" / "execution.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in {"moomoo", "futu", "tradier", "brokers", "requests"}
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in {"moomoo", "futu", "tradier", "brokers", "requests"}


def test_resolve_execution_broker_defaults_moomoo(monkeypatch):
    monkeypatch.delenv(BROKER_ENV, raising=False)
    assert resolve_execution_broker() == DEFAULT_BROKER
    assert resolve_execution_broker("") == "moomoo"
    assert resolve_execution_broker("MOOMOO") == "moomoo"
    assert resolve_execution_broker("futu") == "moomoo"


def test_resolve_execution_broker_tradier_is_explicit(monkeypatch):
    monkeypatch.setenv(BROKER_ENV, "tradier")
    assert resolve_execution_broker() == "tradier"
    monkeypatch.setenv(BROKER_ENV, "TRADIER")
    assert resolve_execution_broker() == "tradier"


def test_resolve_execution_broker_unknown_raises(monkeypatch):
    monkeypatch.setenv(BROKER_ENV, "shadow")
    try:
        resolve_execution_broker()
    except ValueError as exc:
        assert BROKER_ENV in str(exc)
        assert "tradier" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_moomoo_trade_env_does_not_select_tradier(monkeypatch):
    monkeypatch.delenv(BROKER_ENV, raising=False)
    monkeypatch.setenv("MOOMOO_TRADE_ENV", "REAL")
    monkeypatch.setenv("TRADIER_ENV", "live")
    assert resolve_execution_broker() == "moomoo"


def test_isolated_place_order_client_refuses_default_moomoo(monkeypatch):
    monkeypatch.delenv(BROKER_ENV, raising=False)
    try:
        isolated_place_order_client()
    except IsolatedBrokerNotAvailable as exc:
        assert "tradier" in str(exc).lower()
        assert "moomoo" in str(exc).lower()
    else:
        raise AssertionError("expected IsolatedBrokerNotAvailable")


def test_orb_bot_source_still_constructs_moomoo_contexts():
    """Default boot must keep OpenQuote / OpenSecTrade / SIMULATE OrderManager."""
    src = (BACKEND / "fabio_live" / "bot.py").read_text(encoding="utf-8")
    assert "self.quote_ctx = OpenQuoteContext(host=MOOMOO_HOST, port=MOOMOO_PORT)" in src
    assert "self.trade_ctx = OpenSecTradeContext(" in src
    assert "filter_trdmarket=TrdMarket.US" in src
    assert "trd_env = TrdEnv.SIMULATE if PAPER_TRADING else TrdEnv.REAL" in src
    assert "self.order_mgr = OrderManager(self.trade_ctx, self.quote_ctx, trd_env)" in src
    assert "TradierPaperClient" not in src
    assert "FABIO_BROKER" not in src
    assert "isolated_place_order_client" not in src


def test_reconcile_and_parity_modules_do_not_ingest_tradier():
    recon = (BACKEND / "reconcile_moomoo_to_sheets.py").read_text(encoding="utf-8")
    parity = (BACKEND / "tests" / "test_position_parity.py").read_text(encoding="utf-8")
    assert "tradier" not in recon.lower()
    assert "TradierPaperClient" not in parity
    assert "brokers.tradier" not in parity
