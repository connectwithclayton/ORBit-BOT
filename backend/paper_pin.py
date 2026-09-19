"""
Paper-only interlock for Moomoo trade environment.

MOOMOO_TRADE_ENV=REAL is not enough to attach live funds. The live path and
fail-safe refuse TrdEnv.REAL unless FABIO_ALLOW_REAL_TRADING=1 is set explicitly.

Default remains SIMULATE (paper). This module does not enable live funds.
"""

from __future__ import annotations

import os

ALLOW_REAL_ENV = "FABIO_ALLOW_REAL_TRADING"
TRADE_ENV_NAME = "MOOMOO_TRADE_ENV"
LEGACY_FAILSAFE_TRD_ENV_NAME = "MOOMOO_TRD_ENV"

_LIVE_FUNDS_REFUSED_MSG = (
    "REFUSED: TrdEnv.REAL / live funds are blocked. "
    f"Paper-only default. Set {ALLOW_REAL_ENV}=1 to override "
    f"(in addition to {TRADE_ENV_NAME}=REAL). "
    f"Leave {ALLOW_REAL_ENV} unset and use {TRADE_ENV_NAME}=SIMULATE."
)


class LiveFundsRefused(SystemExit):
    """Raised when REAL trading is requested without FABIO_ALLOW_REAL_TRADING=1."""


def allow_real_trading_enabled() -> bool:
    """True only when the explicit allow flag is exactly '1'."""
    return os.getenv(ALLOW_REAL_ENV, "").strip() == "1"


def is_real_trd_env(trd_env) -> bool:
    """True if *trd_env* denotes Moomoo live (REAL), not SIMULATE/paper."""
    if trd_env is None:
        return False
    if isinstance(trd_env, str):
        token = trd_env.strip().upper()
    else:
        name = getattr(trd_env, "name", None)
        if name:
            token = str(name).strip().upper()
        else:
            token = str(trd_env).strip().upper()
    return token == "REAL" or token.endswith(".REAL")


def resolve_moomoo_trd_env_name() -> str:
    """Fail-safe / CLI default: SIMULATE unless an env var is an explicit choice.

    Precedence:
      1. MOOMOO_TRADE_ENV (live bot canonical name)
      2. MOOMOO_TRD_ENV (legacy fail-safe alias — do not treat as live default)
      3. SIMULATE (never silently REAL)
    """
    for key in (TRADE_ENV_NAME, LEGACY_FAILSAFE_TRD_ENV_NAME):
        raw = (os.getenv(key) or "").strip().upper()
        if raw in ("REAL", "SIMULATE"):
            return raw
    return "SIMULATE"


def enforce_paper_trading_pin(trd_env) -> None:
    """Refuse REAL unless FABIO_ALLOW_REAL_TRADING=1. SIMULATE always allowed."""
    if not is_real_trd_env(trd_env):
        return
    if allow_real_trading_enabled():
        return
    raise LiveFundsRefused(_LIVE_FUNDS_REFUSED_MSG)
