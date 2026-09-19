"""Paper-pin + fail-safe default contracts (FM-FABIO-SCOUT-001 slice 1)."""

from __future__ import annotations

import pytest
from moomoo import TrdEnv

from moomoo_eod_failsafe import build_arg_parser, default_trd_env_choice
from paper_pin import (
    ALLOW_REAL_ENV,
    LiveFundsRefused,
    allow_real_trading_enabled,
    enforce_paper_trading_pin,
    is_real_trd_env,
    resolve_moomoo_trd_env_name,
)
from orb_bot_fabio import run_bot_with_guard


def _clear_trade_env(monkeypatch) -> None:
    monkeypatch.delenv("MOOMOO_TRADE_ENV", raising=False)
    monkeypatch.delenv("MOOMOO_TRD_ENV", raising=False)
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)


def test_is_real_trd_env_accepts_moomoo_enum_and_string():
    assert is_real_trd_env(TrdEnv.REAL) is True
    assert is_real_trd_env("REAL") is True
    assert is_real_trd_env("real") is True
    assert is_real_trd_env(TrdEnv.SIMULATE) is False
    assert is_real_trd_env("SIMULATE") is False
    assert is_real_trd_env(None) is False


def test_enforce_refuses_real_without_allow_flag(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    with pytest.raises(LiveFundsRefused) as ei:
        enforce_paper_trading_pin(TrdEnv.REAL)
    msg = str(ei.value)
    assert ALLOW_REAL_ENV in msg
    assert "REAL" in msg


def test_enforce_refuses_real_string_without_allow_flag(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    with pytest.raises(LiveFundsRefused):
        enforce_paper_trading_pin("REAL")


def test_enforce_allows_real_only_with_explicit_flag(monkeypatch):
    monkeypatch.setenv(ALLOW_REAL_ENV, "1")
    enforce_paper_trading_pin(TrdEnv.REAL)
    enforce_paper_trading_pin("REAL")
    assert allow_real_trading_enabled() is True


def test_allow_flag_must_be_exactly_one(monkeypatch):
    monkeypatch.setenv(ALLOW_REAL_ENV, "true")
    with pytest.raises(LiveFundsRefused):
        enforce_paper_trading_pin(TrdEnv.REAL)
    monkeypatch.setenv(ALLOW_REAL_ENV, "yes")
    with pytest.raises(LiveFundsRefused):
        enforce_paper_trading_pin(TrdEnv.REAL)
    monkeypatch.setenv(ALLOW_REAL_ENV, "0")
    with pytest.raises(LiveFundsRefused):
        enforce_paper_trading_pin(TrdEnv.REAL)


def test_enforce_allows_simulate_without_flag(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    enforce_paper_trading_pin(TrdEnv.SIMULATE)
    enforce_paper_trading_pin("SIMULATE")


def test_run_bot_with_guard_refuses_real_without_allow(monkeypatch):
    monkeypatch.setattr("orb_bot_fabio.MOOMOO_TRADE_ENV", TrdEnv.REAL)
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)

    class MustNotStart:
        def __init__(self):
            raise AssertionError("ORBBot must not construct on REAL without allow flag")

        def run(self):
            raise AssertionError("bot must not run")

    with pytest.raises(LiveFundsRefused) as ei:
        run_bot_with_guard(bot_factory=MustNotStart, sleep_fn=lambda _s: None)
    assert ALLOW_REAL_ENV in str(ei.value)


def test_run_bot_with_guard_real_with_allow_reaches_factory(monkeypatch):
    monkeypatch.setattr("orb_bot_fabio.MOOMOO_TRADE_ENV", TrdEnv.REAL)
    monkeypatch.setenv(ALLOW_REAL_ENV, "1")
    monkeypatch.setenv("FABIO_IGNORE_NYSE_CALENDAR", "1")
    started = {"n": 0}

    class Ok:
        def run(self):
            started["n"] += 1

    code = run_bot_with_guard(bot_factory=Ok, sleep_fn=lambda _s: None)
    assert code == 0
    assert started["n"] == 1


def test_resolve_trd_env_defaults_simulate(monkeypatch):
    _clear_trade_env(monkeypatch)
    assert resolve_moomoo_trd_env_name() == "SIMULATE"
    assert default_trd_env_choice() == "SIMULATE"


def test_resolve_trd_env_prefers_live_moomoo_trade_env(monkeypatch):
    monkeypatch.setenv("MOOMOO_TRADE_ENV", "SIMULATE")
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    assert resolve_moomoo_trd_env_name() == "SIMULATE"


def test_resolve_trd_env_legacy_alias_when_canonical_unset(monkeypatch):
    monkeypatch.delenv("MOOMOO_TRADE_ENV", raising=False)
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    assert resolve_moomoo_trd_env_name() == "REAL"


def test_failsafe_parser_default_is_simulate_not_real(monkeypatch):
    _clear_trade_env(monkeypatch)
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.trd_env == "SIMULATE"
    help_text = parser.format_help()
    assert "Default: SIMULATE" in help_text
    assert "Default: REAL" not in help_text


def test_failsafe_parser_help_shows_canonical_env_name(monkeypatch):
    _clear_trade_env(monkeypatch)
    help_text = build_arg_parser().format_help()
    assert "MOOMOO_TRADE_ENV" in help_text
    assert "FABIO_ALLOW_REAL_TRADING" in help_text


def test_failsafe_explicit_real_refused_without_allow(monkeypatch):
    _clear_trade_env(monkeypatch)
    parser = build_arg_parser()
    args = parser.parse_args(["--trd-env", "REAL", "--dry-run"])
    assert args.trd_env == "REAL"
    with pytest.raises(LiveFundsRefused):
        enforce_paper_trading_pin(args.trd_env)


def test_print_effective_config_reports_simulate_and_risk_base(monkeypatch, capsys):
    import print_effective_config as pec

    monkeypatch.setattr(pec, "MOOMOO_TRADE_ENV", TrdEnv.SIMULATE)
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    pec.main()
    out = capsys.readouterr().out
    assert "SIMULATE" in out
    assert "$10,000" in out
    assert "$20,000" in out
    assert "NOT the sizing ceiling" in out
    assert "WARNING: MOOMOO_TRADE_ENV resolves to REAL" not in out


def test_print_effective_config_real_without_allow_warns_refuse(monkeypatch, capsys):
    import print_effective_config as pec

    monkeypatch.setattr(pec, "MOOMOO_TRADE_ENV", TrdEnv.REAL)
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    pec.main()
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "REFUSE" in out
    assert ALLOW_REAL_ENV in out
