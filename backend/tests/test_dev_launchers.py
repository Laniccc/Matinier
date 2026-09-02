from __future__ import annotations

import shutil
import subprocess
import os
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POWERSHELL_SCRIPT = PROJECT_ROOT / "scripts" / "start_dev.ps1"
SHELL_SCRIPT = PROJECT_ROOT / "scripts" / "start_dev.sh"
DOCKER_COMPOSE = PROJECT_ROOT / "docker-compose.yml"
DEMO_LAUNCHER = PROJECT_ROOT / "start_demo.cmd"


@pytest.mark.parametrize("path", [POWERSHELL_SCRIPT, SHELL_SCRIPT])
def test_dev_launcher_contract_is_complete(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    required_fragments = (
        ".env",
        "alembic",
        "uvicorn",
        "app.worker.entrypoint",
        "pnpm",
        "dev",
        "logs_dir",
        "run_demo_replay.py",
        "api.stdout.log",
        "worker.stdout.log",
        "frontend.stdout.log",
    )
    for fragment in required_fragments:
        assert fragment in text


def test_powershell_check_only_is_non_mutating_and_prints_commands() -> None:
    if os.name != "nt":
        pytest.skip("The PowerShell launcher uses the Windows virtualenv layout")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_SCRIPT),
            "-CheckOnly",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert "Check-only passed" in output
    assert "FastAPI:" in output
    assert "Worker:" in output
    assert "Frontend:" in output
    assert "Replay:" in output
    assert "started PID" not in output


def test_frontend_arguments_do_not_forward_a_separator_to_next() -> None:
    powershell_text = POWERSHELL_SCRIPT.read_text(encoding="utf-8")
    shell_text = SHELL_SCRIPT.read_text(encoding="utf-8")

    assert '"dev", "--hostname"' in powershell_text
    assert "dev --hostname 127.0.0.1 --port 3000" in shell_text
    assert "dev -- --hostname" not in powershell_text
    assert "dev -- --hostname" not in shell_text


def test_dev_launchers_force_utf8_python_logs() -> None:
    powershell_text = POWERSHELL_SCRIPT.read_text(encoding="utf-8")
    shell_text = SHELL_SCRIPT.read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in powershell_text
    assert '$env:PYTHONIOENCODING = "utf-8"' in powershell_text
    assert "export PYTHONUTF8=1" in shell_text
    assert "export PYTHONIOENCODING=utf-8" in shell_text


def test_local_livekit_advertises_published_loopback_ports() -> None:
    compose_text = DOCKER_COMPOSE.read_text(encoding="utf-8")

    assert '"--node-ip", "127.0.0.1"' in compose_text
    assert '"7881:7881"' in compose_text
    assert '"7882:7882/udp"' in compose_text


def test_demo_launcher_uses_supervised_demo_mode() -> None:
    text = DEMO_LAUNCHER.read_text(encoding="utf-8")

    assert r"scripts\start_dev.ps1" in text
    assert "-Demo" in text


def test_demo_mode_starts_livekit_waits_and_opens_browser() -> None:
    text = POWERSHELL_SCRIPT.read_text(encoding="utf-8")

    required_fragments = (
        "docker desktop start",
        "compose up -d livekit",
        "compose down",
        "http://127.0.0.1:8000/health/ready",
        "http://127.0.0.1:3000/",
        "Start-Process $demoUrl",
    )
    for fragment in required_fragments:
        assert fragment in text


def test_demo_mode_treats_stopped_docker_probe_as_a_retryable_state() -> None:
    text = POWERSHELL_SCRIPT.read_text(encoding="utf-8")

    probe_start = text.index("function Test-DockerEngine")
    probe_end = text.index("function Start-DockerDesktopEngine")
    probe = text[probe_start:probe_end]
    assert '$ErrorActionPreference = "Continue"' in probe
    assert "2>&1" in probe
    assert "catch {" in probe
    assert "return $false" in probe
    assert "*> $null" not in probe


def test_demo_mode_can_fall_back_to_installed_docker_desktop() -> None:
    text = POWERSHELL_SCRIPT.read_text(encoding="utf-8")

    assert '"Docker\\Docker\\Docker Desktop.exe"' in text
    assert "Start-DockerDesktopEngine" in text
    assert "-WindowStyle Hidden" in text


def test_shell_launcher_has_valid_bash_syntax_when_available() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is not available")
    if os.name == "nt" and Path(bash).parent.name.lower() == "system32":
        pytest.skip("Windows subsystem bash cannot access the workspace path")

    result = subprocess.run(
        [bash, "-n", str(SHELL_SCRIPT)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
