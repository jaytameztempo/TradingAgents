"""Settings page API and the in-app scheduler."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ui.server import jobs, paths, scheduler as scheduler_mod, settings  # noqa: E402
from ui.server.app import app  # noqa: E402

UI_ROOT = Path(__file__).resolve().parents[2] / "ui"
TOUCHED = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
           "TRADINGAGENTS_LLM_PROVIDER", "TRADINGAGENTS_MAX_DEBATE_ROUNDS", "TRADINGAGENTS_DEEP_THINK_LLM"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    env = tmp_path / ".env"
    env.write_text("# my comment\nGOOGLE_API_KEY=old-google\nSEC_EDGAR_USER_AGENT=me\n", encoding="utf-8")
    monkeypatch.setenv("BATS_ENV_PATH", str(env))
    for name in TOUCHED:  # the routes copy saved values into os.environ; monkeypatch restores them
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(app)


# --- automation and trading window ----------------------------------------------------

@pytest.mark.unit
def test_trading_window_round_trips_through_the_file_the_sender_reads(home, client):
    body = {"days": ["mon", "wed"], "sessions": [{"start": "09:35", "end": "11:00"}, {"start": "14:00", "end": "15:55"}],
            "auto_send": {"enabled": True, "times": ["14:05", "09:40"]}}
    assert client.put("/api/settings/trading", json=body).status_code == 200
    saved = json.loads((home / "settings.json").read_text())["trading"]
    assert saved["auto_send"]["times"] == ["09:40", "14:05"]
    from paper_orders.trade_window import load_window

    window = load_window()
    assert window.allows(datetime(2026, 10, 12, 14, 0, tzinfo=UTC))  # Mon 10:00 NY
    assert not window.allows(datetime(2026, 10, 12, 16, 0, tzinfo=UTC))  # Mon 12:00 NY, between sessions
    assert not window.allows(datetime(2026, 10, 13, 14, 0, tzinfo=UTC))  # Tuesday


@pytest.mark.unit
@pytest.mark.parametrize(("body", "message"), [
    ({"days": ["mon"], "sessions": [{"start": "09:35", "end": "15:55"}], "auto_send": {"enabled": True, "times": ["16:30"]}},
     "not inside a session"),
    ({"days": ["mon"], "sessions": [{"start": "10:00", "end": "09:00"}]}, "does not end after"),
    ({"days": ["mon"], "sessions": [{"start": "09:30", "end": "12:00"}, {"start": "11:00", "end": "13:00"}]}, "overlap"),
    ({"days": ["someday"], "sessions": []}, "unknown day"),
    ({"days": ["mon"], "sessions": [{"start": "9:30", "end": "12:00"}]}, "HH:MM"),
])
def test_bad_trading_settings_are_refused(home, client, body, message):
    resp = client.put("/api/settings/trading", json=body)
    assert resp.status_code == 400 and message in resp.json()["detail"]
    assert not (home / "settings.json").exists()


@pytest.mark.unit
def test_automation_needs_days_and_times_to_switch_on(home, client):
    assert client.put("/api/settings/automation", json={"enabled": True, "days": [], "times": ["16:30"]}).status_code == 400
    ok = client.put("/api/settings/automation", json={"enabled": True, "days": ["fri"], "times": ["16:30"], "run": "scans"})
    assert ok.status_code == 200 and settings.automation()["run"] == "scans"


# --- .env: LLM settings and keys ------------------------------------------------------------

@pytest.mark.unit
def test_llm_settings_are_written_to_env_and_keep_other_lines(home, client):
    values = {"TRADINGAGENTS_LLM_PROVIDER": "openai", "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "2",
              "TRADINGAGENTS_DEEP_THINK_LLM": "gpt-5.5"}
    assert client.put("/api/settings/llm", json={"values": values}).status_code == 200
    text = (home / ".env").read_text()
    assert "# my comment" in text and "SEC_EDGAR_USER_AGENT=me" in text
    assert "TRADINGAGENTS_LLM_PROVIDER=openai" in text
    import os

    assert os.environ["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] == "2"  # the next research subprocess inherits it
    got = client.get("/api/settings").json()["llm"]
    assert got["TRADINGAGENTS_DEEP_THINK_LLM"]["value"] == "gpt-5.5"


@pytest.mark.unit
@pytest.mark.parametrize("values", [
    {"TRADINGAGENTS_LLM_PROVIDER": "skynet"},
    {"TRADINGAGENTS_MAX_DEBATE_ROUNDS": "99"},
    {"TRADINGAGENTS_DEEP_THINK_LLM": "gpt\nALPACA_API_KEY=AKLIVE"},
    {"ALPACA_API_KEY": "PKX"},
])
def test_bad_llm_values_are_refused_and_nothing_is_written(home, client, values):
    before = (home / ".env").read_text()
    assert client.put("/api/settings/llm", json={"values": values}).status_code == 400
    assert (home / ".env").read_text() == before


@pytest.mark.unit
def test_keys_are_write_only(home, client):
    resp = client.put("/api/settings/keys", json={"set": {"ALPACA_API_KEY": "PKNEWKEY123", "ALPACA_SECRET_KEY": "s3cr3t-value"},
                                                  "clear": ["GOOGLE_API_KEY"]})
    assert resp.status_code == 200
    text = (home / ".env").read_text()
    assert "PKNEWKEY123" in text and "s3cr3t-value" in text and "old-google" not in text
    page = client.get("/api/settings").text
    assert "PKNEWKEY123" not in page and "s3cr3t-value" not in page
    keys = {k["name"]: k["set"] for k in client.get("/api/settings").json()["keys"]}
    assert keys["ALPACA_API_KEY"] and keys["ALPACA_SECRET_KEY"] and not keys["GOOGLE_API_KEY"]


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    {"set": {"ALPACA_API_KEY": "AKLIVEKEY"}},
    {"set": {"SOME_OTHER_VAR": "x"}},
    {"set": {"OPENAI_API_KEY": "has space"}},
    {"clear": ["SEC_EDGAR_USER_AGENT"]},
])
def test_bad_key_updates_are_refused(home, client, body):
    before = (home / ".env").read_text()
    assert client.put("/api/settings/keys", json=body).status_code == 400
    assert (home / ".env").read_text() == before


# --- scheduler ---------------------------------------------------------------------------

class Clock:
    def __init__(self, when: datetime):
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def _scheduler(clock):
    started = []

    def start(kind, steps, group=None):
        started.append((kind, steps, group))
        return jobs.Job(id=f"j{len(started)}", kind=kind, group=group, steps=steps)

    return scheduler_mod.Scheduler(clock=clock, start=start), started


def _save(**sections):
    for name, value in sections.items():
        settings.save_section(name, value)


@pytest.mark.unit
def test_a_recurring_scan_fires_once_in_its_slot(home):
    _save(automation={"enabled": True, "days": ["fri"], "times": ["16:30"], "run": "daily"})
    clock = Clock(datetime(2026, 10, 9, 20, 29, tzinfo=UTC))  # Fri 16:29 NY: not yet
    sched, started = _scheduler(clock)
    sched.tick()
    assert started == []
    clock.when = datetime(2026, 10, 9, 20, 31, tzinfo=UTC)  # 16:31: due
    sched.tick()
    sched.tick()
    assert len(started) == 1
    kind, steps, group = started[0]
    assert group == "pipeline" and steps[0].script == "run_daily"
    assert steps[0].args == ["--as-of", "2026-10-09"]  # after 16:15 on a weekday: today's bars


@pytest.mark.unit
def test_morning_scans_use_the_previous_trading_day():
    monday_morning = datetime(2026, 10, 12, 8, 0)
    assert scheduler_mod.scan_as_of(monday_morning) == date(2026, 10, 9)


@pytest.mark.unit
def test_auto_send_starts_the_sender_inside_the_window_only_when_on(home):
    tickets = home / "tickets"
    tickets.mkdir()
    (tickets / "paper_tickets_2026-10-09_20261010T000000Z.json").write_text("{}")
    window = {"days": ["mon"], "sessions": [{"start": "09:35", "end": "15:55"}]}
    clock = Clock(datetime(2026, 10, 12, 13, 41, tzinfo=UTC))  # Mon 09:41 NY

    _save(trading={**window, "auto_send": {"enabled": False, "times": ["09:40"]}})
    sched, started = _scheduler(clock)
    sched.tick()
    assert started == []  # off: nothing

    _save(trading={**window, "auto_send": {"enabled": True, "times": ["09:40"]}})
    sched, started = _scheduler(clock)
    sched.tick()
    assert len(started) == 1
    _, steps, group = started[0]
    assert group == "sender" and steps[0].script == jobs.SENDER
    assert steps[0].command[1:] == ["-m", "paper_orders.sender", "--as-of", "2026-10-09", "--send"]


@pytest.mark.unit
def test_auto_send_with_no_recent_tickets_sends_nothing(home):
    _save(trading={"days": ["mon"], "sessions": [{"start": "09:35", "end": "15:55"}],
                   "auto_send": {"enabled": True, "times": ["09:40"]}})
    sched, started = _scheduler(Clock(datetime(2026, 10, 12, 13, 41, tzinfo=UTC)))
    sched.tick()
    assert started == [] and sched.status()["events"][0]["outcome"] == "skipped"


# --- only the scheduler can start the sender ------------------------------------------------

@pytest.mark.unit
def test_no_route_can_start_the_sender():
    for path in (UI_ROOT / "server" / "routes").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"SENDER|paper_orders\.sender|paper_orders_sender|SCHEDULER_ONLY", text), path.name
    assert jobs.SENDER not in jobs.SCRIPTS


# --- weekend and holiday runs -----------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("year", "expected"), [
    (2025, ["2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
            "2025-09-01", "2025-11-27", "2025-12-25"]),
    (2026, ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
            "2026-09-07", "2026-11-26", "2026-12-25"]),
    (2027, ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
            "2027-09-06", "2027-11-25", "2027-12-24"]),
])
def test_market_holidays_match_the_nyse_calendar(year, expected):
    from ui.server import market_calendar

    assert sorted(d.isoformat() for d in market_calendar.holidays(year)) == expected


@pytest.mark.unit
def test_a_saturday_new_years_day_is_not_made_up_on_friday():
    from ui.server import market_calendar

    assert market_calendar.is_trading_day(date(2021, 12, 31))  # Jan 1 2022 was a Saturday


@pytest.mark.unit
@pytest.mark.parametrize(("local", "as_of"), [
    (datetime(2026, 10, 17, 10, 0), date(2026, 10, 16)),  # Saturday -> Friday
    (datetime(2026, 10, 18, 20, 0), date(2026, 10, 16)),  # Sunday evening -> Friday
    (datetime(2026, 4, 4, 9, 0), date(2026, 4, 2)),  # Saturday after Good Friday -> Thursday
    (datetime(2026, 4, 3, 17, 0), date(2026, 4, 2)),  # Good Friday after 16:15: no session that day
    (datetime(2026, 9, 8, 9, 0), date(2026, 9, 4)),  # Tuesday morning after Labor Day -> Friday
    (datetime(2026, 10, 16, 16, 30), date(2026, 10, 16)),  # Friday after the close -> Friday
])
def test_scans_use_the_last_open_session(local, as_of):
    assert scheduler_mod.scan_as_of(local) == as_of


@pytest.mark.unit
def test_a_weekend_scan_fires_and_scans_friday(home):
    _save(automation={"enabled": True, "days": ["sat", "sun"], "times": ["10:00"], "run": "scans"})
    sched, started = _scheduler(Clock(datetime(2026, 10, 17, 14, 1, tzinfo=UTC)))  # Sat 10:01 New York
    sched.tick()
    assert len(started) == 1
    _, steps, _ = started[0]
    assert all(step.args[:2] == ["--as-of", "2026-10-16"] for step in steps)
    assert [s.script for s in steps] == ["run_upward_trend_scan", "run_breakdown_scan",
                                         "run_scanbot_long_channel", "run_scanbot_short_channel"]
    status = sched.status()
    assert status["next_scan"].startswith("2026-10-18T10:00") and status["next_scan_as_of"] == "2026-10-16"
