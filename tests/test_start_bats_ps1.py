"""scripts/start_bats.ps1: it parses, and the task and start settings the runbook relies on are there."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start_bats.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"), reason="Windows PowerShell only")
def test_the_script_parses_with_powershells_own_parser():
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{SCRIPT}',[ref]$null,[ref]$e); $e.Count")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0", out.stdout + out.stderr


@pytest.mark.unit
@pytest.mark.parametrize("setting", [
    "-RestartCount 5", "-RestartInterval (New-TimeSpan -Minutes 5)", "-ExecutionTimeLimit ([TimeSpan]::Zero)",
    "-StartWhenAvailable", "-WakeToRun", "-MultipleInstances IgnoreNew", "-LogonType Interactive",
    "-RunLevel Limited", "-AtLogOn", '-At "08:30"', "Monday, Tuesday, Wednesday, Thursday, Friday",
])
def test_the_task_has_each_setting(setting):
    assert setting in TEXT


@pytest.mark.unit
def test_a_start_exits_quietly_when_the_port_is_taken_and_runs_the_setup_check_first():
    port_check = TEXT.index("Get-NetTCPConnection -LocalPort $Port -State Listen")
    assert "exit 0" in TEXT[port_check:port_check + 400]
    assert TEXT.index("extensions.scripts.check_setup") < TEXT.index('"-m", "ui.server"')
    assert "exit $server.ExitCode" in TEXT  # a crash is a failure Task Scheduler restarts


@pytest.mark.unit
def test_the_script_never_sends_or_stores_a_password():
    for word in ("paper_orders.sender", "--send", "-Password", "ConvertTo-SecureString", "flatten"):
        assert word not in TEXT, word
