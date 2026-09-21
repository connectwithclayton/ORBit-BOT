#!/usr/bin/env bash
# Summarize fail-safe JSONL (one JSON object per line).
# Usage:
#   ./scripts/summarize_failsafe_jsonl.sh [file] [--book BOOK_ID]
#   grep '^{' eod_failsafe.orb-moomoo.jsonl | ./scripts/summarize_failsafe_jsonl.sh --book orb-moomoo
# Requires: jq
#
# Per-book files (gitignored): eod_failsafe.<book_id>.jsonl

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
  echo "jq not found; install jq or use recipes in docs/architecture/architecture-observability.md" >&2
  exit 1
fi

BOOK_FILTER=""
FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --book)
      BOOK_FILTER="${2:-}"
      if [[ -z "$BOOK_FILTER" ]]; then
        echo "--book requires a book id" >&2
        exit 2
      fi
      shift 2
      ;;
    --book=*)
      BOOK_FILTER="${1#--book=}"
      shift
      ;;
    -)
      FILE="-"
      shift
      ;;
    *)
      if [[ -z "$FILE" ]]; then
        FILE="$1"
        shift
      else
        echo "unexpected extra argument: $1" >&2
        exit 2
      fi
      ;;
  esac
done

if [[ -n "${FILE}" && "$FILE" != "-" ]]; then
  INPUT=$(grep '^{' "$FILE" || true)
else
  INPUT=$(grep '^{' || true)
fi

if [[ -z "${INPUT// /}" ]]; then
  echo "No JSON lines found."
  exit 0
fi

if [[ -n "$BOOK_FILTER" ]]; then
  INPUT=$(echo "$INPUT" | jq -c --arg b "$BOOK_FILTER" 'select(.book_id == $b)')
  if [[ -z "${INPUT// /}" ]]; then
    echo "No JSON lines for book_id=${BOOK_FILTER}."
    exit 0
  fi
fi

echo "=== Events (count) ==="
echo "$INPUT" | jq -r '.event // "unknown"' | sort | uniq -c | sort -nr

echo ""
echo "=== place_order_end by reason_code ==="
echo "$INPUT" | jq -r 'select(.event == "place_order_end") | .reason_code // "null"' | sort | uniq -c | sort -nr

echo ""
echo "=== Last run_complete ==="
echo "$INPUT" | jq -c 'select(.event == "run_complete")' | tail -n 1

fail_n=$(echo "$INPUT" | jq -r 'select(.event == "place_order_end" and .reason_code != "ok") | .symbol' | wc -l | tr -d ' ')
echo ""
echo "=== Summary ==="
echo "Non-OK place_order_end rows: $fail_n"
if [[ -n "$BOOK_FILTER" ]]; then
  echo "book_id filter: $BOOK_FILTER"
fi
