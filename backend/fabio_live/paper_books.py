"""Four isolated paper books: ORB×Moomoo, ORB×Tradier, MR×Moomoo, MR×Tradier.

Captain lock: each strategy gets its own $10k starting balance on each
account, with separate OrderManager / circuit / ledger / flatten. Flatten is
never shared across brokers, and a flatten for one book must not close another
book on the same firm.

Default remains paper. No live / REAL. ORB-Moomoo dashboard FIFO notes stay
``moomoo_paper_fifo``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from fabio_live.circuit import RiskCircuitBreaker
from fabio_bot_paths import fabio_bot_root

PAPER_BOOK_STARTING_BALANCE = 10_000.0
PAPER_BOOKS_ENV = "FABIO_PAPER_BOOKS"
TRADIER_PAPER_BOOKS_ENV = "FABIO_TRADIER_PAPER_BOOKS"
LEDGER_DIR_ENV = "FABIO_PAPER_BOOK_LEDGER_DIR"
# Health JSONL: never dump full code lists (size). Preview is sorted, truncated.
LEDGER_CODES_PREVIEW_LIMIT = 8
_LAST_FLATTEN_SUMMARY_KEYS = (
    "book_id",
    "broker",
    "script",
    "started_at_utc",
    "finished_at_utc",
    "dry_run",
    "exit_code",
    "selected_codes_count",
    "reason",
    "layer",
)

BOOK_ORB_MOOMOO = "orb-moomoo"
BOOK_ORB_TRADIER = "orb-tradier"
BOOK_MR_MOOMOO = "mr-moomoo"
BOOK_MR_TRADIER = "mr-tradier"

STRATEGY_ORB = "orb"
STRATEGY_MR = "mr"
BROKER_MOOMOO = "moomoo"
BROKER_TRADIER = "tradier"

SOURCE_ORB_MOOMOO = "moomoo_paper"
SOURCE_ORB_TRADIER = "tradier_paper"
SOURCE_MR_MOOMOO = "mr"
SOURCE_MR_TRADIER = "mr_tradier"

FIFO_NOTES_ORB_MOOMOO = "moomoo_paper_fifo"
FIFO_NOTES_ORB_TRADIER = "tradier_paper_fifo"
FIFO_NOTES_MR_MOOMOO = "mr_paper_fifo"
FIFO_NOTES_MR_TRADIER = "mr_tradier_paper_fifo"

FLATTEN_MOOMOO = "moomoo_eod_failsafe.py"
FLATTEN_TRADIER = "tradier_eod_flatten.py"
PRIMARY_FLATTEN_SCRIPT = "eod_close_all"

LAYER_FAILSAFE = "failsafe"
LAYER_PRIMARY_BOT = "primary_bot"
LAST_FLATTEN_LAYERS = frozenset({LAYER_FAILSAFE, LAYER_PRIMARY_BOT})

REASON_BOOK_EMPTY = "book_empty"
REASON_NO_CLOSABLE = "no_closable"
REASON_FLATTEN = "flatten"
REASON_ABORTED_WINDOW = "aborted_window"
REASON_ERROR = "error"
LAST_FLATTEN_REASONS = frozenset(
    {
        REASON_BOOK_EMPTY,
        REASON_NO_CLOSABLE,
        REASON_FLATTEN,
        REASON_ABORTED_WINDOW,
        REASON_ERROR,
    }
)
LAST_FLATTEN_SUFFIX = ".last_flatten.json"
LAST_FLATTEN_SIDECAR_SUFFIX = LAST_FLATTEN_SUFFIX  # SHIP-007 alias of SHIP-008 path


@dataclass(frozen=True)
class PaperBookSpec:
    """Static identity for one isolated paper book."""

    book_id: str
    strategy: str
    broker: str
    source: str
    fifo_notes: str
    flatten_entry: str
    starting_balance: float = PAPER_BOOK_STARTING_BALANCE

    @property
    def is_firm_default(self) -> bool:
        """True for the broker fail-safe default (ORB book on that firm)."""
        return self.strategy == STRATEGY_ORB


ORB_MOOMOO = PaperBookSpec(
    book_id=BOOK_ORB_MOOMOO,
    strategy=STRATEGY_ORB,
    broker=BROKER_MOOMOO,
    source=SOURCE_ORB_MOOMOO,
    fifo_notes=FIFO_NOTES_ORB_MOOMOO,
    flatten_entry=FLATTEN_MOOMOO,
)
ORB_TRADIER = PaperBookSpec(
    book_id=BOOK_ORB_TRADIER,
    strategy=STRATEGY_ORB,
    broker=BROKER_TRADIER,
    source=SOURCE_ORB_TRADIER,
    fifo_notes=FIFO_NOTES_ORB_TRADIER,
    flatten_entry=FLATTEN_TRADIER,
)
MR_MOOMOO = PaperBookSpec(
    book_id=BOOK_MR_MOOMOO,
    strategy=STRATEGY_MR,
    broker=BROKER_MOOMOO,
    source=SOURCE_MR_MOOMOO,
    fifo_notes=FIFO_NOTES_MR_MOOMOO,
    flatten_entry=FLATTEN_MOOMOO,
)
MR_TRADIER = PaperBookSpec(
    book_id=BOOK_MR_TRADIER,
    strategy=STRATEGY_MR,
    broker=BROKER_TRADIER,
    source=SOURCE_MR_TRADIER,
    fifo_notes=FIFO_NOTES_MR_TRADIER,
    flatten_entry=FLATTEN_TRADIER,
)

ALL_PAPER_BOOKS: tuple[PaperBookSpec, ...] = (
    ORB_MOOMOO,
    ORB_TRADIER,
    MR_MOOMOO,
    MR_TRADIER,
)
PAPER_BOOKS_BY_ID: dict[str, PaperBookSpec] = {b.book_id: b for b in ALL_PAPER_BOOKS}
MOOMOO_BOOK_IDS: tuple[str, ...] = tuple(
    b.book_id for b in ALL_PAPER_BOOKS if b.broker == BROKER_MOOMOO
)
TRADIER_BOOK_IDS: tuple[str, ...] = tuple(
    b.book_id for b in ALL_PAPER_BOOKS if b.broker == BROKER_TRADIER
)
PAPER_BOOK_SOURCES: frozenset[str] = frozenset(b.source for b in ALL_PAPER_BOOKS)
PAPER_BOOK_FIFO_NOTES: frozenset[str] = frozenset(b.fifo_notes for b in ALL_PAPER_BOOKS)
MR_BOOK_SOURCES: frozenset[str] = frozenset(
    b.source for b in ALL_PAPER_BOOKS if b.strategy == STRATEGY_MR
)


class UnknownPaperBook(ValueError):
    """Raised when a book id is not one of the four isolated paper books."""


def get_book(book_id: str) -> PaperBookSpec:
    spec = PAPER_BOOKS_BY_ID.get(str(book_id or "").strip().lower())
    if spec is None:
        raise UnknownPaperBook(
            f"Unknown paper book {book_id!r}. Use one of: "
            + ", ".join(b.book_id for b in ALL_PAPER_BOOKS)
        )
    return spec


def default_flatten_book_id(broker: str) -> str:
    """Fail-safe default: the ORB book on that firm — never the other broker."""
    token = str(broker or "").strip().lower()
    if token == BROKER_MOOMOO:
        return BOOK_ORB_MOOMOO
    if token == BROKER_TRADIER:
        return BOOK_ORB_TRADIER
    raise ValueError(f"Unknown broker {broker!r} for flatten default")


def flatten_entry_for_book(book_id: str) -> str:
    spec = get_book(book_id)
    return spec.flatten_entry


def flatten_scripts_are_isolated(book_a: str, book_b: str) -> bool:
    """True when two books must not share a flatten process."""
    a, b = get_book(book_a), get_book(book_b)
    if a.broker != b.broker:
        return True
    return a.book_id != b.book_id


def modeled_book_equity(
    cb: RiskCircuitBreaker,
    *,
    starting: float = PAPER_BOOK_STARTING_BALANCE,
) -> float:
    """Sizing / CB book value: $10k start plus this book's realized PnL."""
    return float(starting) + float(getattr(cb, "realized_pnl", 0.0) or 0.0)


