"""Emit MR intents to Sheets Decisions + Telegram. Never places an order."""

from __future__ import annotations

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
    skip_line = f"skip={intent.skip}\n" if intent.skip else ""
    return (
        f"⚪ <b>{MODE_SHADOW} MR INTAKE</b>\n"
        f"source={SOURCE_MR} | mode={MODE_SHADOW}\n"
        f"Decision: {intent.decision}\n"
        f"{skip_line}"
        f"Symbol: {intent.symbol}  Side: {intent.direction or '—'}\n"
        f"ts: {intent.as_of_et or '—'}\n"
        f"id: {intent.raw_id}\n"
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
        ld(
            intent.symbol,
            intent.direction or "—",
            intent.decision,
            intent.reason,
            regime=MODE_SHADOW,
        )
    if al is not None:
        al(format_telegram(intent))
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
