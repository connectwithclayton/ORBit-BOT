"""Four paper-book cards for the local dashboard (SHIP-010).

Static HTML shell + runtime JSON. Health JSONL ``books`` and last-flatten
sidecars are the inputs. Never query the other broker to fill a card.
Card equity is that book's CB modeled $10k + realized — not OpenD NAV.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

PAPER_DESK_HEADER = "Four paper books · $10k each · paper only"
PAPER_DESK_HEADER_FULL = (
    "SPY · QQQ · NVDA · 0-1DTE Options · Four paper books · $10k each · paper only"
)

BOOK_ORB_MOOMOO = "orb-moomoo"
BOOK_ORB_TRADIER = "orb-tradier"
BOOK_MR_MOOMOO = "mr-moomoo"
BOOK_MR_TRADIER = "mr-tradier"

FOUR_BOOK_IDS: tuple[str, ...] = (
    BOOK_ORB_MOOMOO,
    BOOK_ORB_TRADIER,
    BOOK_MR_MOOMOO,
    BOOK_MR_TRADIER,
)

# Identity only — used when fabio_live.paper_books is unavailable.
_BOOK_IDENTITY: dict[str, dict[str, str]] = {
    BOOK_ORB_MOOMOO: {
        "broker": "moomoo",
        "strategy": "orb",
        "source": "moomoo_paper",
        "fifo_notes": "moomoo_paper_fifo",
    },
    BOOK_ORB_TRADIER: {
        "broker": "tradier",
        "strategy": "orb",
        "source": "tradier_paper",
        "fifo_notes": "tradier_paper_fifo",
    },
    BOOK_MR_MOOMOO: {
        "broker": "moomoo",
        "strategy": "mr",
        "source": "mr",
        "fifo_notes": "mr_paper_fifo",
    },
    BOOK_MR_TRADIER: {
        "broker": "tradier",
        "strategy": "mr",
        "source": "mr_tradier",
        "fifo_notes": "mr_tradier_paper_fifo",
    },
}

_SOURCE_TO_BOOK: dict[str, str] = {
    spec["source"]: book_id for book_id, spec in _BOOK_IDENTITY.items()
}
_FIFO_TO_BOOK: dict[str, str] = {
    spec["fifo_notes"]: book_id for book_id, spec in _BOOK_IDENTITY.items()
}

MOOMOO_BROKER_CODE_PREFIX = "broker code="
_CB_DAILY_LOSS_PCT_FALLBACK = 0.02
_CB_MAX_TRADES_FALLBACK = 3
STARTING_BALANCE = 10_000.0

_UNBOUND_STATUS = "not enabled / not bound this boot"
_HEALTHY_ZERO_TOKENS = ("$0", "0 trades", "healthy")


def _cb_limits() -> tuple[float, int]:
    try:
        from fabio_live.constants import CB_DAILY_LOSS_PCT, CB_MAX_TRADES

        return float(CB_DAILY_LOSS_PCT), int(CB_MAX_TRADES)
    except Exception:
        return _CB_DAILY_LOSS_PCT_FALLBACK, _CB_MAX_TRADES_FALLBACK


def health_snapshot_path(override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    try:
        from fabio_live.constants import HEALTH_SNAPSHOT_PATH

        return Path(HEALTH_SNAPSHOT_PATH)
    except Exception:
        return Path(__file__).resolve().parent.parent / "bot_health_snapshots.jsonl"


def read_latest_health_snapshot(path: str | Path | None = None) -> dict[str, Any] | None:
    """Last JSONL object, or None. File I/O only — no broker calls."""
    pth = health_snapshot_path(path)
    try:
        text = pth.read_text(encoding="utf-8")
    except OSError:
        return None
    last: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            last = obj
    return last


def book_id_from_open_row(row: Mapping[str, Any] | None) -> str | None:
    """Map an open-position row to one of the four paper books.

    ``broker code=`` is Moomoo EOD snapshot → ``orb-moomoo``. Never Tradier.
    Historical ``moomoo_paper_fifo`` stays ORB-Moomoo (do not retcon onto MR/Tradier).
    """
    if not isinstance(row, Mapping):
        return None
    notes = str(row.get("notes") or "").strip()
    notes_l = notes.lower()
    source = str(row.get("source") or "").strip().lower()
    explicit = str(row.get("book") or "").strip().lower()
    if explicit in _BOOK_IDENTITY:
        # Defense: never honor a Tradier book tag on a Moomoo broker-code row.
        if notes.startswith(MOOMOO_BROKER_CODE_PREFIX) and _BOOK_IDENTITY[explicit][
            "broker"
        ] == "tradier":
            return BOOK_ORB_MOOMOO
        return explicit
    if notes_l.startswith("source="):
        token = notes_l.split("|", 1)[0][len("source=") :].strip()
        mapped = _SOURCE_TO_BOOK.get(token)
        if mapped:
            return mapped
    fifo = _FIFO_TO_BOOK.get(notes) or _FIFO_TO_BOOK.get(notes_l)
    if fifo:
        return fifo
    if source in _SOURCE_TO_BOOK:
        return _SOURCE_TO_BOOK[source]
    if notes.startswith(MOOMOO_BROKER_CODE_PREFIX):
        return BOOK_ORB_MOOMOO
    return None


def is_allowed_open_position_notes(notes: str) -> bool:
    """Open-row notes: ``broker code=``, FIFO tags, or ``source=<book source>``."""
    n = str(notes or "").strip()
    if n.startswith(MOOMOO_BROKER_CODE_PREFIX):
        return True
    if n in _FIFO_TO_BOOK or n.lower() in _FIFO_TO_BOOK:
        return True
    lower = n.lower()
    if lower.startswith("source="):
        token = lower.split("|", 1)[0][len("source=") :].strip()
        return token in _SOURCE_TO_BOOK
    return False


def is_moomoo_broker_code_row(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    return str(row.get("notes") or "").strip().startswith(MOOMOO_BROKER_CODE_PREFIX)


def annotate_open_positions_with_book(
    opens: Iterable[Any] | None,
) -> list[dict[str, Any]]:
    """Copy of open rows with ``book`` derived from notes/source. No broker I/O."""
    out: list[dict[str, Any]] = []
    for item in opens or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        bid = book_id_from_open_row(row)
        if bid:
            row["book"] = bid
        out.append(row)
    return out


def filter_open_positions_by_book(
    opens: Iterable[Any] | None,
    book_id: str | None,
) -> list[dict[str, Any]]:
    """Filter opens for one book. Tradier filters never include Moomoo broker codes."""
    wanted = str(book_id or "").strip().lower()
    rows = [r for r in (opens or []) if isinstance(r, dict)]
    if not wanted:
        return rows
    identity = _BOOK_IDENTITY.get(wanted)
    broker = identity["broker"] if identity else ""
    kept: list[dict[str, Any]] = []
    for row in rows:
        bid = book_id_from_open_row(row)
        if bid != wanted:
            continue
        if broker == "tradier" and is_moomoo_broker_code_row(row):
            continue
        kept.append(row)
    return kept


def binding_status_label(*, enabled: bool, bound: bool) -> str:
    if enabled and bound:
        return "enabled · bound"
    if enabled and not bound:
        return "enabled · not bound this boot"
    if (not enabled) and bound:
        return "not enabled · bound this boot"
    return _UNBOUND_STATUS


def cb_halted_from_snapshot(cb: Mapping[str, Any] | None, *, bound: bool) -> bool | None:
    """Halt from this book's CB fields only. Unbound → None (not 'healthy')."""
    if not bound:
        return None
    if not isinstance(cb, Mapping):
        return False
    loss_limit, max_trades = _cb_limits()
    try:
        daily_loss = float(cb.get("daily_loss_pct") or 0.0)
    except (TypeError, ValueError):
        daily_loss = 0.0
    try:
        trade_count = int(cb.get("trade_count") or 0)
    except (TypeError, ValueError):
        trade_count = 0
    if daily_loss <= -loss_limit:
        return True
    if trade_count >= max_trades:
        return True
    return False


