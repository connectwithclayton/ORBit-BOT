"""Tradier paper client + flatten (FM-FABIO-SCOUT-001 slice 3). Mocked HTTP only."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from brokers.tradier.client import (
    TradierAPIError,
    TradierPaperClient,
    closing_side,
    looks_like_occ_option,
    occ_underlying,
    unwrap_collection,
)
from paper_pin import (
    ALLOW_REAL_ENV,
    LiveFundsRefused,
    TRADIER_LIVE_BASE_URL,
    TRADIER_PAPER_BASE_URL,
    enforce_tradier_paper_pin,
    is_tradier_live_env,
    normalize_tradier_hostname,
    resolve_tradier_env_name,
    tradier_host_is_live,
    tradier_host_is_sandbox,
)
from tradier_eod_flatten import build_arg_parser, default_tradier_env_choice, main

BACKEND = Path(__file__).resolve().parents[1]


class FakeResp:
    def __init__(self, status: int, payload, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = dict(headers or {})
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResp] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def request(
        self,
        method,
        url,
        headers=None,
        data=None,
        params=None,
        timeout=None,
        allow_redirects=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "data": dict(data) if isinstance(data, dict) else data,
                "params": params,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected HTTP {method} {url}")
        return self.responses.pop(0)


def _client(session: FakeSession, **kwargs) -> TradierPaperClient:
    defaults = dict(
        access_token="test-token",
        account_id="VA0001",
        env="paper",
        session=session,
    )
    defaults.update(kwargs)
    return TradierPaperClient(**defaults)


def test_looks_like_occ_option_tradier_not_moomoo():
    assert looks_like_occ_option("SPY260918C00500000") is True
    assert looks_like_occ_option("AAPL260918P00190000") is True
    assert looks_like_occ_option("SPY") is False
    assert looks_like_occ_option("US.SPY250509C00530000") is False
    assert occ_underlying("SPY260918C00500000") == "SPY"
    assert occ_underlying("AAPL") == "AAPL"


def test_closing_side_market_never_exercise():
    assert closing_side("SPY260918C00500000", 2) == "sell_to_close"
    assert closing_side("SPY260918C00500000", -1) == "buy_to_close"
    assert closing_side("AAPL", 10) == "sell"
    assert closing_side("AAPL", -5) == "buy"
    with pytest.raises(ValueError):
        closing_side("SPY", 0)


def test_unwrap_positions_null_single_and_list():
    assert unwrap_collection({"positions": "null"}, "positions", "position") == []
    assert unwrap_collection({"positions": {"position": None}}, "positions", "position") == []
    one = unwrap_collection(
        {"positions": {"position": {"symbol": "SPY260918C00500000", "quantity": 1}}},
        "positions",
        "position",
    )
    assert len(one) == 1
    many = unwrap_collection(
        {
            "positions": {
                "position": [
                    {"symbol": "A", "quantity": 1},
                    {"symbol": "B", "quantity": 2},
                ]
            }
        },
        "positions",
        "position",
    )
    assert [r["symbol"] for r in many] == ["A", "B"]


def test_resolve_tradier_env_defaults_paper_independent_of_moomoo(monkeypatch):
    monkeypatch.delenv("TRADIER_ENV", raising=False)
    monkeypatch.setenv("MOOMOO_TRADE_ENV", "REAL")
    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    assert resolve_tradier_env_name() == "paper"
    assert is_tradier_live_env(env="paper") is False
    assert is_tradier_live_env(env="SIMULATE") is False
    assert is_tradier_live_env(env="sandbox") is False
    assert is_tradier_live_env(env="live") is True
    assert is_tradier_live_env(env="REAL") is True
    assert is_tradier_live_env(base_url=TRADIER_LIVE_BASE_URL) is True
    assert is_tradier_live_env(base_url=TRADIER_PAPER_BASE_URL) is False


def test_enforce_tradier_refuses_live_without_allow(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    with pytest.raises(LiveFundsRefused) as ei:
        enforce_tradier_paper_pin("live")
    msg = str(ei.value)
    assert ALLOW_REAL_ENV in msg
    assert "Tradier" in msg
    assert "MOOMOO_TRADE_ENV" in msg
    with pytest.raises(LiveFundsRefused):
        enforce_tradier_paper_pin("paper", base_url=TRADIER_LIVE_BASE_URL)
    enforce_tradier_paper_pin("paper")
    enforce_tradier_paper_pin("SIMULATE")


def test_enforce_tradier_live_with_allow_flag(monkeypatch):
    monkeypatch.setenv(ALLOW_REAL_ENV, "1")
    enforce_tradier_paper_pin("live")
    enforce_tradier_paper_pin("paper", base_url=TRADIER_LIVE_BASE_URL)


def test_normalize_tradier_hostname_strips_userinfo_port_trailing_dot():
    assert normalize_tradier_hostname("https://x@api.tradier.com") == "api.tradier.com"
    # Userinfo + path (username-only; avoid Basic-Auth user/password URL shape).
    assert normalize_tradier_hostname("https://x@api.tradier.com/v1") == "api.tradier.com"
    assert normalize_tradier_hostname("https://api.tradier.com.") == "api.tradier.com"
    assert normalize_tradier_hostname("https://api.tradier.com./v1") == "api.tradier.com"
    assert normalize_tradier_hostname("https://api.tradier.com:443") == "api.tradier.com"
    assert normalize_tradier_hostname("https://x@api.tradier.com.") == "api.tradier.com"
    assert (
        normalize_tradier_hostname("https://user@sandbox.tradier.com.")
        == "sandbox.tradier.com"
    )
    assert tradier_host_is_live("https://x@api.tradier.com") is True
    assert tradier_host_is_live("https://api.tradier.com.") is True
    assert tradier_host_is_sandbox("https://x@sandbox.tradier.com.") is True
    assert tradier_host_is_sandbox("https://x@api.tradier.com") is False


def test_obfuscated_live_hosts_refused_without_allow(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    live_urls = (
        "https://x@api.tradier.com",
        "https://api.tradier.com.",
        "https://api.tradier.com./v1",
        "https://API.TRADIER.COM",
        "https://api.tradier.com:443",
        "https://x@api.tradier.com.",
        "http://api.tradier.com",
    )
    for url in live_urls:
        assert is_tradier_live_env(base_url=url) is True, url
        with pytest.raises(LiveFundsRefused):
            enforce_tradier_paper_pin("paper", base_url=url)
        with pytest.raises(LiveFundsRefused):
            _client(FakeSession(), env="paper", base_url=url)


def test_sandbox_userinfo_and_trailing_dot_still_allowed(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    enforce_tradier_paper_pin("paper", base_url="https://x@sandbox.tradier.com")
    enforce_tradier_paper_pin("paper", base_url="https://sandbox.tradier.com.")
    client = _client(FakeSession(), env="paper", base_url="https://x@sandbox.tradier.com.")
    assert tradier_host_is_sandbox(client.base_url) is True


def test_non_sandbox_host_refused_without_allow(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    with pytest.raises(LiveFundsRefused):
        enforce_tradier_paper_pin("paper", base_url="https://evil.example.invalid")
    with pytest.raises(LiveFundsRefused):
        _client(FakeSession(), env="paper", base_url="https://evil.example.invalid")


def test_request_refuses_redirect_to_live_and_disables_follow(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    session = FakeSession(
        [
            FakeResp(
                302,
                {},
                headers={
                    "Location": "https://api.tradier.com/v1/accounts/VA0001/orders"
                },
            )
        ]
    )
    client = _client(session)
    with pytest.raises(LiveFundsRefused):
        client.place_order(symbol="SPY", side="buy", quantity=1)
    assert session.calls[0]["allow_redirects"] is False
    assert session.calls[0]["url"].startswith(TRADIER_PAPER_BASE_URL)


def test_client_constructor_refuses_live_url(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    session = FakeSession()
    with pytest.raises(LiveFundsRefused):
        _client(session, env="live")
    with pytest.raises(LiveFundsRefused):
        _client(session, env="paper", base_url="https://api.tradier.com")
    with pytest.raises(LiveFundsRefused):
        TradierPaperClient.from_env(
            session=session,
            env_override="live",
        )


def test_client_defaults_sandbox_and_place_order_mocked(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)

    def boom(*_a, **_k):
        raise AssertionError("live network forbidden")

    monkeypatch.setattr("socket.create_connection", boom)
    monkeypatch.setattr("requests.sessions.Session.request", boom)

    session = FakeSession(
        [FakeResp(200, {"order": {"id": 218620, "status": "ok"}})]
    )
    client = _client(session)
    assert client.base_url == TRADIER_PAPER_BASE_URL
    assert client.env == "paper"
    out = client.place_order(
        symbol="SPY",
        side="buy_to_open",
        quantity=1,
        option_symbol="SPY260918C00500000",
        order_type="market",
    )
    assert out["order"]["id"] == 218620
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{TRADIER_PAPER_BASE_URL}/v1/accounts/VA0001/orders"
    assert "api.tradier.com" not in call["url"]
    assert call["headers"]["Authorization"] == "Bearer test-token"
    assert call["data"]["class"] == "option"
    assert call["data"]["symbol"] == "SPY"
    assert call["data"]["option_symbol"] == "SPY260918C00500000"
    assert call["data"]["side"] == "buy_to_open"
    assert call["data"]["type"] == "market"
    assert call["timeout"] == 30.0
    assert call["allow_redirects"] is False


def test_place_order_refuses_exercise_side():
    session = FakeSession()
    client = _client(session)
    with pytest.raises(ValueError, match="exercise"):
        client.place_order(symbol="SPY260918C00500000", side="exercise", quantity=1)
    assert session.calls == []


def test_place_order_http_error_does_not_include_token():
    session = FakeSession([FakeResp(401, {"fault": {"faultstring": "Invalid access token"}})])
    client = _client(session)
    with pytest.raises(TradierAPIError) as ei:
        client.place_order(symbol="SPY", side="buy", quantity=1)
    assert "test-token" not in str(ei.value)
    assert "401" in str(ei.value)


def test_from_env_uses_paper_and_injected_session(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("TRADIER_ACCOUNT_ID", "VA0001")
    monkeypatch.delenv("TRADIER_ENV", raising=False)
    monkeypatch.delenv("TRADIER_BASE_URL", raising=False)
    monkeypatch.setenv("MOOMOO_TRADE_ENV", "REAL")
    session = FakeSession([FakeResp(200, {"positions": "null"})])
    client = TradierPaperClient.from_env(session=session)
    assert client.env == "paper"
    assert client.base_url == TRADIER_PAPER_BASE_URL
    assert client.list_positions() == []
    assert session.calls[0]["method"] == "GET"


def test_isolated_place_order_client_builds_tradier_when_flag_set(monkeypatch):
    monkeypatch.setenv("FABIO_BROKER", "tradier")
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("TRADIER_ACCOUNT_ID", "VA0001")
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    monkeypatch.delenv("TRADIER_ENV", raising=False)
    session = FakeSession()
    from brokers.names import isolated_place_order_client

    client = isolated_place_order_client(session=session)
    assert isinstance(client, TradierPaperClient)
    assert client.env == "paper"
    assert client.base_url == TRADIER_PAPER_BASE_URL
    assert session.calls == []


def test_from_env_requires_token(monkeypatch):
    monkeypatch.delenv("TRADIER_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    monkeypatch.setenv("TRADIER_ACCOUNT_ID", "VA0001")
    with pytest.raises(ValueError, match="TRADIER_ACCESS_TOKEN"):
        TradierPaperClient.from_env(session=FakeSession())


def test_flatten_options_only_skips_equity_and_posts_market_close():
    session = FakeSession(
        [
            FakeResp(
                200,
                {
                    "positions": {
                        "position": [
                            {"symbol": "AAPL", "quantity": 10},
                            {"symbol": "SPY260918C00500000", "quantity": 2},
                            {"symbol": "QQQ260918P00400000", "quantity": -1},
                        ]
                    }
                },
            ),
            FakeResp(200, {"order": {"id": 1, "status": "ok"}}),
            FakeResp(200, {"order": {"id": 2, "status": "ok"}}),
        ]
    )
    client = _client(session)
    summary = client.flatten_open_positions(
        scope="options",
        dry_run=False,
        sleep_fn=lambda _s: None,
        sleep_between_orders=0.0,
    )
    assert summary["planned"] == 2
    assert summary["failures"] == 0
    posts = [c for c in session.calls if c["method"] == "POST"]
    assert len(posts) == 2
    sides = {c["data"]["side"] for c in posts}
    assert sides == {"sell_to_close", "buy_to_close"}
    assert all(c["data"]["type"] == "market" for c in posts)
    assert all(c["data"]["class"] == "option" for c in posts)
    assert "exercise" not in json.dumps(posts)


def test_flatten_dry_run_does_not_place_order():
    session = FakeSession(
        [
            FakeResp(
                200,
                {
                    "positions": {
                        "position": {"symbol": "SPY260918C00500000", "quantity": 1}
                    }
                },
            )
        ]
    )
    client = _client(session)
    summary = client.flatten_open_positions(scope="options", dry_run=True)
    assert summary["planned"] == 1
    assert summary["results"][0]["status"] == "dry_run"
    assert [c["method"] for c in session.calls] == ["GET"]


def test_flatten_cli_defaults_paper_not_live(monkeypatch):
    monkeypatch.delenv("TRADIER_ENV", raising=False)
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    monkeypatch.setenv("MOOMOO_TRADE_ENV", "REAL")
    assert default_tradier_env_choice() == "paper"
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.env == "paper"
    assert args.scope == "options"
    help_text = parser.format_help()
    assert "Default: paper" in help_text
    assert "FABIO_ALLOW_REAL_TRADING" in help_text
    assert "MOOMOO_TRADE_ENV" in help_text
    assert "Default: REAL" not in help_text
    assert "Default: live" not in help_text


def test_flatten_cli_live_refused_without_allow(monkeypatch):
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    parser = build_arg_parser()
    args = parser.parse_args(["--env", "live", "--dry-run"])
    assert args.env == "live"
    with pytest.raises(LiveFundsRefused):
        main(["--env", "live", "--dry-run"], client=SimpleNamespace())


def test_flatten_main_dry_run_with_injected_client(capsys):
    class FakeClient:
        def flatten_open_positions(self, **kwargs):
            assert kwargs["dry_run"] is True
            assert kwargs["scope"] == "options"
            return {
                "planned": 1,
                "failures": 0,
                "dry_run": True,
                "env": "paper",
                "results": [
                    {
                        "symbol": "SPY260918C00500000",
                        "quantity": 1,
                        "side": "sell_to_close",
                        "status": "dry_run",
                    }
                ],
            }

    code = main(
        ["--dry-run", "--sleep-between-orders", "0"],
        client=FakeClient(),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "SPY260918C00500000" in out
    assert "Dry run" in out


def test_flatten_main_reports_place_order_failures():
    class FakeClient:
        def flatten_open_positions(self, **kwargs):
            return {
                "planned": 1,
                "failures": 1,
                "dry_run": False,
                "results": [
                    {
                        "symbol": "SPY260918C00500000",
                        "quantity": 1,
                        "side": "sell_to_close",
                        "status": "error",
                    }
                ],
            }

    code = main(["--sleep-between-orders", "0"], client=FakeClient())
    assert code == 3


def test_tradier_modules_do_not_import_moomoo():
    files = [
        BACKEND / "brokers" / "tradier" / "client.py",
        BACKEND / "brokers" / "tradier" / "__init__.py",
        BACKEND / "tradier_eod_flatten.py",
        BACKEND / "brokers" / "names.py",
    ]
    forbidden = {"moomoo", "futu"}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden, path.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, path.name
        src = path.read_text(encoding="utf-8")
        assert "OpenSecTradeContext" not in src
        assert "OpenQuoteContext" not in src


def test_print_effective_config_reports_moomoo_broker_and_tradier_paper(
    monkeypatch, capsys
):
    import print_effective_config as pec
    from moomoo import TrdEnv

    monkeypatch.setattr(pec, "MOOMOO_TRADE_ENV", TrdEnv.SIMULATE)
    monkeypatch.delenv(ALLOW_REAL_ENV, raising=False)
    monkeypatch.delenv("FABIO_BROKER", raising=False)
    monkeypatch.delenv("TRADIER_ENV", raising=False)
    monkeypatch.delenv("TRADIER_ACCESS_TOKEN", raising=False)
    pec.main()
    out = capsys.readouterr().out
    assert '"fabio_broker": "moomoo"' in out
    assert '"tradier_env": "paper"' in out
    assert '"tradier_access_token": "(not set)"' in out
    assert "OpenQuoteContext" in out
    assert "isolated place_order" in out
