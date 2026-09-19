"""Idempotent raw_id for MR payloads."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_ANGLE = re.compile(r"^<([^>]+)>$")


def slug_id(value: str) -> str:
    s = str(value).strip()
    m = _ANGLE.match(s)
    if m:
        s = m.group(1)
    return re.sub(r"\s+", "", s)


def canonical_payload_blob(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def make_raw_id(payload: dict[str, Any]) -> str:
    """Stable id: explicit alert/message id, else sha256 of canonical JSON."""
    for key in ("alert_id", "message_id", "id", "raw_id"):
        raw = payload.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return f"mr:{slug_id(text)}"
    digest = hashlib.sha256(canonical_payload_blob(payload).encode("utf-8")).hexdigest()
    return f"mr:{digest[:32]}"


class IdempotencyStore:
    """In-memory seen-set so the same raw_id is processed once per replay."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen_or_add(self, raw_id: str) -> bool:
        if raw_id in self._seen:
            return True
        self._seen.add(raw_id)
        return False
