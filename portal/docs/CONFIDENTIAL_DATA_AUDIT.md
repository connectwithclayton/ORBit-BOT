# Confidential and local-only data (ORBit / public GitHub)

This audit lists what **must not** appear in a public repository. Patterns are enforced in [`.gitignore`](../../.gitignore) at the Fabio tree root (same paths when this folder is the Git repo root, e.g. on ORBit).

| Category | Examples | Risk |
|----------|----------|------|
| Environment | `.env`, `.envrc`, `fabio.env`, `*env*copy*.txt` | API keys, Telegram tokens, sheet IDs, passwords |
| Broker / OAuth files | `google_credentials.json`, `*credentials*.json`, `token.json` | Account access |
| Key material | `*.pem`, `*.key`, `id_rsa`, `id_ed25519` | Signing / SSH |
| Runtime state | `backend/trade_data.json`, `*.log`, `audit_sync*.jsonl`, `bot_health_snapshots.jsonl`, `frontend/bot_live_status.json`, `frontend/bot_ops_feed.json` | Positions, PnL, operational telemetry |
| Generated outputs | `results/`, `Fabio_backtest_*.csv`, `*.png` backtest charts, `*_dashboard.html` | Proprietary results; clutter |
| Tooling | `__pycache__/`, `.venv/`, `.pytest_cache/` | Noise |

**Safe to commit:** `portal/.env.example` (placeholders only), `portal/tooling/.secrets.baseline` (detect-secrets hashes), `backend/requirements*.txt` / `backend/requirements.lock`, source under `backend/`, `frontend/`, `portal/`.

**Verify before push:** `git ls-files` must not list `.env` or credential JSON; use `git check-ignore -v <path>` for probes. If a secret was ever committed, rotate the credential and rewrite Git history (see `portal/SECURITY_RUNBOOK.md`).
