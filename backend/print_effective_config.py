"""
print_effective_config.py — show resolved runtime config for live + backtests.

Run before market open / before starting the live bot (pre-flight).

If MOOMOO_TRADE_ENV resolves to REAL, prints a prominent warning banner first.

Usage:
    python3 print_effective_config.py
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict

from moomoo import TrdEnv

from fabio_bot_paths import fabio_bot_root

from config import (
    GOOGLE_CREDS_PATH,
    GOOGLE_SHEET_ID,
    MOOMOO_HOST,
    MOOMOO_PORT,
    MOOMOO_TRADE_ENV,
    TG_GROUP_ID,
    TG_PERSONAL_ID,
    TG_TOKEN,
)
from backtest.fabio.settings import FabioBacktestSettings
from brokers.names import resolve_execution_broker
from paper_pin import (
    ALLOW_REAL_ENV,
    allow_real_trading_enabled,
    is_real_trd_env,
    resolve_tradier_env_name,
)


def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def _print_real_trading_banner() -> None:
    raw = os.getenv("MOOMOO_TRADE_ENV", "SIMULATE").strip()
    allowed = allow_real_trading_enabled()
    bar = "!" * 72
    print()
    print(bar)
    print("  WARNING: MOOMOO_TRADE_ENV resolves to REAL (live) trading.")
    if allowed:
        print(f"  {ALLOW_REAL_ENV}=1 is set — live path will attach REAL.")
        print("  Confirm account, buying power, and risk limits before starting.")
    else:
        print(f"  Paper pin: live path will REFUSE REAL unless {ALLOW_REAL_ENV}=1.")
        print("  Paper-only default. Do not set the allow flag for paper trading.")
    print(f"  Environment variable: MOOMOO_TRADE_ENV={raw!r}")
    print(bar)
    print()


def main() -> None:
    if is_real_trd_env(MOOMOO_TRADE_ENV) or MOOMOO_TRADE_ENV == TrdEnv.REAL:
        _print_real_trading_banner()

    cfg = FabioBacktestSettings.from_env()
    strategy = asdict(cfg)
    strategy["polygon_api_key"] = _mask(cfg.polygon_api_key)

    cap = float(strategy["strategy_capital_cap"])
    mult = float(strategy["research_risk_capital_multiplier"])
    risk_base_ceiling = cap * mult

    integrations = {
        "env_source": os.getenv("FABIO_ENV_FILE", str(fabio_bot_root() / ".env")),
        "moomoo_host": MOOMOO_HOST,
        "moomoo_port": MOOMOO_PORT,
        "moomoo_trade_env": str(MOOMOO_TRADE_ENV),
        "fabio_allow_real_trading": os.getenv(ALLOW_REAL_ENV, "") or "(not set)",
        "paper_pin_real_allowed": allow_real_trading_enabled(),
        "telegram_token": _mask(TG_TOKEN),
        "telegram_personal_chat_id": _mask(TG_PERSONAL_ID),
        "telegram_group_chat_id": _mask(TG_GROUP_ID),
        "google_sheet_id": _mask(GOOGLE_SHEET_ID),
        "google_creds_path": GOOGLE_CREDS_PATH or "(not set)",
        "fabio_broker": resolve_execution_broker(),
        "tradier_env": resolve_tradier_env_name(),
        "tradier_account_id": _mask(os.getenv("TRADIER_ACCOUNT_ID", "")),
        "tradier_access_token": (
            "(set)"
            if (os.getenv("TRADIER_ACCESS_TOKEN") or os.getenv("TRADIER_TOKEN"))
            else "(not set)"
        ),
    }

    print("Fabio effective runtime config")
    print("=" * 36)
    print("\n[Strategy settings (fabio/settings.py)]")
    print(json.dumps(strategy, indent=2, sort_keys=True))
    print("\n[Integrations (config.py)]")
    print(json.dumps(integrations, indent=2, sort_keys=True))

    print("\n[Sanity summary]")
    print(f"- symbols: {strategy['symbols']}")
    print(
        f"- VIX tiers: skip<{strategy['vix_skip']} / half<={strategy['vix_half_max']} / "
        f"normal<={strategy['vix_normal_max']} / aggressive<={strategy['vix_aggressive_max']}"
    )
    print(
        f"- risk caps: full={strategy['risk_pct_full']:.2f}, half={strategy['risk_pct_half']:.2f}, "
        f"max={strategy['risk_pct_max']:.2f}"
    )
    print(
        f"- strategy_capital_cap: ${cap:,.0f} (strategy / modeled-book cap — NOT the sizing ceiling)"
    )
    print(
        f"- risk_base ceiling: ${risk_base_ceiling:,.0f} "
        f"(= ${cap:,.0f} × {mult:.2f} research_risk_capital_multiplier)"
    )
    print(
        f"- sizing: risk_base = min(portfolio, ${risk_base_ceiling:,.0f}); "
        "risk_dollars = risk_base × risk_pct. Do not size as if the cap were $10,000."
    )
    if is_real_trd_env(MOOMOO_TRADE_ENV) or MOOMOO_TRADE_ENV == TrdEnv.REAL:
        if allow_real_trading_enabled():
            print(
                f"- Moomoo: REAL — live orders allowed because {ALLOW_REAL_ENV}=1 "
                "(not paper / simulate)."
            )
        else:
            print(
                f"- Moomoo: REAL requested but paper pin will REFUSE without {ALLOW_REAL_ENV}=1."
            )
    else:
        print(
            "- Moomoo: SIMULATE — paper-style environment "
            f"(REAL also needs {ALLOW_REAL_ENV}=1; do not set it for paper)."
        )
    broker = resolve_execution_broker()
    print(
        f"- FABIO_BROKER={broker} (default moomoo). "
        "ORB live path still constructs Moomoo OpenQuoteContext / "
        "OpenSecTradeContext / OrderManager; Tradier is isolated place_order + flatten."
    )
    print(
        f"- Tradier flatten: env={resolve_tradier_env_name()} "
        f"(paper/sandbox default; live needs {ALLOW_REAL_ENV}=1). "
        "Does not reuse MOOMOO_TRADE_ENV."
    )
    mr_on = os.getenv("FABIO_MR_PAPER_ENABLED", "").strip() == "1"
    print(
        f"- MR paper auto-trade: {'ON' if mr_on else 'OFF'} "
        "(FABIO_MR_PAPER_ENABLED; default off — ORB loop unchanged). "
        "Moomoo SIMULATE OrderManager only; does not enable Tradier dual books."
    )


if __name__ == "__main__":
    main()

