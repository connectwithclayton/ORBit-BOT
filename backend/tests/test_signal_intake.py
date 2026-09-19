"""MR signal intake (slice 2): fixture replay, shadow emit, no broker calls."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from signal_intake.ids import IdempotencyStore, make_raw_id
from signal_intake.models import DECISION_SHADOW, DECISION_SKIP, MODE_SHADOW, SOURCE_MR
from signal_intake.parse import parse_confidence, parse_payload
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
