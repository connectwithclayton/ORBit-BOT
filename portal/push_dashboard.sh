#!/bin/bash
# push_dashboard.sh — PAUSED (SHIP-009).
#
# Do NOT auto-commit tracked frontend/live_dashboard.html until four-book
# captain UI is quality-ready. GitHub Pages and orbit.clayj.app stay unwired.
#
# Local-only write path for operators (no git, no Pages):
#   DashboardWriter renders frontend/templates/live_dashboard_template.html
#   into frontend/live_dashboard.html and frontend/fabio_live_dashboard.html
#   on the trading host (same process as the desk bot / reconcile).
#   Live pill/ops poll gitignored frontend/bot_live_status.json and
#   frontend/bot_ops_feed.json written from the health snapshot.
#
#   file:// will not poll those JSON files. From the repo:
#     cd frontend && python3 -m http.server 8000
#     open http://127.0.0.1:8000/live_dashboard.html
#
# Regenerated HTML is local-only. Do not git add / commit it.
# Historical Pages snapshot on main stays frozen until a later publish slice.

FABIO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$FABIO_ROOT/dashboard_push.log"

echo "" >> "$LOG"
echo "=== Fabio Push: $(date) ===" >> "$LOG"
echo "⏸  SHIP-009: HTML auto-commit paused. Local dashboard is written by DashboardWriter;" >> "$LOG"
echo "   serve frontend/ with python3 -m http.server — do not git add live_dashboard.html." >> "$LOG"
echo "⏸  Pages / orbit.clayj.app wait-to-wire until four-book UI is ready." >> "$LOG"
exit 0
