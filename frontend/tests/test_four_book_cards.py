"""SHIP-010: four paper-book cards on the local dashboard shell."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from dashboard_writer import (
    DashboardWriter,
    live_status_from_health,
    load_live_dashboard_template,
    render_live_dashboard_html,
)
from paper_book_dashboard import (
    BOOK_MR_MOOMOO,
    BOOK_MR_TRADIER,
    BOOK_ORB_MOOMOO,
    BOOK_ORB_TRADIER,
    FOUR_BOOK_IDS,
    PAPER_DESK_HEADER,
    annotate_open_positions_with_book,
    binding_status_label,
    book_id_from_open_row,
    dashboard_books_payload,
    filter_open_positions_by_book,
    read_latest_health_snapshot,
    unbound_status_is_not_healthy_zero,
)

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "four_book_open_positions.json"
)
_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "live_dashboard_template.html"
_TRACKED_LIVE = Path(__file__).resolve().parents[1] / "live_dashboard.html"
_WRITER_SRC = Path(__file__).resolve().parents[1] / "dashboard_writer.py"
_PAPER_SRC = Path(__file__).resolve().parents[1] / "paper_book_dashboard.py"


def _minimal_daily(date: str) -> dict:
    return {
        "date": date,
        "total_trades": 0,
        "winners": 0,
        "losers": 0,
        "win_rate": 0.0,
        "net_pnl": 0.0,
        "gross_win": 0.0,
        "gross_loss": 0.0,
        "capital": 0.0,
        "daily_return": 0.0,
        "proven_edge": 0.0,
    }


def _orb_only_health_snapshot() -> dict:
    def _row(book_id: str, *, enabled: bool, bound: bool, realized: float = 0.0, trades: int = 0):
        ident = {
            BOOK_ORB_MOOMOO: ("moomoo", "orb", "moomoo_paper"),
            BOOK_ORB_TRADIER: ("tradier", "orb", "tradier_paper"),
            BOOK_MR_MOOMOO: ("moomoo", "mr", "mr"),
            BOOK_MR_TRADIER: ("tradier", "mr", "mr_tradier"),
        }[book_id]
        return {
            "enabled": enabled,
            "bound": bound,
            "broker": ident[0],
            "strategy": ident[1],
            "source": ident[2],
            "starting_balance": 10_000.0,
            "cb": {
                "portfolio_at_open": 10_000.0,
                "realized_pnl": realized,
                "daily_loss_pct": realized / 10_000.0,
                "trade_count": trades,
                "loss_streak": 1 if realized < 0 else 0,
            },
            "ledger": {
                "path": f"/tmp/{book_id}.json",
                "code_count": 2 if bound else 0,
                "codes_preview": ["US.SPY"] if bound else [],
            },
            "modeled_equity": 10_000.0 + realized,
            "last_flatten": None,
        }

    return {
        "ts": "2026-09-21T14:15:00-04:00",
        "books": {
            BOOK_ORB_MOOMOO: _row(
                BOOK_ORB_MOOMOO, enabled=True, bound=True, realized=-125.0, trades=1
            ),
            BOOK_ORB_TRADIER: _row(BOOK_ORB_TRADIER, enabled=False, bound=False),
            BOOK_MR_MOOMOO: _row(BOOK_MR_MOOMOO, enabled=False, bound=False),
            BOOK_MR_TRADIER: _row(BOOK_MR_TRADIER, enabled=False, bound=False),
        },
    }


def test_four_source_tags_map_and_filter_independently():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    opens = annotate_open_positions_with_book(fixture["open_positions"])
    by_book = {row["symbol"]: row["book"] for row in opens}
    assert by_book["SPY"] == BOOK_ORB_MOOMOO
    assert by_book["QQQ"] == BOOK_ORB_TRADIER
    assert by_book["NOK"] == BOOK_MR_MOOMOO
    assert by_book["IBM"] == BOOK_MR_TRADIER
    assert by_book["NVDA"] == BOOK_ORB_MOOMOO  # broker code= is Moomoo, never Tradier

    spy = filter_open_positions_by_book(opens, BOOK_ORB_MOOMOO)
    assert {r["symbol"] for r in spy} == {"SPY", "NVDA"}
    qqq = filter_open_positions_by_book(opens, BOOK_ORB_TRADIER)
    assert [r["symbol"] for r in qqq] == ["QQQ"]
    assert all(not str(r["notes"]).startswith("broker code=") for r in qqq)
    nok = filter_open_positions_by_book(opens, BOOK_MR_MOOMOO)
    assert [r["symbol"] for r in nok] == ["NOK"]
    ibm = filter_open_positions_by_book(opens, BOOK_MR_TRADIER)
    assert [r["symbol"] for r in ibm] == ["IBM"]
    assert all(not str(r["notes"]).startswith("broker code=") for r in ibm)


def test_moomoo_broker_code_never_lands_on_tradier_filter():
    poisoned = {
        "symbol": "SPY",
        "contracts": 1,
        "notes": "broker code=US.SPY260921C00500000",
        "book": BOOK_ORB_TRADIER,
        "source": "tradier_paper",
    }
    assert book_id_from_open_row(poisoned) == BOOK_ORB_MOOMOO
    tradier = filter_open_positions_by_book([poisoned], BOOK_ORB_TRADIER)
    assert tradier == []
    moomoo = filter_open_positions_by_book([poisoned], BOOK_ORB_MOOMOO)
    assert len(moomoo) == 1


def test_orb_only_bound_cards_are_not_healthy_zeros():
    books = dashboard_books_payload(_orb_only_health_snapshot(), overlay_last_flatten=False)
    assert list(books) == list(FOUR_BOOK_IDS)
    orb = books[BOOK_ORB_MOOMOO]
    assert orb["enabled"] is True
    assert orb["bound"] is True
    assert orb["live_metrics"] is True
    assert orb["modeled_equity_display"] == 9875.0
    assert orb["cb"]["trade_count"] == 1
    assert "bound" in orb["binding_status"]

    for bid in (BOOK_ORB_TRADIER, BOOK_MR_MOOMOO, BOOK_MR_TRADIER):
        row = books[bid]
        assert row["enabled"] is False
        assert row["bound"] is False
        assert row["binding_status"] == "not enabled / not bound this boot"
        assert row["live_metrics"] is False
        assert row["modeled_equity_display"] is None
        assert row["cb_halted"] is None
        assert unbound_status_is_not_healthy_zero(row)
        blob = json.dumps(
            {
                "binding_status": row["binding_status"],
                "live_metrics": row["live_metrics"],
                "modeled_equity_display": row["modeled_equity_display"],
            }
        ).lower()
        assert "healthy" not in blob
        assert "$0" not in blob


def test_binding_status_label_covers_enabled_unbound():
    assert binding_status_label(enabled=True, bound=False) == "enabled · not bound this boot"
    assert binding_status_label(enabled=False, bound=False) == (
        "not enabled / not bound this boot"
    )


def test_last_flatten_sidecar_overlays_without_broker_query(tmp_path):
    sidecar = tmp_path / f"{BOOK_ORB_TRADIER}.last_flatten.json"
    sidecar.write_text(
        json.dumps(
            {
                "book_id": BOOK_ORB_TRADIER,
                "broker": "tradier",
                "script": "tradier_eod_flatten.py",
                "started_at_utc": "2026-09-21T19:50:00Z",
                "finished_at_utc": "2026-09-21T19:50:02Z",
                "dry_run": True,
                "exit_code": 0,
                "selected_codes_count": 0,
                "reason": "no_closable",
                "layer": "failsafe",
            }
        ),
        encoding="utf-8",
    )
    books = dashboard_books_payload(
        _orb_only_health_snapshot(),
        ledger_dir=tmp_path,
        overlay_last_flatten=True,
    )
    found = books[BOOK_ORB_TRADIER]["last_flatten"]
    assert found is not None
    assert found["book_id"] == BOOK_ORB_TRADIER
    assert found["reason"] == "no_closable"
    assert books[BOOK_ORB_MOOMOO]["last_flatten"] is None
    assert books[BOOK_MR_MOOMOO]["last_flatten"] is None
    assert books[BOOK_MR_TRADIER]["last_flatten"] is None


def test_read_latest_health_snapshot_last_line(tmp_path):
    path = tmp_path / "bot_health_snapshots.jsonl"
    path.write_text(
        json.dumps({"ts": "1", "books": {}})
        + "\n"
        + json.dumps(_orb_only_health_snapshot())
        + "\n",
        encoding="utf-8",
    )
    snap = read_latest_health_snapshot(path)
    assert snap["ts"] == "2026-09-21T14:15:00-04:00"
    assert snap["books"][BOOK_ORB_MOOMOO]["bound"] is True


def test_live_status_includes_four_book_cards():
    snap = _orb_only_health_snapshot()
    payload = live_status_from_health(snap, pid=42, running=True)
    assert list(payload["books"]) == list(FOUR_BOOK_IDS)
    assert payload["health_ts"] == snap["ts"]
    assert payload["books"][BOOK_ORB_TRADIER]["binding_status"] == (
        "not enabled / not bound this boot"
    )
    assert payload["health"]["books"][BOOK_ORB_MOOMOO]["bound"] is True


def test_template_shell_has_four_cards_ops_and_header():
    html = _TEMPLATE.read_text(encoding="utf-8")
    for bid in FOUR_BOOK_IDS:
        assert f'data-book-id="{bid}"' in html
    assert PAPER_DESK_HEADER in html
    assert "Initial Capital $10,000" not in html
    assert 'id="opsView"' in html
    assert 'id="botStatusPill"' in html
    assert 'id="openBookFilter"' in html
    assert "<th>Book</th>" in html
    assert "python3 -m http.server" in html
    assert "fabio serve" not in html
    assert "function filterOpenPositionsByBook" in html
    assert "function bookIdFromOpenRow" in html
    assert "moomoo_paper:" in html
    assert "tradier_paper:" in html
    assert "mr_tradier:" in html
    assert "FIFO_TO_BOOK" in html


def test_write_html_injects_books_and_does_not_touch_tracked_pages(tmp_path):
    tracked_before = _TRACKED_LIVE.read_bytes() if _TRACKED_LIVE.is_file() else b""
    data_file = tmp_path / "trade_data.json"
    live_html = tmp_path / "live_dashboard.html"
    main_html = tmp_path / "fabio_live_dashboard.html"
    health_path = tmp_path / "bot_health_snapshots.jsonl"
    health_path.write_text(json.dumps(_orb_only_health_snapshot()) + "\n", encoding="utf-8")
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    data_file.write_text(
        json.dumps(
            {
                "trades": [],
                "daily": [_minimal_daily("2026-09-21")],
                "open_positions": fixture["open_positions"],
            }
        ),
        encoding="utf-8",
    )
    with (
        patch("dashboard_writer.DATA_FILE", str(data_file)),
        patch("dashboard_writer.DASH_LOCAL", str(live_html)),
        patch("dashboard_writer.DASH_MAIN", str(main_html)),
        patch("dashboard_writer.HEALTH_JSONL_FILE", str(health_path)),
    ):
        w = DashboardWriter()
        w._write_html()

    html = live_html.read_text(encoding="utf-8")
    assert PAPER_DESK_HEADER in html
    assert 'id="opsView"' in html
    for bid in FOUR_BOOK_IDS:
        assert f'data-book-id="{bid}"' in html
    data_line = next(ln for ln in html.splitlines() if ln.startswith("const DATA = "))
    raw = data_line[len("const DATA = ") :]
    if raw.endswith(";"):
        raw = raw[:-1]
    payload = json.loads(raw)
    assert list(payload["books"]) == list(FOUR_BOOK_IDS)
    assert payload["desk_header"] == PAPER_DESK_HEADER
    assert payload["books"][BOOK_ORB_MOOMOO]["bound"] is True
    assert payload["books"][BOOK_ORB_MOOMOO]["modeled_equity_display"] == 9875.0
    for bid in (BOOK_ORB_TRADIER, BOOK_MR_MOOMOO, BOOK_MR_TRADIER):
        assert payload["books"][bid]["binding_status"] == "not enabled / not bound this boot"
        assert payload["books"][bid]["live_metrics"] is False
    by_sym = {r["symbol"]: r["book"] for r in payload["open_positions"]}
    assert by_sym["SPY"] == BOOK_ORB_MOOMOO
    assert by_sym["QQQ"] == BOOK_ORB_TRADIER
    assert by_sym["NOK"] == BOOK_MR_MOOMOO
    assert by_sym["IBM"] == BOOK_MR_TRADIER
    assert _TRACKED_LIVE.read_bytes() == tracked_before
    assert Path(live_html).resolve() != _TRACKED_LIVE.resolve()


def test_render_fixture_html_filters_four_source_tags():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    opens = annotate_open_positions_with_book(fixture["open_positions"])
    books = dashboard_books_payload(_orb_only_health_snapshot(), overlay_last_flatten=False)
    data = {
        "trades": [],
        "daily": [],
        "open_positions": opens,
        "books": books,
        "health_ts": "2026-09-21T14:15:00-04:00",
        "desk_header": PAPER_DESK_HEADER,
    }
    html = render_live_dashboard_html(load_live_dashboard_template(), json.dumps(data))
    assert PAPER_DESK_HEADER in html
    assert "source=moomoo_paper" in html
    assert "source=tradier_paper" in html
    assert "source=mr" in html
    assert "source=mr_tradier" in html
    for bid in FOUR_BOOK_IDS:
        assert f'data-book-id="{bid}"' in html
        assert f'<option value="{bid}">{bid}</option>' in html


def test_dashboard_helpers_do_not_query_brokers():
    paper = _PAPER_SRC.read_text(encoding="utf-8")
    writer = _WRITER_SRC.read_text(encoding="utf-8")
    assert "get_portfolio_value(" not in paper
    assert "get_portfolio_value(" not in writer
    assert "position_list_query(" not in paper
    assert "list_positions(" not in paper
    assert "Never query the other broker" in paper