def _empty_cb() -> dict[str, Any]:
    return {
        "portfolio_at_open": float(STARTING_BALANCE),
        "realized_pnl": 0.0,
        "daily_loss_pct": 0.0,
        "trade_count": 0,
        "loss_streak": 0,
    }


def canonical_dashboard_books(
    *,
    enabled_ids: Iterable[str] | None = None,
    health_ts: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Four keys always present. Unbound rows are labeled, not '$0 / 0 trades / healthy'."""
    enabled: set[str] = set()
    if enabled_ids is None:
        try:
            from fabio_live.paper_books import enabled_paper_book_ids

            enabled = set(enabled_paper_book_ids(strict=False))
        except Exception:
            enabled = {BOOK_ORB_MOOMOO}
    else:
        enabled = {str(x).strip().lower() for x in enabled_ids if str(x).strip()}
    books: dict[str, dict[str, Any]] = {}
    for book_id in FOUR_BOOK_IDS:
        ident = _BOOK_IDENTITY[book_id]
        is_enabled = book_id in enabled
        books[book_id] = _decorate_book_row(
            book_id,
            {
                "enabled": is_enabled,
                "bound": False,
                "broker": ident["broker"],
                "strategy": ident["strategy"],
                "source": ident["source"],
                "starting_balance": float(STARTING_BALANCE),
                "cb": _empty_cb(),
                "ledger": {"path": "", "code_count": 0, "codes_preview": []},
                "modeled_equity": float(STARTING_BALANCE),
                "last_flatten": None,
            },
            health_ts=health_ts,
        )
    return books


def _decorate_book_row(
    book_id: str,
    row: Mapping[str, Any],
    *,
    health_ts: str | None = None,
) -> dict[str, Any]:
    ident = _BOOK_IDENTITY[book_id]
    enabled = bool(row.get("enabled"))
    bound = bool(row.get("bound"))
    cb = row.get("cb") if isinstance(row.get("cb"), dict) else _empty_cb()
    ledger = row.get("ledger") if isinstance(row.get("ledger"), dict) else {}
    try:
        code_count = int(ledger.get("code_count") or 0)
    except (TypeError, ValueError):
        code_count = 0
    modeled = row.get("modeled_equity")
    try:
        modeled_f = float(modeled) if modeled is not None else float(STARTING_BALANCE)
    except (TypeError, ValueError):
        modeled_f = float(STARTING_BALANCE)
    out = {
        "book_id": book_id,
        "enabled": enabled,
        "bound": bound,
        "broker": str(row.get("broker") or ident["broker"]),
        "strategy": str(row.get("strategy") or ident["strategy"]),
        "source": str(row.get("source") or ident["source"]),
        "starting_balance": float(row.get("starting_balance") or STARTING_BALANCE),
        "cb": {
            "portfolio_at_open": cb.get("portfolio_at_open", STARTING_BALANCE),
            "realized_pnl": cb.get("realized_pnl", 0.0),
            "daily_loss_pct": cb.get("daily_loss_pct", 0.0),
            "trade_count": cb.get("trade_count", 0),
            "loss_streak": cb.get("loss_streak", 0),
        },
        "ledger": {
            "path": str(ledger.get("path") or ""),
            "code_count": code_count,
            "codes_preview": list(ledger.get("codes_preview") or []),
        },
        "modeled_equity": modeled_f,
        "last_flatten": row.get("last_flatten"),
        "health_ts": health_ts or row.get("health_ts"),
        "binding_status": binding_status_label(enabled=enabled, bound=bound),
        "live_metrics": bound,
        "cb_halted": cb_halted_from_snapshot(cb, bound=bound),
    }
    # Unbound/disabled: do not present CB zeros as a healthy $0 book.
    if not bound:
        out["modeled_equity_display"] = None
    else:
        out["modeled_equity_display"] = modeled_f
    return out


def enrich_books_for_dashboard(
    books: Mapping[str, Any] | None,
    *,
    health_ts: str | None = None,
    enabled_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Always four keys. Overlay health ``books``; fill missing ids as unbound."""
    base = canonical_dashboard_books(enabled_ids=enabled_ids, health_ts=health_ts)
    if not isinstance(books, Mapping):
        return base
    for book_id in FOUR_BOOK_IDS:
        raw = books.get(book_id)
        if not isinstance(raw, Mapping):
            continue
        merged = dict(base[book_id])
        merged.update(raw)
        base[book_id] = _decorate_book_row(book_id, merged, health_ts=health_ts)
    return base


def last_flatten_sidecar_file(
    book_id: str, directory: str | Path | None = None
) -> Path | None:
    """``<ledger_dir>/<book_id>.last_flatten.json``. Unknown ids → None."""
    token = str(book_id or "").strip().lower()
    if token not in _BOOK_IDENTITY:
        return None
    if directory is not None:
        root = Path(directory)
    else:
        try:
            from fabio_live.paper_books import ledger_directory

            root = Path(ledger_directory())
        except Exception:
            root = Path(__file__).resolve().parent.parent / "backend" / "paper_book_ledgers"
    return root / f"{token}.last_flatten.json"


def read_last_flatten_sidecar(
    book_id: str, directory: str | Path | None = None
) -> dict[str, Any] | None:
    """Read a last-flatten sidecar. File I/O only — never another book's file."""
    token = str(book_id or "").strip().lower()
    if token not in _BOOK_IDENTITY:
        return None
    try:
        from fabio_live.paper_books import read_last_flatten_summary

        return read_last_flatten_summary(token, directory=directory)
    except Exception:
        pass
    path = last_flatten_sidecar_file(token, directory)
    if path is None or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("book_id") or "").strip().lower()
    if sid and sid != token:
        return None
    return raw


