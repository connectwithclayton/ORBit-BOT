# Paper-book fail-safe flatten (four jobs)

Independent **paper-only** fail-safe jobs, one per book. If `orb_bot_fabio.py` is dead before `eod_close_all` (~15:45 ET), these still flatten **that book’s ledger codes**. Sibling books are not closed by another book’s job.

Positions are closed in the market (sell longs / buy shorts) or expire worthless. **No exercise.** No live / REAL. No dashboard / Pages / Discord.

## Install (Mac trading host)

Host timezone must be **`America/New_York`**. launchd `Hour`/`Minute` use the Mac’s local clock.

```bash
# Preview four labels + argv (each contains --book <that id>)
PYTHONPATH=backend:frontend bash portal/install_paper_flatten_scheduler.sh --dry-run

# Install all four jobs (always all ids; disabled books are cheap no_closable)
PYTHONPATH=backend:frontend bash portal/install_paper_flatten_scheduler.sh

launchctl list | grep claytonorb.paper.flatten
```

| launchd label | `--book` | Typical weekday fire (ET) |
|---|---|---|
| `com.claytonorb.paper.flatten.orb-moomoo` | `orb-moomoo` | 15:50 |
| `com.claytonorb.paper.flatten.mr-moomoo` | `mr-moomoo` | 15:52 |
| `com.claytonorb.paper.flatten.orb-tradier` | `orb-tradier` | 15:54 |
| `com.claytonorb.paper.flatten.mr-tradier` | `mr-tradier` | 15:56 |

Moomoo jobs are staggered first so two OpenD `position_list_query` / `place_order` runs are not in the same minute. Tradier follows.

**Early-close (Slice 1 accepted gap — follow-up):** launchd uses **fixed** 15:50–15:56 ET weekday clocks, not `session_close_et` minus 10 minutes. On a 13:00 early close the fail-safe *window* is already ~12:50; these jobs still fire at 15:50–15:56 (after the session). `--require-after-et` allows that late run. Do **not** expect Slice 1 to move launchd minutes with the calendar. Session_close-relative / calendar-aware fire times are a follow-up.

Wrappers: [`portal/run_paper_flatten_book.sh`](../run_paper_flatten_book.sh). **`--book` is required** on the launchd argv. Inner Python argv is built by [`fabio_live.paper_flatten_jobs`](../../backend/fabio_live/paper_flatten_jobs.py) and always includes `--book <id> --require-after-et --scope options --log-format jsonl`. **Moomoo** jobs also pin `--trd-env SIMULATE` so a host with `MOOMOO_TRADE_ENV=REAL` still paper-flattens instead of refusing. Wrappers do **not** pass `--trd-env REAL`, `--env live`, or `FABIO_ALLOW_REAL_TRADING`.

Uninstall:

```bash
bash portal/install_paper_flatten_scheduler.sh --uninstall
```

## Gates and exit codes

- NYSE non-session (holiday / weekend): wrapper `should-run-failsafe` **skips** (exit 0). No crash-loop (`KeepAlive` is false).
- Before fail-safe cutoff (e.g. 10:00 ET on a session day): flatten script `--require-after-et` exits **4**, sidecar `reason=aborted_window`. Wrapper maps 4 → **0** (skip, not an error loop).
- Empty own ledger: exit **0**, sidecar `no_closable`, **no** `place_order`. That is **not** firm-flat — check the sibling book’s sidecar.
- Partial `place_order` failures: exit **3** (inspect that book’s JSONL).

Fail-safe is **not** invoked from `eod_close_all`. Primary flatten and these jobs are separate processes.

## JSONL (gitignored, per book)

Repo-root files, not mixed into one firm log:

- `eod_failsafe.orb-moomoo.jsonl`
- `eod_failsafe.mr-moomoo.jsonl`
- `eod_failsafe.orb-tradier.jsonl`
- `eod_failsafe.mr-tradier.jsonl`

```bash
./scripts/summarize_failsafe_jsonl.sh eod_failsafe.mr-moomoo.jsonl --book mr-moomoo
```

Last-flatten sidecars (already shipped): `backend/paper_book_ledgers/<book_id>.last_flatten.json`. `--book mr-moomoo` never writes `orb-moomoo.last_flatten.json`.

## Manual verify (bot killed before 15:45)

On a **paper** session, with OpenD / Tradier sandbox as needed:

1. Confirm the four labels are loaded (`launchctl list | grep claytonorb.paper.flatten`). Dry-run first if this is a new host.
2. Put a listed-option code on **one** book’s ledger only (the other sibling ledger empty or a different code).
3. **Kill the desk bot** before primary EOD (`pkill -f orb_bot_fabio.py` or skip starting it). Primary `eod_close_all` must not run.
4. After that book’s fail-safe fire (or run the wrapper once after the cutoff):
   ```bash
   PYTHONPATH=backend:frontend bash portal/run_paper_flatten_book.sh --book orb-moomoo
   ```
   On a live session after ~15:50 ET this market-closes **orb-moomoo ledger codes only**.
5. Confirm:
   - That book’s sidecar `layer=failsafe` and `reason=flatten` (or `no_closable` if already flat).
   - Sibling ledger codes **remain** (other Moomoo book / other Tradier book not flattened).
   - JSONL for that `book_id` only.

Dry-run without placing:

```bash
PYTHONPATH=backend:frontend python3 backend/moomoo_eod_failsafe.py --dry-run --book orb-moomoo --require-after-et --scope options --log-format jsonl --trd-env SIMULATE
PYTHONPATH=backend:frontend python3 backend/tradier_eod_flatten.py --dry-run --book orb-tradier --require-after-et --scope options --log-format jsonl
```

Always pass `--book`. Do not omit it (argparse defaults to the ORB book on that firm).

## Out of scope here

`force_close.py` is still whole-account Moomoo — do not use it for per-book paper flatten. Slice 2. No live funds.