def last_flatten_sidecar_path(
    book_id: str, directory: str | Path | None = None
) -> Path:
    """Alias of ``last_flatten_path``. Health reads; SHIP-008 writers own the file."""
    return last_flatten_path(book_id, directory)


def read_last_flatten_summary(
    book_id: str, directory: str | Path | None = None
) -> dict[str, Any] | None:
    """Compact last-flatten summary, or None when the sidecar is absent/unreadable.

    Uses SHIP-008 ``read_last_flatten`` (book_id-checked). Health never writes.
    """
    data = read_last_flatten(book_id, directory)
    if not data:
        return None
    summary: dict[str, Any] = {}
    for key in _LAST_FLATTEN_SUMMARY_KEYS:
        if key in data:
            summary[key] = data[key]
    for list_key, count_key in (("planned", "planned_count"), ("failures", "failure_count")):
        if list_key not in data:
            continue
        raw = data[list_key]
        if isinstance(raw, list):
            summary[count_key] = len(raw)
        elif count_key not in summary:
            summary[count_key] = raw
    if summary:
        return summary
    scalars: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, (str, int, float, bool)) or val is None:
            scalars[key] = val
    return scalars or {"present": True}


def circuit_breaker_snapshot(
    cb: RiskCircuitBreaker | None,
    *,
    starting: float = PAPER_BOOK_STARTING_BALANCE,
) -> dict[str, Any]:
    """Per-book CB payload. Unbound books still emit every key (zeros + $10k denom)."""
    if cb is None:
        return {
            "portfolio_at_open": float(starting),
            "realized_pnl": 0.0,
            "daily_loss_pct": 0.0,
            "trade_count": 0,
            "loss_streak": 0,
        }
    return {
        "portfolio_at_open": float(getattr(cb, "portfolio_at_open", 0.0) or 0.0),
        "realized_pnl": float(getattr(cb, "realized_pnl", 0.0) or 0.0),
        "daily_loss_pct": round(float(cb.daily_loss_pct), 6),
        "trade_count": int(getattr(cb, "trade_count", 0) or 0),
        "loss_streak": int(getattr(cb, "loss_streak", 0) or 0),
    }


