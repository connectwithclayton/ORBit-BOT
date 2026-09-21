# Morning Audit

Copy/paste checklist to verify bot health, dashboard freshness behavior, and low-overlap execution before market activity.

Use the **Fabio_bot** project root (for example `~/Documents/TRADING/Fabio_bot` or your monorepo path such as `.../Cursor Projects/Fabio_bot`). From that directory, set `export PYTHONPATH=backend:frontend` (launchd installers in `portal/` set this for scheduled runs).

Session and EOD clock rules (holidays, early close): [EXCHANGE_CALENDAR.md](EXCHANGE_CALENDAR.md).

## TL;DR

```bash
cd "/path/to/Fabio_bot"
export PYTHONPATH=backend:frontend
python3 backend/print_effective_config.py
python3 backend/verify_phase2_reliability.py
python3 backend/orb_bot_fabio.py
```

In a second terminal:

```bash
cd "/path/to/Fabio_bot"
tail -f bot_health_snapshots.jsonl
```

Local dashboard (not Pages): the desk writes `frontend/live_dashboard.html` from `frontend/templates/live_dashboard_template.html` plus gitignored `frontend/bot_live_status.json`. **`file://` will not poll.** From `frontend/` run `python3 -m http.server 8000` and open `http://127.0.0.1:8000/live_dashboard.html`. Do not commit regenerated HTML; `portal/push_dashboard.sh` no longer auto-commits it.

## 1) Preflight (run first)

```bash
cd "/path/to/Fabio_bot"
export PYTHONPATH=backend:frontend
python3 backend/print_effective_config.py
python3 backend/verify_phase2_reliability.py
```

### Pass criteria
- No hard-stop reliability failures.
- Config mode and env values match expected session setup.

## 2) Start bot

```bash
cd "/path/to/Fabio_bot"
export PYTHONPATH=backend:frontend
python3 backend/orb_bot_fabio.py
```

### If the bot shows `Skipped — bot paused` immediately

1. Check the first `[Startup] STARTUP_PREFLIGHT …` line: `broker_open_positive_qty` vs `adoptable_precheck`.
2. Check Sheets `Alerts` (or console) for `RECONCILE` / `DATA_FEED` rows with tags like `[reconcile_auto_adopt_none]` or `[vix_unavailable]`.
3. Send Telegram `/status` and read `PauseReason`, `PauseHint`, and `Startup reconcile pending rows`.
4. **Fix root cause before relying on `/resume` alone** (bad option cost basis in broker snapshot, orphan positions you did not intend, or VIX/data feed). Full table and recovery order: [README.md — Startup paused: reconcile triage](../../README.md#startup-paused-reconcile-triage).

## 3) Monitor health snapshots

In another terminal:

```bash
cd "/path/to/Fabio_bot"
tail -f bot_health_snapshots.jsonl
```

## 4) Ops counters to watch

From each snapshot, inspect `ops`:

- `queue_depth`
- `errors`
- `dropped_noncritical`
- `coalesced_updates`
- `dashboard_intraday_refresh_requests`
- `dashboard_intraday_refresh_enqueued`
- `dashboard_intraday_refresh_throttled`
- `dashboard_open_refresh_requests`
- `dashboard_open_refresh_enqueued`
- `dashboard_open_refresh_throttled`

Also inspect `position_parity` (same NDJSON cadence as the rest of the snapshot):

- **`parity_ok`**: `position_list_query` open option rows **in the configured Fabio symbol universe** match in-memory **`OrderManager`** / strategy book (`code` → contracts, qty &gt; 0).
- **`query_ok`**: broker query succeeded; when `false`, treat parity as failing until connectivity recovers.
- **`drifts`**: list of `{code, broker_qty, tracked_qty}` when the broker and tracked maps disagree.

Log lines **`Position open — holding`** in `orb_bot_fabio.log` refer to **tracked** opens (session memory), **not** a live broker poll on every loop. Dashboard **open_positions** / reconcile pipelines use **Moomoo** as source of truth for the published ledger; stale **tracked** rows after closing outside the bot should show up as **`position_parity`** drift until you restart once the broker book is correct (see README operations).

Manual broker vs canonical checks:

```bash
cd "/path/to/Fabio_bot"
export PYTHONPATH=backend:frontend
python3 backend/reconcile_moomoo_to_sheets.py --dry-run
python3 backend/audit_full_positions.py
```

## 5) Healthy vs warning patterns

### Healthy
- `queue_depth` stays low/oscillating (not steadily climbing).
- `errors` remains unchanged.
- `dashboard_*_requests` increases during activity.
- `dashboard_*_throttled` is non-zero (expected; throttle protection active).
- `dashboard_*_enqueued` grows slower than requests.

### Warning
- `queue_depth` trends upward continuously.
- `errors` increases repeatedly.
- `dropped_noncritical` spikes quickly.
- `dashboard_*_enqueued` almost equals requests while queue also grows.

## 6) Quick mitigation (no code changes)

If warnings appear, restart from a shell that sets slower dashboard refresh cadence:

```bash
cd "/path/to/Fabio_bot"
export PYTHONPATH=backend:frontend
export FABIO_DASHBOARD_REFRESH_THROTTLE_SEC=5
export FABIO_DASHBOARD_OPEN_REFRESH_THROTTLE_SEC=120
python3 backend/orb_bot_fabio.py
```

If still noisy:

```bash
cd "/path/to/Fabio_bot"
export PYTHONPATH=backend:frontend
export FABIO_DASHBOARD_REFRESH_THROTTLE_SEC=15
export FABIO_DASHBOARD_OPEN_REFRESH_THROTTLE_SEC=300
python3 backend/orb_bot_fabio.py
```

## 7) End-of-day operational sequence (safe order)

```bash
cd "/path/to/Fabio_bot"
export PYTHONPATH=backend:frontend
python3 backend/verify_phase2_reliability.py
python3 backend/reconcile_moomoo_to_sheets.py --dry-run
python3 backend/reconcile_moomoo_to_sheets.py
python3 backend/scripts/audit_moomoo_sync.py --eod --lookback-min 480
bash backend/scripts/summarize_audit_sync.sh audit_sync.jsonl
```

## 8) Fast triage commands

```bash
cd "/path/to/Fabio_bot"
tail -f orb_bot_fabio.log
tail -f audit_sync_scheduler.log
```

## 9) Open-position entry overlay note

- Open inventory in dashboard remains broker-truth.
- `Entry`, `Entry $`, `VIX`, and `OR/ATR` are display-only enrichment from in-memory runtime state when available.
- After restart/adopt scenarios, these fields can temporarily fall back to broker-derived placeholders until fresh in-session metadata is tracked.

