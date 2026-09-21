#!/usr/bin/env bash
# Thin per-book paper flatten launcher for launchd.
# Requires --book <id> on this argv (never rely on Python argparse defaults).
# NYSE closed days skip (exit 0). Flatten script exit 4 (aborted_window) is skip.
# Paper only: never passes --trd-env REAL, --env live, or FABIO_ALLOW_REAL_TRADING.
# Does not invoke fail-safe from eod_close_all. Closes in the market; no exercise.
#
# Bash 3.2 compatible (macOS /bin/bash). One wrapper, four launchd labels.
set -eu
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/backend:${ROOT}/frontend"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
LOG="${PAPER_FLATTEN_WRAPPER_LOG:-${ROOT}/paper_flatten_wrapper.log}"

_ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

BOOK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --book)
      if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
        echo "$(_ts) [paper-flatten] --book requires a book id" >&2
        exit 2
      fi
      case "$2" in
        -*)
          echo "$(_ts) [paper-flatten] --book requires a book id" >&2
          exit 2
          ;;
      esac
      BOOK="$2"
      shift 2
      ;;
    --book=*)
      BOOK="${1#--book=}"
      shift
      ;;
    *)
      echo "$(_ts) [paper-flatten] unexpected arg: $1 (pass only --book <id>)" >&2
      exit 2
      ;;
  esac
done

if [ -z "$BOOK" ]; then
  echo "$(_ts) [paper-flatten] --book <id> is required (no argparse default)" >&2
  exit 2
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "python3 not executable: $PYTHON_BIN" >&2
  exit 2
fi

cd "$ROOT" || exit 2

if ! "$PYTHON_BIN" -m fabio_live.calendar_gate should-run-failsafe >>"$LOG" 2>&1; then
  {
    echo ""
    echo "$(_ts) [paper-flatten] skip book=${BOOK} (NYSE calendar: not a session day)."
  } >>"$LOG"
  exit 0
fi

# Do not pass REAL / live / FABIO_ALLOW_REAL_TRADING on argv or in this environment.
unset FABIO_ALLOW_REAL_TRADING || true

# run: builds argv with --book, JSONL per book, maps exit 4 → 0.
exec "$PYTHON_BIN" -m fabio_live.paper_flatten_jobs run --book "$BOOK"