def _ledger_codes_preview(codes: Iterable[str]) -> list[str]:
    ordered = sorted({str(c).strip() for c in codes if str(c).strip()})
    return ordered[:LEDGER_CODES_PREVIEW_LIMIT]


def _enabled_book_id_set(enabled_ids: Iterable[str] | None) -> set[str]:
    """Canonical ids only. Unknown tokens are skipped (never a fifth book key)."""
    if enabled_ids is None:
        return set(enabled_paper_book_ids(strict=False))
    enabled: set[str] = set()
    for raw in enabled_ids:
        token = str(raw or "").strip().lower()
        if not token:
            continue
        spec = PAPER_BOOKS_BY_ID.get(token)
        if spec is None:
            print(f"  ⚠  [HEALTH] skipping unknown paper book token {token!r}")
            continue
        enabled.add(spec.book_id)
    return enabled


def canonical_books_health_fallback(
    *, enabled_ids: Iterable[str] = ()
) -> dict[str, dict[str, Any]]:
    """Four canonical keys, no I/O. Used when health books assembly fails."""
    enabled = _enabled_book_id_set(enabled_ids)
    books: dict[str, dict[str, Any]] = {}
    for spec in ALL_PAPER_BOOKS:
        books[spec.book_id] = {
            "enabled": spec.book_id in enabled,
            "bound": False,
            "broker": spec.broker,
            "strategy": spec.strategy,
            "source": spec.source,
            "starting_balance": float(spec.starting_balance),
            "cb": circuit_breaker_snapshot(None, starting=spec.starting_balance),
            "ledger": {
                "path": str(ledger_directory() / f"{spec.book_id}.json"),
                "code_count": 0,
                "codes_preview": [],
            },
            "modeled_equity": float(spec.starting_balance),
            "last_flatten": None,
        }
    return books


