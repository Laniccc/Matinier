# Demo Launcher Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.

**Goal:** Provide one Windows launcher that starts local LiveKit, API, Worker, and Frontend, waits until the app is ready, opens the browser, and cleans up only the resources it owns.

**Architecture:** Extend the existing `scripts/start_dev.ps1` supervisor with a `-Demo` mode instead of duplicating process lifecycle logic. A root `start_demo.cmd` wrapper bypasses local PowerShell execution-policy friction and gives the user a double-click entry point.

**Tech Stack:** Windows PowerShell 5.1, Docker Compose, FastAPI/Uvicorn, LiveKit Agents, Next.js/pnpm, pytest.

---

### Task 1: Add failing launcher contract tests

**Files:**
- Modify: `backend/tests/test_dev_launchers.py`

**Step 1: Write the failing tests**

Add tests that require:

```python
DEMO_LAUNCHER = PROJECT_ROOT / "start_demo.cmd"

def test_demo_launcher_uses_supervised_demo_mode() -> None:
    text = DEMO_LAUNCHER.read_text(encoding="utf-8")
    assert "scripts\\start_dev.ps1" in text
    assert "-Demo" in text

def test_demo_mode_starts_livekit_waits_for_services_and_opens_browser() -> None:
    text = POWERSHELL_SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        "docker desktop start",
        "docker compose up -d livekit",
        "docker compose down",
        "http://127.0.0.1:8000/health",
        "http://127.0.0.1:3000/",
        "Start-Process",
    ):
        assert fragment in text
```

**Step 2: Run the tests and verify failure**

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_dev_launchers.py -q
```

Expected: FAIL because `start_demo.cmd` and `-Demo` do not exist yet.

### Task 2: Implement supervised demo mode

**Files:**
- Modify: `scripts/start_dev.ps1`
- Create: `start_demo.cmd`

**Step 1: Add the `-Demo` parameter**

In demo mode:

1. Validate Docker CLI availability.
2. Start Docker Desktop only when the engine is unavailable.
3. Poll the engine with a finite timeout.
4. Record whether the LiveKit container was already running.
5. Run `docker compose up -d livekit`.
6. Reuse the existing migration and child-process supervisor.
7. Poll API `/health` and Frontend `/` with a finite timeout.
8. Open `http://127.0.0.1:3000/` in the default browser.
9. On exit, stop the recorded API/Worker/Frontend trees and run
   `docker compose down` only when this launcher started LiveKit.

**Step 2: Add the root wrapper**

`start_demo.cmd` must invoke:

```bat
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_dev.ps1" -Demo
```

and preserve the terminal when startup fails so the error remains visible.

**Step 3: Run launcher tests**

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_dev_launchers.py -q
```

Expected: all launcher tests pass, apart from the existing environment-only Bash skip where applicable.

### Task 3: Document the one-click flow

**Files:**
- Modify: `README.md`
- Modify: `docs/stage-records.md`

**Step 1: Update README**

Add `start_demo.cmd` as the recommended Windows demo command. Explain that it
starts Docker Desktop if needed, starts all four services, opens the page, and
uses Ctrl+C to stop launcher-owned processes and LiveKit.

**Step 2: Update stage records**

Record the launcher ownership boundary and measured smoke-test results.

### Task 4: Verify and launch the demo

**Files:**
- No source changes expected.

**Step 1: Run focused and full regression**

Run:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_dev_launchers.py -q
.\backend\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Expected: launcher tests and the full backend suite pass.

**Step 2: Run a real smoke test**

Launch `start_demo.cmd`, then verify:

- LiveKit listens on 7880/7881.
- API `/health` returns 200.
- Frontend `/` returns 200.
- The browser is opened to the workbench.
- The launcher terminal remains available for Ctrl+C cleanup.

Leave the demo running for the user to inspect. Do not commit: this workspace
does not expose a usable Git repository and may contain user-owned changes.
