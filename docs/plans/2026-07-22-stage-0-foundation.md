# LiveCaption Studio Stage 0 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the Stage 0 vertical slice in which a browser creates an empty session, receives a scoped token from FastAPI, joins a LiveKit room, and triggers an automatically dispatched Python worker participant.

**Architecture:** A single FastAPI process owns configuration, token creation, and SQLite session records; a separate LiveKit Agents process registers an empty programmatic participant; a Next.js client calls the API and connects with `livekit-client`. SQLAlchemy models and an Alembic migration create the only Stage 0 table, while JSON logging supplies the required correlation fields.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, SQLite/aiosqlite, LiveKit Python SDK/Agents, pytest, Next.js, TypeScript, pnpm, livekit-client.

---

### Task 1: Backend project, configuration, and structured logging

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/settings.py`
- Create: `backend/app/logging.py`
- Test: `backend/tests/test_settings.py`
- Test: `backend/tests/test_logging.py`

**Steps:**
1. Write tests asserting that absent LiveKit variables produce a readable Pydantic validation error and that JSON logs always contain `process`, `session_id`, `room_name`, `participant_identity`, `event`, and `error_type`.
2. Run `uv run pytest tests/test_settings.py tests/test_logging.py -q` from `backend`; expect failure because the modules do not exist.
3. Add a cached `Settings` object with required Stage 0 LiveKit fields, optional future-provider secrets, configured defaults, and `.env` loading.
4. Add a small JSON formatter and logging setup function; do not add a new logging framework.
5. Re-run the two tests; expect both to pass.

### Task 2: SQLite session persistence and migration

**Files:**
- Create: `backend/app/persistence/__init__.py`
- Create: `backend/app/persistence/database.py`
- Create: `backend/app/persistence/models.py`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/script.py.mako`
- Create: `backend/alembic/versions/20260722_0001_create_sessions.py`
- Test: `backend/tests/test_sessions_api.py`

**Steps:**
1. Write an API test that creates a session and reads it back from a temporary SQLite database.
2. Run `uv run pytest tests/test_sessions_api.py -q`; expect failure because the API and model are absent.
3. Define the `sessions` table with `id`, `room_name`, `status`, `source_type`, `source_name`, `language`, `started_at`, `ended_at`, and `created_at`.
4. Add async SQLAlchemy engine/session helpers and an Alembic migration containing the same schema.
5. Keep database access request-scoped so sessions close after every request.

### Task 3: FastAPI health, session, and LiveKit token endpoints

**Files:**
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/health.py`
- Create: `backend/app/api/sessions.py`
- Create: `backend/app/api/livekit_token.py`
- Create: `backend/app/main.py`
- Test: `backend/tests/conftest.py`
- Test: `backend/tests/test_health.py`
- Test: `backend/tests/test_livekit_token.py`

**Steps:**
1. Add failing tests for `GET /health`, `POST /api/sessions`, `GET /api/sessions/{id}`, missing-session 404 behavior, and the claims in a generated room-scoped JWT.
2. Run `uv run pytest -q`; expect endpoint/import failures.
3. Implement the routers and Pydantic request/response schemas. Token creation must look up the session, generate a unique participant identity when absent, and grant only join/subscribe/data access for that session room.
4. Add CORS for the local Next.js origin and an application lifespan that checks database connectivity.
5. Run `uv run alembic upgrade head` and `uv run pytest -q`; expect migration success and all backend tests passing.

### Task 4: Empty LiveKit worker

**Files:**
- Create: `backend/app/worker/__init__.py`
- Create: `backend/app/worker/entrypoint.py`
- Test: `backend/tests/test_worker_entrypoint.py`

**Steps:**
1. Add an import-level test proving an `AgentServer` is constructed and the entrypoint module is CLI-runnable.
2. Implement an unnamed `@server.rtc_session()` entrypoint so LiveKit automatically dispatches it to each newly created room.
3. Call `ctx.connect()` without starting an `AgentSession`, log room/participant connect and shutdown events, and rely on the framework room lifecycle for cleanup.
4. Run `uv run pytest tests/test_worker_entrypoint.py -q`; expect pass.

### Task 5: Next.js connection page

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/next.config.ts`
- Create: `frontend/next-env.d.ts`
- Create: `frontend/app/layout.tsx`
- Create: `frontend/app/page.tsx`
- Create: `frontend/app/globals.css`
- Create: `frontend/components/room-connection.tsx`
- Create: `frontend/lib/api.ts`
- Create: `frontend/types/session.ts`

**Steps:**
1. Define the API types and fetch helpers for session creation and token retrieval.
2. Build a client component that shows exactly `disconnected`, `connecting`, `connected`, `reconnecting`, or `error`, plus room/session/participant details.
3. Connect a `Room` using `livekit-client`; attach reconnect, reconnected, disconnected, and participant events before connecting; disconnect and remove listeners on unmount.
4. Run `pnpm install`, `pnpm exec tsc --noEmit`, and `pnpm build`; expect successful type-check and production build.

### Task 6: Environment, local LiveKit, and handoff documentation

**Files:**
- Create: `.env.example`
- Create: `.gitignore`
- Create: `docker-compose.yml`
- Create: `README.md`
- Create: `docs/stage-records.md`

**Steps:**
1. Document Python 3.12, uv, Node.js, pnpm, Docker/LiveKit prerequisites and the exact migration/start order for all three processes.
2. Provide safe local dev credentials only in `.env.example`; clearly identify them as the LiveKit server's documented development pair, not production secrets.
3. Add a minimal LiveKit server compose service with signaling, TCP, and UDP ports.
4. Verify required-setting failure, migration, API tests, frontend checks, and (when Docker is available) browser/worker room connection.
5. Record executed checks, current limitations, and the Stage 1 entry point in `docs/stage-records.md`.
