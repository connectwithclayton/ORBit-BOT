"""Tradier paper ExecutionPort — isolated from Moomoo OrderManager.

Each Tradier paper book (ORB-Tradier, MR-Tradier) gets its own instance and
``positions`` dict. Never imports moomoo / OpenD. Never exercises. Paper pin
is enforced via the injected ``TradierPaperClient``.
"""

from __future__ import annotations

from typing import Any

from brokers.tradier.client import TradierAPIError, TradierPaperClient, looks_like_occ_option
from fabio_live.constants import (
    OPTIONS_ONLY_EXECUTION,
    RESEARCH_RISK_CAP_MULTIPLIER,
    STRATEGY_CAPITAL,
)
from paper_pin import enforce_tradier_paper_pin, resolve_tradier_env_name


def _build_occ(symbol: str, expiry: str, direction: str, strike: float) -> str:
    """Bare OCC (no Moomoo ``US.`` prefix)."""
    sym = str(symbol or "").strip().upper()
    exp = str(expiry or "").strip().replace("-", "")
    if len(exp) == 8:
        exp = exp[2:]
    right = "P" if str(direction).upper() == "PUT" else "C"
    strike_int = int(round(float(strike) * 1000))
    return f"{sym}{exp}{right}{strike_int:08d}"


def tradier_occ_from_moomoo_code(code: str) -> str:
    """Strip the Moomoo ``US.`` prefix; Tradier wants bare OCC."""
    raw = str(code or "").strip().upper()
    if raw.startswith("US."):
        raw = raw[3:]
    return raw


def tradier_occ(symbol: str, expiry: str, direction: str, strike: float) -> str:
    return _build_occ(symbol, expiry, direction, strike)


class TradierOrderManager:
    """ExecutionPort for one Tradier paper book. Do not share ``positions``."""

    def __init__(
        self,
        client: TradierPaperClient,
        *,
        source: str = "tradier_paper",
        book_id: str = "orb-tradier",
        options_only: bool | None = None,
        get_ask: Any | None = None,
    ) -> None:
        enforce_tradier_paper_pin(client.env, base_url=client.base_url)
        self.client = client
        self.source = source
        self.book_id = book_id
        # SIMULATE token so shared paper-only gates treat this as paper, not REAL.
        self.trd_env = "SIMULATE"
        self.tradier_env = resolve_tradier_env_name(client.env)
        self.positions: dict[str, dict[str, Any]] = {}
        self.options_only = OPTIONS_ONLY_EXECUTION if options_only is None else options_only
        self.get_ask = get_ask

    def enter(
        self,
        symbol: str,
        direction: str,
        price: float,
        risk_pct: float,
        portfolio_val: float,
    ) -> Any:
        """ORB-style enter requires a named contract on Tradier (no ATM chain)."""
        raise NotImplementedError(
            "Tradier ORB book uses enter_option_contract with a named OCC "
            "(mirrored from the Moomoo fill). No equity BUY path."
        )

    def enter_option_contract(
        self,
        symbol: str,
        direction: str,
        *,
        strike: float,
        expiry: str,
        premium: float | None,
        risk_pct: float,
        portfolio_val: float,
        source: str | None = None,
    ) -> Any:
        """Buy a named expiry/strike. Market buy-to-open. Never exercise."""
        if symbol in self.positions:
            print(
                f"   ✗ [{symbol}] Tradier enter refused — underlying already tracked "
                f"(source={self.positions[symbol].get('source') or 'unmarked'})."
            )
            return
        occ = tradier_occ(symbol, expiry, direction, strike)
        if self.options_only and not looks_like_occ_option(occ):
            print(
                f"   ✗ [{symbol}] Options-only safety blocked BUY for non-option: {occ}"
            )
            return
        ask = self._ask(occ, premium, strike)
        risk_base = min(float(portfolio_val), STRATEGY_CAPITAL * RESEARCH_RISK_CAP_MULTIPLIER)
        risk_dollars = risk_base * float(risk_pct)
        qty = max(1, int(risk_dollars / (ask * 100))) if ask > 0 else 1
        tag_src = source if source is not None else self.source
        try:
            self.client.place_order(
                symbol=occ,
                side="buy_to_open",
                quantity=qty,
                order_type="market",
                duration="day",
                option_symbol=occ,
                tag=f"{self.book_id}_{symbol}"[:20],
            )
        except (TradierAPIError, ValueError) as exc:
            print(f"   ✗ [{symbol}] Tradier place_order failed: {exc}")
            return
        rec = {
            "direction": direction,
            "code": occ,
            "original_qty": qty,
            "remaining_qty": qty,
            "entry_option_price": ask,
            "trim_level": 0,
            "realized_trim_pnl": 0.0,
            "strike": float(strike),
            "expiry": str(expiry),
            "source": tag_src,
            "book_id": self.book_id,
        }
        self.positions[symbol] = rec
        print(
            f"   ✓ Tradier position recorded: {symbol} {direction} "
            f"× {qty} {occ} @ ${ask:.2f} source={tag_src}"
        )
        return rec

    def check_profit_trim(self, symbol: str) -> dict:
        rem = int((self.positions.get(symbol) or {}).get("remaining_qty") or 0)
        return {
            "qty_sold": 0,
            "pnl_leg": 0.0,
            "remaining_after": rem,
            "closed_fully": False,
            "position_total_pnl": 0.0,
        }

    def exit(self, symbol: str, reason: str = "") -> float:
        return float(self.exit_result(symbol, reason).get("pnl", 0.0) or 0.0)

    def exit_result(self, symbol: str, reason: str = "") -> dict:
        if symbol not in self.positions:
            return {
                "success": False,
                "pnl": 0.0,
                "error": "symbol_not_tracked",
                "symbol": symbol,
                "reason": reason,
            }
        pos = self.positions[symbol]
        occ = str(pos.get("code") or "")
        rem = int(pos.get("remaining_qty") or 0)
        if rem <= 0 or not occ:
            del self.positions[symbol]
            return {
                "success": True,
                "pnl": float(pos.get("realized_trim_pnl") or 0.0),
                "symbol": symbol,
                "reason": reason,
                "error": "",
            }
        try:
            self.client.place_order(
                symbol=occ,
                side="sell_to_close",
                quantity=rem,
                order_type="market",
                duration="day",
                option_symbol=occ,
                tag=f"eod_{self.book_id}"[:20],
            )
        except (TradierAPIError, ValueError) as exc:
            return {
                "success": False,
                "pnl": 0.0,
                "error": str(exc),
                "symbol": symbol,
                "reason": reason,
            }
        exit_px = self._ask(occ, pos.get("entry_option_price"), pos.get("strike") or 0)
        rem_pnl = (exit_px - float(pos.get("entry_option_price") or 0.0)) * rem * 100
        total = float(pos.get("realized_trim_pnl") or 0.0) + rem_pnl
        del self.positions[symbol]
        return {
            "success": True,
            "pnl": float(total),
            "pnl_final_leg": float(rem_pnl),
            "qty_final_leg": rem,
            "error": "",
            "symbol": symbol,
            "reason": reason,
        }

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def open_count(self) -> int:
        return len(self.positions)

    def _ask(self, occ: str, premium: float | None, strike: float) -> float:
        if callable(self.get_ask):
            try:
                got = float(self.get_ask(occ))
                if got > 0:
                    return got
            except (TypeError, ValueError):
                pass
        if premium is not None:
            try:
                px = float(premium)
                if px > 0:
                    return px
            except (TypeError, ValueError):
                pass
        try:
            return max(float(strike) * 0.01, 0.05)
        except (TypeError, ValueError):
            return 0.05
