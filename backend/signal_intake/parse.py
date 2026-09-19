"""Parse anonymized MR email/site payloads. No I/O, no brokers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from signal_intake.ids import IdempotencyStore, make_raw_id
from signal_intake.models import (
    DECISION_SHADOW,
    DECISION_SKIP,
    MODE_SHADOW,
    SOURCE_MR,
    NormalizedIntent,
)

ET = ZoneInfo("America/New_York")

SKIP_MULTI_LEG = "multi-leg"
SKIP_DUPLICATE = "duplicate"
SKIP_UNPARSEABLE = "unparseable"
SKIP_MISSING_TS = "missing-ts"

_TICKER_FIELD = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z]{1,2})?$")
_TICKER_LABEL = re.compile(
    r"\b(?:ticker|symbol)\s*[:=]\s*\$?([A-Z]{1,6}(?:\.[A-Z]{1,2})?)\b",
    re.I,
)
_CASHTAG = re.compile(r"\$([A-Z]{1,5})\b")
_ALERT_SUBJ = re.compile(r"\balert:\s*\$?([A-Z]{1,6})\b", re.I)
_DIR_LABEL = re.compile(
    r"\b(?:direction|right)\s*[:=]\s*(calls?|puts?|equity|stock|shares)\b",
    re.I,
)
_BUY_CALLS = re.compile(r"\b(?:buy|buying|long)\s+calls?\b", re.I)
_BUY_PUTS = re.compile(r"\b(?:buy|buying|long)\s+puts?\b", re.I)
_LOOKING_PUTS = re.compile(r"\b(?:looking at|look at)\s+puts?\b", re.I)
_LOOKING_CALLS = re.compile(r"\b(?:looking at|look at)\s+calls?\b", re.I)
_EQUITY_HINT = re.compile(r"\b(?:shares?|equity|stock)\b", re.I)
_CONF_PCT = re.compile(
    r"\bconf(?:idence)?\s*[:=]?\s*(\d{1,3}(?:\.\d+)?)\s*%",
    re.I,
)
_CONF_NUM = re.compile(
    r"\bconf(?:idence)?\s*[:=]\s*(\d(?:\.\d+)?)\b",
    re.I,
)
_CONF_WORD = re.compile(
    r"\bconf(?:idence)?\s*[:=]\s*(high|strong|medium|moderate|low|weak)\b",
    re.I,
)
_MULTI_TEXT = re.compile(
    r"\b(?:iron\s+condor|condor|butterfly|straddle|strangle|"
    r"(?:call|put|debit|credit|vertical|calendar|diagonal)\s+spread|"
    r"multi[\s-]?leg)\b",
    re.I,
)

_CONF_WORDS = {
    "high": 0.85,
    "strong": 0.85,
    "medium": 0.60,
    "moderate": 0.60,
    "low": 0.35,
    "weak": 0.35,
}

_MULTI_STRUCTURE = {
    "spread",
    "call_spread",
    "put_spread",
    "debit_spread",
    "credit_spread",
    "vertical",
    "vertical_spread",
    "calendar",
    "calendar_spread",
    "diagonal",
    "iron_condor",
    "condor",
    "butterfly",
    "straddle",
    "strangle",
    "combo",
    "multi-leg",
    "multileg",
    "multi_leg",
}

# Explicit single-leg labels only. "option" / "equity" / "stock" are instrument
# types, not a promise the payload is one leg — those must still scan body text.
_TRUE_SINGLE_STRUCTURE = {
    "single",
    "single-leg",
    "single_leg",
    "outright",
}

_BUY_SELL = {"buy", "sell", "long", "short"}

# Unix ms (current era ~1.7e12) vs seconds (~1.7e9). Seconds this large are
# far-future and must not SHADOW-accept.
_UNIX_MS_THRESHOLD = 1_000_000_000_000  # 1e12
_TS_YEAR_MIN = 1970
_TS_YEAR_MAX = 2100


def parse_confidence(value: Any, text: str = "") -> float | None:
    """Return 0..1 confidence, or None if absent/unparseable."""
    parsed = _confidence_from_value(value)
    if parsed is not None:
        return parsed
    return _confidence_from_text(text)


def _clamp_conf(n: float) -> float | None:
    if n > 1.0 and n <= 100.0:
        n = n / 100.0
    if 0.0 <= n <= 1.0:
        return round(n, 4)
    return None


def _confidence_from_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _clamp_conf(float(value))
    s = str(value).strip().lower()
    if s in _CONF_WORDS:
        return _CONF_WORDS[s]
    if s.endswith("%"):
        try:
            return _clamp_conf(float(s[:-1].strip()))
        except ValueError:
            return None
    try:
        return _clamp_conf(float(s))
    except ValueError:
        return None


def _confidence_from_text(text: str) -> float | None:
    if not text:
        return None
    m = _CONF_PCT.search(text)
    if m:
        return _clamp_conf(float(m.group(1)))
    w = _CONF_WORD.search(text)
    if w:
        return _CONF_WORDS[w.group(1).lower()]
    n = _CONF_NUM.search(text)
    if n:
        return _clamp_conf(float(n.group(1)))
    return None


def _datetime_to_et_iso(dt: datetime) -> str | None:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    else:
        dt = dt.astimezone(ET)
    if dt.year < _TS_YEAR_MIN or dt.year > _TS_YEAR_MAX:
        return None
    return dt.isoformat()


def parse_as_of_et(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    numeric: float | None = None
    if isinstance(raw, (int, float)):
        numeric = float(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        numeric = float(raw.strip())
    if numeric is not None:
        if numeric >= _UNIX_MS_THRESHOLD:
            numeric = numeric / 1000.0
        try:
            dt = datetime.fromtimestamp(numeric, tz=ET)
        except (OverflowError, OSError, ValueError):
            return None
        return _datetime_to_et_iso(dt)
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return _datetime_to_et_iso(dt)


def _blob(payload: dict[str, Any]) -> str:
    parts = [
        str(payload.get("subject") or ""),
        str(payload.get("body_text") or payload.get("body") or payload.get("text") or ""),
    ]
    return "\n".join(parts)


def _extract_symbol(payload: dict[str, Any], text: str) -> str:
    for key in ("ticker", "symbol"):
        raw = payload.get(key)
        if raw is None:
            continue
        token = str(raw).strip().lstrip("$").upper()
        if _TICKER_FIELD.match(token):
            return token
    m = _TICKER_LABEL.search(text)
    if m:
        return m.group(1).upper()
    c = _CASHTAG.search(text)
    if c:
        return c.group(1).upper()
    a = _ALERT_SUBJ.search(text)
    if a:
        return a.group(1).upper()
    return ""


def _norm_right(token: str) -> str:
    t = token.strip().upper()
    if t in ("CALL", "CALLS"):
        return "CALL"
    if t in ("PUT", "PUTS"):
        return "PUT"
    if t in ("EQUITY", "STOCK", "SHARES", "SHARE"):
        return "EQUITY"
    return ""


def _extract_direction_and_instrument(
    payload: dict[str, Any], text: str
) -> tuple[str, str]:
    instrument_raw = str(
        payload.get("instrument") or payload.get("asset_class") or ""
    ).strip().lower()
    if instrument_raw in ("equity", "stock", "shares", "share"):
        return "EQUITY", "equity"

    for key in ("right", "direction"):
        raw = payload.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        if key == "direction" and str(raw).strip().lower() in _BUY_SELL:
            continue
        mapped = _norm_right(str(raw))
        if mapped == "EQUITY":
            return "EQUITY", "equity"
        if mapped in ("CALL", "PUT"):
            return mapped, "option"

    side = payload.get("side")
    if side is not None and str(side).strip().lower() not in _BUY_SELL:
        mapped = _norm_right(str(side))
        if mapped == "EQUITY":
            return "EQUITY", "equity"
        if mapped in ("CALL", "PUT"):
            return mapped, "option"

    m = _DIR_LABEL.search(text)
    if m:
        mapped = _norm_right(m.group(1))
        if mapped == "EQUITY":
            return "EQUITY", "equity"
        if mapped in ("CALL", "PUT"):
            return mapped, "option"

    if _BUY_PUTS.search(text) or _LOOKING_PUTS.search(text):
        return "PUT", "option"
    if _BUY_CALLS.search(text) or _LOOKING_CALLS.search(text):
        return "CALL", "option"

    if instrument_raw in ("option", "options"):
        return "", "option"

    if _EQUITY_HINT.search(text) and not (_BUY_CALLS.search(text) or _BUY_PUTS.search(text)):
        return "EQUITY", "equity"

    return "", ""


def _legs_count(payload: dict[str, Any]) -> int | None:
    legs = payload.get("legs")
    if legs is None:
        return None
    if isinstance(legs, int) and not isinstance(legs, bool):
        return legs
    if isinstance(legs, list):
        return len(legs)
    return None


def _is_multi_leg(payload: dict[str, Any], text: str) -> bool:
    """True if legs>1, named multi structure, or body text looks multi-leg.

    True single-leg tokens (single / single_leg / outright) do not by themselves
    skip the body-text scan: option/equity/stock used to short-circuit and hide
    phrases like "call spread" / "iron condor". Always OR with ``_MULTI_TEXT``.
    """
    n = _legs_count(payload)
    legs_multi = n is not None and n > 1
    structure = str(
        payload.get("structure") or payload.get("strategy") or ""
    ).strip().lower().replace(" ", "_")
    structure_multi = structure in _MULTI_STRUCTURE
    text_multi = bool(_MULTI_TEXT.search(text))
    if structure in _TRUE_SINGLE_STRUCTURE and not legs_multi and not text_multi:
        return False
    return bool(legs_multi or structure_multi or text_multi)


def _reason(
    *,
    decision: str,
    raw_id: str,
    confidence: float | None,
    skip: str | None,
    symbol: str,
    direction: str,
) -> str:
    parts = [
        f"source={SOURCE_MR}",
        f"mode={MODE_SHADOW}",
        f"decision={decision}",
        f"id={raw_id}",
    ]
    if confidence is not None:
        parts.append(f"conf={confidence:.4f}")
    else:
        parts.append("conf=")
    if skip:
        parts.append(f"skip={skip}")
    else:
        parts.append(f"mapped={symbol} {direction}".rstrip())
    return " | ".join(parts)


def parse_payload(
    payload: dict[str, Any],
    *,
    store: IdempotencyStore | None = None,
) -> NormalizedIntent:
    """Normalize one MR payload. Always returns an intent; never places an order."""
    if not isinstance(payload, dict):
        payload = {}
    channel = str(payload.get("channel") or "").strip().lower() or "unknown"
    raw_id = make_raw_id(payload)
    text = _blob(payload)
    ts = parse_as_of_et(
        payload.get("published_at")
        or payload.get("received_at")
        or payload.get("as_of_et")
        or payload.get("as_of")
        or payload.get("ts")
    )
    symbol = _extract_symbol(payload, text)
    direction, instrument = _extract_direction_and_instrument(payload, text)
    confidence = parse_confidence(payload.get("confidence"), text)

    skip: str | None = None
    if store is not None and store.seen_or_add(raw_id):
        skip = SKIP_DUPLICATE
    elif _is_multi_leg(payload, text):
        skip = SKIP_MULTI_LEG
        direction = ""
        instrument = ""
    elif not ts:
        skip = SKIP_MISSING_TS
        direction = ""
        instrument = ""
    elif not symbol or direction not in ("CALL", "PUT", "EQUITY"):
        skip = SKIP_UNPARSEABLE
        direction = ""
        instrument = ""
        if not symbol:
            symbol = "UNKNOWN"

    if skip:
        decision = DECISION_SKIP
        accepted = False
        if not symbol:
            symbol = "UNKNOWN"
    else:
        decision = DECISION_SHADOW
        accepted = True

    reason = _reason(
        decision=decision,
        raw_id=raw_id,
        confidence=confidence,
        skip=skip,
        symbol=symbol,
        direction=direction,
    )
    return NormalizedIntent(
        source=SOURCE_MR,
        symbol=symbol,
        direction=direction,
        instrument=instrument,
        as_of_et=ts or "",
        raw_id=raw_id,
        confidence=confidence,
        decision=decision,
        reason=reason,
        accepted=accepted,
        skip=skip,
        channel=channel,
    )
