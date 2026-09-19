"""
ORB Trading Bot - SPY/QQQ 0-7DTE Options
Moomoo OpenAPI (paper trading mode)
Strategy: Opening Range Breakout with dynamic EMA-based exit timeframe

Live execution is aligned to Fabio_orb_backtest (research): 5m OR 09:30–09:44,
same entry/retest (no CJ pre-entry bar invalidation), sizing min(portfolio, 2×strategy cap)×risk,
profit lock disables midpoint/EMA only; 2×ATR always on.

Merged rule set:
  ORBIT → VIX tiers, gap filter, OR quality, 2 PM entry cutoff, risk caps,
           profit trimming (50% every double), circuit breakers
  CJ    → binary trend scoring; Fabio exits use 3m midpoint + EMA 10/20 (research);
           dynamic exit TF still computed for logging but Fabio path uses 3m above

Implementation is split under `fabio_live/` (regime, signals, orders, async ops, etc.).
"""

import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from fabio_bot_paths import fabio_bot_root

load_dotenv(fabio_bot_root() / ".env")

from config import MOOMOO_TRADE_ENV
from paper_pin import enforce_paper_trading_pin
import telegram_bot as tg
from fabio_live.bot import ORBBot
from sheets_logger import SheetsLogger

# Backward-compatible re-exports (optional imports from this module)
from fabio_live.async_ops import AsyncOpsWorker
from fabio_live.circuit import RiskCircuitBreaker
from fabio_live.constants import STRATEGY_NAME, SYMBOLS
from fabio_live.orders import OrderManager
from fabio_live.regime import MarketRegime
from fabio_live.signals import SignalEngine

__all__ = [
    "ORBBot",
    "AsyncOpsWorker",
    "RiskCircuitBreaker",
    "OrderManager",
    "MarketRegime",
    "SignalEngine",
    "STRATEGY_NAME",
    "SYMBOLS",
]


def _runtime_guard_config() -> tuple[int, int, int]:
    max_restarts = int(os.getenv("FABIO_RUNTIME_MAX_RESTARTS", "5"))
    backoff_base_sec = int(os.getenv("FABIO_RUNTIME_BACKOFF_BASE_SEC", "15"))
    backoff_cap_sec = int(os.getenv("FABIO_RUNTIME_BACKOFF_CAP_SEC", "300"))
    return max_restarts, backoff_base_sec, backoff_cap_sec


def _security_preflight_warning() -> None:
    root = fabio_bot_root()
    allow_local = os.getenv("FABIO_ALLOW_LOCAL_PLAINTEXT_SECRETS", "0") == "1"
    local_secret_paths = [root / ".env", root / "google_credentials.json"]
    present = [str(p) for p in local_secret_paths if p.exists()]
    if present and not allow_local:
        msg = (
            "⚠️ <b>Security preflight warning</b>\n"
            "Local plaintext secret files detected in project directory:\n"
            + "\n".join(f"- {p}" for p in present)
            + "\nSet FABIO_ALLOW_LOCAL_PLAINTEXT_SECRETS=1 to acknowledge, "
              "or move secrets to external secure storage."
        )
        print(msg.replace("<b>", "").replace("</b>", ""))
        _escalate_runtime_failure(msg)


def _escalate_runtime_failure(msg: str) -> None:
    try:
        tg.alert(msg)
    except Exception as e:
        print(f"[RuntimeGuard] Telegram alert failed: {e}")
    try:
        SheetsLogger().log_alert("RUNTIME_GUARD", msg, "")
    except Exception as e:
        print(f"[RuntimeGuard] Sheets alert failed: {e}")


def _calendar_allows_manual_start() -> bool:
    if os.getenv("FABIO_IGNORE_NYSE_CALENDAR", "").strip() == "1":
        return True
    from fabio_live.calendar_gate import should_start_bot

    return should_start_bot()


def run_bot_with_guard(bot_factory=ORBBot, sleep_fn=time.sleep) -> int:
    """
    Run bot with restart/backoff protection.
    Returns process exit code semantics:
      0 = clean stop
      1 = hard failure after restart budget exhausted
    """
    # Paper pin before calendar / OpenD attach: REAL is refused unless allow flag.
    enforce_paper_trading_pin(MOOMOO_TRADE_ENV)

    if not _calendar_allows_manual_start():
        tz = ZoneInfo("America/New_York")
        d = datetime.now(tz).date().isoformat()
        print(
            f"[Calendar] FABIO skip: NYSE closed {d} ({tz.key}). "
            "See portal/docs/EXCHANGE_CALENDAR.md. Override: FABIO_IGNORE_NYSE_CALENDAR=1"
        )
        return 0

    max_restarts, backoff_base_sec, backoff_cap_sec = _runtime_guard_config()
    attempt = 0

    while True:
        try:
            bot = bot_factory()
            bot.run()
            return 0
        except KeyboardInterrupt:
            print("\n[RuntimeGuard] KeyboardInterrupt received; exiting.")
            return 0
        except Exception as e:
            attempt += 1
            tb = traceback.format_exc(limit=8)
            msg = (
                "🚨 <b>FABIO runtime crash</b>\n"
                f"Attempt: {attempt}/{max_restarts}\n"
                f"Error: {e}\n"
                f"Traceback:\n<pre>{tb[-1200:]}</pre>"
            )
            _escalate_runtime_failure(msg)

            if attempt >= max_restarts:
                fatal_msg = (
                    "🛑 <b>FABIO hard failure</b>\n"
                    f"Exceeded restart budget ({max_restarts}). Manual intervention required."
                )
                _escalate_runtime_failure(fatal_msg)
                return 1

            sleep_sec = min(backoff_cap_sec, backoff_base_sec * (2 ** (attempt - 1)))
            print(
                f"[RuntimeGuard] Crash attempt {attempt}/{max_restarts}. "
                f"Restarting in {sleep_sec}s..."
            )
            sleep_fn(float(sleep_sec))


if __name__ == "__main__":
    _security_preflight_warning()
    raise SystemExit(run_bot_with_guard())
