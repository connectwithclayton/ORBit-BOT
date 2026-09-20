"""Replay anonymized MR fixtures → stable normalized JSON (no brokers)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from signal_intake.ids import IdempotencyStore
from signal_intake.models import NormalizedIntent
from signal_intake.parse import parse_payload

PACKAGE_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = PACKAGE_DIR / "fixtures"
SHELF_DIR = FIXTURE_DIR / "shelf"
GOLDEN_PATH = PACKAGE_DIR / "expected" / "replay_golden.json"


def load_fixture(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"fixture must be a JSON object: {path}")
    return data


def iter_fixture_paths(directory: Path | None = None) -> list[Path]:
    root = directory or FIXTURE_DIR
    return sorted(p for p in root.glob("*.json") if p.is_file())


def replay_fixture_dir(
    directory: Path | None = None,
    *,
    store: IdempotencyStore | None = None,
) -> list[NormalizedIntent]:
    """Parse fixtures in sorted filename order with shared idempotency store."""
    seen = store if store is not None else IdempotencyStore()
    intents: list[NormalizedIntent] = []
    for path in iter_fixture_paths(directory):
        intents.append(parse_payload(load_fixture(path), store=seen))
    return intents


def intents_to_stable_json(intents: list[NormalizedIntent]) -> list[dict[str, Any]]:
    return [i.to_stable_dict() for i in intents]


def replay_stable_records(directory: Path | None = None) -> list[dict[str, Any]]:
    return intents_to_stable_json(replay_fixture_dir(directory))
