"""CLI: python -m signal_intake  →  stable JSON on stdout. No orders."""

from __future__ import annotations

import argparse
import json
import sys

from signal_intake.replay import intents_to_stable_json, replay_fixture_dir
from signal_intake.shadow import emit_shadow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay anonymized Market Rebellion fixtures to normalized JSON. "
            "Shadow only: never places an order."
        )
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="Also write SHADOW/SKIP via SheetsLogger.log_decision + telegram_bot.alert",
    )
    args = parser.parse_args(argv)
    intents = replay_fixture_dir()
    if args.emit:
        from signal_intake.shadow import bind_live_helpers

        log_decision, alert = bind_live_helpers()
        for intent in intents:
            emit_shadow(intent, log_decision=log_decision, alert=alert)
    json.dump(intents_to_stable_json(intents), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
