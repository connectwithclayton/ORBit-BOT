"""FABIO_BROKER flag: default Moomoo; Tradier is explicit and isolated."""

from __future__ import annotations

import os
from typing import Any

BROKER_ENV = "FABIO_BROKER"
DEFAULT_BROKER = "moomoo"
ALLOWED_BROKERS = ("moomoo", "tradier")


class IsolatedBrokerNotAvailable(ValueError):
    """Raised when an isolated HTTP place_order client is not applicable."""


def resolve_execution_broker(raw: str | None = None) -> str:
    """Return ``moomoo`` (default) or ``tradier``.

    Does not read ``MOOMOO_TRADE_ENV``. Unknown values raise ValueError
    rather than silently selecting Tradier.
    """
    token = (raw if raw is not None else os.getenv(BROKER_ENV, "")).strip().lower()
    if token in ("", DEFAULT_BROKER, "futu"):
        return DEFAULT_BROKER
    if token == "tradier":
        return "tradier"
    raise ValueError(
        f"Unknown {BROKER_ENV}={token!r}. Use {DEFAULT_BROKER} (default) or tradier."
    )


def isolated_place_order_client(
    broker: str | None = None,
    **kwargs: Any,
):
    """Build the isolated Tradier paper client when explicitly selected.

    Moomoo ORB execution stays on OpenD ``OrderManager``; there is no HTTP
    place_order client for the default broker.
    """
    name = resolve_execution_broker(broker)
    if name == "tradier":
        from brokers.tradier.client import TradierPaperClient

        return TradierPaperClient.from_env(**kwargs)
    raise IsolatedBrokerNotAvailable(
        "Moomoo is the default ORB execution path (OpenD OrderManager). "
        f"Isolated HTTP place_order is Tradier paper only; set {BROKER_ENV}=tradier."
    )
