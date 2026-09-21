"""Four isolated paper flatten jobs — argv + launchd schedule (Slice 1).

One process per book. Moomoo books never emit ``tradier_eod_flatten.py``.
Tradier books never emit ``moomoo_eod_failsafe.py``. ``--book <id>`` is always
present; argparse defaults are not used. Wrappers must not pass REAL / live /
``FABIO_ALLOW_REAL_TRADING``. Moomoo scheduled argv pins ``--trd-env SIMULATE``
so a host with ``MOOMOO_TRADE_ENV=REAL`` still paper-flattens (does not refuse).

Does not invoke fail-safe from ``eod_close_all``. Paper only. No exercise.

Slice 1 launchd clocks are fixed 15:50–15:56 ET (not session_close-relative).
Early-close / calendar-aware fire times are a follow-up.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from fabio_bot_paths import fabio_bot_root
from fabio_live.calendar_gate import should_run_failsafe
from fabio_live.paper_books import (
    ALL_PAPER_BOOKS,
    BROKER_MOOMOO,
    BROKER_TRADIER,
    FLATTEN_MOOMOO,
    FLATTEN_TRADIER,
    get_book,
)

LAUNCHD_LABEL_PREFIX = "com.claytonorb.paper.flatten"
JSONL_NAME_PREFIX = "eod_failsafe"
ABORTED_WINDOW_EXIT = 4
# Stagger: Moomoo first (shared OpenD), then Tradier. Host TZ must be America/New_York.
# Fixed weekday clocks — not session_close-relative (early-close fire times: follow-up).
PAPER_FLATTEN_STAGGER_ET: tuple[tuple[str, int, int], ...] = (
    ("orb-moomoo", 15, 50),
    ("mr-moomoo", 15, 52),
    ("orb-tradier", 15, 54),
    ("mr-tradier", 15, 56),
)
# Never emit these. Moomoo jobs pass ``--trd-env SIMULATE`` (not in this set).
FORBIDDEN_ARGV_TOKENS = frozenset(
    {
        "REAL",
        "live",
        "--env",
        "FABIO_ALLOW_REAL_TRADING",
        "--live",
    }
)
MOOMOO_TRD_ENV_PIN = "SIMULATE"


@dataclass(frozen=True)
class PaperFlattenJob:
    """One independent paper fail-safe schedule row."""

    book_id: str
    broker: str
    script: str
    label: str
    hour: int
    minute: int
    jsonl: str
    argv: tuple[str, ...]


def launchd_label_for_book(book_id: str) -> str:
    spec = get_book(book_id)
    return f"{LAUNCHD_LABEL_PREFIX}.{spec.book_id}"


def jsonl_filename_for_book(book_id: str) -> str:
    spec = get_book(book_id)
    return f"{JSONL_NAME_PREFIX}.{spec.book_id}.jsonl"


def flatten_script_filename(book_id: str) -> str:
    spec = get_book(book_id)
    if spec.broker == BROKER_MOOMOO:
        return FLATTEN_MOOMOO
    if spec.broker == BROKER_TRADIER:
        return FLATTEN_TRADIER
    raise ValueError(f"No flatten script for broker {spec.broker!r}")


def flatten_script_relpath(book_id: str) -> str:
    return f"backend/{flatten_script_filename(book_id)}"


def paper_flatten_argv(book_id: str) -> list[str]:
    """Python argv (script + flags). Always includes ``--book <id>``.

    Never emits the other broker's flatten script. Never includes REAL / live
    / ``FABIO_ALLOW_REAL_TRADING`` / ``--env``. Moomoo jobs pin
    ``--trd-env SIMULATE`` (do not rely on host ``MOOMOO_TRADE_ENV``).
    """
    spec = get_book(book_id)
    script = flatten_script_relpath(spec.book_id)
    argv = [
        script,
        "--book",
        spec.book_id,
        "--require-after-et",
        "--scope",
        "options",
        "--log-format",
        "jsonl",
    ]
    if spec.broker == BROKER_MOOMOO:
        argv.extend(["--trd-env", MOOMOO_TRD_ENV_PIN])
    _assert_isolated_argv(spec.book_id, argv)
    return argv


def _assert_isolated_argv(book_id: str, argv: Sequence[str]) -> None:
    spec = get_book(book_id)
    if "--book" not in argv:
        raise ValueError(f"flatten argv for {book_id} omitted --book")
    book_idx = list(argv).index("--book")
    if book_idx + 1 >= len(argv) or argv[book_idx + 1] != spec.book_id:
        raise ValueError(f"flatten argv for {book_id} missing --book {spec.book_id}")
    joined_tokens = set(argv)
    leak = FORBIDDEN_ARGV_TOKENS & joined_tokens
    if leak:
        raise ValueError(f"flatten argv for {book_id} contains forbidden tokens: {sorted(leak)}")
    script = argv[0]
    if spec.broker == BROKER_MOOMOO:
        if FLATTEN_TRADIER in script or "tradier_eod_flatten" in script:
            raise ValueError(f"Moomoo book {book_id} must not emit Tradier flatten script")
        if FLATTEN_MOOMOO not in script:
            raise ValueError(f"Moomoo book {book_id} must emit {FLATTEN_MOOMOO}")
        if "--trd-env" not in argv:
            raise ValueError(f"Moomoo book {book_id} must pin --trd-env {MOOMOO_TRD_ENV_PIN}")
        env_idx = list(argv).index("--trd-env")
        if env_idx + 1 >= len(argv) or argv[env_idx + 1] != MOOMOO_TRD_ENV_PIN:
            raise ValueError(
                f"Moomoo book {book_id} --trd-env must be {MOOMOO_TRD_ENV_PIN}, never REAL"
            )
    elif spec.broker == BROKER_TRADIER:
        if FLATTEN_MOOMOO in script or "moomoo_eod_failsafe" in script:
            raise ValueError(f"Tradier book {book_id} must not emit Moomoo flatten script")
        if FLATTEN_TRADIER not in script:
            raise ValueError(f"Tradier book {book_id} must emit {FLATTEN_TRADIER}")
        if "--trd-env" in argv:
            raise ValueError(f"Tradier book {book_id} must not pass Moomoo --trd-env")


def wrapper_process_exit_code(script_exit_code: int) -> int:
    """Map flatten-script exit 4 (aborted_window) to skip so launchd does not error-loop."""
    if int(script_exit_code) == ABORTED_WINDOW_EXIT:
        return 0
    return int(script_exit_code)


def run_book_flatten(
    book_id: str,
    *,
    python_bin: str | None = None,
    repo_root: str | Path | None = None,
) -> int:
    """Calendar-gate, invoke that book's flatten script, map exit 4 to skip.

    Separate process from ``eod_close_all``. Paper pin stays in the script.
    This runner never adds REAL / live / ``FABIO_ALLOW_REAL_TRADING``.
    """
    spec = get_book(book_id)
    root = fabio_bot_root() if repo_root is None else Path(repo_root)
    python_bin = python_bin or sys.executable
    if not should_run_failsafe():
        print(
            f"skip book={spec.book_id} (NYSE calendar: not a session day).",
            file=sys.stderr,
        )
        return 0
    argv = paper_flatten_argv(spec.book_id)
    script_path = root / argv[0]
    cmd = [python_bin, str(script_path), *argv[1:]]
    jsonl_path = root / jsonl_filename_for_book(spec.book_id)
    env = os.environ.copy()
    env.pop("FABIO_ALLOW_REAL_TRADING", None)
    env["PYTHONPATH"] = f"{root / 'backend'}:{root / 'frontend'}"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as out:
        proc = subprocess.run(cmd, cwd=str(root), stdout=out, env=env, check=False)
    rc = int(proc.returncode)
    if rc == ABORTED_WINDOW_EXIT:
        print(
            f"book={spec.book_id} aborted_window (exit 4); skip, not crash-loop.",
            file=sys.stderr,
        )
    return wrapper_process_exit_code(rc)


def scheduled_paper_flatten_jobs() -> tuple[PaperFlattenJob, ...]:
    """Always all four books (disabled books → cheap empty-ledger no_closable)."""
    jobs: list[PaperFlattenJob] = []
    seen_ids = {row[0] for row in PAPER_FLATTEN_STAGGER_ET}
    expected = {b.book_id for b in ALL_PAPER_BOOKS}
    if seen_ids != expected:
        raise RuntimeError(
            f"PAPER_FLATTEN_STAGGER_ET must list every paper book; got {sorted(seen_ids)}"
        )
    for book_id, hour, minute in PAPER_FLATTEN_STAGGER_ET:
        spec = get_book(book_id)
        argv = tuple(paper_flatten_argv(book_id))
        jobs.append(
            PaperFlattenJob(
                book_id=spec.book_id,
                broker=spec.broker,
                script=flatten_script_filename(book_id),
                label=launchd_label_for_book(book_id),
                hour=int(hour),
                minute=int(minute),
                jsonl=jsonl_filename_for_book(book_id),
                argv=argv,
            )
        )
    return tuple(jobs)


def job_as_dict(job: PaperFlattenJob) -> dict[str, Any]:
    data = asdict(job)
    data["argv"] = list(job.argv)
    return data


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Paper flatten job argv/schedule. Requires --book for print-argv. "
            "Does not place orders. Paper only."
        )
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("print-argv", help="Print one argv token per line (requires --book).")
    pa.add_argument(
        "--book",
        required=True,
        help="Paper book id (required; never defaulted).",
    )

    pj = sub.add_parser("print-jsonl", help="Print gitignored JSONL filename for --book.")
    pj.add_argument("--book", required=True)

    pl = sub.add_parser("print-label", help="Print launchd label for --book.")
    pl.add_argument("--book", required=True)

    ps = sub.add_parser("print-schedule", help="Print four jobs (always all book ids).")
    ps.add_argument("--json", action="store_true")
    ps.add_argument(
        "--bash",
        action="store_true",
        help="One line per job: book_id hour minute label jsonl",
    )

    me = sub.add_parser(
        "map-exit",
        help="Map flatten script exit code for launchd (4 → 0 skip).",
    )
    me.add_argument("code", type=int)

    rn = sub.add_parser("run", help="Calendar-gate + run one book's flatten script.")
    rn.add_argument("--book", required=True, help="Paper book id (required; never defaulted).")

    args = p.parse_args(argv)

    if args.cmd == "print-argv":
        for token in paper_flatten_argv(args.book):
            print(token)
        return 0

    if args.cmd == "print-jsonl":
        print(jsonl_filename_for_book(args.book))
        return 0

    if args.cmd == "print-label":
        print(launchd_label_for_book(args.book))
        return 0

    if args.cmd == "print-schedule":
        jobs = scheduled_paper_flatten_jobs()
        if args.json:
            print(json.dumps([job_as_dict(j) for j in jobs], separators=(",", ":")))
            return 0
        if args.bash:
            for job in jobs:
                print(
                    f"flatten-job {job.book_id} {job.hour} {job.minute} "
                    f"{job.label} {job.jsonl}"
                )
            return 0
        for job in jobs:
            inner = " ".join(job.argv)
            print(
                f"label={job.label} weekday={job.hour:02d}:{job.minute:02d}ET "
                f"argv={inner} jsonl={job.jsonl}"
            )
            print(f"  launchd: /bin/bash portal/run_paper_flatten_book.sh --book {job.book_id}")
        return 0

    if args.cmd == "map-exit":
        print(wrapper_process_exit_code(args.code))
        return 0

    if args.cmd == "run":
        return run_book_flatten(args.book)

    return 2


def main() -> int:
    try:
        return _main(sys.argv[1:])
    except Exception as e:
        print(f"paper_flatten_jobs error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
