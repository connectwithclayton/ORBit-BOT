"""Tradier PAPER HTTP client — isolated ``place_order``, no OpenD.

Default base URL is the Tradier sandbox. Live ``api.tradier.com`` is refused
unless ``FABIO_ALLOW_REAL_TRADING=1`` (same allow flag as Moomoo paper pin).
Does not read ``MOOMOO_TRADE_ENV``. Does not exercise options.

Inject ``session`` in tests; production uses ``requests.Session``.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urljoin

import requests

from paper_pin import (
    LiveFundsRefused,
    TRADIER_LIVE_BASE_URL,
    TRADIER_PAPER_BASE_URL,
    enforce_tradier_paper_pin,
    resolve_tradier_env_name,
)

# OCC-style option: underlying + yymmdd + C/P + 8-digit strike (Tradier, no US. prefix)
_OCC_OPTION = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", re.IGNORECASE)

_EXERCISE_SIDES = frozenset({"exercise", "assignment", "exercise_option"})


class TradierAPIError(RuntimeError):
    """HTTP or payload error from Tradier (never includes the access token)."""


def looks_like_occ_option(symbol: str) -> bool:
    """True for Tradier OCC option symbols (not Moomoo ``US.*`` codes)."""
    s = (symbol or "").strip().upper()
    if s.startswith("US."):
        return False
    return bool(_OCC_OPTION.match(s))


def occ_underlying(symbol: str) -> str:
    """Underlying root from an OCC option symbol; else the symbol itself."""
    s = (symbol or "").strip().upper()
    if not _OCC_OPTION.match(s):
        return s
    return re.sub(r"\d{6}[CP]\d{8}$", "", s)


def closing_side(symbol: str, quantity: float) -> str:
    """Market close side. Never exercise / assignment."""
    qty = float(quantity)
    if qty == 0:
        raise ValueError("quantity is zero")
    option = looks_like_occ_option(symbol)
    if qty > 0:
        return "sell_to_close" if option else "sell"
    return "buy_to_close" if option else "buy"


def unwrap_collection(payload: Any, collection_key: str, item_key: str) -> list[dict]:
    """Normalize Tradier JSON: null / single object / list under a collection key."""
    if payload is None or payload == "null":
        return []
    if not isinstance(payload, dict):
        return []
    node = payload.get(collection_key, payload)
    if node is None or node == "null":
        return []
    if isinstance(node, dict) and item_key in node:
        inner = node[item_key]
        if inner is None or inner == "null":
            return []
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            return [inner]
        return []
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    if isinstance(node, dict):
        return [node]
    return []


class TradierPaperClient:
    """Sandbox Tradier REST client. Paper pin is enforced at construction."""

    PAPER_BASE_URL = TRADIER_PAPER_BASE_URL
    LIVE_BASE_URL = TRADIER_LIVE_BASE_URL

    def __init__(
        self,
        *,
        access_token: str,
        account_id: str,
        env: str = "paper",
        base_url: str | None = None,
        session: Any | None = None,
        timeout_sec: float = 30.0,
    ) -> None:
        resolved_env = resolve_tradier_env_name(env)
        resolved_base = (base_url or "").strip() or None
        if resolved_base is None:
            resolved_base = (
                self.LIVE_BASE_URL if resolved_env == "live" else self.PAPER_BASE_URL
            )
        enforce_tradier_paper_pin(resolved_env, base_url=resolved_base)
        token = (access_token or "").strip()
        acct = (account_id or "").strip()
        if not token:
            raise ValueError("TRADIER_ACCESS_TOKEN is required")
        if not acct:
            raise ValueError("TRADIER_ACCOUNT_ID is required")
        self.env = resolved_env
        self.account_id = acct
        self.base_url = resolved_base.rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self._token = token
        self._session = session if session is not None else requests.Session()

    @classmethod
    def from_env(
        cls,
        *,
        session: Any | None = None,
        env_override: str | None = None,
        timeout_sec: float = 30.0,
    ) -> TradierPaperClient:
        env = resolve_tradier_env_name(env_override)
        base = os.getenv("TRADIER_BASE_URL", "").strip() or None
        token = (
            os.getenv("TRADIER_ACCESS_TOKEN", "").strip()
            or os.getenv("TRADIER_TOKEN", "").strip()
        )
        account = os.getenv("TRADIER_ACCOUNT_ID", "").strip()
        return cls(
            access_token=token,
            account_id=account,
            env=env,
            base_url=base,
            session=session,
            timeout_sec=timeout_sec,
        )

    def _headers(self, *, form: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    @staticmethod
    def _redirect_location(resp: Any, request_url: str) -> str | None:
        headers = getattr(resp, "headers", None) or {}
        raw = None
        getter = getattr(headers, "get", None)
        if callable(getter):
            raw = getter("Location") or getter("location")
        if not raw and isinstance(headers, dict):
            for key, value in headers.items():
                if str(key).lower() == "location":
                    raw = value
                    break
        if not raw:
            return None
        return urljoin(request_url, str(raw))

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        enforce_tradier_paper_pin(self.env, base_url=url)
        try:
            resp = self._session.request(
                method.upper(),
                url,
                headers=self._headers(form=data is not None),
                data=data,
                params=params,
                timeout=self.timeout_sec,
                allow_redirects=False,
            )
        except LiveFundsRefused:
            raise
        except Exception as exc:
            raise TradierAPIError(f"{method.upper()} {path} failed: {type(exc).__name__}") from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        if 300 <= status < 400:
            location = self._redirect_location(resp, url)
            if location:
                enforce_tradier_paper_pin(self.env, base_url=location)
            raise TradierAPIError(
                f"{method.upper()} {path} HTTP {status}: redirects disabled"
            )
        try:
            payload = resp.json() if hasattr(resp, "json") else {}
        except Exception:
            payload = {}
        if status >= 400 or status == 0:
            snippet = ""
            if isinstance(payload, dict) and payload:
                snippet = str({k: payload[k] for k in list(payload)[:6]})
            else:
                snippet = (getattr(resp, "text", "") or "")[:200]
            raise TradierAPIError(f"{method.upper()} {path} HTTP {status}: {snippet}")
        if not isinstance(payload, dict):
            return {}
        return payload

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        duration: str = "day",
        option_symbol: str | None = None,
        price: float | None = None,
        preview: bool = False,
        tag: str | None = None,
    ) -> dict:
        """POST /v1/accounts/{id}/orders. Market closes only; no exercise."""
        side_l = (side or "").strip().lower()
        if side_l in _EXERCISE_SIDES:
            raise ValueError("exercise/assignment is not supported")
        qty = int(quantity)
        if qty <= 0:
            raise ValueError("quantity must be positive")
        opt = (option_symbol or "").strip().upper() or None
        if opt is None and looks_like_occ_option(symbol):
            opt = symbol.strip().upper()
        if opt:
            underlying = occ_underlying(opt)
            asset_class = "option"
        else:
            underlying = (symbol or "").strip().upper()
            asset_class = "equity"
        if not underlying:
            raise ValueError("symbol is required")
        body: dict[str, Any] = {
            "class": asset_class,
            "symbol": underlying,
            "side": side_l,
            "quantity": str(qty),
            "type": (order_type or "market").strip().lower(),
            "duration": (duration or "day").strip().lower(),
        }
        if opt:
            body["option_symbol"] = opt
        if price is not None and body["type"] != "market":
            body["price"] = str(price)
        if preview:
            body["preview"] = "true"
        if tag:
            body["tag"] = tag
        payload = self._request(
            "POST",
            f"/v1/accounts/{self.account_id}/orders",
            data=body,
        )
        return payload

    def list_positions(self) -> list[dict]:
        payload = self._request("GET", f"/v1/accounts/{self.account_id}/positions")
        return unwrap_collection(payload, "positions", "position")

    def flatten_open_positions(
        self,
        *,
        scope: str = "options",
        dry_run: bool = False,
        sleep_fn=None,
        sleep_between_orders: float = 0.0,
        only_symbols: Any | None = None,
        exclude_symbols: Any | None = None,
    ) -> dict[str, Any]:
        """Market-flatten open rows. Options-only by default. Never exercises.

        ``only_symbols`` / ``exclude_symbols`` scope a single paper book so
        ORB-Tradier flatten cannot close MR-Tradier (and vice versa).
        """
        if scope not in ("options", "all"):
            raise ValueError("scope must be 'options' or 'all'")
        only = None
        if only_symbols is not None:
            only = {str(s).strip().upper() for s in only_symbols if str(s).strip()}
        exclude = {
            str(s).strip().upper() for s in (exclude_symbols or []) if str(s).strip()
        }
        positions = self.list_positions()
        planned: list[dict[str, Any]] = []
        for row in positions:
            symbol = str(row.get("symbol") or "").strip().upper()
            try:
                qty = float(row.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty == 0 or not symbol:
                continue
            if scope == "options" and not looks_like_occ_option(symbol):
                continue
            if only is not None and symbol not in only:
                continue
            if symbol in exclude:
                continue
            side = closing_side(symbol, qty)
            planned.append(
                {
                    "symbol": symbol,
                    "quantity": abs(int(qty)),
                    "side": side,
                    "raw_qty": qty,
                }
            )

        results: list[dict[str, Any]] = []
        failures = 0
        for i, item in enumerate(planned):
            if dry_run:
                results.append({**item, "status": "dry_run"})
                continue
            try:
                resp = self.place_order(
                    symbol=item["symbol"],
                    side=item["side"],
                    quantity=item["quantity"],
                    order_type="market",
                    duration="day",
                    tag=f"eod_tf_{i + 1:03d}",
                )
                results.append({**item, "status": "ok", "response": resp})
            except (TradierAPIError, ValueError) as exc:
                failures += 1
                results.append({**item, "status": "error", "error": str(exc)})
            if sleep_fn is not None and sleep_between_orders > 0:
                sleep_fn(sleep_between_orders)

        return {
            "scope": scope,
            "dry_run": dry_run,
            "env": self.env,
            "planned": len(planned),
            "failures": failures,
            "results": results,
        }
