# Continuous Plugin Event Delivery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver new durable subtitle and terminal events to connected plugins without a page reload, reconnect or user command.

**Architecture:** Keep the existing model-free MediaEventProjector and durable ACK protocol. Add a Host-owned dispatcher for already-open runtime scopes, with one serialized delivery stream per plugin/session, independently scheduled bounded batches and shutdown cancellation. Do not create bindings for unrelated history or change plugin permissions, realtime window thresholds or caption publication.

**Tech Stack:** Python asyncio, FastAPI, SQLAlchemy/SQLite, pytest, isolated Docker plugins.

---

## Scope and implementation decision

The user requested repair of the diagnosed delivery gap. Background Host dispatch is preferred to repeated frontend bridge requests: it continues after the page closes and keeps ingestion out of read-only view endpoints. A direct projector callback would couple durable projection to plugin latency and miss recovery polling; retain independent polling instead.

This is restoration of previously approved realtime behavior, not a new scenario feature. No valid Git repository is present; use stage records instead of commits/worktrees. Do not restart the normal development API or replay private historical sessions during synthetic verification. Its current process has no automatic reload, so source edits can be tested separately without changing active runtime state.

## Task 1: Reproduce without reconnect

**Files:** Modify `backend/tests/test_course_organizer_e2e.py`; create `backend/tests/test_plugin_event_delivery.py`.

1. Allow the existing synthetic Session helper to omit initial captions.
2. Create a real-projector E2E: install course plugin, connect exactly once while no captions exist, append a window later, poll only views, and assert realtime notes appear. Append a second window and assert another model call without a bridge request. Complete the Session and assert a complete document, realtime section and ACK catch-up without reconnect.
3. Run `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_course_organizer_e2e.py -k without_reconnecting -q`. Expected before fix: timeout waiting for notes, not a model or setup failure.
4. Add dispatcher tests for lifecycle, independent slow/failing plugins, cancellation, scope eligibility and concurrent foreground/background cursor serialization.

## Task 2: Implement Host dispatch

**File:** Modify `backend/app/plugins/bootstrap.py`.

1. Store dispatcher task, per-target tasks and per-target asyncio locks on `PluginHostRuntime`.
2. Start polling with Host startup; derive eligible targets only from current supervised ready/degraded identities and active session scopes. Never enumerate all historical MediaSessions to open new bindings.
3. Each poll schedules at most one task per eligible target; process one existing durable event batch per background pass. Keep foreground catch-up compatible, using the same per-target lock so two callers cannot read and advance an old cursor concurrently.
4. Reuse the existing subscription filter, delivery cursor and validated ACK. Catch per-target delivery failures with safe metadata-only logging; retry from durable ACK later. No synthetic ACK or silent skip.
5. On stop cancel/await dispatcher and delivery tasks before shutting down supervisor/projector; clear task/lock registries. Preserve existing behavior when the framework is disabled or no binding is open.
6. Run the new E2E and `backend/tests/test_plugin_event_delivery.py`; expected all pass.

## Task 3: Verify real Docker delivery

**File:** Modify `backend/smoke/course_organizer_plugin_smoke.py`.

1. Remove the explicit second bridge call after newly appended captions and Session completion. Wait for background ACK and complete document through read-only endpoints.
2. Record an explicit continuous-delivery boolean; retain all isolation, evidence, export, language and restart checks.
3. Run `backend/.venv/Scripts/python.exe backend/smoke/course_organizer_plugin_smoke.py`. Expected functional/isolation flags true, `caption_regression=false`, and cleanup of only exact temporary resources.

## Task 4: Regression and handoff

**Files:** Update `docs/plugin-operations.md` and `docs/stage-records.md`.

1. Run focused course/framework/delivery tests five consecutive times.
2. Run `backend/.venv/Scripts/python.exe -m compileall -q backend/app backend/tests` and full `backend/.venv/Scripts/python.exe -m pytest backend/tests -q`.
3. Run remaining sandbox/framework Docker smokes sequentially. No frontend behavior is changed; document the prior frontend result without claiming a new browser check.
4. Review delivery concurrency, shutdown, disabled scopes and test coverage. Record exact results and clearly distinguish synthetic realtime verification from historical model replay. Report whether normal API restart is still required; do not claim source changes are active in the existing process.

## Authorized runtime handoff — 2026-08-31

After read-only diagnosis of Session `f5703db6-f633-4d5e-ae2d-76d4ca92f524`, the user explicitly authorized
restarting the API and processing already-bound historical backlog, including possible DeepSeek calls.
This supersedes the earlier no-restart/no-private-replay restriction for this operational handoff only.

1. Confirm no current capture or new in-flight model call, identify the exact API process/console and
   back up SQLite with its online backup API; preserve frontend, Worker and base Docker services.
2. Gracefully stop only the verified API console and start the same API entrypoint with the repaired code.
3. Observe restored durable bindings without additional bridge/command calls. Validate ACK catch-up,
   realtime notes, automatic complete document, evidence closure, exports, database integrity and health.
4. Preserve immutable earlier documents and version-pinned historical bindings. Record separate old-version
   failures honestly; do not silently migrate old plugin versions or claim a new live-browser capture test.

Completed: API PID 29388 loaded the repair; target course/diagnostic ACK reached 12/12, view v7 contains
3 realtime notes, and complete document v2 contains those 3 notes plus 7 knowledge items. Evidence and export
checks passed. Exact backup, artifact and remaining old-version caveat are recorded in `docs/stage-records.md`.
