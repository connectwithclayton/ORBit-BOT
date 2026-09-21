# ORBit

**Canonical repo:** [https://github.com/connectwithclayton/ORBit-BOT](https://github.com/connectwithclayton/ORBit-BOT)

ORBit is a **paper-first multi-book trading desk**. Two strategies (opening-range breakout and Market Rebellion paper intake) × two brokers (Moomoo OpenD and Tradier sandbox) = **four isolated paper books**, **$10,000** each.

Each book has its own ledger, risk circuit, and flatten path. Flatten is **never** shared across brokers, and flattening one book must not close another book at the same firm. There is **no live / REAL** trading unless you set the explicit allow flag. Leave it unset.

Research backtests still live under [`backend/backtest/`](backend/backtest/). They are one tool in this repo, not the whole product.

**Disclaimer:** Backtests are not predictions. Past results do not guarantee future performance. This is research and **paper trading** software, not financial advice. Use at your own risk. Positions are opened and closed in the market; this repo does not model or instruct option exercise.

---

## Paper posture

- Default trade env is Moomoo **`SIMULATE`** and Tradier **`paper`** (sandbox). Tradier does **not** read `MOOMOO_TRADE_ENV`.
- Live funds (`MOOMOO_TRADE_ENV=REAL` or Tradier `--env live` / `api.tradier.com`) are **refused** unless `FABIO_ALLOW_REAL_TRADING=1`. Do not set that flag for paper.
- Circuit daily-loss for **all four books** (including ORB-Moomoo) uses the **$10k paper-book start**, not OpenD `get_portfolio_value`.
- The same risk-circuit box gates entries on each book. Confirm the pin with `backend/print_effective_config.py` before a session.

Default boot is **ORB-Moomoo**. `FABIO_MR_PAPER_ENABLED=1` adds the MR-Moomoo book. `FABIO_TRADIER_PAPER_BOOKS=1` constructs the Tradier books (sandbox only). Optional `FABIO_PAPER_BOOKS` can pin an explicit comma list of the four ids below.

| Book | Strategy | Broker | Flatten (that book only) |
|------|----------|--------|--------------------------|
| `orb-moomoo` | ORB | Moomoo SIMULATE | `backend/moomoo_eod_failsafe.py --book orb-moomoo` |
| `orb-tradier` | ORB | Tradier paper | `backend/tradier_eod_flatten.py --book orb-tradier` |
| `mr-moomoo` | MR | Moomoo SIMULATE | `backend/moomoo_eod_failsafe.py --book mr-moomoo` |
| `mr-tradier` | MR | Tradier paper | `backend/tradier_eod_flatten.py --book mr-tradier` |

Flags and exit codes: `--help` on those scripts, not this README.

---

## Layout

| Path | Role |
|------|------|
| [`backend/`](backend/) | Desk runtime, brokers, paper books, tests, pinned requirements |
| [`backend/fabio_live/`](backend/fabio_live/) | Live stack (bot, signals, orders, circuits, paper books) |
| [`backend/backtest/`](backend/backtest/) | Research ORB engine and CLI runners |
| [`frontend/`](frontend/) | Dashboard, Sheets logger, debug board |
| [`portal/`](portal/) | Schedulers, env example, operator docs |
| [`docs/`](docs/) | Architecture and setup notes |
| [`.github/`](.github/) | CI (Python 3.11) and secret scan |

From the **repository root**, run Python with `PYTHONPATH=backend:frontend` (CI and [`backend/pytest.ini`](backend/pytest.ini) do this).

---

## Install and tests

Python **3.10+**. GitHub Actions uses **3.11** — match that locally when you can.

```bash
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend:frontend python3 -m pytest -c backend/pytest.ini
```

- Runtime pins: [`backend/requirements.txt`](backend/requirements.txt). Optional freeze: [`backend/requirements.lock`](backend/requirements.lock).
- Google Sheets extras: [`backend/requirements-optional.txt`](backend/requirements-optional.txt).
- Moomoo fail-safe pins: [`backend/requirements-moomoo.txt`](backend/requirements-moomoo.txt) (aligned with core).

Pre-flight (SIMULATE vs REAL, enabled paper books, masked env):

```bash
PYTHONPATH=backend:frontend python3 backend/print_effective_config.py
```

---

## Env and secrets

Never commit `.env`, API keys, tokens, or `google_credentials.json`. There is **no** root `.env.example`. Copy [`portal/.env.example`](portal/.env.example) to a **secure path outside this tree** and set `FABIO_ENV_FILE` to that file. The example documents **variable names** (Moomoo, Tradier, Polygon, Telegram, Google) — do not paste real values into Git, issues, or this README.

python-dotenv does **not** override keys already in the environment. Flatten scripts (`moomoo_eod_failsafe.py`, `tradier_eod_flatten.py`) XOR: `FABIO_ENV_FILE` if set, else repo-root `.env`. The desk entrypoint (`orb_bot_fabio.py`) always loads repo-root `.env` first, then `config.py` loads `FABIO_ENV_FILE` (if set) without overriding — so overlapping keys in a leftover root `.env` win. When you rely on `FABIO_ENV_FILE` for the desk, remove or empty root `.env` (or export variables only via the shell).

If exposure is suspected, follow [`portal/SECURITY_RUNBOOK.md`](portal/SECURITY_RUNBOOK.md).

---

## Current entrypoints

```bash
# Paper desk (default ORB-Moomoo; paper pin refuses REAL)
PYTHONPATH=backend:frontend python3 backend/orb_bot_fabio.py

# Research backtest (CSV/PNG at repo root, or results/research/<run_id>/ if those filenames already exist)
PYTHONPATH=backend:frontend python3 backend/backtest/Fabio_orb_backtest.py

# Dry-run flatten for one book (never cross-broker; always pass --book)
PYTHONPATH=backend:frontend python3 backend/moomoo_eod_failsafe.py --dry-run --book orb-moomoo
PYTHONPATH=backend:frontend python3 backend/tradier_eod_flatten.py --dry-run --book orb-tradier

# Four paper fail-safe launchd jobs (preview labels + --book argv; Mac host TZ = America/New_York)
PYTHONPATH=backend:frontend bash portal/install_paper_flatten_scheduler.sh --dry-run
```

Independent flatten if the desk bot dies before 15:45 ET: [`portal/docs/Paper-Book-Flatten.md`](portal/docs/Paper-Book-Flatten.md).

Shared strategy tunables: [`backend/backtest/fabio/settings.py`](backend/backtest/fabio/settings.py). Integration env (hosts, tokens): [`backend/config.py`](backend/config.py). Paper-book identity: [`backend/fabio_live/paper_books.py`](backend/fabio_live/paper_books.py).

---

## Local dashboard (not Pages)

The desk renders [`frontend/templates/live_dashboard_template.html`](frontend/templates/live_dashboard_template.html) into **local** `frontend/live_dashboard.html`. Do **not** commit regenerated HTML until four-book UI is ready. [`portal/push_dashboard.sh`](portal/push_dashboard.sh) no longer auto-commits tracked HTML. GitHub Pages / `orbit.clayj.app` stay unwired.

Live status polling reads gitignored `frontend/bot_live_status.json` and `frontend/bot_ops_feed.json` (written from the health snapshot, beside the HTML). **`file://` will not poll** those files. Serve the `frontend/` directory:

```bash
cd frontend && python3 -m http.server 8000
# open http://127.0.0.1:8000/live_dashboard.html
```

There is no `fabio serve` CLI in this repo.

---

## Deeper docs

Do not treat this README as the ops dump. Use the tree:

- Architecture index: [`docs/architecture/README.md`](docs/architecture/README.md)
- System context: [`docs/architecture/architecture-system-context.md`](docs/architecture/architecture-system-context.md)
- Runtime diagrams: [`portal/docs/ARCHITECTURE.md`](portal/docs/ARCHITECTURE.md)
- NYSE session / EOD timing: [`portal/docs/EXCHANGE_CALENDAR.md`](portal/docs/EXCHANGE_CALENDAR.md)
- Four-book paper fail-safe jobs: [`portal/docs/Paper-Book-Flatten.md`](portal/docs/Paper-Book-Flatten.md)
- Morning audit / reconcile: [`portal/docs/Morning-Audit.md`](portal/docs/Morning-Audit.md), [`portal/docs/audit-runbook.md`](portal/docs/audit-runbook.md)
- Google Sheets: [`docs/GOOGLE_SETUP.md`](docs/GOOGLE_SETUP.md)
