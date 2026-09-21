# Exchange calendar (XNYS) and EOD timing

Fabio uses **`exchange_calendars`** calendar **`XNYS`** (NYSE regular sessions) for America/New_York session **open** and **official close**, including **scheduled early-close** sessions.

## Primary trading bot (`orb_bot_fabio.py` / `ORBBot.run`)

- **Opening range end:** session open + `FABIO_OPENING_RANGE_DURATION_MINUTES` (default 15).
- **Entry cutoff:** `effective_signal_end_et` — earlier of (`SIGNAL_END_HOUR` default 14:00 ET) and `session_close_et` minus `FABIO_SIGNAL_END_BUFFER_BEFORE_SESSION_CLOSE_MINUTES` (default 5).
- **Scheduled flatten (`eod_close_all`):** `session_close_et` minus **`FABIO_EOD_CLOSE_BEFORE_SESSION_MINUTES`** — **default 15**, matching legacy fixed **15:45 ET** vs **16:00** full sessions.
- **EOD summary log:** `session_close_et` plus **`FABIO_EOD_SUMMARY_AFTER_SESSION_CLOSE_MINUTES`** (default 1).

launchd still fires weekdays at **09:25** local clock; **`portal/run_fabio_if_nyse_trading_day.sh`** skips non-session days. Manual runs exit early unless **`FABIO_IGNORE_NYSE_CALENDAR=1`**.

## Broker fail-safe (four paper books)

Separate processes from the primary bot (`eod_close_all` does **not** call these). Ledger-scoped `--book` only — not a firm-wide leftover sweep.

Installer: [`portal/install_paper_flatten_scheduler.sh`](../install_paper_flatten_scheduler.sh) (**`--dry-run`** prints four `launchctl` labels; each argv contains `--book <that id>`). Operator notes + manual verify: [`Paper-Book-Flatten.md`](Paper-Book-Flatten.md).

| Typical weekday (host TZ = `America/New_York`) | Label | Script |
|---|---|---|
| 15:50 | `com.claytonorb.paper.flatten.orb-moomoo` | `moomoo_eod_failsafe.py --book orb-moomoo --require-after-et --trd-env SIMULATE` |
| 15:52 | `com.claytonorb.paper.flatten.mr-moomoo` | `moomoo_eod_failsafe.py --book mr-moomoo --require-after-et --trd-env SIMULATE` |
| 15:54 | `com.claytonorb.paper.flatten.orb-tradier` | `tradier_eod_flatten.py --book orb-tradier --require-after-et` |
| 15:56 | `com.claytonorb.paper.flatten.mr-tradier` | `tradier_eod_flatten.py --book mr-tradier --require-after-et` |

**`FABIO_FAILSAFE_CLOSE_BEFORE_SESSION_MINUTES`** (default **10**) before **`session_close_et`**.

With **`--require-after-et`** on the **installed argv**: allowed only **on** NYSE session days and **after** that fail-safe cutoff (e.g. ~15:50 ET on a full day, ~12:50 ET before a 13:00 early close). A 10:00 fire cannot flatten. Wrapper `should-run-failsafe` skips holidays; script exit **4** (`aborted_window`) is treated as skip, not a crash-loop. Use **`--legacy-fixed-cutoff-et`** only for the old Mon–Fri + fixed hour/minute guard. Moomoo scheduled argv pins **`--trd-env SIMULATE`** (never REAL; ignores host `MOOMOO_TRADE_ENV` for that process).

**Early-close fire times (Slice 1 accepted gap):** launchd minutes are **fixed 15:50–15:56 ET**, not computed from `session_close_et`. On early-close days the *window* moves (~12:50) but the jobs still fire at 15:50–15:56. Calendar-aware / session_close-relative fire times are a **follow-up**; this ship does not implement them.

## Sync audit schedulers

Intraday and EOD audit wrappers use **`fabio_live.calendar_gate`**. EOD audit stamp file: **`.sync_audit_calendar_eod_date`** (gitignored). See **`install_sync_audit_scheduler.sh`** for launch times.

## Dependencies

`exchange-calendars` is listed in **`backend/requirements.txt`** and **`backend/requirements-moomoo.txt`** for fail-safe calendar use.
