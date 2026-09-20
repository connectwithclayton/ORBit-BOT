"""Optional MR paper auto-trade through the existing Moomoo risk box.

Default **off**. Set ``FABIO_MR_PAPER_ENABLED=1`` to drain accepted MR intents
through ``RiskCircuitBreaker.can_enter`` + the same sizing formula into
``OrderManager`` on **SIMULATE/paper only**.

Does not touch ``SignalEngine.check_breakout`` or ``MarketRegime``.
Does not enable Tradier dual books (slice 5). A Tradier paper adapter may
exist for isolated ``place_order``; this executor still uses Moomoo
``OrderManager`` only. Hard isolate: MR never overwrites ORB ``positions[symbol]``;
lock-in / MR leftover sells only close ``source=mr`` (or executor-owned) legs;
SPY/QQQ/NVDA entries are denied unless ``FABIO_MR_ALLOW_ORB_SYMBOLS=1``.
Never exercises; EOD still market-closes via ``OrderManager._sell``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from fabio_live.circuit import RiskCircuitBreaker
from fabio_live.constants import (
    CB_DAILY_LOSS_PCT,
    OPTIONS_ONLY_EXECUTION,
    RESEARCH_RISK_CAP_MULTIPLIER,
    RISK_PCT_FULL,
    RISK_PCT_MAX,
    STRATEGY_CAPITAL,
    SYMBOLS,
)
from paper_pin import enforce_paper_trading_pin, is_real_trd_env
from signal_intake.ids import IdempotencyStore
from signal_intake.models import ACTION_BUY, ACTION_EXIT, SOURCE_MR, NormalizedIntent

MR_PAPER_ENABLED_ENV = "FABIO_MR_PAPER_ENABLED"
MR_CB_PARTITION_ENV = "FABIO_MR_CB_PARTITION"
MR_QUEUE_PATH_ENV = "FABIO_MR_QUEUE_PATH"
MR_ALLOW_ORB_SYMBOLS_ENV = "FABIO_MR_ALLOW_ORB_SYMBOLS"

SKIP_DISABLED = "mr_paper_disabled"
SKIP_NOT_ACCEPTED = "not_accepted"
SKIP_MULTI_LEG = "multi-leg"
SKIP_EQUITY_OPTIONS_ONLY = "options_only_blocks_shares"
SKIP_NO_CONTRACT = "missing_contract"
SKIP_NOT_OPEN = "not_open"
SKIP_NOT_MR = "not_mr_position"
SKIP_UNDERLYING_OCCUPIED = "underlying_occupied"
SKIP_ORB_SYMBOL = "orb_symbol_denied"
SKIP_REAL_REFUSED = "real_refused"
SKIP_CB = "circuit_breaker"
SKIP_COMBINED_DAILY_LOSS = "combined_daily_loss"
SKIP_ENTRIES_CLOSED = "entries_window_closed"
SKIP_NO_FILL = "no_fill"
SKIP_NO_CURSOR = "queue_cursor_missing"


class _OrderMgr(Protocol):
    positions: dict
    trd_env: Any

    def enter_option_contract(self, *args: Any, **kwargs: Any) -> Any: ...
    def has_position(self, symbol: str) -> bool: ...
    def open_count(self) -> int: ...
    def exit_result(self, symbol: str, reason: str = "") -> dict: ...


class _OpsLike(Protocol):
    def log_decision(self, *args: Any, **kwargs: Any) -> Any: ...
    def alert(self, text: str) -> Any: ...


def mr_paper_enabled() -> bool:
    return os.getenv(MR_PAPER_ENABLED_ENV, "").strip() == "1"


def mr_cb_partitioned() -> bool:
    return os.getenv(MR_CB_PARTITION_ENV, "").strip() == "1"


def sizing_risk_pct(cb: RiskCircuitBreaker) -> float:
    """Same box as live ORB: full rung × CB streak modifier, capped at RISK_PCT_MAX."""
    return min(RISK_PCT_FULL * cb.size_modifier(), RISK_PCT_MAX)


def risk_base_dollars(portfolio_val: float) -> float:
    return min(float(portfolio_val), STRATEGY_CAPITAL * RESEARCH_RISK_CAP_MULTIPLIER)


def combined_daily_loss_blocks(
    breakers: Sequence[RiskCircuitBreaker],
    *,
    modeled_book: float = STRATEGY_CAPITAL,
    daily_loss_pct: float = CB_DAILY_LOSS_PCT,
) -> tuple[bool, str]:
    """True when summed realized PnL / modeled $10k ≤ −2%.

    Used when ``cb_orb`` / ``cb_mr`` are partitioned so two books cannot each
    lose 2% of modeled equity.
    """
    if modeled_book <= 0:
        return False, ""
    combined = sum(float(cb.realized_pnl) for cb in breakers)
    if combined / modeled_book <= -daily_loss_pct:
        pct = combined / modeled_book * 100
        return True, (
            f"Combined daily loss {pct:.1f}% hit -{daily_loss_pct*100:.0f}% "
            f"of modeled ${modeled_book:,.0f}"
        )
    return False, ""


def mr_allow_orb_symbols() -> bool:
    return os.getenv(MR_ALLOW_ORB_SYMBOLS_ENV, "").strip() == "1"


def orb_entry_deny_symbols() -> frozenset[str]:
    """SPY/QQQ/NVDA (live ORB universe) unless FABIO_MR_ALLOW_ORB_SYMBOLS=1."""
    if mr_allow_orb_symbols():
        return frozenset()
    return frozenset(s.upper() for s in SYMBOLS)


def is_mr_position(pos: dict | None, *, owned: bool = False) -> bool:
    """True only for tagged MR legs (or same-process executor ownership)."""
    if not pos:
        return False
    if str(pos.get("source") or "").strip().lower() == SOURCE_MR:
        return True
    return bool(owned)


def queue_cursor_path(queue_path: str) -> Path:
    return Path(str(queue_path) + ".cursor.json")


class DurableMrCursor:
    """Byte offset + idempotency beside FABIO_MR_QUEUE_PATH.

    Restarts load this file so retained JSONL is not re-drained. If the queue
    file has content and no cursor can be loaded or saved, file drain is refused.
    """

    def __init__(self, queue_path: str, *, store: IdempotencyStore | None = None) -> None:
        self.queue_path = str(queue_path)
        self.path = queue_cursor_path(self.queue_path)
        self.offset = 0
        self.store = store if store is not None else IdempotencyStore()
        self.refused = False
        self.refuse_reason = ""

    def load_or_create(self) -> bool:
        """Load cursor, or create offset=0. Refuse if queue has bytes and load fails."""
        q = Path(self.queue_path)
        queue_bytes = 0
        if q.is_file():
            try:
                queue_bytes = q.stat().st_size
            except OSError:
                queue_bytes = 0
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                self.refused = True
                self.refuse_reason = f"cursor unreadable: {exc}"
                return False
            if not isinstance(data, dict):
                self.refused = True
                self.refuse_reason = "cursor is not a JSON object"
                return False
            try:
                self.offset = int(data.get("offset") or 0)
            except (TypeError, ValueError):
                self.offset = 0
            if self.offset < 0:
                self.offset = 0
            self.store.load_durable_dict(data)
            return True
        if queue_bytes > 0:
            # Retained JSONL without a cursor — do not re-fire paper entries.
            self.refused = True
            self.refuse_reason = (
                f"refusing to drain {self.queue_path} without durable cursor "
                f"{self.path}"
            )
            return False
        return self.save()

    def save(self) -> bool:
        if self.refused:
            return False
        payload = self.store.to_durable_dict()
        payload["offset"] = int(self.offset)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            self.refused = True
            self.refuse_reason = f"cursor save failed: {exc}"
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    def next_rows(self) -> list[tuple[dict[str, Any] | None, int]] | None:
        """New JSON objects with the byte offset after each line. Does not save.

        ``None`` payload means a blank/invalid line that still advances the
        cursor. Caller must ``commit_offset`` after each consider so a restart
        cannot re-place. Return value ``None`` (not a list) means refuse.
        """
        if self.refused:
            return None
        q = Path(self.queue_path)
        if not q.is_file():
            return []
        try:
            raw = q.read_bytes()
        except OSError as exc:
            self.refused = True
            self.refuse_reason = f"queue read failed: {exc}"
            return None
        start = self.offset
        if start > len(raw):
            start = 0
        out: list[tuple[dict[str, Any] | None, int]] = []
        pos = start
        for line in raw[start:].splitlines(keepends=True):
            pos += len(line)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                out.append((None, pos))
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                out.append((None, pos))
                continue
            if isinstance(parsed, dict):
                out.append((parsed, pos))
            else:
                out.append((None, pos))
        return out

    def commit_offset(self, offset: int) -> bool:
        self.offset = max(0, int(offset))
        return self.save()


class MrPaperExecutor:
    """Run accepted MR intents through the existing CB + Moomoo paper OrderManager."""

    def __init__(
        self,
        order_mgr: _OrderMgr,
        cb: RiskCircuitBreaker,
        *,
        orb_cb: RiskCircuitBreaker | None = None,
        ops: _OpsLike | None = None,
        paper_only: bool = True,
        options_only: bool | None = None,
        modeled_book: float = STRATEGY_CAPITAL,
        enabled: bool | None = None,
        get_portfolio: Callable[[], float] | None = None,
    ) -> None:
        self.order_mgr = order_mgr
        self.cb = cb
        self.orb_cb = orb_cb
        self.ops = ops
        self.paper_only = paper_only
        self.options_only = OPTIONS_ONLY_EXECUTION if options_only is None else options_only
        self.modeled_book = float(modeled_book)
        self._enabled = mr_paper_enabled() if enabled is None else bool(enabled)
        self.get_portfolio = get_portfolio
        self._owned: set[str] = set()
        if self.paper_only:
            enforce_paper_trading_pin(getattr(order_mgr, "trd_env", "SIMULATE"))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def owns(self, symbol: str) -> bool:
        return symbol in self._owned

    def release(self, symbol: str) -> None:
        self._owned.discard(symbol)

    def record_close(self, pnl: float) -> None:
        self.cb.record_result(pnl)

    def consider(
        self,
        intent: NormalizedIntent,
        *,
        portfolio_val: float | None = None,
        allow_entries: bool = True,
    ) -> dict[str, Any]:
        """Gate + size + enter/exit. No-op when the paper flag is off."""
        if not self._enabled:
            return self._outcome(intent, SKIP_DISABLED, placed=False)
        if self.paper_only and is_real_trd_env(getattr(self.order_mgr, "trd_env", None)):
            return self._outcome(intent, SKIP_REAL_REFUSED, placed=False)

        if intent.skip == "multi-leg" or (
            not intent.accepted and intent.skip == "multi-leg"
        ):
            return self._skip(intent, SKIP_MULTI_LEG)
        if not intent.accepted:
            return self._skip(intent, intent.skip or SKIP_NOT_ACCEPTED)

        action = (intent.action or ACTION_BUY).lower()
        if action == ACTION_EXIT:
            return self._lock_in(intent)

        if not allow_entries:
            return self._skip(intent, SKIP_ENTRIES_CLOSED)

        deny = orb_entry_deny_symbols()
        if intent.symbol and intent.symbol.upper() in deny:
            return self._skip(intent, SKIP_ORB_SYMBOL, detail=intent.symbol.upper())

        if self.order_mgr.has_position(intent.symbol):
            existing = self.order_mgr.positions.get(intent.symbol) or {}
            tag = existing.get("source") or "unmarked"
            return self._skip(
                intent,
                SKIP_UNDERLYING_OCCUPIED,
                detail=f"{intent.symbol} source={tag}",
            )

        if intent.direction == "EQUITY" or intent.instrument == "equity":
            if self.options_only:
                return self._skip(intent, SKIP_EQUITY_OPTIONS_ONLY)

        if intent.direction not in ("CALL", "PUT"):
            return self._skip(intent, SKIP_NO_CONTRACT)
        if not intent.expiry or intent.strike is None:
            return self._skip(intent, SKIP_NO_CONTRACT)

        n_open = self.order_mgr.open_count()
        allowed, reason = self.cb.can_enter(n_open)
        if not allowed:
            return self._skip(intent, SKIP_CB, detail=reason)

        if self.orb_cb is not None and self.orb_cb is not self.cb:
            blocked, combined_reason = combined_daily_loss_blocks(
                [self.cb, self.orb_cb], modeled_book=self.modeled_book
            )
            if blocked:
                return self._skip(
                    intent, SKIP_COMBINED_DAILY_LOSS, detail=combined_reason
                )

        port = (
            float(portfolio_val)
            if portfolio_val is not None
            else (float(self.get_portfolio()) if self.get_portfolio else self.modeled_book)
        )
        risk_pct = sizing_risk_pct(self.cb)
        self._log_decision(
            intent,
            "ENTER",
            f"source={SOURCE_MR} | paper=SIMULATE | "
            f"risk_pct={risk_pct:.4f} | {intent.contract_key}",
        )
        self.order_mgr.enter_option_contract(
            intent.symbol,
            intent.direction,
            strike=float(intent.strike),
            expiry=intent.expiry,
            premium=intent.premium,
            risk_pct=risk_pct,
            portfolio_val=risk_base_dollars(port),
            source=SOURCE_MR,
        )
        if not self.order_mgr.has_position(intent.symbol):
            return self._skip(intent, SKIP_NO_FILL)
        pos = self.order_mgr.positions.get(intent.symbol, {})
        if not is_mr_position(pos, owned=False):
            # Safety: never take ownership of an unmarked/ORB overwrite.
            return self._skip(intent, SKIP_UNDERLYING_OCCUPIED, detail="untagged_after_enter")
        self._owned.add(intent.symbol)
        return {
            "status": "entered",
            "source": SOURCE_MR,
            "symbol": intent.symbol,
            "action": ACTION_BUY,
            "placed": True,
            "code": pos.get("code", ""),
            "qty": pos.get("original_qty", 0),
            "risk_pct": risk_pct,
            "skip": None,
        }

    def _lock_in(self, intent: NormalizedIntent) -> dict[str, Any]:
        pos = self.order_mgr.positions.get(intent.symbol)
        if not pos:
            return self._skip(intent, SKIP_NOT_OPEN)
        owned = self.owns(intent.symbol)
        if not is_mr_position(pos, owned=owned):
            return self._skip(
                intent,
                SKIP_NOT_MR,
                detail=f"{intent.symbol} source={pos.get('source') or 'unmarked'}",
            )
        if intent.strike is not None and pos.get("strike") is not None:
            if float(pos["strike"]) != float(intent.strike):
                return self._skip(intent, SKIP_NOT_OPEN, detail="strike_mismatch")
        if intent.expiry and pos.get("expiry") and str(pos["expiry"]) != intent.expiry:
            return self._skip(intent, SKIP_NOT_OPEN, detail="expiry_mismatch")
        result = self.order_mgr.exit_result(intent.symbol, reason="MR_LOCK_IN")
        if not result.get("success"):
            return self._skip(intent, result.get("error") or "exit_failed")
        pnl = float(result.get("pnl", 0.0) or 0.0)
        self.cb.record_result(pnl)
        self.release(intent.symbol)
        self._log_decision(
            intent,
            "EXIT",
            f"source={SOURCE_MR} | lock-in | market sell-to-close | pnl={pnl:.2f}",
        )
        return {
            "status": "exited",
            "source": SOURCE_MR,
            "symbol": intent.symbol,
            "action": ACTION_EXIT,
            "placed": True,
            "pnl": pnl,
            "skip": None,
            "reason": "MR_LOCK_IN",
        }

    def _skip(
        self, intent: NormalizedIntent, skip: str, *, detail: str = ""
    ) -> dict[str, Any]:
        reason = f"source={SOURCE_MR} | skip={skip}"
        if detail:
            reason = f"{reason} | {detail}"
        self._log_decision(intent, "SKIP", reason)
        return self._outcome(intent, skip, placed=False, detail=detail)

    def _outcome(
        self,
        intent: NormalizedIntent,
        skip: str,
        *,
        placed: bool,
        detail: str = "",
    ) -> dict[str, Any]:
        return {
            "status": "skipped",
            "source": SOURCE_MR,
            "symbol": intent.symbol,
            "action": intent.action or "",
            "placed": placed,
            "skip": skip,
            "detail": detail,
        }

    def _log_decision(self, intent: NormalizedIntent, decision: str, reason: str) -> None:
        if self.ops is None:
            return
        try:
            self.ops.log_decision(
                intent.symbol,
                intent.direction or "—",
                decision,
                reason,
                regime=SOURCE_MR,
            )
        except Exception as exc:
            print(f"[mr_paper] log_decision failed: {exc}")
