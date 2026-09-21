# Fabio Bot Architecture and Workflow

This document provides a high-level visual of the live system architecture and the intraday execution workflow.

Runtime session boundaries are evaluated in `America/New_York` using **XNYS** (`exchange_calendars`) for open/close times (including early-close days). Primary bot flatten offset defaults to **15 minutes** before official session close (`FABIO_EOD_CLOSE_BEFORE_SESSION_MINUTES`). See [`EXCHANGE_CALENDAR.md`](EXCHANGE_CALENDAR.md). Health snapshots are retained as local telemetry with rolling pruning.

## System Architecture

```mermaid
flowchart TD
    Scheduler["Launcher (manual or schedule)"] --> Entrypoint["backend/orb_bot_fabio.py (runtime guard)"]
    Entrypoint --> Bot["ORBBot (fabio_live/bot.py)"]

    Bot --> QuoteCtx["Moomoo Quote Context"]
    Bot --> TradeCtx["Moomoo Trade Context"]

    Bot --> MarketData["Market Data (fabio_live/market_data.py)"]
    MarketData --> MoomooBars["Moomoo candles (3m,5m,1D)"]
    MarketData --> VixFeed["yfinance VIX (fail-safe blocks entries when unavailable)"]

    Bot --> Regime["MarketRegime (fabio_live/regime.py)"]
    Bot --> Signals["SignalEngine (fabio_live/signals.py)"]
    Bot --> Circuit["RiskCircuitBreaker (fabio_live/circuit.py)"]
    Bot --> Orders["OrderManager (fabio_live/orders.py)"]

    Bot --> AsyncOps["AsyncOpsWorker (bounded queue, coalesce/drop)"]
    AsyncOps --> Telegram["backend/telegram_bot.py"]
    AsyncOps --> Sheets["frontend/sheets_logger.py"]
    AsyncOps --> Dashboard["frontend/dashboard_writer.py"]

    Bot --> HealthSnap["Health Snapshot JSONL"]
    Bot --> LiveStatus["frontend/bot_live_status.json (gitignored, local HTTP)"]
    Bot --> StatusCmd["Telegram /status, /pause, /resume, /stop confirm"]
    Bot --> Reconcile["Startup broker-position reconcile gate"]
```

**Startup reconcile gate:** `ORBBot` runs `position_list_query` at init. With `FABIO_AUTO_ADOPT_OPEN_POSITIONS=1` (default), open option rows must be adoptable (valid US option code, positive qty, positive cost basis fields) or the bot may **pause entries** and log `STARTUP_PREFLIGHT` plus a stable `PauseReason` (see README “Startup paused: reconcile triage”). Day initialization can also pause with `vix_unavailable` if the VIX snapshot cannot be loaded.

## Trading-Day Workflow

```mermaid
flowchart TD
    Start["Bot start"] --> Guard["Runtime guard init (restart/backoff)"]
    Guard --> WaitOpen["Wait for market open"]
    WaitOpen --> Prefetch["OR window prefetch (VIX, portfolio, daily bars)"]
    Prefetch --> InitDay["Initialize regimes + circuit baseline"]

    InitDay --> SignalLoop["Signal loop (5m fresh candles)"]
    SignalLoop --> Tradeable{"Tradeable + CB allows entry?"}
    Tradeable -- No --> SignalLoop
    Tradeable -- Yes --> Breakout{"Breakout + filters pass?"}
    Breakout -- No --> SignalLoop
    Breakout -- Yes --> Enter["OrderManager enter (limit retry / partial fill handling)"]

    Enter --> ExitLoop["Exit loop (3m fresh candles)"]
    ExitLoop --> ExitChecks["2xATR hard stop / OR midpoint / EMA cross / EOD"]
    ExitChecks --> Manage["Record PnL, update CB, log/alert async"]
    Manage --> SignalLoop

    SignalLoop --> Health["Periodic health snapshot + ops health checks"]
    Health --> EOD["EOD close + orphan sweep + summary logs"]
    EOD --> Stop["Graceful shutdown"]
```
