"""SHIP-007: four-book health snapshot (no UI)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from fabio_live.bot import ORBBot
from fabio_live.circuit import RiskCircuitBreaker
from fabio_live.paper_books import (
    ALL_PAPER_BOOKS,
    BOOK_MR_MOOMOO,
    BOOK_MR_TRADIER,
    BOOK_ORB_MOOMOO,
    BOOK_ORB_TRADIER,
    LEDGER_CODES_PREVIEW_LIMIT,
    PAPER_BOOK_STARTING_BALANCE,
    PaperBookLedger,
    PaperBookRegistry,
    UnknownPaperBook,
    enabled_paper_book_ids,
    init_book_circuit,
    last_flatten_sidecar_path,
    paper_books_health_map,
)
import verify_phase2_reliability as gate

FOUR_BOOK_IDS = (
    BOOK_ORB_MOOMOO,
    BOOK_ORB_TRADIER,
    BOOK_MR_MOOMOO,
    BOOK_MR_TRADIER,
)
BOOK_FIELDS = (
    "enabled",
    "bound",
    "broker",
    "strategy",
    "source",
    "starting_balance",
    "cb",
    "ledger",
    "modeled_equity",
)
CB_FIELDS = (
    "portfolio_at_open",
    "realized_pnl",
    "daily_loss_pct",
    "trade_count",
    "loss_streak",
)
LEDGER_FIELDS = ("path", "code_count", "codes_preview")
OPS_FIELDS = (
    "queue_depth",
    "queue_max",
    "errors",
    "thread_alive",
    "dropped_noncritical",
    "dropped_critical",
)


def _assert_four_book_shape(books: dict) -> None:
    assert list(books) == list(FOUR_BOOK_IDS)
    for spec in ALL_PAPER_BOOKS:
        row = books[spec.book_id]
        for key in BOOK_FIELDS:
            assert key in row, f"{spec.book_id} missing {key}"
        assert row["broker"] == spec.broker
        assert row["strategy"] == spec.strategy
        assert row["source"] == spec.source
        assert row["starting_balance"] == PAPER_BOOK_STARTING_BALANCE
        for key in CB_FIELDS:
            assert key in row["cb"], f"{spec.book_id} cb missing {key}"
        for key in LEDGER_FIELDS:
            assert key in row["ledger"], f"{spec.book_id} ledger missing {key}"
        assert isinstance(row["enabled"], bool)
        assert isinstance(row["bound"], bool)
        assert isinstance(row["ledger"]["codes_preview"], list)
        assert row["ledger"]["code_count"] >= len(row["ledger"]["codes_preview"])


def test_health_map_always_has_four_keys_when_nothing_bound(tmp_path):
    books = paper_books_health_map(
        None, enabled_ids=(BOOK_ORB_MOOMOO,), ledger_dir=tmp_path
    )
    _assert_four_book_shape(books)
    assert books[BOOK_ORB_MOOMOO]["enabled"] is True
    assert books[BOOK_ORB_MOOMOO]["bound"] is False
    for bid in (BOOK_ORB_TRADIER, BOOK_MR_MOOMOO, BOOK_MR_TRADIER):
        assert books[bid]["enabled"] is False
        assert books[bid]["bound"] is False
        assert books[bid]["cb"]["realized_pnl"] == 0.0
        assert books[bid]["cb"]["trade_count"] == 0
        assert books[bid]["modeled_equity"] == PAPER_BOOK_STARTING_BALANCE


def test_orb_moomoo_cb_matches_bound_runtime_not_siblings(tmp_path):
    orb_cb = init_book_circuit()
    orb_cb.record_result(-250.0)
    mr_cb = init_book_circuit()
    mr_cb.record_result(40.0)
    reg = PaperBookRegistry()
    orb_rt = reg.bind(
        BOOK_ORB_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        cb=orb_cb,
        ledger_dir=tmp_path,
    )
    reg.bind(
        BOOK_MR_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        cb=mr_cb,
        ledger_dir=tmp_path,
    )
    books = paper_books_health_map(
        reg,
        enabled_ids=(BOOK_ORB_MOOMOO, BOOK_MR_MOOMOO),
        ledger_dir=tmp_path,
    )
    _assert_four_book_shape(books)
    assert orb_rt.cb is orb_cb
    orb_row = books[BOOK_ORB_MOOMOO]
    assert orb_row["bound"] is True
    assert orb_row["cb"]["realized_pnl"] == pytest.approx(orb_cb.realized_pnl)
    assert orb_row["cb"]["trade_count"] == orb_cb.trade_count
    assert orb_row["cb"]["loss_streak"] == orb_cb.loss_streak
    assert orb_row["cb"]["daily_loss_pct"] == pytest.approx(round(orb_cb.daily_loss_pct, 6))
    assert orb_row["cb"]["portfolio_at_open"] == PAPER_BOOK_STARTING_BALANCE
    assert orb_row["modeled_equity"] == pytest.approx(10_000.0 - 250.0)

    mr_row = books[BOOK_MR_MOOMOO]
    assert mr_row["bound"] is True
    assert mr_row["cb"]["realized_pnl"] == pytest.approx(40.0)
    assert mr_row["cb"]["realized_pnl"] != orb_row["cb"]["realized_pnl"]
    assert mr_row["modeled_equity"] == pytest.approx(10_040.0)

    for bid in (BOOK_ORB_TRADIER, BOOK_MR_TRADIER):
        assert books[bid]["bound"] is False
        assert books[bid]["cb"]["realized_pnl"] == 0.0
        assert books[bid]["modeled_equity"] == PAPER_BOOK_STARTING_BALANCE


def test_mr_only_bind_does_not_copy_orb_pnl_onto_mr(tmp_path):
    orb_cb = init_book_circuit()
    orb_cb.record_result(-500.0)
    assert orb_cb.realized_pnl == -500.0

    mr_cb = init_book_circuit()
    reg = PaperBookRegistry()
    reg.bind(
        BOOK_MR_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        cb=mr_cb,
        ledger_dir=tmp_path,
    )
    books = paper_books_health_map(
        reg, enabled_ids=(BOOK_MR_MOOMOO,), ledger_dir=tmp_path
    )
    _assert_four_book_shape(books)
    assert books[BOOK_MR_MOOMOO]["enabled"] is True
    assert books[BOOK_MR_MOOMOO]["bound"] is True
    assert books[BOOK_ORB_MOOMOO]["bound"] is False
    assert books[BOOK_ORB_MOOMOO]["enabled"] is False
    # Unbound ORB must not leak the detached ORB CB; MR must stay at its own zeros.
    assert books[BOOK_ORB_MOOMOO]["cb"]["realized_pnl"] == 0.0
    assert books[BOOK_MR_MOOMOO]["cb"]["realized_pnl"] == 0.0
    assert books[BOOK_MR_MOOMOO]["cb"]["realized_pnl"] != orb_cb.realized_pnl
    assert books[BOOK_MR_MOOMOO]["cb"]["trade_count"] == 0
    assert books[BOOK_MR_MOOMOO]["modeled_equity"] == PAPER_BOOK_STARTING_BALANCE


def test_unbound_ledger_reads_disk_codes_preview_truncated(tmp_path):
    codes = [f"US.SPY261016C{i:07d}" for i in range(12)]
    ledger = PaperBookLedger(BOOK_ORB_TRADIER, directory=tmp_path)
    ledger.replace_codes(codes)
    assert ledger.save() is True
    books = paper_books_health_map(
        None, enabled_ids=(BOOK_ORB_MOOMOO,), ledger_dir=tmp_path
    )
    row = books[BOOK_ORB_TRADIER]
    assert row["bound"] is False
    assert row["ledger"]["code_count"] == 12
    assert len(row["ledger"]["codes_preview"]) == LEDGER_CODES_PREVIEW_LIMIT
    assert row["ledger"]["codes_preview"] == sorted(codes)[:LEDGER_CODES_PREVIEW_LIMIT]
    assert str(tmp_path / f"{BOOK_ORB_TRADIER}.json") == row["ledger"]["path"]


def test_last_flatten_read_if_exists_does_not_invent_writer(tmp_path):
    payload = {
        "book_id": BOOK_MR_MOOMOO,
        "broker": "moomoo",
        "script": "moomoo_eod_failsafe.py",
        "started_at_utc": "2026-09-21T19:50:00+00:00",
        "finished_at_utc": "2026-09-21T19:50:02+00:00",
        "dry_run": True,
        "exit_code": 0,
        "planned": ["US.NOK261016C00010000"],
        "failures": [],
        "selected_codes_count": 1,
        "reason": "flatten",
        "layer": "failsafe",
    }
    path = last_flatten_sidecar_path(BOOK_MR_MOOMOO, tmp_path)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    books = paper_books_health_map(
        None, enabled_ids=(BOOK_ORB_MOOMOO,), ledger_dir=tmp_path
    )
    _assert_four_book_shape(books)
    found = books[BOOK_MR_MOOMOO]["last_flatten"]
    assert found is not None
    assert found["book_id"] == BOOK_MR_MOOMOO
    assert found["reason"] == "flatten"
    assert found["dry_run"] is True
    assert found["planned_count"] == 1
    assert found["failure_count"] == 0
    assert "planned" not in found
    for bid in (BOOK_ORB_MOOMOO, BOOK_ORB_TRADIER, BOOK_MR_TRADIER):
        assert books[bid]["last_flatten"] is None
    # Health is read-only: no flatten writer lives in paper_books.
    src = Path(__file__).resolve().parents[1] / "fabio_live" / "paper_books.py"
    text = src.read_text(encoding="utf-8")
    assert "This module does not write flatten last-run files" in text
    assert "last_flatten_sidecar_path(" in text
    assert ".write_text(" not in text.split("def last_flatten_sidecar_path")[1].split(
        "def read_last_flatten_summary"
    )[0]


def _ops_health() -> dict:
    return {
        "queue_depth": 2,
        "queue_max": 500,
        "errors": 0,
        "thread_alive": True,
        "dropped_noncritical": 0,
        "dropped_critical": 0,
        "coalesced_updates": 0,
        "inline_critical_fallbacks": 0,
        "dashboard_intraday_refresh_requests": 1,
        "dashboard_intraday_refresh_enqueued": 1,
        "dashboard_intraday_refresh_throttled": 0,
        "dashboard_open_refresh_requests": 0,
        "dashboard_open_refresh_enqueued": 0,
        "dashboard_open_refresh_throttled": 0,
    }


def _stub_health_bot(tmp_path, monkeypatch) -> ORBBot:
    monkeypatch.setattr(
        "fabio_live.bot.HEALTH_SNAPSHOT_PATH", str(tmp_path / "bot_health_snapshots.jsonl")
    )
    monkeypatch.setattr("fabio_live.bot.MR_PAPER_ENABLED", False)
    monkeypatch.setattr(
        "fabio_live.bot.enabled_paper_book_ids",
        lambda **_k: (BOOK_ORB_MOOMOO,),
    )
    bot = ORBBot.__new__(ORBBot)
    bot.paused = False
    bot.stopped = False
    bot._pause_reason_code = ""
    bot.signals = {"SPY": "CALL"}
    bot.cb = init_book_circuit()
    bot._paper_books = PaperBookRegistry()
    bot._paper_books.bind(
        BOOK_ORB_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        cb=bot.cb,
        ledger_dir=tmp_path,
    )
    bot._data_health = {
        ("SPY", "5m"): {"state": "OK", "last_age_sec": 3.2, "source": "opend"},
    }
    bot._position_parity_latest = {"parity_ok": True, "query_ok": True, "drift_count": 0}
    bot._health_snapshot_last_ts = 0.0
    bot._loop_cadence_mode = "active"
    bot._loop_sleep_sec = 25.0
    bot._now_market = lambda: datetime(
        2026, 9, 21, 10, 15, tzinfo=ZoneInfo("America/New_York")
    )
    bot._refresh_position_parity = lambda: None
    bot.ops = SimpleNamespace(health=_ops_health, log_alert=lambda *_a, **_k: None)
    return bot


def test_emit_health_snapshot_four_books_and_circuit_alias(tmp_path, monkeypatch):
    bot = _stub_health_bot(tmp_path, monkeypatch)
    bot.cb.record_result(-125.0)
    bot._emit_health_snapshot(force=True)
    path = tmp_path / "bot_health_snapshots.jsonl"
    snap = json.loads(path.read_text(encoding="utf-8").strip())
    assert set(OPS_FIELDS).issubset(snap["ops"])
    assert snap["ops"]["queue_max"] == 500
    assert snap["ops"]["thread_alive"] is True
    assert snap["data_health"]["SPY_5m"]["state"] == "OK"
    assert snap["data_health"]["SPY_5m"]["source"] == "opend"
    _assert_four_book_shape(snap["books"])
    orb_cb = snap["books"][BOOK_ORB_MOOMOO]["cb"]
    assert orb_cb["realized_pnl"] == pytest.approx(bot.cb.realized_pnl)
    assert orb_cb["trade_count"] == bot.cb.trade_count
    assert orb_cb["loss_streak"] == bot.cb.loss_streak
    assert orb_cb["daily_loss_pct"] == pytest.approx(round(bot.cb.daily_loss_pct, 6))
    # Deprecated alias of orb-moomoo CB (same three fields as pre-slice).
    assert snap["circuit"] == {
        "daily_loss_pct": orb_cb["daily_loss_pct"],
        "trade_count": orb_cb["trade_count"],
        "loss_streak": orb_cb["loss_streak"],
    }
    for bid in (BOOK_ORB_TRADIER, BOOK_MR_MOOMOO, BOOK_MR_TRADIER):
        assert snap["books"][bid]["bound"] is False
        assert snap["books"][bid]["cb"]["realized_pnl"] == 0.0


def test_emit_mr_only_bind_does_not_copy_orb_pnl(tmp_path, monkeypatch):
    bot = _stub_health_bot(tmp_path, monkeypatch)
    bot.cb.record_result(-80.0)
    mr_cb = init_book_circuit()
    bot._paper_books.bind(
        BOOK_MR_MOOMOO,
        order_mgr=SimpleNamespace(positions={}),
        cb=mr_cb,
        ledger_dir=tmp_path,
    )
    monkeypatch.setattr(
        "fabio_live.bot.enabled_paper_book_ids",
        lambda **_k: (BOOK_MR_MOOMOO,),
    )
    bot._emit_health_snapshot(force=True)
    snap = json.loads((tmp_path / "bot_health_snapshots.jsonl").read_text().strip())
    _assert_four_book_shape(snap["books"])
    assert snap["books"][BOOK_ORB_MOOMOO]["cb"]["realized_pnl"] == pytest.approx(-80.0)
    assert snap["books"][BOOK_MR_MOOMOO]["bound"] is True
    assert snap["books"][BOOK_MR_MOOMOO]["cb"]["realized_pnl"] == 0.0
    assert snap["circuit"]["trade_count"] == bot.cb.trade_count


def _gate_args(snapshot_path: Path):
    return type(
        "Args",
        (),
        {
            "snapshot_path": str(snapshot_path),
            "max_age_min": 999999.0,
            "allow_stale_data": False,
            "max_queue_ratio": 0.95,
            "sync_audit_jsonl": "",
            "sync_audit_max_age_min": -1.0,
        },
    )()


def _gate_ops_payload() -> dict:
    return {
        "ts": "2099-01-01T09:30:00-05:00",
        "ops": {
            "queue_depth": 1,
            "queue_max": 500,
            "thread_alive": True,
            "errors": 0,
            "dropped_critical": 0,
            "dropped_noncritical": 0,
        },
        "data_health": {"SPY_5m": {"state": "OK"}},
    }


def test_verify_phase2_reliability_passes_pre_slice_and_new_snapshot(
    monkeypatch, tmp_path: Path
):
    snap = tmp_path / "snap.jsonl"
    pre = _gate_ops_payload()
    assert "books" not in pre
    snap.write_text(json.dumps(pre) + "\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_parse_args", lambda: _gate_args(snap))
    assert gate.main() == 0

    books = paper_books_health_map(
        None, enabled_ids=(BOOK_ORB_MOOMOO,), ledger_dir=tmp_path
    )
    new_payload = dict(pre)
    new_payload["circuit"] = {
        "daily_loss_pct": 0.0,
        "trade_count": 0,
        "loss_streak": 0,
    }
    new_payload["books"] = books
    snap.write_text(json.dumps(new_payload) + "\n", encoding="utf-8")
    assert gate.main() == 0


def test_enabled_paper_book_ids_strict_raises_unknown(monkeypatch):
    monkeypatch.setenv("FABIO_PAPER_BOOKS", "orb-moomoo,not-a-book")
    with pytest.raises(UnknownPaperBook):
        enabled_paper_book_ids(strict=True)


def test_enabled_paper_book_ids_fail_soft_skips_unknown(monkeypatch):
    monkeypatch.setenv("FABIO_PAPER_BOOKS", "orb-moomoo,not-a-book,mr-moomoo")
    ids = enabled_paper_book_ids(strict=False)
    assert ids == (BOOK_ORB_MOOMOO, BOOK_MR_MOOMOO)
    assert "not-a-book" not in ids


def test_health_map_skips_unknown_enabled_token_keeps_four_keys(tmp_path):
    books = paper_books_health_map(
        None,
        enabled_ids=(BOOK_ORB_MOOMOO, "not-a-book"),
        ledger_dir=tmp_path,
    )
    _assert_four_book_shape(books)
    assert "not-a-book" not in books
    assert books[BOOK_ORB_MOOMOO]["enabled"] is True
    assert books[BOOK_MR_MOOMOO]["enabled"] is False


def test_emit_health_snapshot_survives_bad_paper_books_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FABIO_PAPER_BOOKS", "orb-moomoo,totally-unknown-book")
    bot = _stub_health_bot(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "fabio_live.bot.enabled_paper_book_ids", enabled_paper_book_ids
    )
    bot._emit_health_snapshot(force=True)
    snap = json.loads((tmp_path / "bot_health_snapshots.jsonl").read_text().strip())
    _assert_four_book_shape(snap["books"])
    assert snap["ops"]["thread_alive"] is True
    assert snap["books"][BOOK_ORB_MOOMOO]["enabled"] is True
    assert snap["books"][BOOK_ORB_MOOMOO]["bound"] is True
    assert "totally-unknown-book" not in snap["books"]


def test_emit_health_snapshot_survives_books_map_exception(tmp_path, monkeypatch):
    bot = _stub_health_bot(tmp_path, monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("books assembly exploded")

    monkeypatch.setattr("fabio_live.bot.paper_books_health_map", _boom)
    bot._emit_health_snapshot(force=True)
    snap = json.loads((tmp_path / "bot_health_snapshots.jsonl").read_text().strip())
    assert snap["ops"]["queue_max"] == 500
    _assert_four_book_shape(snap["books"])
    for bid in FOUR_BOOK_IDS:
        assert snap["books"][bid]["bound"] is False
        assert snap["books"][bid]["enabled"] is False
