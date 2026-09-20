"""MR signal intake (slice 2): fixture replay, shadow emit, no broker calls."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from signal_intake.ids import IdempotencyStore, make_raw_id
from signal_intake.models import (
    DECISION_SHADOW,
    DECISION_SKIP,
    MODE_SHADOW,
    SOURCE_MR,
    NormalizedIntent,
)
from signal_intake.parse import parse_as_of_et, parse_confidence, parse_payload
from signal_intake.replay import (
    FIXTURE_DIR,
    GOLDEN_PATH,
    intents_to_stable_json,
    replay_fixture_dir,
    replay_stable_records,
)
from signal_intake.shadow import emit_shadow, format_telegram

BACKEND_ROOT = Path(__file__).resolve().parents[1]
INTAKE_ROOT = BACKEND_ROOT / "signal_intake"
REPO_ROOT = BACKEND_ROOT.parent

FORBIDDEN_IMPORT_ROOTS = {
    "moomoo",
    "futu",
    "tradier",
    "fabio_live",
    "config",
}

FORBIDDEN_NAMES = {"place_order", "OpenSecTradeContext", "OpenD", "OrderManager"}


class _RecordingOps:
    def __init__(self) -> None:
        self.decisions: list[tuple[tuple, dict]] = []
        self.alerts: list[str] = []

    def log_decision(self, *args, **kwargs):
        self.decisions.append((args, kwargs))

    def alert(self, text: str):
        self.alerts.append(text)


def test_fixture_replay_is_byte_stable():
    first = json.dumps(replay_stable_records(), indent=2, sort_keys=True)
    second = json.dumps(replay_stable_records(), indent=2, sort_keys=True)
    assert first == second
    assert GOLDEN_PATH.is_file()
    golden = GOLDEN_PATH.read_text(encoding="utf-8")
    assert first + "\n" == golden


def test_replay_maps_call_put_equity_and_drops_multi_leg():
    rows = replay_stable_records()
    records = {}
    for r in rows:
        records.setdefault(r["raw_id"], r)
    spy = records["mr:mr-fx-001"]
    assert spy["accepted"] is True
    assert spy["source"] == SOURCE_MR
    assert spy["decision"] == DECISION_SHADOW
    assert spy["mapped"] == {
        "symbol": "SPY",
        "side": "CALL",
        "ts": "2026-09-18T10:15:00-04:00",
    }
    assert spy["confidence"] == 0.82

    qqq = records["mr:mr-fx-002@alerts.example.invalid"]
    assert qqq["mapped"]["symbol"] == "QQQ"
    assert qqq["mapped"]["side"] == "PUT"
    assert qqq["confidence"] == 0.75
    assert qqq["decision"] == DECISION_SHADOW

    eq = records["mr:mr-fx-003"]
    assert eq["mapped"]["side"] == "EQUITY"
    assert eq["instrument"] == "equity"
    assert eq["confidence"] == 0.85

    spread = records["mr:mr-fx-004"]
    assert spread["accepted"] is False
    assert spread["skip"] == "multi-leg"
    assert spread["decision"] == DECISION_SKIP
    assert spread["direction"] == ""
    assert spread["mapped"]["side"] == ""

    junk = records["mr:mr-fx-005@alerts.example.invalid"]
    assert junk["skip"] == "unparseable"
    assert junk["decision"] == DECISION_SKIP

    dup = [r for r in replay_stable_records() if r["skip"] == "duplicate"]
    assert len(dup) == 1
    assert dup[0]["raw_id"] == "mr:mr-fx-001"
    assert dup[0]["decision"] == DECISION_SKIP

    amd = records["mr:mr-fx-007@alerts.example.invalid"]
    assert amd["mapped"]["symbol"] == "AMD"
    assert amd["mapped"]["side"] == "CALL"
    assert amd["confidence"] == 0.6


def test_raw_id_is_idempotent_for_same_payload():
    payload = {
        "channel": "site",
        "alert_id": "mr-fx-001",
        "ticker": "SPY",
        "right": "call",
        "published_at": "2026-09-18T10:15:00-04:00",
    }
    assert make_raw_id(payload) == make_raw_id(dict(payload))
    hashed = make_raw_id({"ticker": "MSFT", "right": "put", "published_at": "2026-09-18T10:00:00-04:00"})
    assert hashed.startswith("mr:")
    assert len(hashed) == 3 + 32


def test_duplicate_raw_id_skips_second_parse():
    store = IdempotencyStore()
    payload = json.loads((FIXTURE_DIR / "01_site_spy_call.json").read_text())
    a = parse_payload(payload, store=store)
    b = parse_payload(payload, store=store)
    assert a.accepted is True
    assert b.accepted is False
    assert b.skip == "duplicate"
    assert a.raw_id == b.raw_id


def test_parse_confidence_numeric_and_words():
    assert parse_confidence(0.82) == 0.82
    assert parse_confidence(75) == 0.75
    assert parse_confidence("80%") == 0.8
    assert parse_confidence("high") == 0.85
    assert parse_confidence(None, "Confidence: 60%") == 0.6
    assert parse_confidence(None, "confidence: low") == 0.35
    assert parse_confidence(None) is None


def test_emit_shadow_marks_sheets_and_telegram_without_orders():
    intent = parse_payload(
        json.loads((FIXTURE_DIR / "01_site_spy_call.json").read_text())
    )
    ops = _RecordingOps()
    out = emit_shadow(intent, ops=ops)
    assert out["source"] == SOURCE_MR
    assert out["decision"] == DECISION_SHADOW
    assert len(ops.decisions) == 1
    args, kwargs = ops.decisions[0]
    assert args[0] == "SPY"
    assert args[1] == "CALL"
    assert args[2] == DECISION_SHADOW
    assert "source=mr" in args[3]
    assert "mode=SHADOW" in args[3]
    assert kwargs["regime"] == MODE_SHADOW
    assert len(ops.alerts) == 1
    tg = ops.alerts[0]
    assert "source=mr" in tg
    assert MODE_SHADOW in tg
    assert "No order" in tg
    assert "place_order" not in tg.lower()


def test_emit_skip_multi_leg_still_tagged_source_mr_shadow():
    intent = parse_payload(
        json.loads((FIXTURE_DIR / "04_site_nvda_call_spread.json").read_text())
    )
    ops = _RecordingOps()
    emit_shadow(intent, ops=ops)
    args, kwargs = ops.decisions[0]
    assert args[2] == DECISION_SKIP
    assert "source=mr" in args[3]
    assert "mode=SHADOW" in args[3]
    assert kwargs["regime"] == MODE_SHADOW
    assert "source=mr" in ops.alerts[0]
    assert MODE_SHADOW in ops.alerts[0]


def test_emit_accepts_async_ops_worker_shape():
    """Wire to helpers that look like AsyncOpsWorker (log_decision + alert)."""
    intent = next(i for i in replay_fixture_dir() if i.accepted)
    ops = SimpleNamespace(log_decision=lambda *a, **k: None, alert=lambda t: None)
    emit_shadow(intent, ops=ops)


def test_format_telegram_never_instructs_an_order():
    intent = next(i for i in replay_fixture_dir() if i.accepted)
    msg = format_telegram(intent)
    assert "source=mr" in msg
    assert "SHADOW" in msg
    assert "enter" not in msg.lower()
    assert "place_order" not in msg


def test_fixtures_are_anonymized():
    banned_needles = (
        "@gmail.com",
        "@yahoo.com",
        "@outlook.com",
        "imap.",
        "BEGIN ",
        "password",
        "api_key",
        "TELEGRAM_BOT_TOKEN",
        "MOOMOO_",
        "TRADIER",
    )
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        for needle in banned_needles:
            assert needle.lower() not in lower, f"{path.name} contains {needle!r}"
        assert "example.invalid" in text or "alert_id" in text


def test_intake_package_has_no_broker_imports_or_place_order():
    py_files = [p for p in INTAKE_ROOT.rglob("*.py") if p.is_file()]
    assert py_files
    for path in py_files:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in FORBIDDEN_IMPORT_ROOTS, f"{path} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in FORBIDDEN_IMPORT_ROOTS, f"{path} imports {node.module}"
            elif isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_NAMES, f"{path.name} uses {node.id}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_NAMES, f"{path.name} uses .{node.attr}"


def test_this_test_module_does_not_import_moomoo_or_tradier():
    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in {"moomoo", "tradier", "futu"}
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in {"moomoo", "tradier", "futu", "fabio_live"}


def test_replay_subprocess_does_not_load_moomoo_or_tradier():
    code = (
        "import sys\n"
        "from signal_intake.replay import replay_stable_records\n"
        "from signal_intake.shadow import emit_shadow\n"
        "from signal_intake.replay import replay_fixture_dir\n"
        "class Ops:\n"
        "    def log_decision(self, *a, **k): pass\n"
        "    def alert(self, t): pass\n"
        "recs = replay_stable_records()\n"
        "assert recs\n"
        "for intent in replay_fixture_dir():\n"
        "    emit_shadow(intent, ops=Ops())\n"
        "assert 'moomoo' not in sys.modules\n"
        "assert 'tradier' not in sys.modules\n"
        "assert 'futu' not in sys.modules\n"
        "print('ok', len(recs))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{BACKEND_ROOT}:{REPO_ROOT / 'frontend'}"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout


def test_orb_signal_engine_and_regime_have_no_mr_hooks():
    for rel in ("fabio_live/signals.py", "fabio_live/regime.py"):
        text = (BACKEND_ROOT / rel).read_text(encoding="utf-8")
        lower = text.lower()
        assert "signal_intake" not in text
        assert "market rebellion" not in lower
        assert "source=mr" not in lower
        assert "mode=shadow" not in lower


def test_cli_replay_stdout_matches_golden():
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{BACKEND_ROOT}:{REPO_ROOT / 'frontend'}"
    proc = subprocess.run(
        [sys.executable, "-m", "signal_intake"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == GOLDEN_PATH.read_text(encoding="utf-8")


def _site_call_payload(**overrides):
    base = {
        "channel": "site",
        "alert_id": "mr-fx-body-scan",
        "published_at": "2026-09-18T10:15:00-04:00",
        "ticker": "SPY",
        "right": "call",
        "instrument": "option",
        "confidence": 0.8,
        "structure": "option",
        "body_text": "Buy the call spread on SPY.",
    }
    base.update(overrides)
    return base


def test_option_structure_still_drops_call_spread_in_body():
    intent = parse_payload(_site_call_payload())
    assert intent.accepted is False
    assert intent.skip == "multi-leg"
    assert intent.decision == DECISION_SKIP


def test_equity_structure_still_drops_iron_condor_in_body():
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-condor",
            ticker="NVDA",
            instrument="equity",
            structure="equity",
            right="",
            body_text="Opening an iron condor here.",
        )
    )
    assert intent.accepted is False
    assert intent.skip == "multi-leg"


def test_true_single_leg_structure_still_accepts_outright():
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-outright",
            structure="outright",
            body_text="Single-leg call. Outright only.",
        )
    )
    assert intent.accepted is True
    assert intent.decision == DECISION_SHADOW
    assert intent.mapped()["side"] == "CALL"


def test_true_single_leg_still_drops_when_body_is_multi():
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-single-plus-spread",
            structure="single_leg",
            body_text="Ignore the header — this is a call spread.",
        )
    )
    assert intent.accepted is False
    assert intent.skip == "multi-leg"


def test_bare_spread_in_body_is_multi_leg_skip():
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-bare-spread",
            structure="outright",
            body_text="Buy a spread on SPY.",
        )
    )
    assert intent.accepted is False
    assert intent.skip == "multi-leg"
    assert intent.decision == DECISION_SKIP


def test_bid_ask_spread_false_positive_is_accepted_skip():
    """Captain B: bare \\bspread\\b is multi-leg; 'bid-ask spread' false SKIP is OK."""
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-bid-ask-spread",
            structure="outright",
            body_text="Watch the bid-ask spread on this SPY call.",
        )
    )
    assert intent.accepted is False
    assert intent.skip == "multi-leg"


def test_plural_spreads_in_body_are_multi_leg_skip():
    phrases = (
        "Buy spreads on SPY.",
        "Looking at call spreads.",
        "Looking at put spreads.",
        "This is debit spreads territory.",
        "Opening credit spreads here.",
        "Vertical spreads on the name.",
    )
    for i, body in enumerate(phrases):
        intent = parse_payload(
            _site_call_payload(
                alert_id=f"mr-fx-spreads-{i}",
                structure="outright",
                body_text=body,
            )
        )
        assert intent.accepted is False, body
        assert intent.skip == "multi-leg", body
        assert intent.decision == DECISION_SKIP, body


def test_plural_structure_tokens_skip_without_body_hit():
    for structure in ("spreads", "call_spreads", "butterflies", "iron_condors"):
        intent = parse_payload(
            _site_call_payload(
                alert_id=f"mr-fx-struct-{structure}",
                structure=structure,
                body_text="Buy calls on SPY.",
            )
        )
        assert intent.accepted is False, structure
        assert intent.skip == "multi-leg", structure
        assert intent.decision == DECISION_SKIP, structure


def test_plural_multi_nouns_in_body_are_multi_leg_skip():
    phrases = (
        "Buy iron condors on SPY.",
        "Opening straddles here.",
        "Butterflies on the name.",
        "Looking at strangles.",
        "These combos are not outright.",
        "Multi-legs on SPY.",
    )
    for i, body in enumerate(phrases):
        intent = parse_payload(
            _site_call_payload(
                alert_id=f"mr-fx-nouns-{i}",
                structure="outright",
                body_text=body,
            )
        )
        assert intent.accepted is False, body
        assert intent.skip == "multi-leg", body
        assert intent.decision == DECISION_SKIP, body


def test_format_telegram_escapes_html_in_payload_fields():
    intent = NormalizedIntent(
        source=SOURCE_MR,
        symbol="SPY<b>x",
        direction="CALL",
        instrument="option",
        as_of_et="2026-09-18T10:15:00-04:00<script>",
        raw_id="mr:id&x",
        confidence=0.5,
        decision=DECISION_SHADOW,
        reason="source=mr | mode=SHADOW",
        accepted=True,
        skip=None,
        channel="site",
    )
    msg = format_telegram(intent)
    assert "SPY&lt;b&gt;x" in msg
    assert "SPY<b>x" not in msg
    assert "mr:id&amp;x" in msg
    assert "&lt;script&gt;" in msg
    assert "<script>" not in msg
    assert "<b>SHADOW MR INTAKE</b>" in msg


def test_emit_shadow_isolates_sink_failures(capsys):
    intent = parse_payload(
        json.loads((FIXTURE_DIR / "01_site_spy_call.json").read_text())
    )
    alerts: list[str] = []
    decisions: list[tuple] = []

    def boom_decision(*_a, **_k):
        raise RuntimeError("sheets down")

    def ok_alert(text: str):
        alerts.append(text)

    out = emit_shadow(intent, log_decision=boom_decision, alert=ok_alert)
    assert out["raw_id"] == intent.raw_id
    assert len(alerts) == 1
    assert "sheets down" in capsys.readouterr().out

    def ok_decision(*args, **kwargs):
        decisions.append((args, kwargs))

    def boom_alert(_text: str):
        raise RuntimeError("telegram down")

    emit_shadow(intent, log_decision=ok_decision, alert=boom_alert)
    assert len(decisions) == 1
    assert "telegram down" in capsys.readouterr().out


def test_parse_as_of_et_accepts_unix_ms_and_seconds():
    et = ZoneInfo("America/New_York")
    dt = datetime(2026, 9, 18, 10, 15, tzinfo=et)
    seconds = dt.timestamp()
    ms = int(seconds * 1000)
    assert parse_as_of_et(ms) == dt.isoformat()
    assert parse_as_of_et(str(ms)) == dt.isoformat()
    assert parse_as_of_et(int(seconds)) == dt.isoformat()


def test_parse_as_of_et_rejects_far_future_numeric():
    # 9.9e15 ms → still far-future after one /1000; must not SHADOW-accept.
    assert parse_as_of_et(9.9e15) is None
    payload = _site_call_payload(
        alert_id="mr-fx-ms-future",
        published_at=1_758_204_900_000_000,
        body_text="Buy calls.",
        structure="outright",
    )
    intent = parse_payload(payload)
    assert intent.accepted is False
    assert intent.decision == DECISION_SKIP
    assert intent.skip == "missing-ts"


def test_unix_ms_published_at_can_shadow_accept():
    et = ZoneInfo("America/New_York")
    dt = datetime(2026, 9, 18, 10, 15, tzinfo=et)
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-ms-ok",
            published_at=int(dt.timestamp() * 1000),
            structure="outright",
            body_text="Buy calls.",
        )
    )
    assert intent.accepted is True
    assert intent.as_of_et == dt.isoformat()
    assert intent.decision == DECISION_SHADOW


def test_yyyymmdd_numeric_does_not_shadow_via_1970():
    """20260918 as Unix seconds is ~1970-08-23; must not SHADOW-accept."""
    assert parse_as_of_et(20260918) is None
    assert parse_as_of_et("20260918") is None
    assert parse_as_of_et(20260918.0) is None
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-yyyymmdd",
            published_at=20260918,
            structure="outright",
            body_text="Buy calls.",
        )
    )
    assert intent.accepted is False
    assert intent.decision == DECISION_SKIP
    assert intent.skip == "missing-ts"
    assert "1970" not in (intent.as_of_et or "")


def test_string_and_float_legs_count_skips_multi_leg():
    base = _site_call_payload(
        structure="outright",
        body_text="Buy calls.",
        instrument="option",
        right="call",
    )
    for legs in ("2", 2.0, "2.0", " 2 "):
        intent = parse_payload({**base, "alert_id": f"mr-fx-legs-{legs}", "legs": legs})
        assert intent.accepted is False, legs
        assert intent.skip == "multi-leg", legs
        assert intent.decision == DECISION_SKIP
        assert intent.direction == ""


def test_unambiguous_single_leg_numeric_still_shadows():
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-legs-one",
            structure="outright",
            body_text="Buy calls.",
            legs="1",
        )
    )
    assert intent.accepted is True
    assert intent.mapped()["side"] == "CALL"


def test_non_integer_float_legs_does_not_count_as_multi():
    intent = parse_payload(
        _site_call_payload(
            alert_id="mr-fx-legs-half",
            structure="outright",
            body_text="Buy calls.",
            legs=2.5,
        )
    )
    assert intent.skip != "multi-leg"
    assert intent.accepted is True


def test_equity_instrument_plus_call_right_is_contradictory_skip():
    intent = parse_payload(
        {
            "channel": "site",
            "alert_id": "mr-fx-eq-call",
            "published_at": "2026-09-18T10:15:00-04:00",
            "ticker": "AAPL",
            "instrument": "equity",
            "right": "call",
            "confidence": 0.8,
            "structure": "outright",
            "body_text": "Listed as shares.",
        }
    )
    assert intent.accepted is False
    assert intent.skip == "contradictory"
    assert intent.decision == DECISION_SKIP
    assert intent.direction == ""
    assert intent.mapped()["side"] == ""
    assert "EQUITY" not in intent.reason
    assert "mapped=AAPL" not in intent.reason


def test_equity_instrument_plus_put_direction_is_contradictory_skip():
    intent = parse_payload(
        {
            "channel": "site",
            "alert_id": "mr-fx-eq-put",
            "published_at": "2026-09-18T10:15:00-04:00",
            "ticker": "MSFT",
            "asset_class": "stock",
            "direction": "PUT",
            "structure": "outright",
        }
    )
    assert intent.accepted is False
    assert intent.skip == "contradictory"


def test_plain_equity_without_option_right_still_shadows():
    intent = parse_payload(
        json.loads((FIXTURE_DIR / "03_site_aapl_equity.json").read_text())
    )
    assert intent.accepted is True
    assert intent.mapped()["side"] == "EQUITY"
    assert intent.instrument == "equity"
