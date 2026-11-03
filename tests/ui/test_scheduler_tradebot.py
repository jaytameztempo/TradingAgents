"""The scheduler and the Tradebot: intraday checks per mode, auto chains, catch-up, reconcile and the heartbeat."""

import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from paper_orders.trade_window import SettingsError  # noqa: E402
from ui.server import (  # noqa: E402
    jobs,
    paths,
    scheduler as scheduler_mod,  # noqa: E402
    settings,
)

WINDOW = {"days": ["mon", "tue", "wed", "thu", "fri"], "sessions": [{"start": "09:35", "end": "15:55"}],
          "auto_send": {"enabled": False, "times": ["09:40"]}}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return tmp_path


def _save(**sections):
    for name, value in sections.items():
        settings.save_section(name, value)


def _sched(when):
    started = []

    def start(kind, steps, group=None):
        started.append((kind, [(s.module, s.args, s.always) for s in steps], group))
        return jobs.Job(id=f"j{len(started)}", kind=kind, group=group, steps=steps)

    clock = type("C", (), {"when": when, "__call__": lambda self: self.when})()
    return scheduler_mod.Scheduler(clock=clock, start=start), started, clock


def ny(h, m, day=13):  # 2026-10-13 is a Tuesday; New York is UTC-4
    return datetime(2026, 10, day, h + 4, m, tzinfo=UTC)


@pytest.mark.unit
def test_manual_mode_schedules_nothing_for_the_tradebot():
    _save(trading=WINDOW, tradebot={"mode": "manual"})
    sched, started, _ = _sched(ny(10, 36))
    sched.tick()
    assert started == []


@pytest.mark.unit
def test_semi_runs_the_intraday_check_only():
    _save(trading=WINDOW, tradebot={"mode": "semi"})
    sched, started, _ = _sched(ny(10, 36))
    sched.tick()
    assert started == [("scheduled:tradebot-intraday",
                        [("extensions.scripts.run_tradebot", ["--stage", "intraday"], False)], "pipeline")]


@pytest.mark.unit
def test_auto_intraday_tickets_sends_and_reconciles_with_the_last_two_always():
    _save(trading=WINDOW, tradebot={"mode": "auto"})
    sched, started, _ = _sched(ny(10, 36))
    sched.tick()
    [(_, steps, group)] = started
    assert group == "sender" and steps == [
        ("extensions.scripts.run_tradebot", ["--stage", "intraday", "--ticket"], False),
        ("paper_orders.sender", ["--tradebot", "--send"], True),
        ("paper_orders.reconcile", [], True)]


@pytest.mark.unit
def test_a_slot_fires_once_and_not_outside_the_window():
    _save(trading=WINDOW, tradebot={"mode": "semi", "intraday_times": ["10:35", "16:30"]})
    sched, started, clock = _sched(ny(10, 36))
    sched.tick()
    sched.tick()
    clock.when = ny(16, 31)  # after the session: the window is closed
    sched.tick()
    assert len(started) == 1


@pytest.mark.unit
def test_catch_up_runs_only_the_latest_missed_slot_and_records_the_rest():
    _save(trading=WINDOW, tradebot={"mode": "semi"})
    sched, started, _ = _sched(ny(13, 50))  # the app started at 13:50: 10:35 .. 13:35 were missed
    sched.tick()
    assert len(started) == 1
    events = sched.status()["events"]
    assert events[0]["slot"] == "13:35" and events[0]["outcome"] == "started"
    assert [e["slot"] for e in events if e["outcome"] == "skipped"] == ["12:35", "11:35", "10:35"]


@pytest.mark.unit
def test_no_catch_up_on_another_day_or_a_holiday():
    _save(trading=WINDOW, tradebot={"mode": "semi"})
    sched, started, _ = _sched(datetime(2026, 11, 26, 18, 0, tzinfo=UTC))  # Thanksgiving 13:00 New York
    sched.tick()
    assert started == []


