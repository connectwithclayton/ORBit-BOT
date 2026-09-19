"""Market Rebellion signal intake (shadow only — no orders).

Slice 2: parse anonymized MR email/site payloads into normalized intents,
replay fixtures to stable JSON, and log SHADOW/SKIP rows via existing
Sheets Decisions + Telegram helpers. Never submits a broker order.

Does not modify SignalEngine.check_breakout or MarketRegime.
"""

from signal_intake.models import SOURCE_MR, NormalizedIntent
from signal_intake.parse import parse_payload
from signal_intake.replay import FIXTURE_DIR, replay_fixture_dir, intents_to_stable_json
from signal_intake.shadow import emit_shadow

__all__ = [
    "SOURCE_MR",
    "NormalizedIntent",
    "parse_payload",
    "FIXTURE_DIR",
    "replay_fixture_dir",
    "intents_to_stable_json",
    "emit_shadow",
]
