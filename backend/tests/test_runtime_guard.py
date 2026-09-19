from __future__ import annotations

from orb_bot_fabio import run_bot_with_guard


def test_runtime_guard_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("FABIO_IGNORE_NYSE_CALENDAR", "1")

    class FlakyBot:
        attempts = 0

        def __init__(self):
            pass

        def run(self):
            FlakyBot.attempts += 1
            if FlakyBot.attempts < 3:
                raise RuntimeError("boom")

    sleeps: list[float] = []
    code = run_bot_with_guard(
        bot_factory=FlakyBot,
        sleep_fn=lambda s: sleeps.append(float(s)),
    )
    assert code == 0
    assert FlakyBot.attempts == 3
    assert sleeps == [15.0, 30.0]


def test_runtime_guard_hard_fails_after_budget(monkeypatch):
    monkeypatch.setenv("FABIO_IGNORE_NYSE_CALENDAR", "1")
    monkeypatch.setenv("FABIO_RUNTIME_MAX_RESTARTS", "2")

    class AlwaysFail:
        def __init__(self):
            pass

        def run(self):
            raise RuntimeError("still failing")

    sleeps: list[float] = []
    code = run_bot_with_guard(
        bot_factory=AlwaysFail,
        sleep_fn=lambda s: sleeps.append(float(s)),
    )
    assert code == 1
    assert sleeps == [15.0]