@pytest.mark.unit
def test_auto_send_catches_up_inside_the_window():
    _save(trading={**WINDOW, "auto_send": {"enabled": True, "times": ["09:40"]}},
          tradebot={"mode": "auto", "intraday_times": []})
    sched, started, _ = _sched(ny(11, 0))  # 09:40 was missed
    sched.tick()
    assert [k for k, *_ in started] == ["scheduled:tradebot-send"]


@pytest.mark.unit
def test_reconcile_runs_at_16_05_on_a_trading_day():
    _save(trading=WINDOW, tradebot={"mode": "semi", "intraday_times": []})
    sched, started, _ = _sched(ny(16, 6))
    sched.tick()
    assert started == [("scheduled:reconcile", [("paper_orders.reconcile", [], False)], "reconcile")]


@pytest.mark.unit
def test_semi_without_a_window_says_why():
    _save(tradebot={"mode": "semi"})
    sched, started, _ = _sched(ny(10, 36))
    sched.tick()
    assert started == [] and "no trading window is saved" in sched.status()["events"][0]["detail"]


@pytest.mark.unit
def test_bad_tradebot_settings_are_reported_and_nothing_runs(home):
    (home / "settings.json").write_text(json.dumps({"trading": WINDOW, "tradebot": {"mode": "yolo"}}),
                                        encoding="utf-8")
    sched, started, _ = _sched(ny(10, 36))
    sched.tick()
    assert started == [] and sched.status()["events"][0]["outcome"] == "error"


@pytest.mark.unit
def test_the_status_shows_the_heartbeat_mode_and_next_intraday():
    _save(trading={**WINDOW, "auto_send": {"enabled": True, "times": ["09:40"]}}, tradebot={"mode": "auto"})
    sched, _, _ = _sched(ny(10, 0))
    assert sched.status()["last_tick"] is None
    sched.tick()
    status = sched.status()
    assert status["last_tick"].startswith("2026-10-13T14:00") and status["tradebot_mode"] == "auto"
    assert status["auto_send_sends"] is True and status["next_intraday"].startswith("2026-10-13T10:35")


@pytest.mark.unit
def test_auto_is_locked_without_a_trading_window(home):
    with pytest.raises(SettingsError, match="no trading window is saved"):
        settings.validate_tradebot({"mode": "auto"})
    _save(trading=WINDOW)
    assert settings.validate_tradebot({"mode": "auto"})["mode"] == "auto"


@pytest.mark.unit
def test_auto_is_locked_if_a_safety_module_is_missing(home, monkeypatch):
    _save(trading=WINDOW)
    monkeypatch.setattr(settings, "AUTO_NEEDS", ("paper_orders.guard", "paper_orders.not_a_module"))
    with pytest.raises(SettingsError, match="not_a_module is not installed"):
        settings.validate_tradebot({"mode": "auto"})


@pytest.mark.unit
def test_the_tradebot_settings_route_saves_and_refuses(home):
    from fastapi.testclient import TestClient

    from ui.server.app import app

    client = TestClient(app)
    assert client.put("/api/settings/tradebot", json={"mode": "auto"}).status_code == 400
    ok = client.put("/api/settings/tradebot", json={"mode": "semi", "filters": {"rsi_extreme": False}}).json()
    assert ok["mode"] == "semi" and ok["filters"]["rsi_extreme"] is False and ok["filters"]["light_volume"] is True
    body = client.get("/api/settings").json()
    assert body["tradebot"]["mode"] == "semi" and "no trading window is saved" in body["auto_blockers"]


@pytest.mark.unit
def test_a_job_step_marked_always_runs_after_a_failure(monkeypatch):
    import sys

    fail = jobs.Step("run_tradebot", ["--x"])
    always = jobs.Step(jobs.RECONCILE, [], always=True)
    skipped = jobs.Step("run_enqueue", [])
    monkeypatch.setattr(jobs.Step, "command", property(lambda s: [sys.executable, "-c",
                                                                  "import sys; sys.exit(1 if '--x' in sys.argv else 0)",
                                                                  *s.args]))
    job = jobs.wait_for(jobs.JobRunner().start("t", [fail, skipped, always]))
    assert [s.exit_code for s in job.steps] == [1, None, 0] and job.exit_code == 1 and job.status == "failed"
