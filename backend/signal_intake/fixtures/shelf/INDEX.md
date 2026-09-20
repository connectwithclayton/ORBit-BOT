# Market Rebellion email shelf

Anonymized fixtures for Fabio/ORBit MR intake (vendor alerts; no captain PII).

## Categories seen
- **RRP Morning Note** — macro/overnight (not trade intents)
- **RR Rebel Roundup** — newsletter (not trade intents)
- **RRP Buy / BUY** — primary entry alerts (parse these)
- **RRP Trade Log** — often mirrors Buy; dedupe by symbol+expiry+strike
- **RRP Lock in** — take-profit / exit alerts
- **Model Portfolio / On the Horn** — context only

## Fixtures
See `*.json` in this directory.

## Live query (docs only — no poller in this repo)

`from:subscription-alerts@marketrebellion.example.invalid newer_than:90d`
