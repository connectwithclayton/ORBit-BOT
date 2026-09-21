#!/usr/bin/env bash
# install_paper_flatten_scheduler.sh
# Install FOUR independent paper-only fail-safe launchd jobs (one per book).
# Not a mega-script across brokers. Always installs all four labels so enabling
# FABIO_MR_PAPER_ENABLED / FABIO_TRADIER_PAPER_BOOKS mid-week is already protected
# (disabled books → empty ledger → no_closable sidecar, cheap).
#
# Stagger (host local clock; set Mac TZ to America/New_York):
#   15:50 orb-moomoo, 15:52 mr-moomoo, 15:54 orb-tradier, 15:56 mr-tradier
# Slice 1: these are FIXED weekday clocks, not session_close-relative.
# NYSE early-close (e.g. 13:00) is an accepted gap — jobs still fire at 15:50–15:56.
# Calendar-aware / session_close-relative fire times are a follow-up.
#
# Each job argv contains --book <that id>. Moomoo inner argv pins --trd-env SIMULATE
# (never REAL; do not rely on host MOOMOO_TRADE_ENV). Wrappers never pass REAL / live /
# FABIO_ALLOW_REAL_TRADING. KeepAlive false. Exit 4 (aborted_window) is skip.
#
# Usage:
#   bash portal/install_paper_flatten_scheduler.sh --dry-run
#   bash portal/install_paper_flatten_scheduler.sh
#   bash portal/install_paper_flatten_scheduler.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FABIO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHONPATH_VAL="${FABIO_ROOT}/backend:${FABIO_ROOT}/frontend"
export PYTHONPATH="$PYTHONPATH_VAL"
WRAP="$FABIO_ROOT/portal/run_paper_flatten_book.sh"
BOOT_LOG_DIR="$HOME/Library/Logs/ClaytonOrb"
MODE="install"
if [[ "${1:-}" == "--dry-run" ]]; then
  MODE="dry-run"
elif [[ "${1:-}" == "--uninstall" ]]; then
  MODE="uninstall"
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--dry-run|--uninstall]" >&2
  exit 2
fi

PYTHON="${PYTHON_BIN:-$(command -v python3 || true)}"
if [[ -z "$PYTHON" ]]; then
  echo "❌ python3 not found in PATH." >&2
  exit 1
fi

if [[ ! -f "$WRAP" ]]; then
  echo "❌ Missing wrapper: $WRAP" >&2
  exit 1
fi

print_dry_run() {
  echo "Paper flatten jobs (always all four). Host TZ must be America/New_York."
  echo "launchd Hour/Minute is the Mac local clock. KeepAlive=false. Paper only."
  echo "Moomoo argv pins --trd-env SIMULATE (never REAL / host MOOMOO_TRADE_ENV)."
  echo "Early-close fire times are a follow-up: Slice 1 uses fixed 15:50–15:56 ET."
  echo ""
  "$PYTHON" -m fabio_live.paper_flatten_jobs print-schedule
  echo ""
  echo "launchctl labels (after install):"
  while read -r _pfx BOOK HOUR MINUTE LABEL JSONL; do
    echo "  $LABEL"
    echo "    StartCalendarInterval: weekday ${HOUR}:$(printf '%02d' "$MINUTE") local"
    echo "    ProgramArguments: /bin/bash $WRAP --book $BOOK"
    echo "    inner argv: $("$PYTHON" -m fabio_live.paper_flatten_jobs print-argv --book "$BOOK" | tr '\n' ' ')"
    echo "    jsonl (gitignored): $FABIO_ROOT/$JSONL"
    echo ""
  done < <(flatten_job_rows)
  echo "Useful:"
  echo "  launchctl list | grep claytonorb.paper.flatten"
  echo "  Uninstall: bash portal/install_paper_flatten_scheduler.sh --uninstall"
  echo "  Manual verify: portal/docs/Paper-Book-Flatten.md"
}

flatten_job_rows() {
  "$PYTHON" -m fabio_live.paper_flatten_jobs print-schedule --bash | grep '^flatten-job '
}

weekday_calendar_xml() {
  local hour="$1" minute="$2"
  local wd
  for wd in 1 2 3 4 5; do
    printf '    <dict><key>Weekday</key><integer>%s</integer><key>Hour</key><integer>%s</integer><key>Minute</key><integer>%s</integer></dict>\n' "$wd" "$hour" "$minute"
  done
}

write_plist() {
  local book="$1" hour="$2" minute="$3" label="$4"
  local plist="$HOME/Library/LaunchAgents/${label}.plist"
  local boot_log="${BOOT_LOG_DIR}/paper-flatten.${book}.boot.log"
  mkdir -p "$HOME/Library/LaunchAgents" "$BOOT_LOG_DIR"
  cat > "$plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${WRAP}</string>
    <string>--book</string>
    <string>${book}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${FABIO_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>${PYTHONPATH_VAL}</string>
  </dict>
  <!-- ${hour}:$(printf '%02d' "$minute") America/New_York weekdays (set host TZ). Gate skips NYSE closed days. -->
  <key>StartCalendarInterval</key>
  <array>
$(weekday_calendar_xml "$hour" "$minute")
  </array>
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardOutPath</key>
  <string>${boot_log}</string>
  <key>StandardErrorPath</key>
  <string>${boot_log}</string>
</dict>
</plist>
EOF
  echo "$plist"
}

if [[ "$MODE" == "dry-run" ]]; then
  print_dry_run
  exit 0
fi

chmod +x "$WRAP" 2>/dev/null || true

if [[ "$MODE" == "uninstall" ]]; then
  while read -r _pfx BOOK HOUR MINUTE LABEL JSONL; do
    plist="$HOME/Library/LaunchAgents/${LABEL}.plist"
    if command -v launchctl >/dev/null 2>&1; then
      launchctl unload "$plist" 2>/dev/null || true
    fi
    rm -f "$plist"
    echo "Removed $LABEL"
  done < <(flatten_job_rows)
  echo "✅ Paper flatten schedulers uninstalled."
  exit 0
fi

LOADED=0
while read -r _pfx BOOK HOUR MINUTE LABEL JSONL; do
  plist="$(write_plist "$BOOK" "$HOUR" "$MINUTE" "$LABEL")"
  if command -v launchctl >/dev/null 2>&1; then
    launchctl unload "$plist" 2>/dev/null || true
    if launchctl load "$plist"; then
      LOADED=$((LOADED + 1))
    else
      echo "❌ Failed to load $plist" >&2
      exit 1
    fi
  else
    echo "⚠ launchctl not found; wrote $plist (load on the Mac trading host)."
  fi
done < <(flatten_job_rows)

echo "✅ Paper flatten schedulers installed (four labels, paper only)."
echo "   Host TZ must be America/New_York."
echo "   Dry-run this installer anytime: bash portal/install_paper_flatten_scheduler.sh --dry-run"
echo "   launchctl list | grep claytonorb.paper.flatten"
if [[ "$LOADED" -gt 0 ]]; then
  echo "   Loaded $LOADED job(s)."
fi
echo "   Manual verify: portal/docs/Paper-Book-Flatten.md"
