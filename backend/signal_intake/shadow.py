"""Emit MR intents to Sheets Decisions + Telegram. Never places an order."""

from __future__ import annotations

import html
from typing import Any, Callable, Protocol

from signal_intake.models import MODE_SHADOW, SOURCE_MR, NormalizedIntent

LogDecision = Callable[..., Any]
AlertFn = Callable[[str], Any]


class _OpsLike(Protocol):
    def log_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    def alert(self, text: str) -> Any: ...


def format_telegram(intent: NormalizedIntent) -> str:
    """Operator-facing SHADOW notice. No fill / no order language as a command."""
    conf = "" if intent.confidence is None else f"{intent.confidence:.4f}"
    skip_line = f"skip={html.escape(str(intent.skip))}\n" if intent.skip else ""
    symbol = html.escape(intent.symbol or "")
    raw_id = html.escape(intent.raw_id or "")
    as_of = html.escape(intent.as_of_et) if intent.as_of_et else "—"
    side = html.escape(intent.direction) if intent.direction else "—"
    return (
        f"⚪ <b>{MODE_SHADOW} MR INTAKE</b>\n"
        f"source={SOURCE_MR} | mode={MODE_SHADOW}\n"
        f"Decision: {html.escape(intent.decision)}\n"
        f"{skip_line}"
        f"Symbol: {symbol}  Side: {side}\n"
        f"ts: {as_of}\n"
        f"id: {raw_id}\n"
        f"confidence: {conf or '—'}\n"
        f"No order (shadow only)"
    )


def emit_shadow(
    intent: NormalizedIntent,
    *,
    ops: _OpsLike | None = None,
    log_decision: LogDecision | None = None,
    alert: AlertFn | None = None,
) -> dict[str, Any]:
    """Log SHADOW/SKIP to Sheets + Telegram. Does not call any broker.

    Pass ``ops`` (AsyncOpsWorker) or explicit ``log_decision`` / ``alert``
    callables. Sinks are optional so fixture replay can stay pure JSON.
    """
    ld = log_decision or (ops.log_decision if ops is not None else None)
    al = alert or (ops.alert if ops is not None else None)
    if ld is not None:
        try:
            ld(
                intent.symbol,
                intent.direction or "—",
                intent.decision,
                intent.reason,
                # Decisions.Regime column is reused as a SHADOW tag for MR rows
                # (not ORB day-color). Intentional slice-2 marker.
                regime=MODE_SHADOW,
            )
        except Exception as exc:
            print(f"[signal_intake] log_decision failed: {exc}")
    if al is not None:
        try:
            al(format_telegram(intent))
        except Exception as exc:
            print(f"[signal_intake] telegram alert failed: {exc}")
    return intent.to_stable_dict()


def bind_live_helpers() -> tuple[LogDecision, AlertFn]:
    """Optional production wiring to existing SheetsLogger + telegram_bot.alert.

    Not imported at module load so unit tests never pull broker or config modules.
    Still does not place orders — only Decisions rows and Telegram text.
    """
    from sheets_logger import SheetsLogger
    import telegram_bot as tg

    sheets = SheetsLogger()
    return sheets.log_decision, tg.alert
