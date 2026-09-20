"""Live-bot tunables derived from shared settings (keeps one source with backtests)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from fabio_bot_paths import fabio_bot_root

_ROOT = fabio_bot_root()
load_dotenv(_ROOT / ".env")

from config import MOOMOO_TRADE_ENV
from backtest.fabio.settings import FabioBacktestSettings
from moomoo import TrdEnv

STRATEGY_NAME = "FABIO"

CFG = FabioBacktestSettings.from_env()
SYMBOLS = list(CFG.symbols)

PAPER_TRADING = MOOMOO_TRADE_ENV != TrdEnv.REAL

VIX_SKIP = CFG.vix_skip
VIX_HALF_MAX = CFG.vix_half_max
VIX_NORMAL_MAX = CFG.vix_normal_max
VIX_AGGRESSIVE_MAX = CFG.vix_aggressive_max

STRATEGY_CAPITAL = CFG.strategy_capital_cap
RESEARCH_RISK_CAP_MULTIPLIER = CFG.research_risk_capital_multiplier

RISK_PCT_FULL = CFG.risk_pct_full
RISK_PCT_HALF = CFG.risk_pct_half
RISK_PCT_QTR = 0.00
RISK_PCT_MAX = CFG.risk_pct_max

PROFIT_LOCK_MULTIPLE = CFG.profit_lock_multiple
ATR_HARD_STOP_MULT = 2.0

GAP_SKIP_PCT = CFG.gap_skip_pct
GAP_RETEST_PCT = CFG.gap_retest_pct

OR_SKIP_PCT_ATR = CFG.or_skip_pct_atr
OR_NORMAL_MIN_ATR = CFG.or_normal_min_atr
OR_WIDE_PCT_ATR = CFG.or_wide_pct_atr

EMA_GAP_TIGHT = 0.05
EMA_GAP_WIDE = 0.15

ENTRY_MAX_ATTEMPTS = 5
ENTRY_FILL_WAIT_SEC = 60
MAIN_LOOP_SLEEP_ACTIVE_SEC = float(os.getenv("FABIO_MAIN_LOOP_SLEEP_ACTIVE_SEC", "25"))
SIGNAL_END_HOUR = 14
ENTRY_SIGNAL_MAX_AGE_MIN = 15

OPS_QUEUE_WARN_THRESHOLD = 100
OPS_ALERT_COOLDOWN_SEC = 120
OPS_QUEUE_MAXSIZE = 500
OPS_DASHBOARD_REFRESH_THROTTLE_SEC = float(
    os.getenv("FABIO_DASHBOARD_REFRESH_THROTTLE_SEC", "2.0")
)
OPS_DASHBOARD_OPEN_REFRESH_THROTTLE_SEC = float(
    os.getenv("FABIO_DASHBOARD_OPEN_REFRESH_THROTTLE_SEC", "60.0")
)
HEALTH_SNAPSHOT_INTERVAL_SEC = 600
HEALTH_SNAPSHOT_PATH = str(_ROOT / "bot_health_snapshots.jsonl")
HEALTH_SNAPSHOT_RETENTION_DAYS = 14
MARKET_TIMEZONE = "America/New_York"
# NYSE session schedule (XNYS via exchange-calendars). See portal/docs/EXCHANGE_CALENDAR.md.
# Primary bot flatten (eod_close_all): default 15 min before official session close — matches
# legacy fixed 15:45 ET vs 16:00 regular close. Override with FABIO_EOD_CLOSE_BEFORE_SESSION_MINUTES.
EOD_CLOSE_BEFORE_SESSION_MINUTES = int(
    os.getenv("FABIO_EOD_CLOSE_BEFORE_SESSION_MINUTES", "15")
)
# Separate from primary bot: post-close broker fail-safe (moomoo_eod_failsafe.py) window.
FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES = int(
    os.getenv("FABIO_FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES", "10")
)
EOD_SUMMARY_AFTER_SESSION_CLOSE_MINUTES = int(
    os.getenv("FABIO_EOD_SUMMARY_AFTER_SESSION_CLOSE_MINUTES", "1")
)
SIGNAL_END_BUFFER_BEFORE_SESSION_CLOSE_MINUTES = int(
    os.getenv("FABIO_SIGNAL_END_BUFFER_BEFORE_SESSION_CLOSE_MINUTES", "5")
)
OPENING_RANGE_DURATION_MINUTES = int(os.getenv("FABIO_OPENING_RANGE_DURATION_MINUTES", "15"))
ENTRY_STALE_BLOCK_SEC = 10 * 60
TELEGRAM_CMD_MIN_INTERVAL_SEC = 2.0
TELEGRAM_STOP_CONFIRM_TTL_SEC = 30
OPTIONS_ONLY_EXECUTION = os.getenv("FABIO_OPTIONS_ONLY_EXECUTION", "1") == "1"
AUTO_ADOPT_OPEN_POSITIONS = os.getenv("FABIO_AUTO_ADOPT_OPEN_POSITIONS", "1") == "1"
# Slice 4: MR paper auto-trade. Default off — ORB loop unchanged.
MR_PAPER_ENABLED = os.getenv("FABIO_MR_PAPER_ENABLED", "0") == "1"
MR_CB_PARTITION = os.getenv("FABIO_MR_CB_PARTITION", "0") == "1"
# Hard isolate: deny MR entries on the ORB universe unless explicitly allowed.
MR_ALLOW_ORB_SYMBOLS = os.getenv("FABIO_MR_ALLOW_ORB_SYMBOLS", "0") == "1"

CB_DAILY_LOSS_PCT = CFG.cb_daily_loss_pct
CB_MAX_TRADES = CFG.cb_max_trades
CB_MAX_LOSS_STREAK = CFG.cb_max_loss_streak
CB_MAX_OPEN_POS = CFG.cb_max_open_pos

TRIM_MULTIPLE = CFG.trim_multiple
TRIM_PCT = CFG.trim_pct
