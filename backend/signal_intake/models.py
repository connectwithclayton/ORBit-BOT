"""Frozen MR intake intent. Shadow-only; never an order ticket."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SOURCE_MR = "mr"
MODE_SHADOW = "SHADOW"
DECISION_SHADOW = "SHADOW"
DECISION_SKIP = "SKIP"
ACTION_BUY = "buy"
ACTION_EXIT = "exit"


@dataclass(frozen=True)
class NormalizedIntent:
    """Normalized MR intent plus shadow/skip decision.

    ``mapped`` is ``{symbol, CALL|PUT|EQUITY, ts}`` when accepted.
    Multi-leg and unparseable rows are SKIP (still source=mr / mode=SHADOW).
    Optional contract fields (action/expiry/strike/premium/contract_key) are
    omitted from ``to_stable_dict`` when empty so slice-2 golden JSON stays
    byte-stable.
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
    action: str = ""
    expiry: str = ""
    strike: float | None = None
    premium: float | None = None
    contract_key: str = ""

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
        if payload.get("strike") is not None:
            strike = float(payload["strike"])
            payload["strike"] = int(strike) if strike.is_integer() else round(strike, 8)
        if payload.get("premium") is not None:
            payload["premium"] = round(float(payload["premium"]), 8)
        for optional in ("action", "expiry", "contract_key"):
            if not payload.get(optional):
                payload.pop(optional, None)
        if payload.get("strike") is None:
            payload.pop("strike", None)
        if payload.get("premium") is None:
            payload.pop("premium", None)
        return payload
