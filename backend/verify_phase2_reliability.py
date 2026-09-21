"""
verify_phase2_reliability.py — pre-open reliability gate for Phase 2 controls.

Usage:
    python3 verify_phase2_reliability.py
    python3 verify_phase2_reliability.py --max-age-min 30 --allow-stale-data
    python3 verify_phase2_reliability.py --sync-audit-jsonl audit_sync.jsonl --sync-audit-max-age-min 120
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from fabio_live.constants import HEALTH_SNAPSHOT_PATH

from verify_canonical_publish import audit_jsonl_gate_failures


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate Phase 2 reliability signals.")
    p.add_argument(
        "--snapshot-path",
        default=HEALTH_SNAPSHOT_PATH,
        help="Path to bot health snapshot JSONL file.",
    )
    p.add_argument(
        "--max-age-min",
        type=float,
        default=20.0,
        help="Maximum allowed age for latest snapshot.",
    )
    p.add_argument(
        "--allow-stale-data",
        action="store_true",
        help="Do not fail when data_health entries are STALE.",
    )
    p.add_argument(
        "--max-queue-ratio",
        type=float,
        default=0.95,
        help="Fail if queue_depth / queue_max exceeds this ratio.",
    )
    p.add_argument(
        "--sync-audit-jsonl",
        default="",
        help=(
            "If set, also require scripts/audit_moomoo_sync last line to not be FAIL/ERROR "
            "(same format as audit_sync.jsonl)."
        ),
    )
    p.add_argument(
        "--sync-audit-max-age-min",
        type=float,
        default=-1,
        help="With --sync-audit-jsonl, fail when record ts older than N minutes (-1=skip)",
    )
    return p.parse_args()


def _load_last_snapshot(snapshot_path: Path) -> dict:
    if not snapshot_path.exists():
        raise FileNotFoundError(f"snapshot file not found: {snapshot_path}")
    last = None
    with snapshot_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        raise RuntimeError(f"snapshot file is empty: {snapshot_path}")
    return json.loads(last)


def _age_minutes(ts: str) -> float:
    # Supports both naive and TZ-aware ISO strings produced by isoformat().
    dt = datetime.fromisoformat(ts)
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return max(0.0, (now - dt).total_seconds() / 60.0)


def main() -> int:
    args = _parse_args()
    snapshot_path = Path(args.snapshot_path)

    failures: list[str] = []
    warnings: list[str] = []

    try:
        snap = _load_last_snapshot(snapshot_path)
    except Exception as e:
        print(f"FAIL: cannot load snapshot: {e}")
        return 1

    # Desk-level gate: ops / data_health / worker liveness. Top-level `circuit`
    # is a deprecated alias of the orb-moomoo book CB (SHIP-007). This gate does
    # not require `books`, so pre-slice snapshots still PASS. Prefer
    # books["orb-moomoo"]["cb"] when present; per-book checks are a later option.

    ts = str(snap.get("ts", ""))
    if not ts:
        failures.append("missing snapshot timestamp")
        age_min = float("inf")
    else:
        age_min = _age_minutes(ts)
        if age_min > args.max_age_min:
            failures.append(
                f"snapshot too old ({age_min:.1f}m > {args.max_age_min:.1f}m)"
            )

    ops = snap.get("ops", {})
    q_depth = int(ops.get("queue_depth", 0))
    q_max = int(ops.get("queue_max", 0))
    thread_alive = bool(ops.get("thread_alive", False))
    errors = int(ops.get("errors", 0))
    dropped_critical = int(ops.get("dropped_critical", 0))
    dropped_noncritical = int(ops.get("dropped_noncritical", 0))

    if not thread_alive:
        failures.append("async worker not alive")
    if q_max <= 0:
        failures.append("invalid queue_max in snapshot")
    elif q_depth / q_max > args.max_queue_ratio:
        failures.append(
            f"queue pressure high ({q_depth}/{q_max} > {args.max_queue_ratio:.2f})"
        )
    if dropped_critical > 0:
        failures.append(f"critical events dropped ({dropped_critical})")
    if errors > 0:
        warnings.append(f"async errors observed ({errors})")
    if dropped_noncritical > 0:
        warnings.append(f"noncritical drops observed ({dropped_noncritical})")

    data_health = snap.get("data_health", {})
    stale_keys = sorted(k for k, v in data_health.items() if v.get("state") == "STALE")
    if stale_keys and not args.allow_stale_data:
        failures.append(f"stale feeds present: {', '.join(stale_keys)}")
    elif stale_keys:
        warnings.append(f"stale feeds allowed by flag: {', '.join(stale_keys)}")

    sa_path = str(args.sync_audit_jsonl).strip()
    if sa_path:
        max_age = None if float(args.sync_audit_max_age_min) < 0 else float(args.sync_audit_max_age_min)
        for msg in audit_jsonl_gate_failures(Path(sa_path), max_age_min=max_age):
            failures.append(f"sync audit: {msg}")

    print("Phase 2 Reliability Gate")
    print("=" * 26)
    print(f"Snapshot: {snapshot_path}")
    print(f"Timestamp: {ts or '(missing)'}")
    print(f"Age: {age_min:.1f}m")
    print(f"Ops: queue={q_depth}/{q_max} errors={errors} thread_alive={thread_alive}")
    print(
        "Drops: "
        f"noncritical={dropped_noncritical} critical={dropped_critical}"
    )

    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print(f"- {w}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"- {f}")
        print("\nRESULT: FAIL")
        return 1

    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