def overlay_last_flatten_from_sidecars(
    books: dict[str, dict[str, Any]],
    ledger_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Fill missing last_flatten from disk sidecars. Never another book's file."""
    for book_id in FOUR_BOOK_IDS:
        row = books.get(book_id)
        if not isinstance(row, dict):
            continue
        if row.get("last_flatten"):
            continue
        found = read_last_flatten_sidecar(book_id, directory=ledger_dir)
        if found:
            row = dict(row)
            row["last_flatten"] = found
            books[book_id] = row
    return books


def dashboard_books_payload(
    snapshot: Mapping[str, Any] | None = None,
    *,
    ledger_dir: str | Path | None = None,
    overlay_last_flatten: bool = True,
) -> dict[str, dict[str, Any]]:
    """Assemble the DATA.books map from a health snapshot (or canonical fallback)."""
    snap = snapshot if isinstance(snapshot, Mapping) else None
    health_ts = None
    raw_books = None
    enabled_ids = None
    if snap is not None:
        health_ts = snap.get("ts")
        raw_books = snap.get("books") if isinstance(snap.get("books"), Mapping) else None
        if isinstance(raw_books, Mapping):
            enabled_ids = [
                bid for bid in FOUR_BOOK_IDS if bool((raw_books.get(bid) or {}).get("enabled"))
            ]
    books = enrich_books_for_dashboard(
        raw_books, health_ts=str(health_ts) if health_ts else None, enabled_ids=enabled_ids
    )
    if overlay_last_flatten:
        books = overlay_last_flatten_from_sidecars(books, ledger_dir=ledger_dir)
    return books


def live_status_books_payload(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Subset of live-status JSON the Overview cards poll."""
    snap = snapshot if isinstance(snapshot, Mapping) else {}
    ts = snap.get("ts")
    return {
        "books": dashboard_books_payload(snap, overlay_last_flatten=False),
        "health_ts": ts,
    }


def unbound_status_is_not_healthy_zero(row: Mapping[str, Any]) -> bool:
    """Acceptance helper: unbound cards must not read as $0 / 0 trades / healthy."""
    if row.get("bound"):
        return True
    status = str(row.get("binding_status") or "")
    if _UNBOUND_STATUS not in status and "not bound this boot" not in status:
        return False
    if row.get("live_metrics") is True:
        return False
    if row.get("cb_halted") is not None:
        return False
    blob = json.dumps(
        {
            "binding_status": status,
            "live_metrics": row.get("live_metrics"),
            "modeled_equity_display": row.get("modeled_equity_display"),
        }
    ).lower()
    return not all(tok in blob for tok in _HEALTHY_ZERO_TOKENS)
