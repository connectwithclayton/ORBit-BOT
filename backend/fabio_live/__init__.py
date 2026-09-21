"""
Live Moomoo execution stack (Fabio ORB bot).

Entry point remains `orb_bot_fabio.py`; logic is split here for maintainability.

``ORBBot`` is imported lazily so ``python -m fabio_live.calendar_gate`` and
``python -m fabio_live.paper_flatten_jobs`` keep stdout clean (no SheetsLogger
import noise) for launchd wrappers.
"""

from typing import Any

__all__ = ["ORBBot"]


def __getattr__(name: str) -> Any:
    if name == "ORBBot":
        from fabio_live.bot import ORBBot

        return ORBBot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
