"""The order ledger: line format, monthly files, the sender hook, concurrent appends, and the OneDrive refusal."""

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from paper_orders import guard, ledger
from paper_orders.sender import Fill, Outcome

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_UI_HOME", str(tmp_path))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.unit
def test_a_line_carries_the_order_and_its_tradebot_item():
    order = {"client_order_id": "tb-0123456789ab-1-VST-buy", "status": "filled", "qty": "36", "filled_qty": "36",
             "filled_avg_price": "138.40"}
    row = ledger.line("tradebot", "entry", "VST", action="sent", order=order, at=datetime(2026, 11, 3, 15, tzinfo=UTC))
    assert row["item_id"] == "0123456789ab" and row["client_order_id"] == order["client_order_id"]
    assert (row["status"], row["filled_qty"], row["filled_avg_price"]) == ("filled", "36", "138.40")
    assert set(ledger.FIELDS) <= set(row) and row["at"] == "2026-11-03T15:00:00+00:00"
    assert ledger.line("daily", "entry", "X", client_order_id="pt-2026-09-30-X-buy")["item_id"] is None


@pytest.mark.unit
def test_lines_go_to_the_month_of_their_time_and_read_back():
    nov = ledger.line("manual", "entry", "A", at=datetime(2026, 11, 3, tzinfo=UTC))
    dec = ledger.line("manual", "entry", "B", at=datetime(2026, 12, 1, tzinfo=UTC))
    ledger.append([nov])
    ledger.append([dec])
    assert [r["symbol"] for r in ledger.read("2026-11")] == ["A"]
    assert [r["symbol"] for r in ledger.read("2026-12")] == ["B"]
    assert ledger.read("2025-01") == []


@pytest.mark.unit
def test_the_sender_hook_logs_entries_exits_and_guard_skips(home, capsys):
    entries = [Outcome("VST", "tb-abc-1-VST-buy", "sent", order={"status": "new", "qty": "36"})]
    exits = [Outcome("VST", "tb-abc-1-VST-exit", "exit_sent", order={"status": "new"}, fill=Fill(36, 138.4),
                     stop=137.23)]
    d = guard.Decision(skipped={"VIR": "gross exposure would pass 60%"})
    ledger.record_send("tradebot", entries, exits, d)
    rows = ledger.read()
    assert [(r["kind"], r["symbol"], r["action"]) for r in rows] == [
        ("entry", "VST", "sent"), ("exit", "VST", "exit_sent"), ("guard", "VIR", "skipped")]
    assert rows[1]["stop"] == 137.23 and rows[1]["entry_fill_price"] == 138.4 and rows[1]["item_id"] == "abc"
    assert "ledger:" in capsys.readouterr().out


@pytest.mark.unit
def test_a_ledger_that_cannot_be_written_only_warns(home, monkeypatch, capsys):
    monkeypatch.setenv("OneDrive", str(home.parent))
    ledger.record_send("daily", [Outcome("X", "pt-x-X-buy", "sent")], [], None)
    assert "order ledger was not written" in capsys.readouterr().err
    with pytest.raises(ledger.LedgerError, match="OneDrive"):
        ledger.append([ledger.line("daily", "entry", "X")])


@pytest.mark.unit
def test_a_broken_line_is_an_error_not_skipped(home):
    path = ledger.ledger_path("2026-11")
    path.parent.mkdir(parents=True)
    path.write_text('{"at": "x"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="line 2"):
        ledger.read("2026-11")


@pytest.mark.unit
def test_processes_appending_at_once_lose_and_mix_no_line(home):
    code = textwrap.dedent("""
        import sys
        from paper_orders import ledger
        for n in range(40):
            ledger.append([ledger.line("tradebot", "entry", f"P{sys.argv[1]}", reason="x" * 2000 + str(n))])
    """)
    env = {**os.environ, "TRADINGAGENTS_UI_HOME": str(home)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(i)], cwd=REPO_ROOT, env=env,
                              stderr=subprocess.PIPE, text=True) for i in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs), [p.stderr.read() for p in procs]
    month = datetime.now(UTC).strftime("%Y-%m")
    rows = ledger.read(month)  # every line parses: none were interleaved
    assert len(rows) == 160 and {r["symbol"] for r in rows} == {"P0", "P1", "P2", "P3"}
    assert json.loads(ledger.ledger_path(month).read_text(encoding="utf-8").splitlines()[0])["mode"] == "tradebot"


@pytest.mark.unit
def test_the_sender_command_line_logs_every_send():
    import paper_orders.sender as sender

    tail = Path(sender.__file__).read_text(encoding="utf-8").rsplit('if __name__ == "__main__":', 1)[1]
    assert "main(on_outcomes=ledger.record_send)" in tail
