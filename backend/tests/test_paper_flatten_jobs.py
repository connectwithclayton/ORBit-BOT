"""Four-book paper flatten jobs (SHIP-011 slice 1) — argv, schedule, wrapper."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabio_live.paper_books import (
    ALL_PAPER_BOOKS,
    BOOK_MR_MOOMOO,
    BOOK_MR_TRADIER,
    BOOK_ORB_MOOMOO,
    BOOK_ORB_TRADIER,
    BROKER_MOOMOO,
    FLATTEN_MOOMOO,
    FLATTEN_TRADIER,
    LEDGER_DIR_ENV,
    REASON_ABORTED_WINDOW,
    REASON_NO_CLOSABLE,
    read_last_flatten,
)
from fabio_live.paper_flatten_jobs import (
    ABORTED_WINDOW_EXIT,
    FORBIDDEN_ARGV_TOKENS,
    LAUNCHD_LABEL_PREFIX,
    MOOMOO_TRD_ENV_PIN,
    _main as flatten_jobs_main,
    jsonl_filename_for_book,
    launchd_label_for_book,
    paper_flatten_argv,
    run_book_flatten,
    scheduled_paper_flatten_jobs,
    wrapper_process_exit_code,
)
from moomoo_eod_failsafe import main as moomoo_main
from tests.test_flatten_last_run import FakeMoomooCtx, _option_row, SPY_MOOMOO
from tests.test_tradier_paper import FakeResp, FakeSession, _client
from tradier_eod_flatten import main as tradier_main

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "portal" / "install_paper_flatten_scheduler.sh"
WRAPPER = REPO / "portal" / "run_paper_flatten_book.sh"


def test_argv_builder_never_emits_tradier_script_for_moomoo_book():
    for book_id in (BOOK_ORB_MOOMOO, BOOK_MR_MOOMOO):
        argv = paper_flatten_argv(book_id)
        assert "--book" in argv
        assert book_id in argv
        assert argv[argv.index("--book") + 1] == book_id
        assert argv[0].endswith(FLATTEN_MOOMOO)
        joined = " ".join(argv)
        assert FLATTEN_TRADIER not in joined
        assert "tradier_eod_flatten" not in joined
        assert "--require-after-et" in argv
        assert "--scope" in argv and "options" in argv
        assert "--log-format" in argv and "jsonl" in argv
        assert "--trd-env" in argv
        assert argv[argv.index("--trd-env") + 1] == MOOMOO_TRD_ENV_PIN == "SIMULATE"
        assert "REAL" not in argv
        assert "FABIO_ALLOW_REAL_TRADING" not in argv
        assert not (FORBIDDEN_ARGV_TOKENS & set(argv))


def test_argv_builder_never_omits_book_and_never_crosses_brokers():
    for spec in ALL_PAPER_BOOKS:
        argv = paper_flatten_argv(spec.book_id)
        assert argv.count("--book") == 1
        assert argv[argv.index("--book") + 1] == spec.book_id
        if spec.broker == BROKER_MOOMOO:
            assert FLATTEN_MOOMOO in argv[0]
            assert FLATTEN_TRADIER not in argv[0]
            assert argv[argv.index("--trd-env") + 1] == "SIMULATE"
        else:
            assert FLATTEN_TRADIER in argv[0]
            assert FLATTEN_MOOMOO not in argv[0]
            assert "moomoo_eod_failsafe" not in " ".join(argv)
            assert "--trd-env" not in argv
            assert "SIMULATE" not in argv


def test_print_argv_requires_book_flag():
    with pytest.raises(SystemExit) as ei:
        flatten_jobs_main(["print-argv"])
    assert ei.value.code == 2


def test_scheduled_jobs_are_four_staggered_independent_labels():
    jobs = scheduled_paper_flatten_jobs()
    assert len(jobs) == 4
    assert [j.book_id for j in jobs] == [
        BOOK_ORB_MOOMOO,
        BOOK_MR_MOOMOO,
        BOOK_ORB_TRADIER,
        BOOK_MR_TRADIER,
    ]
    assert [(j.hour, j.minute) for j in jobs] == [
        (15, 50),
        (15, 52),
        (15, 54),
        (15, 56),
    ]
    labels = [j.label for j in jobs]
    assert labels == [f"{LAUNCHD_LABEL_PREFIX}.{j.book_id}" for j in jobs]
    assert len(set(labels)) == 4
    for job in jobs:
        assert f"--book {job.book_id}" in " ".join(job.argv)
        assert job.argv[job.argv.index("--book") + 1] == job.book_id
        assert job.jsonl == f"eod_failsafe.{job.book_id}.jsonl"
        if job.book_id in (BOOK_ORB_MOOMOO, BOOK_MR_MOOMOO):
            assert "--trd-env" in job.argv
            assert job.argv[job.argv.index("--trd-env") + 1] == "SIMULATE"
            assert "REAL" not in job.argv
        else:
            assert "--trd-env" not in job.argv


def test_wrapper_maps_aborted_window_exit_to_skip():
    assert wrapper_process_exit_code(4) == 0
    assert wrapper_process_exit_code(ABORTED_WINDOW_EXIT) == 0
    assert wrapper_process_exit_code(0) == 0
    assert wrapper_process_exit_code(1) == 1
    assert wrapper_process_exit_code(3) == 3


def test_run_book_flatten_maps_exit_4_and_never_passes_live(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env") or {}
        return SimpleNamespace(returncode=4)

    monkeypatch.setattr(
        "fabio_live.paper_flatten_jobs.should_run_failsafe", lambda *a, **k: True
    )
    monkeypatch.setattr("fabio_live.paper_flatten_jobs.subprocess.run", fake_run)
    rc = run_book_flatten(BOOK_ORB_MOOMOO, repo_root=tmp_path)
    assert rc == 0
    cmd = captured["cmd"]
    assert "--book" in cmd
    assert cmd[cmd.index("--book") + 1] == BOOK_ORB_MOOMOO
    assert FLATTEN_MOOMOO in cmd[1]
    assert FLATTEN_TRADIER not in " ".join(cmd)
    assert "--trd-env" in cmd
    assert cmd[cmd.index("--trd-env") + 1] == "SIMULATE"
    assert "--env" not in cmd
    assert "REAL" not in cmd
    assert "live" not in cmd
    assert "FABIO_ALLOW_REAL_TRADING" not in captured["env"]
    jsonl = tmp_path / jsonl_filename_for_book(BOOK_ORB_MOOMOO)
    assert jsonl.is_file()


def test_run_book_flatten_holiday_skip_does_not_invoke_script(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise AssertionError("flatten script must not run on a closed day")

    monkeypatch.setattr(
        "fabio_live.paper_flatten_jobs.should_run_failsafe", lambda *a, **k: False
    )
    monkeypatch.setattr("fabio_live.paper_flatten_jobs.subprocess.run", boom)
    assert run_book_flatten(BOOK_MR_TRADIER, repo_root=tmp_path) == 0


def test_run_book_flatten_tradier_book_never_calls_moomoo(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "fabio_live.paper_flatten_jobs.should_run_failsafe", lambda *a, **k: True
    )
    monkeypatch.setattr("fabio_live.paper_flatten_jobs.subprocess.run", fake_run)
    assert run_book_flatten(BOOK_ORB_TRADIER, repo_root=tmp_path) == 0
    cmd = captured["cmd"]
    assert cmd[cmd.index("--book") + 1] == BOOK_ORB_TRADIER
    assert FLATTEN_TRADIER in cmd[1]
    assert FLATTEN_MOOMOO not in " ".join(cmd)
    assert "--trd-env" not in cmd
    assert "SIMULATE" not in cmd
    assert "REAL" not in cmd


def test_installer_dry_run_prints_four_labels_and_book_argv():
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO / 'backend'}:{REPO / 'frontend'}"
    proc = subprocess.run(
        ["bash", str(INSTALLER), "--dry-run"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "SheetsLogger" not in out
    assert "gspread" not in out
    for spec in ALL_PAPER_BOOKS:
        label = launchd_label_for_book(spec.book_id)
        assert label in out
        assert f"--book {spec.book_id}" in out
        assert f"com.claytonorb.paper.flatten.{spec.book_id}" in out
        if spec.broker == BROKER_MOOMOO:
            assert f"--trd-env SIMULATE" in out
    assert "--trd-env REAL" not in out
    assert "FABIO_ALLOW_REAL_TRADING" not in out


def test_print_schedule_bash_is_exactly_four_job_rows(capsys):
    assert flatten_jobs_main(["print-schedule", "--bash"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 4
    assert all(ln.startswith("flatten-job ") for ln in lines)
    books = [ln.split()[1] for ln in lines]
    assert books == [
        BOOK_ORB_MOOMOO,
        BOOK_MR_MOOMOO,
        BOOK_ORB_TRADIER,
        BOOK_MR_TRADIER,
    ]


def test_wrapper_requires_book_flag():
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO / 'backend'}:{REPO / 'frontend'}"
    proc = subprocess.run(
        ["bash", str(WRAPPER)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 2
    assert "--book" in (proc.stderr + proc.stdout)


def test_mr_moomoo_argv_does_not_target_orb_moomoo():
    argv = paper_flatten_argv(BOOK_MR_MOOMOO)
    assert argv[argv.index("--book") + 1] == BOOK_MR_MOOMOO
    assert BOOK_ORB_MOOMOO not in argv


def test_scheduled_moomoo_argv_pins_simulate_never_real():
    """Captain B: scheduled Moomoo jobs pin SIMULATE so MOOMOO_TRADE_ENV=REAL still papers."""
    for book_id in (BOOK_ORB_MOOMOO, BOOK_MR_MOOMOO):
        argv = paper_flatten_argv(book_id)
        assert argv[argv.index("--book") + 1] == book_id
        assert "--trd-env" in argv
        assert argv[argv.index("--trd-env") + 1] == "SIMULATE"
        assert argv.count("SIMULATE") == 1
        assert "REAL" not in argv
        assert "--env" not in argv
        assert "live" not in argv
        assert "FABIO_ALLOW_REAL_TRADING" not in argv
        assert FLATTEN_TRADIER not in " ".join(argv)
    for book_id in (BOOK_ORB_TRADIER, BOOK_MR_TRADIER):
        argv = paper_flatten_argv(book_id)
        assert "--trd-env" not in argv
        assert "SIMULATE" not in argv
        assert "REAL" not in argv


def test_moomoo_empty_ledger_without_dry_run_no_place_order(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    ctx = FakeMoomooCtx([_option_row(SPY_MOOMOO)])
    code = moomoo_main(
        ["--book", "orb-moomoo", "--log-format", "jsonl"],
        trd_ctx=ctx,
    )
    assert code == 0
    assert ctx.place_calls == []
    data = read_last_flatten(BOOK_ORB_MOOMOO, tmp_path)
    assert data is not None
    assert data["reason"] == REASON_NO_CLOSABLE
    assert data["exit_code"] == 0
    assert data["selected_codes_count"] == 0
    assert read_last_flatten(BOOK_MR_MOOMOO, tmp_path) is None


def test_tradier_require_after_et_aborted_window_writes_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(
        "tradier_eod_flatten._xnys_failsafe_cutoff_ok",
        lambda *_a, **_k: (False, "before_failsafe_cutoff want_>=x"),
    )
    session = FakeSession(
        [
            FakeResp(
                200,
                {"positions": {"position": [{"symbol": "SPY261016C00500000", "quantity": "1"}]}},
            )
        ]
    )
    client = _client(session)
    code = tradier_main(
        ["--require-after-et", "--book", "orb-tradier", "--dry-run"],
        client=client,
    )
    assert code == 4
    assert session.calls == []
    data = read_last_flatten(BOOK_ORB_TRADIER, tmp_path)
    assert data is not None
    assert data["reason"] == REASON_ABORTED_WINDOW
    assert data["exit_code"] == 4
    assert read_last_flatten(BOOK_ORB_MOOMOO, tmp_path) is None
    assert read_last_flatten(BOOK_MR_TRADIER, tmp_path) is None


def test_eod_close_all_does_not_invoke_failsafe():
    bot_src = (REPO / "backend" / "fabio_live" / "bot.py").read_text(encoding="utf-8")
    assert "moomoo_eod_failsafe" not in bot_src
    assert "tradier_eod_flatten" not in bot_src
    assert "paper_flatten_jobs" not in bot_src


def test_map_exit_cli(capsys):
    assert flatten_jobs_main(["map-exit", "4"]) == 0
    assert capsys.readouterr().out.strip() == "0"


def test_moomoo_calendar_gate_import_error_is_xnys_unavailable(monkeypatch):
    import moomoo_eod_failsafe as moomoo_fs

    monkeypatch.setitem(sys.modules, "fabio_live.calendar_gate", None)
    ok, detail = moomoo_fs._xnys_failsafe_cutoff_ok(datetime(2025, 10, 1, 16, 0))
    assert ok is False
    assert detail.startswith("xnys_calendar_unavailable:")


def test_moomoo_require_after_et_import_error_uses_legacy_cutoff(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    monkeypatch.setitem(sys.modules, "fabio_live.calendar_gate", None)
    monkeypatch.setattr(
        "moomoo_eod_failsafe._us_weekday_after_cutoff",
        lambda *_a, **_k: True,
    )
    ctx = FakeMoomooCtx([])
    code = moomoo_main(
        ["--require-after-et", "--book", "orb-moomoo", "--dry-run"],
        trd_ctx=ctx,
    )
    assert code == 0
    assert ctx.place_calls == []


def test_tradier_calendar_gate_import_error_is_xnys_unavailable_and_weekday_fallback(
    monkeypatch,
):
    import tradier_eod_flatten as tradier_fs

    monkeypatch.setitem(sys.modules, "fabio_live.calendar_gate", None)
    ok, detail = tradier_fs._xnys_failsafe_cutoff_ok(datetime(2025, 10, 1, 16, 0))
    assert ok is False
    assert detail.startswith("xnys_calendar_unavailable:")

    tz = pytest.importorskip("zoneinfo").ZoneInfo("America/New_York")
    after = datetime(2025, 10, 1, 16, 0, tzinfo=tz)
    before = datetime(2025, 10, 1, 10, 0, tzinfo=tz)
    weekend = datetime(2025, 10, 4, 16, 0, tzinfo=tz)
    assert tradier_fs._us_weekday_after_cutoff(after, 15, 45) is True
    assert tradier_fs._us_weekday_after_cutoff(before, 15, 45) is False
    assert tradier_fs._us_weekday_after_cutoff(weekend, 15, 45) is False


def test_tradier_require_after_et_import_error_uses_legacy_cutoff(monkeypatch, tmp_path):
    monkeypatch.setenv(LEDGER_DIR_ENV, str(tmp_path))
    monkeypatch.setitem(sys.modules, "fabio_live.calendar_gate", None)
    monkeypatch.setattr(
        "tradier_eod_flatten._us_weekday_after_cutoff",
        lambda *_a, **_k: True,
    )
    session = FakeSession(
        [FakeResp(200, {"positions": {"position": []}})]
    )
    client = _client(session)
    code = tradier_main(
        ["--require-after-et", "--book", "orb-tradier", "--dry-run"],
        client=client,
    )
    assert code == 0
    posts = [c for c in session.calls if c.get("method") == "POST"]
    assert posts == []
