"""Narrow execution port extracted from OrderManager.

ORB live trading stays on Moomoo ``OrderManager`` (OpenD TCP). This Protocol is
structural so a later adapter can satisfy the same enter/exit/trim surface
without rewriting ``ORBBot`` or ``SignalEngine``.

Slice 3 does **not** rewire ``ORBBot.__init__``: default boot still constructs
``OpenQuoteContext`` / ``OpenSecTradeContext`` / ``OrderManager``. Tradier paper
``place_order`` lives in ``brokers.tradier`` and is constructed only behind
``FABIO_BROKER=tradier`` (isolated client + flatten), not as the ORB default.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecutionPort(Protocol):
    """Public OrderManager surface used by the live ORB loop.

    ``positions`` is the in-memory tracked book (Moomoo OCC-style codes today).
    Dual-broker books must not share this dict (scout §3.2 / §5.3).
    """

    positions: dict

    def enter(
        self,
        symbol: str,
        direction: str,
        price: float,
        risk_pct: float,
        portfolio_val: float,
    ) -> Any: ...

    def exit(self, symbol: str, reason: str = "") -> float: ...

    def exit_result(self, symbol: str, reason: str = "") -> dict: ...

    def check_profit_trim(self, symbol: str) -> dict: ...

    def has_position(self, symbol: str) -> bool: ...

    def open_count(self) -> int: ...
