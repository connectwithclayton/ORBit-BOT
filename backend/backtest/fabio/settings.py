"""Central configuration for Fabio backtests (symbols, risk, data source)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from fabio_bot_paths import fabio_bot_root


def _load_dotenv_into_environ() -> None:
    """Match legacy Fabio_orb_backtest .env resolution."""
    candidates = [
        fabio_bot_root() / ".env",
        Path.home() / "Documents" / "TRADING" / "Fabio_bot" / ".env",
    ]
    for p in candidates:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break


@dataclass
class FabioBacktestSettings:
    """All parameters shared by research and live-mirror backtests."""

    symbols: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "NVDA"])
    start_date: str = "2023-05-03"
    end_date: str = "2026-05-03"
    initial_capital: float = 10_000.0
    strategy_capital_cap: float = 10_000.0  # $10k strategy/modeled-book cap; NOT risk_base ceiling

    data_source: str = "polygon"  # "polygon" | "yfinance"
    polygon_api_key: str = ""

    risk_pct_half: float = 0.05
    risk_pct_full: float = 0.10
    risk_pct_aggressive: float = 0.10
    risk_pct_max: float = 0.10

    vix_skip: float = 14
    vix_half_max: float = 16
    vix_normal_max: float = 20
    vix_aggressive_max: float = 28

    gap_skip_pct: float = 3.0
    gap_retest_pct: float = 1.5

    or_skip_pct_atr: float = 8
    or_normal_min_atr: float = 15
    or_wide_pct_atr: float = 60

    cb_daily_loss_pct: float = 0.02
    cb_max_trades: int = 3
    cb_max_loss_streak: int = 3
    cb_max_open_pos: int = 3

    trim_multiple: float = 2.0
    trim_pct: float = 0.50
    profit_lock_multiple: float = 1.2

    iv_base: float = 0.20
    option_dte: int = 1
    slippage_pct: float = 0.02
    commission: float = 1.30

    # risk_base = min(portfolio, strategy_capital_cap * this) → $20k ceiling, not $10k
    research_risk_capital_multiplier: float = 2.0

    @classmethod
    def from_env(cls) -> FabioBacktestSettings:
        _load_dotenv_into_environ()
        key = os.environ.get("POLYGON_API_KEY", "")
        ds = os.environ.get("FABIO_DATA_SOURCE", "").strip().lower()
        data_source = ds if ds in ("polygon", "yfinance") else "polygon"
        return cls(polygon_api_key=key, data_source=data_source)
