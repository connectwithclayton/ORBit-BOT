"""Frozen MR intake intent. Shadow-only; never an order ticket."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SOURCE_MR = "mr"
MODE_SHADOW = "SHADOW"
DECISION_SHADOW = "SHADOW"
DECISION_SKIP = "SKIP"


@dataclass(frozen=True)
class NormalizedIntent:
    """Normalized MR intent plus shadow/skip decision.

    ``mapped`` is ``{symbol, CALL|PUT|EQUITY, ts}`` when accepted.
    Multi-leg and unparseable rows are SKIP (still source=mr / mode=SHADOW).
    """

    source: str
    symbol: str
    direction: str
    instrument: str
    as_of_et: str
    raw_id: str
    confidence: float | None
    decision: str
    reason: str
    accepted: bool
    skip: str | None
    channel: str

    def mapped(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "side": self.direction,
            "ts": self.as_of_et,
        }

    def to_stable_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mapped"] = self.mapped()
        if payload["confidence"] is not None:
            payload["confidence"] = round(float(payload["confidence"]), 4)
        return payload
