# Fabio ORB backtest

[![Release channel — BETA](https://img.shields.io/badge/release-BETA-FFD740?logo=git&logoColor=black)](portal/beta_manifest.json)

**Canonical GitHub (Fabio / ORBit bot):** [ORBit-BOT](https://github.com/connectwithclayton-cpu/ORBit-BOT) — treat this tree as the **repo root** there. Day-to-day: commit here and `git push origin main` so collaborators always see what you are working on.

**ORBit + Pages (optional mirror):** [ORBit](https://github.com/connectwithclayton-cpu/ORBit) · [GitHub Pages](https://connectwithclayton-cpu.github.io/ORBit/) — same codebase layout when synced; update separately if you still maintain that export.

Python backtest for a **Fabio / ORBit-style opening range breakout** strategy with **0DTE-style options simulation** (Black–Scholes, fixed DTE, slippage, commissions). This tree also contains related **live** helpers under `backend/` (entry `backend/orb_bot_fabio.py`), dashboard/Sheets under `frontend/`, and operators/publish tooling under `portal/`. The main research entry point is `backend/backtest/Fabio_orb_backtest.py`.

**Layout:** `backend/` (live bot, reconcile, `moomoo_eod_failsafe.py`, `requirements*.txt`, `pytest.ini`, **`backtest/`** for research engine + CLI runners), `frontend/` (dashboard HTML/JSON, `sheets_logger.py`, `debug_board_writer.py`, `fabio_beta_identity.py`, `manual_position_omissions.py`), `portal/` (schedulers, `push_dashboard.sh`, `beta_manifest.json`, `docs/`, `portal/.env.example`, `portal/tooling/` for pre-commit + detect-secrets baseline), `docs/` ([`GOOGLE_SETUP.md`](docs/GOOGLE_SETUP.md), [`docs/architecture/`](docs/architecture/README.md)), `scripts/` (e.g. [`summarize_failsafe_jsonl.sh`](scripts/summarize_failsafe_jsonl.sh)), [`Fabio History/`](Fabio%20History/README.md) (archival / reference policy). From this directory run Python with `PYTHONPATH=backend:frontend` (set automatically in CI and in `backend/pytest.ini` for tests).

**Root files outside those folders:** `README.md` (this file), machine-local **`.env`** (gitignored), **`.gitignore`**, and **`.github/`** (CI workflows — GitHub requires this path at the repo root).

**Beta tracking:** Channel is **BETA**. Committed milestones (rolling **last 3**) live in [`portal/beta_manifest.json`](portal/beta_manifest.json). The live dashboard and debug board show **BETA** plus the short `git` revision used at HTML generation time. Record a new milestone after meaningful changes: `PYTHONPATH=backend:frontend python3 portal/record_beta_milestone.py "optional note"`, then commit.

**Disclaimer:** Backtests are not predictions. Past results do not guarantee future performance. This is research tooling, not financial advice.

### This folder as the Git root (not the parent `Cursor Projects` repo)

If **`Fabio_bot/`** still lives inside a monorepo whose `.git` is **above** this folder, make **this directory** the repository once:

1. Open **Terminal.app** (or iTerm), **not** the restricted Cursor agent shell, if `git init` errors on `.git/hooks`.
2. From this directory run:

```bash
bash portal/tooling/init_standalone_repo.sh
```

3. Then commit the parent cleanup and push Fabio (the script prints the exact `cd` / `git commit` / `git push` lines).

After that, **open only `Fabio_bot/`** in Cursor or GitHub Desktop so `.git` lives here and `git status` shows this tree.

---

## Requirements

- **Python 3.10+** (the script uses modern type hints such as `str | None`). **GitHub Actions CI uses Python 3.11** — use **3.11** locally for the closest match to CI. On **3.13+**, some third-party libraries (e.g. protobuf via `moomoo-api`) may emit `DeprecationWarning` at import time; the test suite configures pytest to filter the known protobuf case only.
- Install core dependencies (versions are **pinned** in `backend/requirements.txt` for reproducibility):

```bash
pip install -r backend/requirements.txt
```

- For an **exact** transitive snapshot (as produced by `pip freeze` after installing core deps), use:

```bash
pip install -r backend/requirements.lock
```

- Optional integrations (Google Sheets logging):

```bash
pip install -r backend/requirements-optional.txt
```

- Tests and dev tools (`backend/requirements-dev.txt` **includes** core deps via `-r requirements.txt`; one install is enough for `pytest`):

```bash
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend:frontend python3 -m pytest -c backend/pytest.ini -v
```

To **refresh** pins after upgrading packages: reinstall into a clean venv, run `pip freeze > backend/requirements.lock`, and update `==` lines in `backend/requirements.txt` / `backend/requirements-optional.txt` / `backend/requirements-dev.txt` for the packages you care about.

### Post–EOD Moomoo fail-safe (broker flatten)

This tree ships **`backend/moomoo_eod_failsafe.py`**. It is a **separate scheduled process** from the ORBit bot—run after your primary EOD close. Install pins with `backend/requirements-moomoo.txt` (aligned with `backend/requirements.txt` for `moomoo-api`).

```bash
pip install -r backend/requirements-moomoo.txt
python3 backend/moomoo_eod_failsafe.py --dry-run
python3 backend/moomoo_eod_failsafe.py --dry-run --require-after-et
```

**Paper pin (do not skip):** `--trd-env` defaults to **`SIMULATE`**, reading **`MOOMOO_TRADE_ENV`** first (same name as the live bot), then the legacy alias **`MOOMOO_TRD_ENV`**. It does **not** silently default to `REAL`. Targeting a live Moomoo book additionally requires `FABIO_ALLOW_REAL_TRADING=1`. Do not set that flag for paper trading. This script closes in the market only; it does not exercise options.

<a id="exit-codes-moomoo_eod_failsafepy"></a>

**Exit codes (`backend/moomoo_eod_failsafe.py`):**

| Code | Meaning |
|------|---------|
| `0` | Success: empty book, nothing closable after filters, dry-run only, or live run with **no** failed `place_order` responses. |
| `1` | Error: missing `moomoo` package, `zoneinfo` unavailable when using `--require-after-et`, missing live password, `unlock_trade` failed, initial `position_list_query` failed, or invalid `--security-firm`. |
| `2` | **Invalid CLI** (Python `argparse`): unknown flag or bad argument value. |
| `3` | Live run reached `run_complete` but **one or more** `place_order` calls failed; see logs or JSONL (`reason_code` / `run_partial_failure` event). |
| `4` | Aborted: `--require-after-et` but current US/Eastern time is not a weekday after your cutoff (intentional guard). |

Use `python3 backend/moomoo_eod_failsafe.py --help` for full flags. Schedulers: alert on `1` or `3`; treat `4` as outside the ET window unless the schedule is wrong.

---

## Quick start

From this directory:

```bash
PYTHONPATH=backend:frontend python3 backend/backtest/Fabio_orb_backtest.py
```

The script prints a summary to the console and writes CSVs and a chart (see [Outputs](#outputs)).

Pre-flight config check (recommended before live runs and in your pre-open ritual):

```bash
PYTHONPATH=backend:frontend python3 backend/print_effective_config.py
```

If `MOOMOO_TRADE_ENV=REAL`, this script prints a **prominent warning** first; otherwise the sanity summary states **SIMULATE**. The live bot still **refuses REAL** unless `FABIO_ALLOW_REAL_TRADING=1` (paper-only default — do not set that flag). The sanity summary also prints **`strategy_capital_cap` ($10,000)** vs the **`risk_base` ceiling ($20,000)**.

### Modeled paper equity (Moomoo SIMULATE)

Moomoo paper `total_assets` is often a large notional (e.g. \$1M). The live bot reads `accinfo_query` through `get_portfolio_value()` in [`backend/fabio_live/market_data.py`](backend/fabio_live/market_data.py) and, **in SIMULATE only** (unless you override), applies:

`modeled_equity = FABIO_DISPLAY_EQUITY_START + (raw_total_assets − FABIO_MOOMOO_REFERENCE_EQUITY)`

Defaults: **\$10,000** modeled book vs **\$1,000,000** broker reference (equivalent to subtracting **\$990,000** from raw `total_assets` at that reference).

- **Unchanged:** Per-fill and per-trade dollar P&amp;L rows (Sheets, reconcile FIFO, dashboard legs) stay **broker-scale**.
- **Adjusted:** Reported **capital** end-of-day, **daily return %** denominators, circuit-breaker **daily loss %** base, and the **`risk_base`** sizing input use the modeled equity.

**Sizing (`risk_base`) is not the $10k cap.** Operators: do not size as if the book were capped at `strategy_capital_cap` ($10,000).

```
risk_base    = min(portfolio, strategy_capital_cap × research_risk_capital_multiplier)
             = min(portfolio, $10,000 × 2.0)
             = min(portfolio, $20,000)
risk_dollars = risk_base × risk_pct
```

`$10,000` is the strategy / modeled-book cap. The live and research sizing ceiling is **$20,000** of modeled equity. `print_effective_config.py` prints both numbers.

| Variable | Default | Meaning |
|----------|---------|---------|
| `FABIO_MOOMOO_REFERENCE_EQUITY` | `1000000` | Raw Moomoo equity taken as the reference point |
| `FABIO_DISPLAY_EQUITY_START` | `10000` | Modeled starting book when raw equals the reference |
| `FABIO_MODELED_EQUITY_ENABLED` | unset → **on** for SIMULATE, **off** for REAL | Set `1` / `0` to force |

---

## Backtest runners

- `backend/backtest/Fabio_orb_backtest.py` — primary **research** backtest (source of truth). Uses `OpeningRangeStyle.RESEARCH` in the engine: OR = **09:30–09:44 ET** on **5-minute** bars (same definition as the **current** live bot in `backend/fabio_live/regime.py`).
- `backend/backtest/Fabio_live_mirror_backtest.py` — **legacy live-mirror** backtest: uses `OpeningRangeStyle.LIVE_MIRROR` in `backend/backtest/fabio/regime.py` (OR window **09:30–09:40 ET** on 5m data). This approximates an **older** live-bot OR definition; it does **not** match today’s live bot, which was aligned back to research.
- `backend/backtest/FabioOrb_copy_backtest.py` — compatibility wrapper alias to `Fabio_orb_backtest.py` (kept to avoid drift from duplicate code).

---

## Data sources

**Precedence (research runner `backend/backtest/Fabio_orb_backtest.py` only):**

1. `FabioBacktestSettings.from_env()` reads optional env `FABIO_DATA_SOURCE` (`polygon` or `yfinance`) into the config object.
2. The module then runs **`_cfg.data_source = DATA_SOURCE`**, where `DATA_SOURCE` is the constant near the top of `backend/backtest/Fabio_orb_backtest.py`.

So the **in-file `DATA_SOURCE` always wins** for that script. Env `FABIO_DATA_SOURCE` affects other callers of `FabioBacktestSettings.from_env()` (for example `backend/print_effective_config.py` and live `backend/fabio_live/constants.py`) but **not** the research backtest’s data source unless you change or remove the override in `backend/backtest/Fabio_orb_backtest.py`.

| Value | Behavior |
|-------|----------|
| `polygon` | Uses [Polygon.io](https://polygon.io/) for daily + intraday bars (recommended for full history). VIX still comes from **yfinance**. |
| `yfinance` | Uses **yfinance** only (simpler setup; intraday history may be more limited). |

If the effective source is `polygon` but no API key is set, the loader **falls back to yfinance** and prints a warning.

---

## Environment (Polygon)

Use [`portal/.env.example`](portal/.env.example) as a template and prefer storing real secrets outside the repo tree.

- Preferred: set `FABIO_ENV_FILE=/secure/path/fabio.env` and keep that file outside this project.
- Legacy fallback: `Fabio_bot/.env` is still supported for local compatibility, but not recommended.

Add:

```env
POLYGON_API_KEY=your_polygon_key_here
# Optional: polygon | yfinance — used by FabioBacktestSettings.from_env() (see Data sources for research script override)
FABIO_DATA_SOURCE=polygon

# Optional: append NDJSON timing lines from Fabio_orb_backtest / legacy mirror (default: off)
# FABIO_BACKTEST_DEBUG_LOG=./fabio_backtest_debug.ndjson
```

Never commit real API keys. Keep `.env` out of git.

Also treat `google_credentials.json` and any service-account credential files as secrets; they should never be committed.

---

## Configuration (edit the script)

Strategy tunables live in `backend/backtest/fabio/settings.py` (`FabioBacktestSettings`) and are shared by backtests + live bot.

`config.py` is integrations-only (Moomoo/Telegram/optional Google env values).

**Data source:** Prefer editing `DATA_SOURCE` in `backend/backtest/Fabio_orb_backtest.py` for the research backtest, or set `FABIO_DATA_SOURCE` in `.env` for tools that only call `from_env()` (see [Data sources](#data-sources)).

Primary strategy knobs:

- **Universe & dates:** `SYMBOLS`, `START_DATE`, `END_DATE`
- **Capital:** `INITIAL_CAPITAL` (backtest start, $10,000). **`strategy_capital_cap` ($10,000)** is the strategy / modeled-book cap — **not** the live sizing ceiling. **`risk_base = min(portfolio, $20,000)`** because `research_risk_capital_multiplier` defaults to **2.0**.
- **Risk (Fabio v5 — half of ORB-style defaults):** `RISK_PCT_*`, `RISK_PCT_MAX`
- **VIX tiers:** `VIX_SKIP`, `VIX_HALF_MAX`, `VIX_NORMAL_MAX`, `VIX_AGGRESSIVE_MAX`
- **Gap / OR quality:** `GAP_SKIP_PCT`, `GAP_RETEST_PCT`, `OR_SKIP_PCT_ATR`, `OR_NORMAL_MIN_ATR`, `OR_WIDE_PCT_ATR`
- **Circuit breakers:** `CB_DAILY_LOSS_PCT`, `CB_MAX_TRADES`, `CB_MAX_LOSS_STREAK`, `CB_MAX_OPEN_POS`
- **Exits / trims:** `TRIM_MULTIPLE`, `TRIM_PCT`, `PROFIT_LOCK_MULTIPLE`
- **Options model:** `IV_BASE`, `OPTION_DTE`, `SLIPPAGE_PCT`, `COMMISSION`

Some comments in the file are marked **TODO** if you want to align thresholds exactly with your written rule set.

---

## Strategy rule subset (as implemented)

This mirrors the logic in `check_entry`, `check_exit`, `DayRegime`, and `run_backtest`:

### Regime / filters

- **Opening range (OR), research + current live bot:** High/low of **5-minute** bars from **09:30–09:44 ET** (`DayRegime` / `MarketRegime` research path). The **legacy live-mirror** backtest uses **09:30–09:40 ET** only (`OpeningRangeStyle.LIVE_MIRROR`); use that runner only for historical comparison, not for parity with today’s bot.
- **VIX:** Risk scaling by VIX band; trades skipped if VIX is below `VIX_SKIP`.
- **Gap:** Skip day if gap ≥ `GAP_SKIP_PCT`. Moderate gaps (`GAP_RETEST_PCT`–`GAP_SKIP_PCT`) require a **retest** near the OR boundary before a breakout counts.
- **OR width vs ATR:** `or_size_factor` scales or skips entries based on OR size relative to ATR.
- **Trend:** Daily EMA10 > EMA20 and close > EMA50 defines **bullish** regime.
- **Counter-trend:** **Disabled** — no trades against the daily trend.
- **Fabio v5 VIX filters (backtest loop):**
  - VIX &lt; ~16.1: **no trades**
  - VIX in elevated band (20–28): **CALL** entries skipped (PUTs allowed)

### Entry (ORBit checklist–style)

- **CALL:** Two consecutive **5-minute closes** above OR high.
- **PUT:** Two consecutive **5-minute closes** below OR low.
- **Window:** Signals scanned **09:45–14:00** ET.

### Exit simulation

- **Bars:** Prefers **3-minute** bars after entry for exit logic; falls back to resampled 5-minute if 3-minute data is missing.
- **Strategy exit:** (1) Two consecutive closes on the wrong side of **OR midpoint**, or (2) **EMA10 vs EMA20** crossover on the exit bar series.
- **Profit lock:** If modeled option P&amp;L reaches `PROFIT_LOCK_MULTIPLE` × entry premium, strategy exits are **disabled** until EOD; **2×ATR** stock stop still applies.
- **Hard stop:** **2×ATR** (daily ATR) move against the position on the underlying.
- **Trim:** Partial exits at multiples of entry premium (`TRIM_*`).
- **EOD:** Positions closed by end of session logic in the loop.

---

## Outputs

Written to the **current working directory** when you run the script (usually this folder):

| File | Contents |
|------|----------|
| `Fabio_backtest_trades.csv` | One row per trade |
| `Fabio_backtest_equity.csv` | Daily equity curve |
| `Fabio_backtest_report.png` | Equity, monthly P&amp;L, win rate, exits, histogram, rolling Sharpe |

---

## Other files in this project (pointers)

| File | Role |
|------|------|
| `backend/orb_bot_fabio.py` | Live bot entrypoint (loads `.env`, runs `ORBBot`) |
| `backend/fabio_live/` | Live stack: `constants`, `market_data`, `regime`, `signals`, `orders`, `circuit`, `async_ops`, `bot` |
| `backend/config.py` | Shared configuration for live tooling |
| `backend/backtest/FabioOrb_copy_backtest.py` | Compatibility alias to canonical research backtest |
| `backend/backtest/Fabio_live_mirror_backtest.py` | Legacy live-mirror backtest (`BacktestMode.LIVE_MIRROR`) |
| `frontend/dashboard_writer.py`, `backend/trade_data.json`, `frontend/live_dashboard.html`, `frontend/fabio_live_dashboard.html` | Dashboard pipeline (`live_dashboard.html` is a **committed** shareable snapshot with inlined data; `fabio_live_dashboard.html` is gitignored and regenerated locally beside it) |
| `backend/telegram_bot.py`, `frontend/sheets_logger.py` | Notifications / logging |

Persistent dashboard/trade JSON lives only at **`backend/trade_data.json`** (`dashboard_writer`, `reconcile_moomoo_to_sheets`, `verify_trades`).

---

## Terminal commands (operations)

**Copy-paste:** Prefer fenced `bash` blocks. Avoid command tables because copied `|` separators can break terminal input.

### Safety rails (run first)

Set working directory to the **Fabio_bot** project root (examples):

```bash
cd ~/Documents/TRADING/Fabio_bot
# or, in this monorepo:
# cd "/path/to/Cursor Projects/Fabio_bot"
export PYTHONPATH=backend:frontend
```

Quick environment sanity:

```bash
PYTHONPATH=backend:frontend python3 backend/print_effective_config.py
```

Confirm **SIMULATE**, **paper pin** (`FABIO_ALLOW_REAL_TRADING` not set / not `1`), and **risk_base ceiling $20,000** vs **strategy_capital_cap $10,000**.

Security-first env-file check (recommended):

```bash
FABIO_ENV_FILE=/secure/path/fabio.env PYTHONPATH=backend:frontend python3 backend/print_effective_config.py
```

Verify key defaults are enabled before live execution:

```bash
python3 - <<'PY'
import os
print('FABIO_OPTIONS_ONLY_EXECUTION=', os.getenv('FABIO_OPTIONS_ONLY_EXECUTION','1'))
print('FABIO_AUTO_ADOPT_OPEN_POSITIONS=', os.getenv('FABIO_AUTO_ADOPT_OPEN_POSITIONS','1'))
PY
```

### Runtime operations

Live bot (foreground/debug). Paper pin: refuses `MOOMOO_TRADE_ENV=REAL` unless `FABIO_ALLOW_REAL_TRADING=1`. Do not set that flag for paper.

```bash
PYTHONPATH=backend:frontend python3 backend/orb_bot_fabio.py
```

Follow live bot log:

```bash
tail -f orb_bot_fabio.log
```

Intraday dashboard freshness (isolated from trading loop):

- Ledger mutations (open/trim/close) now enqueue a **coalesced async** dashboard refresh.
- Optional broker open-position snapshot refresh runs via async worker (display-only).
- No blocking dashboard I/O is executed on the trading decision/order path.

Tune cadence with env vars (defaults shown):

```env
FABIO_DASHBOARD_REFRESH_THROTTLE_SEC=2.0
FABIO_DASHBOARD_OPEN_REFRESH_THROTTLE_SEC=60.0
```

Health snapshot counters now include:
- `dashboard_intraday_refresh_requests` / `dashboard_intraday_refresh_enqueued` / `dashboard_intraday_refresh_throttled`
- `dashboard_open_refresh_requests` / `dashboard_open_refresh_enqueued` / `dashboard_open_refresh_throttled`

Each **`bot_health_snapshots.jsonl`** line also carries **`position_parity`**, comparing Moomoo `position_list_query` option opens (symbols in the Fabio universe) against the session **`OrderManager`** book. **`parity_ok`** stays `true` only when broker and tracked qty match per option `code`; **`drifts`** lists `{code, broker_qty, tracked_qty}` when they disagree (for example after closing outside the bot). Sheets may receive **`POSITION_PARITY`** alerts; Telegram uses the same cooldown as other ops warnings (`OPS_ALERT_COOLDOWN_SEC`). See [`portal/docs/Morning-Audit.md`](portal/docs/Morning-Audit.md) for interpreting **`holding`** log lines versus broker truth.

Rollback / reduce refresh pressure safely (no code changes):

- Increase either throttle value (e.g. `30` and `300`) to slow updates.
- Set very large values to effectively disable frequent intraday refresh behavior.
- Canonical EOD/reconcile flows remain unchanged.

Reliability gate snapshot check:

```bash
PYTHONPATH=backend:frontend python3 backend/verify_phase2_reliability.py
```

Debug board HTML (config + artifacts + log tail):

```bash
PYTHONPATH=backend:frontend python3 frontend/debug_board_writer.py
```

### Data sync and canonical reconcile

Dry-run first, then publish canonical tabs and regenerate dashboard:

```bash
PYTHONPATH=backend:frontend python3 backend/reconcile_moomoo_to_sheets.py --dry-run
PYTHONPATH=backend:frontend python3 backend/reconcile_moomoo_to_sheets.py
```

Canonical outputs in Sheets:
- `Broker Fills`
- `Reconciled Trades`
- `Open Inventory`

Precedence notes:
- **`--dry-run`** only prints intentions — it does **not** persist `backend/trade_data.json`, dashboard HTML, or replace canonical tabs (see [portal/docs/audit-runbook.md](portal/docs/audit-runbook.md) publishing rule).
- Successful reconcile sets `backend/trade_data.json` → `open_positions` from FIFO/Open Inventory **after** broker vs FIFO inventory matches.
- Bot-only EOD (without reconcile) sets open positions from Moomoo `position_list_query`.
- `RECONCILE_MISMATCH` skips **both** dashboard persistence and canonical tab replaces until resolved.
- If `position_list_query` returns **no rows** for SIMULATE while FIFO still infers opens (often OpenD paper), set **`FABIO_RECONCILE_TRUST_FIFO_IF_BROKER_QUERY_EMPTY=1`** and rerun reconcile so **closed trades**, **Broker Fills**, **Reconciled Trades**, and **Open Inventory** publish; dashboard **`open_positions`** stays empty until broker rows return. With **`FABIO_STRICT_BROKER_POSITION_GATE=1`**, also set **`FABIO_RECONCILE_TRUST_FIFO_IF_BROKER_ROWS_ALL_ZERO_QTY=1`**.

Full-history consistency check:

```bash
PYTHONPATH=backend:frontend python3 backend/audit_full_positions.py
```

Optional gate on the scheduled sync audit log:

```bash
PYTHONPATH=backend:frontend python3 backend/verify_canonical_publish.py --jsonl audit_sync.jsonl --max-age-min 120
```

### Startup paused: reconcile triage

On process start, `ORBBot` queries Moomoo open positions and may **pause new entries** until orphans are safe to track.

**Console / Sheets signals**

- One-line preflight: `[Startup] STARTUP_PREFLIGHT …` (counts of broker opens vs adoptable pre-check).
- Sheets row type `STARTUP_PREFLIGHT` with the same payload.
- Pause rows use bracketed reason tags, e.g. `[reconcile_auto_adopt_none]`, `[reconcile_query_failed]`, `[reconcile_manual_required]`, `[reconcile_exception]`, `[vix_unavailable]` (day init).

**Stable `PauseReason` codes** (also in Telegram `/status` when paused)

| Code | Typical cause |
|------|----------------|
| `reconcile_query_failed` | `position_list_query` returned error — OpenD/API/trd_env |
| `reconcile_manual_required` | Open broker positions but `FABIO_AUTO_ADOPT_OPEN_POSITIONS=0` |
| `reconcile_auto_adopt_none` | Auto-adopt on but zero positions adopted (often missing option cost_basis / bad code / duplicate symbol) |
| `reconcile_exception` | Unexpected error during reconcile path |
| `vix_unavailable` | Day init blocked — VIX feed returned nothing |
| `manual_operator` | Telegram `/pause` |

**Recovery order (recommended)**

1. Read `/status` (or stdout): `PauseReason`, `PauseHint`, and pending unreconciled symbols.
2. In Moomoo, confirm real open qty and that option rows expose a positive **`cost_price` / `average_cost` / `avg_price`** for each adopted contract (auto-adopt needs a valid option premium basis).
3. If inventory should be flat, close or roll in the market so the broker shows **no positive-qty** orphans, then restart.
4. If you intentionally run with manual adoption only, set `FABIO_AUTO_ADOPT_OPEN_POSITIONS=0` knowing the bot **pauses** while orphans exist; otherwise keep `1` after validating broker data.
5. `/resume` clears the pause flag and Telegram pause metadata but **does not fix** bad broker basis; restart after fixing data so startup adopt succeeds.

See also: [portal/docs/Morning-Audit.md](portal/docs/Morning-Audit.md) (preflight + triage), [portal/docs/audit-runbook.md](portal/docs/audit-runbook.md) (broker vs Sheets inventory).

### Moomoo-authoritative sync audit

Read-only audit checks (dry-run -> normal -> EOD):

```bash
PYTHONPATH=backend:frontend python3 backend/scripts/audit_moomoo_sync.py --dry-run --lookback-min 180
PYTHONPATH=backend:frontend python3 backend/scripts/audit_moomoo_sync.py --lookback-min 180
PYTHONPATH=backend:frontend python3 backend/scripts/audit_moomoo_sync.py --eod --lookback-min 480
```

Summarize audit artifacts:

```bash
bash backend/scripts/summarize_audit_sync.sh audit_sync.jsonl
```

Sync audit references:
- `portal/docs/audit-sync-spec.md`
- `portal/docs/audit-runbook.md`

### EOD operational sequence (safe order)

Use this sequence after market-close workflows:

```bash
# 1) Confirm bot health snapshot / no pressure issues
PYTHONPATH=backend:frontend python3 backend/verify_phase2_reliability.py

# 2) Reconcile broker -> canonical tabs (dry-run then publish)
PYTHONPATH=backend:frontend python3 backend/reconcile_moomoo_to_sheets.py --dry-run
PYTHONPATH=backend:frontend python3 backend/reconcile_moomoo_to_sheets.py

# 3) Run EOD sync audit confirmation
PYTHONPATH=backend:frontend python3 backend/scripts/audit_moomoo_sync.py --eod --lookback-min 480

# 4) Review audit summary
bash backend/scripts/summarize_audit_sync.sh audit_sync.jsonl
```

**NYSE session and EOD timing:** Canonical reference is [`portal/docs/EXCHANGE_CALENDAR.md`](portal/docs/EXCHANGE_CALENDAR.md). The primary bot’s scheduled flatten uses **`FABIO_EOD_CLOSE_BEFORE_SESSION_MINUTES`** (default **15**) before official **XNYS** session close (same as legacy 15:45 vs 16:00 on full sessions). The separate **`moomoo_eod_failsafe.py`** process uses **`FABIO_FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES`** (default **10**) before session close.

### Scheduler lifecycle (bot + sync audit)

Install bot weekday scheduler:

```bash
bash portal/install_fabio_scheduler.sh
```

Install sync-audit schedulers (15m + EOD):

```bash
bash portal/install_sync_audit_scheduler.sh
```

Verify launchd jobs are loaded:

```bash
launchctl print "gui/$(id -u)/com.claytonorb.fabio"
launchctl print "gui/$(id -u)/com.claytonorb.fabio.syncaudit.15m"
launchctl print "gui/$(id -u)/com.claytonorb.fabio.syncaudit.eod"
```

Run scheduler smoke tests now (without waiting for trigger times):

```bash
launchctl start com.claytonorb.fabio
bash backend/scripts/run_moomoo_sync_audit.sh
bash backend/scripts/run_moomoo_sync_audit.sh --eod
```

Uninstall schedulers:

```bash
launchctl unload ~/Library/LaunchAgents/com.claytonorb.fabio.plist && rm ~/Library/LaunchAgents/com.claytonorb.fabio.plist
launchctl unload ~/Library/LaunchAgents/com.claytonorb.fabio.syncaudit.15m.plist && rm ~/Library/LaunchAgents/com.claytonorb.fabio.syncaudit.15m.plist
launchctl unload ~/Library/LaunchAgents/com.claytonorb.fabio.syncaudit.eod.plist && rm ~/Library/LaunchAgents/com.claytonorb.fabio.syncaudit.eod.plist
```

### Guarded actions (high impact)

Stop bot process manually (use only when scheduler/controls are insufficient):

```bash
pkill -f orb_bot_fabio.py
```

Push dashboard to **GitHub Pages** for this repo (ORBit-BOT) so you can text a short link:

- **Short link:** [https://connectwithclayton-cpu.github.io/ORBit-BOT/](https://connectwithclayton-cpu.github.io/ORBit-BOT/) — root `index.html` redirects to the live dashboard.
- **Direct:** [https://connectwithclayton-cpu.github.io/ORBit-BOT/frontend/live_dashboard.html](https://connectwithclayton-cpu.github.io/ORBit-BOT/frontend/live_dashboard.html)

After regenerating `frontend/live_dashboard.html`, run:

```bash
bash portal/push_dashboard.sh
```

That commits and pushes dashboard updates to **`origin/main`** (same clone). To publish to a **different** clone instead, set `REPO_DIR` (and optionally `LIVE_REL`) — e.g. legacy flat repo `export REPO_DIR=~/Documents/TRADING/orb-live-dashboard` and `export LIVE_REL=fabio_live_dashboard.html`.

### Shared `live_dashboard.html` (collaborators)

The file **`frontend/live_dashboard.html`** is tracked in Git as a **static snapshot**: chart/table data is **inlined** in the HTML, so someone can open it in a browser with no server and no `trade_data.json` required for viewing.

To **refresh** the snapshot after trading or reconcile, regenerate the dashboard (live bot, `reconcile_moomoo_to_sheets.py`, or any path that invokes `DashboardWriter`), then run **`bash portal/push_dashboard.sh`** (or commit the updated `frontend/live_dashboard.html` yourself).

**Privacy:** The HTML embeds trade and P&amp;L detail. For a **public** GitHub repo + Pages, only push snapshots you are willing to expose; use a private repo or a redacted export if needed.

### Incident triage quick paths

Reconcile mismatch investigation loop:

```bash
PYTHONPATH=backend:frontend python3 backend/reconcile_moomoo_to_sheets.py --dry-run
PYTHONPATH=backend:frontend python3 backend/scripts/audit_moomoo_sync.py --lookback-min 480 --dry-run
bash backend/scripts/summarize_audit_sync.sh audit_sync.jsonl
```

Tail scheduler/audit logs:

```bash
tail -f audit_sync_scheduler.log
```

### Maintenance and quality

Install/update dependencies:

```bash
pip install -r backend/requirements-dev.txt
pip install -r backend/requirements-optional.txt
```

(`backend/requirements-dev.txt` pulls in `requirements.txt`; add `backend/requirements-optional.txt` when using Sheets.)

Syntax checks:

```bash
PYTHONPATH=backend:frontend python3 -m py_compile backend/orb_bot_fabio.py
PYTHONPATH=backend:frontend python3 -m py_compile backend/scripts/audit_moomoo_sync.py
```

Targeted tests and full tests:

```bash
PYTHONPATH=backend:frontend python3 -m pytest -c backend/pytest.ini backend/tests/test_audit_moomoo_sync.py -q
PYTHONPATH=backend:frontend python3 -m pytest -c backend/pytest.ini -v
```

Secrets scan hooks:

```bash
pre-commit install -c portal/tooling/pre-commit-config.yaml
pre-commit run -c portal/tooling/pre-commit-config.yaml --all-files
```

**Note:** launchd jobs run with minimal environment (`PATH=/usr/bin:/bin:/usr/sbin:/sbin`). Keep interpreter paths and env assumptions explicit.

---

## Troubleshooting

- **`No matching distribution for moomoo`:** The PyPI package is **`moomoo-api`** (`import moomoo` unchanged). Use `backend/requirements.txt` as written.
- **`yfinance not found`:** Run the `pip install` line above.
- **Polygon rate limits / slow runs:** The script sleeps between paginated requests (`POLYGON_SLEEP_SEC`). Expect long runs for multi-symbol, multi-year history.
- **No trades:** Tighten filters in CONFIG, shorten the date range, or confirm data loaded (empty intraday → no signals).
- **Reconcile mismatch alert (`RECONCILE_MISMATCH`):** Broker open positions and computed `Open Inventory` disagree. **Both** `backend/trade_data.json`/HTML refresh and canonical Sheets tab replaces are skipped. Re-run reconcile after fixing Moomoo/FIFO inventory visibility, then publish again.

---

## License / use

Use at your own risk. Verify all rules and parameters against your own trading plan before relying on any automation.

## Push to GitHub (ORBit-BOT)

When this directory is the **root** of [ORBit-BOT](https://github.com/connectwithclayton-cpu/ORBit-BOT), collaborators see updates as soon as you push:

```bash
git status
git add -A
git commit -m "Describe your change"
git remote add origin https://github.com/connectwithclayton-cpu/ORBit-BOT.git   # once, if missing
git push -u origin main
```

If you still keep a parent **monorepo** that contains `Fabio_bot/` as a subfolder, you can **extract** full history for this subtree with `git filter-repo --subdirectory-filter Fabio_bot/` on a fresh clone, then add `origin` above and push (one-time migration). For the **pre-built subtree branch** from this monorepo, see [`docs/PUSH_ORBIT_BOT_MIGRATION.md`](docs/PUSH_ORBIT_BOT_MIGRATION.md).

## Repo hygiene

- Use `.gitignore` to exclude secrets and generated outputs (`.env`, credentials JSON, logs, generated CSV/PNG).
- **Before your first `git commit`:** run `git init` (if needed), then `git status` and confirm nothing sensitive or machine-local appears (e.g. `backend/trade_data.json`, logs). Add patterns to `.gitignore` if you introduce new local-only stores.
- Install local secret scanning hooks:
  ```bash
  pip install -r backend/requirements-dev.txt
  pre-commit install -c portal/tooling/pre-commit-config.yaml
  pre-commit run -c portal/tooling/pre-commit-config.yaml --all-files
  ```
- CI now enforces a secret scan (`.github/workflows/secret-scan.yml`) on push/PR.
- If exposure is suspected, follow [`portal/SECURITY_RUNBOOK.md`](portal/SECURITY_RUNBOOK.md) immediately (revoke/rotate first, then verify scans).
- Phase 2 architecture note for exit-timeframe parity is tracked in [`portal/PHASE2_EXIT_TIMEFRAME_DECISION.md`](portal/PHASE2_EXIT_TIMEFRAME_DECISION.md).
- Keep plaintext secrets out of this directory; use `portal/.env.example` and `FABIO_ENV_FILE` for externalized secret paths.
- Runtime telemetry retention:
  - `bot_health_snapshots.jsonl` is local-only and auto-pruned to recent history by the bot.
  - Keep logs/artifacts local and rotate/delete older operational files on a schedule.
- Google Sheets service account setup: [`docs/GOOGLE_SETUP.md`](docs/GOOGLE_SETUP.md).
- Dependency files (under `backend/`):
  - `backend/requirements.txt` (core runtime, pinned)
  - `backend/requirements.lock` (full `pip freeze` after core install — optional stricter reproducibility)
  - `backend/requirements-optional.txt` (Google Sheets extras, pinned)
  - `backend/requirements-dev.txt` (pytest, pre-commit, etc.; includes `requirements.txt` via `-r`)

## Architecture visual

- See [portal/docs/ARCHITECTURE.md](portal/docs/ARCHITECTURE.md) for the system architecture and trading-day workflow diagrams.
- Runtime sessions are evaluated in `America/New_York`; keep host clock synchronized.
