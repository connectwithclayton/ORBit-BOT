"""
Paper-only interlock for Moomoo and Tradier trade environments.

MOOMOO_TRADE_ENV=REAL is not enough to attach live funds. The live path and
fail-safe refuse TrdEnv.REAL unless FABIO_ALLOW_REAL_TRADING=1 is set explicitly.

Tradier uses TRADIER_ENV (paper/sandbox vs live) independently — never
MOOMOO_TRADE_ENV. Live Tradier (api.tradier.com) is refused unless the same
allow flag is exactly 1.

Default remains SIMULATE / paper. This module does not enable live funds.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

ALLOW_REAL_ENV = "FABIO_ALLOW_REAL_TRADING"
TRADE_ENV_NAME = "MOOMOO_TRADE_ENV"
LEGACY_FAILSAFE_TRD_ENV_NAME = "MOOMOO_TRD_ENV"
TRADIER_ENV_NAME = "TRADIER_ENV"
TRADIER_PAPER_BASE_URL = "https://sandbox.tradier.com"
TRADIER_LIVE_BASE_URL = "https://api.tradier.com"

_LIVE_FUNDS_REFUSED_MSG = (
    "REFUSED: TrdEnv.REAL / live funds are blocked. "
    f"Paper-only default. Set {ALLOW_REAL_ENV}=1 to override "
    f"(in addition to {TRADE_ENV_NAME}=REAL). "
    f"Leave {ALLOW_REAL_ENV} unset and use {TRADE_ENV_NAME}=SIMULATE."
)

_TRADIER_LIVE_REFUSED_MSG = (
    "REFUSED: Tradier live/REAL funds are blocked. "
    "Paper-only default (sandbox). "
    f"Set {ALLOW_REAL_ENV}=1 to override "
    f"(in addition to {TRADIER_ENV_NAME}=live). "
    f"Leave {ALLOW_REAL_ENV} unset and use {TRADIER_ENV_NAME}=paper. "
    "Do not reuse MOOMOO_TRADE_ENV for Tradier."
)

_TRADIER_PAPER_TOKENS = frozenset({"", "paper", "sandbox", "simulate", "sim"})
_TRADIER_LIVE_TOKENS = frozenset({"live", "real", "production", "prod"})
_TRADIER_LIVE_HOSTS = frozenset({"api.tradier.com", "api.tradier.com:443"})


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


def resolve_tradier_env_name(raw: str | None = None) -> str:
    """Tradier env: paper unless an explicit live token is set.

    Independent of MOOMOO_TRADE_ENV. Unknown values default to paper
    (never silently live).
    """
    token = (raw if raw is not None else os.getenv(TRADIER_ENV_NAME, "")).strip().lower()
    if token in _TRADIER_LIVE_TOKENS:
        return "live"
    if token in _TRADIER_PAPER_TOKENS:
        return "paper"
    return "paper"


def tradier_host_is_live(base_url: str | None) -> bool:
    """True when *base_url* points at Tradier production (api.tradier.com)."""
    if not base_url:
        return False
    host = (urlparse(base_url.strip()).netloc or "").lower()
    if not host:
        # allow passing a bare host
        host = base_url.strip().lower().split("/")[0]
    return host in _TRADIER_LIVE_HOSTS


def is_tradier_live_env(*, env: str | None = None, base_url: str | None = None) -> bool:
    """True if Tradier env name or base URL denotes funded/live, not sandbox paper."""
    if env is not None and resolve_tradier_env_name(env) == "live":
        return True
    return tradier_host_is_live(base_url)


def enforce_tradier_paper_pin(env=None, *, base_url: str | None = None) -> None:
    """Refuse Tradier live unless FABIO_ALLOW_REAL_TRADING=1. Paper always allowed."""
    if not is_tradier_live_env(env=env, base_url=base_url):
        return
    if allow_real_trading_enabled():
        return
    raise LiveFundsRefused(_TRADIER_LIVE_REFUSED_MSG)