def paper_books_health_map(
    registry: PaperBookRegistry | None = None,
    *,
    enabled_ids: Iterable[str] | None = None,
    ledger_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Health JSONL ``books`` map: all four ids, always present.

    Unbound / disabled books still include every field with ``enabled`` /
    ``bound`` false. Never omit a book key (omission looks like healthy-empty).
    Each book's CB is that runtime's circuit only — ORB PnL is never copied
    onto MR or Tradier books. Unknown enabled-id tokens are skipped, not raised.
    """
    enabled = _enabled_book_id_set(enabled_ids)
    books: dict[str, dict[str, Any]] = {}
    for spec in ALL_PAPER_BOOKS:
        rt = registry.get(spec.book_id) if registry is not None else None
        bound = rt is not None
        dir_for: str | Path | None = ledger_dir
        if dir_for is None and registry is not None:
            try:
                dir_for = registry._ledger_dir_for(spec.book_id)
            except Exception:
                dir_for = ledger_dir
        try:
            if bound:
                codes = set(rt.ledger.codes)
                path = str(rt.ledger.path)
                cb_snap = circuit_breaker_snapshot(
                    rt.cb, starting=spec.starting_balance
                )
                modeled = float(rt.modeled_equity())
            else:
                # Unbound CB stays zeros / modeled_equity=starting_balance even
                # when a disk ledger has residual codes — ledger fields are the
                # source of truth for those codes.
                disk = PaperBookLedger(spec.book_id, directory=dir_for)
                disk.load()
                codes = set(disk.codes)
                path = str(disk.path)
                cb_snap = circuit_breaker_snapshot(
                    None, starting=spec.starting_balance
                )
                modeled = float(spec.starting_balance)
            preview = _ledger_codes_preview(codes)
            books[spec.book_id] = {
                "enabled": spec.book_id in enabled,
                "bound": bound,
                "broker": spec.broker,
                "strategy": spec.strategy,
                "source": spec.source,
                "starting_balance": float(spec.starting_balance),
                "cb": cb_snap,
                "ledger": {
                    "path": path,
                    "code_count": len(codes),
                    "codes_preview": preview,
                },
                "modeled_equity": modeled,
                "last_flatten": read_last_flatten_summary(
                    spec.book_id, directory=dir_for
                ),
            }
        except Exception as exc:
            print(f"  ⚠  [HEALTH] book {spec.book_id} snapshot failed: {exc}")
            fallback = canonical_books_health_fallback(enabled_ids=enabled)
            row = fallback[spec.book_id]
            row["bound"] = bound
            books[spec.book_id] = row
    return books


def init_book_circuit(
    cb: RiskCircuitBreaker | None = None,
    *,
    starting: float = PAPER_BOOK_STARTING_BALANCE,
) -> RiskCircuitBreaker:
    circuit = cb if cb is not None else RiskCircuitBreaker()
    circuit.set_portfolio_open(float(starting))
    return circuit


def ledger_directory(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    raw = os.getenv(LEDGER_DIR_ENV, "").strip()
    if raw:
        return Path(raw)
    return fabio_bot_root() / "backend" / "paper_book_ledgers"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_flatten_path(
    book_id: str, directory: str | Path | None = None
) -> Path:
    """``<ledger_dir>/<book_id>.last_flatten.json`` — never another book's file."""
    spec = get_book(book_id)
    return ledger_directory(directory) / f"{spec.book_id}{LAST_FLATTEN_SUFFIX}"


def flatten_sidecar_reason(
    *,
    aborted: bool = False,
    error: bool = False,
    broker_empty: bool = False,
    selected_codes_count: int = 0,
) -> str:
    """Map a flatten outcome onto the last-run reason enum."""
    if aborted:
        return REASON_ABORTED_WINDOW
    if error:
        return REASON_ERROR
    if broker_empty:
        return REASON_BOOK_EMPTY
    if int(selected_codes_count) <= 0:
        return REASON_NO_CLOSABLE
    return REASON_FLATTEN


def last_flatten_payload(
    *,
    book_id: str,
    broker: str | None = None,
    script: str,
    started_at_utc: str,
    finished_at_utc: str | None = None,
    dry_run: bool,
    exit_code: int,
    planned: int,
    failures: int,
    selected_codes_count: int,
    reason: str,
    layer: str,
) -> dict[str, Any] | None:
    """Build a last-run dict, or ``None`` when broker/book would cross-write."""
    spec = get_book(book_id)
    wanted = str(broker or spec.broker).strip().lower()
    if spec.broker != wanted:
        return None
    reason_token = str(reason or "").strip()
    if reason_token not in LAST_FLATTEN_REASONS:
        return None
    layer_token = str(layer or "").strip()
    if layer_token not in LAST_FLATTEN_LAYERS:
        return None
    return {
        "book_id": spec.book_id,
        "broker": spec.broker,
        "script": str(script),
        "layer": layer_token,
        "started_at_utc": str(started_at_utc),
        "finished_at_utc": str(finished_at_utc or utc_now_iso()),
        "dry_run": bool(dry_run),
        "exit_code": int(exit_code),
        "planned": int(planned),
        "failures": int(failures),
        "selected_codes_count": int(selected_codes_count),
        "reason": reason_token,
    }


def write_last_flatten(
    *,
    book_id: str,
    broker: str | None = None,
    script: str,
    started_at_utc: str,
    finished_at_utc: str | None = None,
    dry_run: bool,
    exit_code: int,
    planned: int,
    failures: int,
    selected_codes_count: int,
    reason: str,
    layer: str,
    directory: str | Path | None = None,
) -> Path | None:
    """Atomically write one book's last-flatten sidecar.

    Refuses to write when ``broker`` does not match the book's firm, so a
    Moomoo fail-safe cannot create a Tradier sidecar (and vice versa). The
    path is always ``<book_id>.last_flatten.json`` for *this* book only.
    """
    payload = last_flatten_payload(
        book_id=book_id,
        broker=broker,
        script=script,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        dry_run=dry_run,
        exit_code=exit_code,
        planned=planned,
        failures=failures,
        selected_codes_count=selected_codes_count,
        reason=reason,
        layer=layer,
    )
    if payload is None:
        return None
    path = last_flatten_path(payload["book_id"], directory)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return path


def read_last_flatten(
    book_id: str, directory: str | Path | None = None
) -> dict[str, Any] | None:
    """Load a sidecar if present. Slice 1 / dashboard may ``read_if_exists``."""
    path = last_flatten_path(book_id, directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("book_id") != get_book(book_id).book_id:
        return None
    return data


def write_failsafe_last_flatten(
    *,
    book_id: str,
    started_at_utc: str,
    finished_at_utc: str | None = None,
    dry_run: bool,
    exit_code: int,
    planned: int,
    failures: int,
    selected_codes_count: int,
    reason: str,
    directory: str | Path | None = None,
) -> Path | None:
    """Fail-safe scripts only. ``layer: failsafe``; script from the book spec."""
    spec = get_book(book_id)
    return write_last_flatten(
        book_id=spec.book_id,
        broker=spec.broker,
        script=spec.flatten_entry,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        dry_run=dry_run,
        exit_code=exit_code,
        planned=planned,
        failures=failures,
        selected_codes_count=selected_codes_count,
        reason=reason,
        layer=LAYER_FAILSAFE,
        directory=directory,
    )


def write_primary_last_flatten(
    *,
    book_id: str,
    started_at_utc: str,
    finished_at_utc: str | None = None,
    dry_run: bool = False,
    exit_code: int = 0,
    planned: int,
    failures: int,
    selected_codes_count: int,
    reason: str,
    directory: str | Path | None = None,
) -> Path | None:
    """In-process ``eod_close_all``. ``layer: primary_bot`` so it is not merged
    with fail-safe last-run files.
    """
    spec = get_book(book_id)
    return write_last_flatten(
        book_id=spec.book_id,
        broker=spec.broker,
        script=PRIMARY_FLATTEN_SCRIPT,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        dry_run=dry_run,
        exit_code=exit_code,
        planned=planned,
        failures=failures,
        selected_codes_count=selected_codes_count,
        reason=reason,
        layer=LAYER_PRIMARY_BOT,
        directory=directory,
    )


def tradier_paper_books_enabled() -> bool:
    return os.getenv(TRADIER_PAPER_BOOKS_ENV, "").strip() == "1"


def enabled_paper_book_ids(
    *, mr_paper: bool | None = None, strict: bool = True
) -> tuple[str, ...]:
    """Books that may be constructed at runtime.

    Specs for all four always exist. Default runtime is ORB-Moomoo; MR-Moomoo
    joins when MR paper is on; Tradier books join only behind
    ``FABIO_TRADIER_PAPER_BOOKS=1``. ``FABIO_PAPER_BOOKS`` can pin an explicit
    comma list of the four ids.

    ``strict=True`` (default) raises ``UnknownPaperBook`` on a bad pin token.
    Health / live-loop callers pass ``strict=False`` so a typo cannot abort
    ``run()`` — unknown tokens are logged and skipped.
    """
    raw = os.getenv(PAPER_BOOKS_ENV, "").strip()
    if raw:
        ids = []
        unknown: list[str] = []
        for part in raw.split(","):
            token = part.strip().lower()
            if not token:
                continue
            spec = PAPER_BOOKS_BY_ID.get(token)
            if spec is None:
                unknown.append(token)
                if strict:
                    get_book(token)
                continue
            ids.append(spec.book_id)
        if unknown and not strict:
            print(
                f"  ⚠  Skipping unknown {PAPER_BOOKS_ENV} token(s): "
                + ", ".join(unknown)
            )
        if ids:
            return tuple(dict.fromkeys(ids))
    out = [BOOK_ORB_MOOMOO]
    mr_on = (
        mr_paper
        if mr_paper is not None
        else os.getenv("FABIO_MR_PAPER_ENABLED", "").strip() == "1"
    )
    if mr_on:
        out.append(BOOK_MR_MOOMOO)
    if tradier_paper_books_enabled():
        out.append(BOOK_ORB_TRADIER)
        if mr_on:
            out.append(BOOK_MR_TRADIER)
    return tuple(dict.fromkeys(out))


@dataclass
class PaperBookLedger:
    """Tracked broker codes for one book. Used by that book's flatten only."""

    book_id: str
    directory: Path | None = None
    codes: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.book_id = get_book(self.book_id).book_id
        if self.directory is not None:
            self.directory = Path(self.directory)
        self.codes = {str(c).strip() for c in self.codes if str(c).strip()}

    @property
    def path(self) -> Path:
        return ledger_directory(self.directory) / f"{self.book_id}.json"

    @property
    def spec(self) -> PaperBookSpec:
        return get_book(self.book_id)

    def replace_codes(self, codes: Iterable[str]) -> None:
        self.codes = {str(c).strip() for c in codes if str(c).strip()}

    def merge_codes(self, codes: Iterable[str]) -> None:
        """Union broker codes from fills. Never used to wipe disk truth."""
        for raw in codes:
            token = str(raw or "").strip()
            if token:
                self.codes.add(token)

    def add(self, code: str) -> None:
        token = str(code or "").strip()
        if token:
            self.codes.add(token)

    def discard(self, code: str) -> None:
        self.codes.discard(str(code or "").strip())

    def load(self) -> bool:
        path = self.path
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        raw = data.get("codes") or []
        if not isinstance(raw, list):
            return False
        self.replace_codes(str(x) for x in raw)
        return True

    def save(self, *, allow_empty: bool = False) -> bool:
        """Persist this book's codes.

        Refuse to write an empty code set over a non-empty on-disk ledger
        unless ``allow_empty`` (intentional flatten / clear). An empty OM
        after restart is not a flatten.
        """
        if not self.codes and not allow_empty:
            existing = PaperBookLedger(self.book_id, directory=self.directory)
            if existing.load() and existing.codes:
                self.codes = set(existing.codes)
                return False
            return True
        path = self.path
        payload = {
            "book_id": self.book_id,
            "strategy": self.spec.strategy,
            "broker": self.spec.broker,
            "source": self.spec.source,
            "starting_balance": self.spec.starting_balance,
            "codes": sorted(self.codes),
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True


def load_all_ledgers(directory: str | Path | None = None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {b.book_id: set() for b in ALL_PAPER_BOOKS}
    for spec in ALL_PAPER_BOOKS:
        ledger = PaperBookLedger(spec.book_id, directory=directory)
        ledger.load()
        out[spec.book_id] = set(ledger.codes)
    return out


def codes_from_order_mgr(order_mgr: Any) -> set[str]:
    positions = getattr(order_mgr, "positions", None) or {}
    out: set[str] = set()
    for pos in positions.values():
        if not isinstance(pos, dict):
            continue
        code = str(pos.get("code") or "").strip()
        if code:
            out.add(code)
    return out


def flatten_symbol_filters(
    book_id: str,
    broker: str,
    ledgers: Mapping[str, Iterable[str]] | None = None,
) -> tuple[set[str], set[str]]:
    """``(only_symbols, exclude_symbols)`` for one firm's flatten script.

    Always fail closed: ``only_symbols`` is a set, never ``None``. An empty
    own ledger flattens nothing rather than the whole firm (which would
    market-close the sibling strategy when that ledger was also unsaved).
    Sibling codes on the same firm are always excluded.
    """
    spec = get_book(book_id)
    wanted = str(broker or "").strip().lower()
    if spec.broker != wanted:
        raise ValueError(
            f"book {spec.book_id} is broker={spec.broker}; "
            f"cannot flatten via {wanted} entry"
        )
    tables = {
        bid: {str(c).strip() for c in (codes or []) if str(c).strip()}
        for bid, codes in (ledgers or {}).items()
    }
    other: set[str] = set()
    for other_spec in ALL_PAPER_BOOKS:
        if other_spec.broker != spec.broker or other_spec.book_id == spec.book_id:
            continue
        other.update(tables.get(other_spec.book_id) or ())
    own = set(tables.get(spec.book_id) or ())
    if own:
        return own, other
    return set(), other


def select_flatten_codes(
    *,
    book_id: str,
    broker: str,
    account_codes: Sequence[str],
    ledgers: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """Codes this book's flatten may close.

    Never returns another book's codes on the same firm. Never includes the
    other broker (caller must use that firm's flatten script).
    """
    spec = get_book(book_id)
    wanted = str(broker or "").strip().lower()
    if spec.broker != wanted:
        raise ValueError(
            f"book {spec.book_id} is broker={spec.broker}; "
            f"cannot flatten via {wanted} entry"
        )
    tables = {
        bid: {str(c).strip() for c in (codes or []) if str(c).strip()}
        for bid, codes in (ledgers or {}).items()
    }
    other: set[str] = set()
    for other_spec in ALL_PAPER_BOOKS:
        if other_spec.broker != spec.broker or other_spec.book_id == spec.book_id:
            continue
        other.update(tables.get(other_spec.book_id) or ())
    own = set(tables.get(spec.book_id) or ())
    selected: list[str] = []
    seen: set[str] = set()
    for raw in account_codes:
        code = str(raw or "").strip()
        if not code or code in seen:
            continue
        if code in other:
            continue
        if not own or code not in own:
            # Empty own ledger: flatten nothing (fail closed), never the firm.
            continue
        selected.append(code)
        seen.add(code)
    return selected


def is_allowed_open_position_notes(notes: str) -> bool:
    """Dashboard open-row notes, including per-book source tags.

    ``moomoo_paper_fifo`` stays valid so Moomoo FIFO preservation is unchanged.
    """
    n = str(notes or "").strip()
    if n.startswith("broker code="):
        return True
    if n in PAPER_BOOK_FIFO_NOTES:
        return True
    lower = n.lower()
    if lower.startswith("source="):
        token = lower.split("|", 1)[0][len("source=") :].strip()
        return token in PAPER_BOOK_SOURCES
    return False


@dataclass
class PaperBookRuntime:
    """Live handles for one book: isolated OM, CB, ledger."""

    spec: PaperBookSpec
    order_mgr: Any
    cb: RiskCircuitBreaker
    ledger: PaperBookLedger

    def modeled_equity(self) -> float:
        return modeled_book_equity(self.cb, starting=self.spec.starting_balance)

    def sync_ledger_from_positions(self, *, replace: bool = False) -> None:
        """Update the ledger from this book's OrderManager.

        Default is union: new OM fills are added, but an empty OM (restart,
        hydrate) does not wipe persisted codes. ``replace=True`` is only for
        an intentional flatten that should match remaining OM positions.
        """
        om_codes = codes_from_order_mgr(self.order_mgr)
        if replace:
            self.ledger.replace_codes(om_codes)
            return
        if not om_codes:
            return
        self.ledger.merge_codes(om_codes)

    def _merge_disk_codes(self) -> None:
        disk = PaperBookLedger(self.spec.book_id, directory=self.ledger.directory)
        if disk.load() and disk.codes:
            self.ledger.merge_codes(disk.codes)

    def flatten_tracked(self, *, reason: str = "EOD") -> list[dict[str, Any]]:
        """Market-close only this book's tracked OM symbols. Never another book.

        An empty OrderManager after restart is not an intentional flatten
        (Tradier books are not rehydrated from broker/ledger). Keep the disk
        ledger so ``tradier_eod_flatten`` / fail-safe can still select those
        codes.

        Captain A: codes that were on the ledger but never in the pre-flatten
        OM (restart orphans) stay on disk. ``allow_empty`` is denied while
        those orphans remain so a same-session fill cannot wipe them.
        """
        results: list[dict[str, Any]] = []
        mgr = self.order_mgr
        positions = dict(getattr(mgr, "positions", {}) or {})
        if not self.ledger.codes:
            self._merge_disk_codes()
        if not positions:
            if self.ledger.codes:
                print(
                    f"  ↷ flatten_tracked refused ledger clear for "
                    f"{self.spec.book_id} — OM empty, {len(self.ledger.codes)} "
                    "tracked code(s) remain on disk"
                )
            return results
        pre_om = codes_from_order_mgr(mgr)
        orphans = set(self.ledger.codes) - pre_om
        for symbol in list(positions):
            if not hasattr(mgr, "exit_result"):
                break
            result = mgr.exit_result(symbol, reason=reason)
            if result.get("success"):
                pnl = float(result.get("pnl", 0.0) or 0.0)
                self.cb.record_result(pnl)
            results.append(result)
        remaining = codes_from_order_mgr(self.order_mgr)
        self.ledger.replace_codes(set(remaining) | orphans)
        # Deny allow_empty while ledger-only orphans remain (still open at broker).
        self.ledger.save(allow_empty=not orphans)
        if orphans:
            print(
                f"  ↷ flatten_tracked retained {len(orphans)} ledger-only "
                f"code(s) for {self.spec.book_id} — never in OM; fail-safe "
                "must flatten"
            )
        return results


class PaperBookRegistry:
    """Named runtimes. Positions dicts are never shared across books."""

    def __init__(self) -> None:
        self._books: dict[str, PaperBookRuntime] = {}

    def bind(
        self,
        book_id: str,
        *,
        order_mgr: Any,
        cb: RiskCircuitBreaker | None = None,
        ledger_dir: str | Path | None = None,
    ) -> PaperBookRuntime:
        spec = get_book(book_id)
        for existing in self._books.values():
            if existing.order_mgr is order_mgr:
                raise ValueError(
                    f"cannot bind {spec.book_id}: OrderManager already bound to "
                    f"{existing.spec.book_id} (separate ledgers/OMs required)"
                )
        circuit = init_book_circuit(cb, starting=spec.starting_balance)
        ledger = PaperBookLedger(spec.book_id, directory=ledger_dir)
        ledger.load()
        runtime = PaperBookRuntime(
            spec=spec, order_mgr=order_mgr, cb=circuit, ledger=ledger
        )
        # Union OM fills onto disk truth. Empty OM after restart must not wipe.
        runtime.sync_ledger_from_positions()
        self._books[spec.book_id] = runtime
        return runtime

    def get(self, book_id: str) -> PaperBookRuntime | None:
        return self._books.get(get_book(book_id).book_id)

    def __contains__(self, book_id: object) -> bool:
        try:
            return get_book(str(book_id)).book_id in self._books
        except UnknownPaperBook:
            return False

    def books_for_broker(self, broker: str) -> list[PaperBookRuntime]:
        token = str(broker or "").strip().lower()
        return [rt for rt in self._books.values() if rt.spec.broker == token]

    def _ledger_dir_for(self, book_id: str) -> Path | None:
        rt = self._books.get(get_book(book_id).book_id)
        if rt is not None:
            return rt.ledger.directory
        for existing in self._books.values():
            if existing.ledger.directory is not None:
                return existing.ledger.directory
        return None

    def protected_codes_for_broker(
        self, broker: str, *, except_book: str | None = None
    ) -> set[str]:
        """Sibling book codes on this firm that the ORB orphan sweep must skip.

        Disk ledgers are included even when the sibling OM is empty after a
        restart (or the sibling runtime is not bound this boot).
        """
        skip = get_book(except_book).book_id if except_book else None
        token = str(broker or "").strip().lower()
        out: set[str] = set()
        for spec in ALL_PAPER_BOOKS:
            if spec.broker != token or spec.book_id == skip:
                continue
            disk = PaperBookLedger(
                spec.book_id, directory=self._ledger_dir_for(spec.book_id)
            )
            disk.load()
            out.update(disk.codes)
            rt = self.get(spec.book_id)
            if rt is None:
                continue
            rt.sync_ledger_from_positions()
            out.update(rt.ledger.codes)
        return out

    def ledgers_map(self) -> dict[str, set[str]]:
        out = {b.book_id: set() for b in ALL_PAPER_BOOKS}
        for bid, rt in self._books.items():
            rt.sync_ledger_from_positions()
            out[bid] = set(rt.ledger.codes)
        return out

    def all_runtimes(self) -> list[PaperBookRuntime]:
        return list(self._books.values())

    def apply_starting_balances(self) -> None:
        """Force every bound book's CB denominator to that book's $10k start.

        Includes ORB-Moomoo. OpenD ``get_portfolio_value`` is never the CB base.
        """
        for rt in self._books.values():
            rt.cb.set_portfolio_open(float(rt.spec.starting_balance))

    def sync_and_save(self, book_id: str | None = None) -> None:
        """Persist tracked codes for one book, or every bound book."""
        if book_id is not None:
            rt = self.get(book_id)
            if rt is None:
                return
            rt.sync_ledger_from_positions()
            rt.ledger.save()
            return
        for rt in self._books.values():
            rt.sync_ledger_from_positions()
            rt.ledger.save()

    def sync_strategy_ledgers(self, strategy: str) -> None:
        token = str(strategy or "").strip().lower()
        for rt in self._books.values():
            if rt.spec.strategy != token:
                continue
            rt.sync_ledger_from_positions()
            rt.ledger.save()


def new_isolated_circuit() -> RiskCircuitBreaker:
    return init_book_circuit()


def try_bind_tradier_books(
    registry: PaperBookRegistry,
    *,
    mr_paper: bool,
    client: Any | None = None,
    ledger_dir: str | Path | None = None,
) -> list[str]:
    """Construct ORB-Tradier / MR-Tradier runtimes. Never used as the ORB default.

    Returns bound book ids. Missing tokens or pin failures skip quietly (paper).
    """
    if not tradier_paper_books_enabled() and client is None:
        return []
    try:
        from brokers.tradier.client import TradierPaperClient
        from brokers.tradier.orders import TradierOrderManager
    except Exception as exc:
        print(f"  ⚠  Tradier paper books unavailable: {exc}")
        return []
    try:
        http = client if client is not None else TradierPaperClient.from_env()
    except Exception as exc:
        print(f"  ⚠  Tradier paper books not bound: {exc}")
        return []
    bound: list[str] = []
    orb_om = TradierOrderManager(http, source=SOURCE_ORB_TRADIER, book_id=BOOK_ORB_TRADIER)
    registry.bind(
        BOOK_ORB_TRADIER,
        order_mgr=orb_om,
        cb=new_isolated_circuit(),
        ledger_dir=ledger_dir,
    )
    bound.append(BOOK_ORB_TRADIER)
    if mr_paper:
        mr_om = TradierOrderManager(
            http, source=SOURCE_MR_TRADIER, book_id=BOOK_MR_TRADIER
        )
        registry.bind(
            BOOK_MR_TRADIER,
            order_mgr=mr_om,
            cb=new_isolated_circuit(),
            ledger_dir=ledger_dir,
        )
        bound.append(BOOK_MR_TRADIER)
    return bound


def mirror_orb_fill_to_tradier_book(
    registry: PaperBookRegistry,
    symbol: str,
    moomoo_pos: Mapping[str, Any],
    *,
    risk_pct: float,
    portfolio_val: float,
) -> dict[str, Any] | None:
    """Copy a filled ORB-Moomoo contract onto the ORB-Tradier book only."""
    rt = registry.get(BOOK_ORB_TRADIER)
    if rt is None:
        return None
    parsed = parse_moomoo_option_code(str(moomoo_pos.get("code") or ""))
    if not parsed:
        return None
    mgr = rt.order_mgr
    if not hasattr(mgr, "enter_option_contract"):
        return None
    try:
        mgr.enter_option_contract(
            symbol,
            str(parsed["direction"]),
            strike=float(parsed["strike"]),
            expiry=str(parsed["expiry"]),
            premium=moomoo_pos.get("entry_option_price"),
            risk_pct=float(risk_pct),
            portfolio_val=float(portfolio_val),
            source=SOURCE_ORB_TRADIER,
        )
    except Exception as exc:
        print(f"  ⚠  ORB-Tradier mirror skipped [{symbol}]: {exc}")
        return None
    rt.sync_ledger_from_positions()
    rt.ledger.save()
    return mgr.positions.get(symbol)


def make_mr_executor_for_book(
    runtime: PaperBookRuntime,
    *,
    ops: Any | None = None,
    enabled: bool = True,
) -> Any:
    """MrPaperExecutor bound to one MR paper book (Moomoo or Tradier)."""
    from fabio_live.mr_paper import MrPaperExecutor

    return MrPaperExecutor(
        runtime.order_mgr,
        runtime.cb,
        ops=ops,
        paper_only=True,
        modeled_book=runtime.spec.starting_balance,
        enabled=enabled,
        get_portfolio=runtime.modeled_equity,
        source=runtime.spec.source,
        book_id=runtime.spec.book_id,
    )


def parse_moomoo_option_code(code: str) -> dict[str, str | float] | None:
    """Underlying / expiry / right / strike from a Moomoo ``US.`` OCC code."""
    raw = str(code or "").strip().upper()
    if not raw.startswith("US.") or len(raw) < 15:
        return None
    core = raw[3:]
    # Underlying then yymmdd + C/P + strike digits.
    i = 0
    while i < len(core) and core[i].isalpha():
        i += 1
    if i == 0 or i + 7 > len(core):
        return None
    underlying = core[:i]
    yymmdd = core[i : i + 6]
    right = core[i + 6 : i + 7]
    strike_raw = core[i + 7 :]
    if right not in ("C", "P") or not yymmdd.isdigit() or not strike_raw.isdigit():
        return None
    try:
        strike = int(strike_raw) / 1000.0
        yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
        expiry = f"{2000 + yy:04d}-{mm:02d}-{dd:02d}"
    except (TypeError, ValueError):
        return None
    return {
        "symbol": underlying,
        "expiry": expiry,
        "direction": "PUT" if right == "P" else "CALL",
        "strike": strike,
        "occ": core,
    }
