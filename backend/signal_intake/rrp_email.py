"""Parse RRP/Buy/Trade-Log/Lock-in email bodies. No I/O, no brokers."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from signal_intake.models import ACTION_BUY, ACTION_EXIT

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Buy the October 16th IBM $250 Call for $6.38
_BUY_MONTH_FIRST = re.compile(
    rf"\b(?:buy|bought)\s+the\s+"
    rf"({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s+([A-Z]{{1,6}})\s+\$(\d+(?:\.\d+)?)\s+(calls?|puts?)"
    rf"(?:\s+for\s+\$(\d+(?:\.\d+)?))?",
    re.I,
)

# Buy the NOK October 16 $10 Call for $0.79
_BUY_TICKER_FIRST = re.compile(
    rf"\b(?:buy|bought)\s+the\s+"
    rf"([A-Z]{{1,6}})\s+({_MONTH_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s+\$(\d+(?:\.\d+)?)\s+(calls?|puts?)"
    rf"(?:\s+for\s+\$(\d+(?:\.\d+)?))?",
    re.I,
)

# Subject: RRP - BUY RUN 16October 9 Calls
_SUBJ_DAY_MONTH_STRIKE = re.compile(
    rf"\b(?:buy|buying)\s+([A-Z]{{1,6}})\s+"
    rf"(\d{{1,2}})({_MONTH_ALT})\s+(\d+(?:\.\d+)?)\s+(calls?|puts?)\b",
    re.I,
)

_RRP_BUY_SUBJ = re.compile(
    r"\b(?:rrp\s*-\s*)?(?:buy|buying)\s+(?!calls?\b|puts?\b)([A-Z]{1,6})\b",
    re.I,
)
_RRP_LOCK_SUBJ = re.compile(
    r"\block\s+in\s+([A-Z]{1,6})\s+(calls?|puts?)\b",
    re.I,
)
_TRADE_LOG = re.compile(r"\btrade\s+log\b", re.I)
_VERTICAL = re.compile(r"\bverticals?\b", re.I)
_SELL_THE = re.compile(r"\band\s+sell\s+the\b", re.I)
_LOCK_IN = re.compile(r"\block\s+in\b", re.I)


def make_contract_key(
    symbol: str,
    expiry: str,
    strike: float | None,
    right: str,
) -> str:
    """Stable Buy vs Trade Log identity: symbol+expiry+strike+right."""
    if not symbol or not expiry or strike is None or not right:
        return ""
    if float(strike).is_integer():
        strike_s = str(int(strike))
    else:
        strike_s = f"{float(strike):.8g}"
    return f"{symbol.upper()}|{expiry}|{strike_s}|{right.upper()}"


def is_trade_log(text: str) -> bool:
    return bool(_TRADE_LOG.search(text or ""))


def is_lock_in(text: str) -> bool:
    return bool(_LOCK_IN.search(text or ""))


def is_rrp_vertical(text: str) -> bool:
    return bool(_VERTICAL.search(text or "") or _SELL_THE.search(text or ""))


def _expiry_iso(month_token: str, day_token: str, as_of: datetime | None) -> str:
    month = _MONTHS.get(month_token.strip().lower())
    if month is None:
        return ""
    try:
        day = int(day_token)
    except (TypeError, ValueError):
        return ""
    if as_of is not None:
        year = as_of.year
        as_of_d = as_of.date()
    else:
        year = date.today().year
        as_of_d = date.today()
    try:
        exp = date(year, month, day)
    except ValueError:
        return ""
    if exp < as_of_d:
        try:
            exp = date(year + 1, month, day)
        except ValueError:
            return ""
    return exp.isoformat()


def _norm_right(token: str) -> str:
    t = (token or "").strip().upper()
    if t in ("CALL", "CALLS"):
        return "CALL"
    if t in ("PUT", "PUTS"):
        return "PUT"
    return ""


def _row(
    *,
    symbol: str,
    right: str,
    expiry: str,
    strike: float | None,
    premium: float | None,
) -> dict[str, Any]:
    return {
        "symbol": symbol.upper() if symbol else "",
        "right": right,
        "expiry": expiry,
        "strike": strike,
        "premium": premium,
        "contract_key": make_contract_key(symbol, expiry, strike, right),
    }


def extract_option_legs(text: str, as_of: datetime | None) -> list[dict[str, Any]]:
    """Single-leg Buy/bought lines in body (and compact subject forms)."""
    blob = text or ""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(row: dict[str, Any]) -> None:
        key = row.get("contract_key") or repr(row)
        if key in seen:
            return
        seen.add(key)
        found.append(row)

    for m in _BUY_MONTH_FIRST.finditer(blob):
        right = _norm_right(m.group(5))
        prem = float(m.group(6)) if m.group(6) else None
        _add(
            _row(
                symbol=m.group(3),
                right=right,
                expiry=_expiry_iso(m.group(1), m.group(2), as_of),
                strike=float(m.group(4)),
                premium=prem,
            )
        )
    for m in _BUY_TICKER_FIRST.finditer(blob):
        right = _norm_right(m.group(5))
        prem = float(m.group(6)) if m.group(6) else None
        _add(
            _row(
                symbol=m.group(1),
                right=right,
                expiry=_expiry_iso(m.group(2), m.group(3), as_of),
                strike=float(m.group(4)),
                premium=prem,
            )
        )
    for m in _SUBJ_DAY_MONTH_STRIKE.finditer(blob):
        right = _norm_right(m.group(5))
        _add(
            _row(
                symbol=m.group(1),
                right=right,
                expiry=_expiry_iso(m.group(3), m.group(2), as_of),
                strike=float(m.group(4)),
                premium=None,
            )
        )
    return found


def parse_rrp_overlay(
    payload: dict[str, Any],
    text: str,
    *,
    as_of: datetime | None,
) -> dict[str, Any]:
    """Overlay fields from RRP email subject/body. Empty dict if not RRP-shaped."""
    blob = text or ""
    kind = str(payload.get("kind") or "").strip().lower()
    overlay: dict[str, Any] = {}

    if is_rrp_vertical(blob):
        overlay["rrp_multi_leg"] = True

    legs = extract_option_legs(blob, as_of)
    if len(legs) > 1:
        overlay["rrp_multi_leg"] = True

    lock = is_lock_in(blob) or kind.startswith("exit")
    if lock:
        overlay["action"] = ACTION_EXIT
        m = _RRP_LOCK_SUBJ.search(blob)
        if m:
            overlay["symbol"] = m.group(1).upper()
            overlay["direction"] = _norm_right(m.group(2))
            overlay["instrument"] = "option"
        if legs:
            leg = legs[0]
            overlay.setdefault("symbol", leg["symbol"])
            overlay.setdefault("direction", leg["right"])
            overlay["instrument"] = "option"
            overlay["expiry"] = leg["expiry"]
            overlay["strike"] = leg["strike"]
            overlay["premium"] = leg["premium"]
            overlay["contract_key"] = leg["contract_key"]
        return overlay

    if legs:
        leg = legs[0]
        overlay["action"] = ACTION_BUY
        overlay["symbol"] = leg["symbol"]
        overlay["direction"] = leg["right"]
        overlay["instrument"] = "option"
        overlay["expiry"] = leg["expiry"]
        overlay["strike"] = leg["strike"]
        overlay["premium"] = leg["premium"]
        overlay["contract_key"] = leg["contract_key"]
        if is_trade_log(blob):
            overlay["trade_log"] = True
        return overlay

    m = _RRP_BUY_SUBJ.search(blob)
    if m and not lock:
        overlay["action"] = ACTION_BUY
        overlay["symbol"] = m.group(1).upper()
        if re.search(r"\bputs?\b", blob, re.I):
            overlay["direction"] = "PUT"
            overlay["instrument"] = "option"
        elif re.search(r"\bcalls?\b", blob, re.I):
            overlay["direction"] = "CALL"
            overlay["instrument"] = "option"
        if is_trade_log(blob):
            overlay["trade_log"] = True
    return overlay
