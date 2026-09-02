# Matinier Private Meeting Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend Matinier into a low-presence private meeting Agent with incremental meeting state, fast private turns, durable action runs, atomic handoff, scoped authorization, and Linear issue creation as its first external capability.

**Architecture:** Preserve the existing LiveKit caption hot path and immutable Package workflows. Add in-process sidecar services under the FastAPI lifespan: a polling Meeting State projector, a bounded Fast Turn runner, a durable Action Run runner, and typed task-system adapters over a shared SQLAlchemy state substrate.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, asyncio, httpx, SQLite WAL, DeepSeek structured completion, Linear GraphQL API, pytest, Next.js 16, React 19, TypeScript 6.

---

## Execution rules

- Execute tasks in order. Add only the five positive tests listed below; do not create per-module negative/error suites.
- Do not add an LLM call, network request, or new callback to `backend/app/captions/runtime.py`.
- Do not migrate existing Package workflows into the Agent runner.
- All model outputs and tool inputs must pass strict Pydantic validation.
- Do not store hidden reasoning, Provider secrets, raw authorization headers, or unbounded third-party error payloads.
- External writes require an active ActionGrant and a persisted idempotency key.
- The current workspace does not contain usable Git metadata. Execute commit steps only after restoring a valid repository; do not claim commits otherwise.
- Tasks 1–10 remain platform-independent. Task 11 implements the selected Linear Adapter; do not leak Linear-specific fields into Planner, Action Run, or TaskSystemAdapter contracts.

## Minimal test policy

Create only:

- `backend/tests/test_private_meeting_agent_core.py` with four positive tests:
  1. Final Caption → Meeting State → Fast Ask using Head plus Final tail;
  2. successful Fast→Slow Handoff with root/parent linkage;
  3. successful Action Run search/create/get with FakeTaskSystemAdapter plus idempotent replay;
  4. two Action Runs advancing independently while one waits.
- `backend/tests/test_linear_task_adapter.py` with one positive mocked GraphQL search/create/get test.

Do not add tests for invalid input, permission denial, timeout, crash, 429/5xx, malformed response, database lock, unknown write, recovery, reconciliation ambiguity, or other error/fault paths. Do not add an evaluation dataset, load benchmark, or frontend unit-test framework. During implementation run only the relevant positive test; run the existing full backend suite, frontend typecheck/build, and migration check once at the end.

## Task 1: Add meeting-intelligence persistence

**Files:**

- Create: `backend/alembic/versions/20260811_0019_create_meeting_intelligence.py`
- Modify: `backend/app/persistence/models.py`
- Create: `backend/app/meeting_state/__init__.py`
- Create: `backend/app/meeting_state/models.py`
- Create: `backend/app/meeting_state/repository.py`

**Step 1: Add strict domain models**

Implement frozen Pydantic models with these discriminated values:

```python
MeetingStateStatus = Literal["ready", "lagging", "stale", "rebuilding"]
MarkOrigin = Literal["manual", "automatic"]
MarkKind = Literal["highlight", "decision", "action", "conflict"]
MarkStatus = Literal["candidate", "accepted", "dismissed"]
ActionReadiness = Literal[
    "detected", "recordable", "executable", "fully_specified"
]
CandidateContentStatus = Literal[
    "active", "dismissed", "superseded", "cancelled"
]
CandidateExecutionStatus = Literal[
    "not_requested", "handed_off", "executing", "executed", "failed"
]
```

`MeetingState` must contain typed tuples for topics, entities, decisions, action candidates, highlights, conflicts, and user concerns. Every extracted item must carry one or more `source_segment_ids`.

Implement `evaluate_action_readiness(candidate, execution_request, grant)` as a deterministic domain function. It must not call the model. `executable` requires evidence-backed title/deliverable, current evidence revisions, an explicit external-action request, a valid `task.create` Grant, no blocking conflict, and remaining side-effect budget. Assignee, due date, and priority are optional unless the user request explicitly constrains them.

Define `GroundedValue[T]` with `value`, `origin`, `resolution`, `evidence_message_ids`, optional confidence, and bounded user-facing explanation. Define `EvidenceMessageSnapshot` as a discriminated caption/user-input union. Caption snapshots include session, Segment ID/revision, track, nullable speaker label, language, raw/display text, audio bounds, confidence, receive/final timestamps, and content hash. User-input snapshots include session, actor, original text, created time, and content hash. Do not infer speaker identity from track ID.

Keep content status and execution status independent from readiness. A cancelled candidate can still retain its previous readiness for audit, and an executed candidate can later become content-cancelled without silently modifying the Linear Issue.

**Step 2: Add the migration and ORM records**

Create:

- `meeting_projection_offsets` with unique `(session_id, segment_id)`;
- `meeting_state_heads` with one row per Session and columns for latest Final time, projected frontier, lag, pending count, last success, and sanitized last error;
- `meeting_marks` with indexes on `session_id`, `status`, and `kind`.
- `action_candidates` with indexes on Session, content status, and execution status;
- `action_candidate_revisions` with unique `(candidate_id, revision)` and immutable content JSON.
- `identity_bindings` with unique `(actor_id, linear_team_id, normalized_mention, status)` for active confirmed bindings.

Use `ondelete="CASCADE"` from these tables to `sessions.id`. JSON columns must have non-null empty defaults where appropriate.

**Step 3: Implement repository methods**

Implement `advance_offset`, `get_head`, `replace_head`, `create_mark`, `update_mark_status`, `list_marks`, `create_candidate`, `append_candidate_revision`, `supersede_candidate`, `transition_candidate_execution`, `confirm_identity_binding`, `revoke_identity_binding`, and `find_identity_binding`. `replace_head` and candidate revision append must compare expected versions and raise a domain conflict on stale writes.

**Step 4: Verify imports**

Run from `backend`:

```powershell
.\.venv\Scripts\python.exe -m compileall app/meeting_state app/persistence/models.py
```

Expected: compilation succeeds. Functional behavior is covered later by core tests 1–3.

## Task 2: Add the shared Agent execution substrate

**Files:**

- Create: `backend/alembic/versions/20260811_0020_create_assistant_execution_state.py`
- Modify: `backend/app/persistence/models.py`
- Create: `backend/app/assistant/__init__.py`
- Create: `backend/app/assistant/models.py`
- Create: `backend/app/assistant/repository.py`
- Create: `backend/app/assistant/state_machine.py`

**Step 1: Define statuses and terminal-state helpers**

Use explicit literals, not arbitrary strings:

```python
ExecutionProfile = Literal["fast_turn", "action_run"]

FastTurnStatus = Literal[
    "received", "contextualizing", "deciding", "executing_reads",
    "responding", "handed_off", "completed", "failed", "cancelled",
]

ActionRunStatus = Literal[
    "queued", "planning", "executing", "observing", "waiting_external",
    "reconciling", "needs_input", "completed", "partial", "failed",
    "cancelled",
]

ExternalActionClaimStatus = Literal[
    "reserved", "requesting", "unknown", "succeeded", "failed_safe",
]
```

**Step 2: Add tables**

The migration creates:

- `assistant_context_snapshots`;
- `action_grants`;
- `assistant_executions`;
- `assistant_steps`;
- `assistant_tool_calls`;
- `assistant_observations`;
- `assistant_handoffs`;
- `assistant_subagent_runs`;
- `external_action_claims`;
- `assistant_client_operations`;
- `assistant_events`.

Add indexes for session/status/profile, root/parent execution, execution/sequence, tool-call status, Subagent execution/round/role, Grant status/expiry, action-claim lease status, session event lookup, and event `(execution_id, id)`. Add a unique constraint on external action claim `(provider, capability, logical_action_key)` and on client operation `(execution_id, client_operation_id)`.

`assistant_context_snapshots` must include a non-null `evidence_messages_json` payload in addition to state slice and source-reference metadata. Its content is immutable after insert.

**Step 3: Implement the repositories**

Repositories must expose small transaction-friendly methods rather than committing internally. Include:

- create/get/list execution;
- transition with expected version;
- append step, observation, event;
- create and consume Grant;
- prepare/update ToolCall;
- create/transition/list Subagent runs by execution and planning round;
- reserve/observe/transition/reconcile external action claims with expected state and arguments hash;
- create Handoff exactly once;
- record/replay an idempotent client operation;
- list session events after a global integer cursor.

**Step 4: Verify imports**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app/assistant app/persistence/models.py
```

Expected: compilation succeeds. State integration is exercised by core tests 2–4.

## Task 3: Implement incremental Meeting State projection

**Files:**

- Create: `backend/app/meeting_state/contracts.py`
- Create: `backend/app/meeting_state/extractor.py`
- Create: `backend/app/meeting_state/merge.py`
- Create: `backend/app/meeting_state/candidates.py`
- Create: `backend/app/meeting_state/projector.py`
- Create: `backend/app/meeting_state/bootstrap.py`
- Modify: `backend/app/settings.py`

**Step 1: Add settings**

Add validated settings with safe defaults:

```text
ASSISTANT_ENABLED=false
MEETING_STATE_POLL_INTERVAL_MS=400
MEETING_STATE_BATCH_SEGMENTS=10
MEETING_STATE_BATCH_TRIGGER_CHARS=2000
MEETING_STATE_MAX_INPUT_CHARS=12000
MEETING_STATE_MAX_BATCH_WAIT_SECONDS=4
MEETING_STATE_SCAN_OVERLAP_SECONDS=2
MEETING_STATE_STALE_AFTER_SECONDS=10
MEETING_STATE_CONCURRENCY=2
MEETING_STATE_PRIORITY_BURST=2
MEETING_STATE_RETRY_DELAYS_SECONDS=1,2,5,10,30
```

Do not start the projector when `ASSISTANT_ENABLED=false`.

**Step 2: Define extraction output**

The model produces a `MeetingStateDelta` whose items always cite Segment IDs. Reject foreign Segment IDs. Generate stable internal item IDs from kind, normalized content, and sorted evidence IDs; do not trust model-generated IDs.

**Step 3: Implement deterministic merge**

Implement CandidateMergePolicy separately from model extraction. It validates proposed create/revise/merge/split/cancel operations against Session, expected revision, evidence references, content status, and execution status. Every projected state item stores the exact revision of every source Segment; revising any one of those Segments invalidates the whole old item before re-projection. Preserve manual user concerns and calculate a canonical state hash. A split creates new candidate IDs with `derived_from` links. A safe merge retains the older ID and supersedes the duplicate only when title/deliverable, assignee identity, due time, priority, and conflict evidence are compatible; the surviving revision unions compatible field values and immutable evidence from both candidates.

**Step 4: Implement polling**

Read Final Segment rows changed after the overlap boundary. Compare each row with its persisted processed revision and buffer work independently by Session. Polling never calls the model for an empty or below-threshold buffer. Trigger a batch on 10 segments, approximately 2,000 characters, four seconds since the first pending segment, session finalization, or an explicit high-priority catch-up. Split input above the hard model-input limit.

Run extraction outside caption transactions, then atomically update the Head, Candidate revisions, offsets, and source frontier only after successful validation. If one Final Segment alone exceeds the input limit, split its text into bounded extractor calls, coalesce their operations, and commit the original Segment revision only after every fragment succeeds. A failed extraction keeps the last good semantic Head and hash, does not advance offsets, persists `stale`, `last_error_at`, and a sanitized error code, and retries that Session with the configured backoff. Implement two concurrency slots with a priority queue that services the oldest normal batch after two consecutive priority batches. Session finalization flushes all remaining Final segments.

Expose `request_catch_up(session_id)` and a waitable target frontier. A catch-up request raises scheduling priority but cannot cancel an in-flight batch.

**Step 5: Verify imports**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app/meeting_state
```

Expected: compilation succeeds. The positive Final Caption projection path is covered by core test 1.

## Task 4: Build context snapshots and marks APIs

**Files:**

- Create: `backend/app/assistant/context.py`
- Create: `backend/app/api/meeting_state.py`
- Create: `backend/alembic/versions/20260811_0021_harden_meeting_mark_evidence.py`
- Modify: `backend/app/persistence/models.py`
- Modify: `backend/app/meeting_state/repository.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/main.py`

**Step 1: Implement ContextBuilder**

Inputs are Session ID, user goal, optional mark IDs, and size budget. Output includes the state version, source frontier, freshness metadata, state slice, immutable `EvidenceMessageSnapshot` entries, field-level evidence references, tail Segment snapshots, and `relevant_context_hash`. Save raw/display message text together with the contemporaneous Segment or user-input metadata. Skip projected state items whose stored source revisions no longer match current Final Segments. Selected marks use their creation-time evidence messages rather than re-reading later Segment text. Never default to the complete transcript. For Fast Ask, return the last good Head plus unprojected Final tail immediately and request catch-up without awaiting it.

**Step 2: Add endpoints**

Implement:

- `GET /api/sessions/{session_id}/meeting-state`;
- `GET /api/sessions/{session_id}/marks`;
- `POST /api/sessions/{session_id}/marks`;
- `PATCH /api/sessions/{session_id}/marks/{mark_id}`.

Validate that every mark evidence ID belongs to the Session. At mark creation, freeze the Segment revisions, contemporaneous Caption evidence, and a `UserInputEvidenceMessage` containing actor plus raw/display mark text. Accepting an automatic mark must carry evidence revisions that match both the frozen proposal and the current Final Segments. Return 404 for missing Session/mark and 409 for stale or mismatched evidence revisions.

**Step 3: Verify imports**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app/assistant/context.py app/api/meeting_state.py
```

Expected: compilation succeeds. Context assembly is covered by core test 1.

## Task 5: Add bounded structured planning

**Files:**

- Create: `backend/app/assistant/planner.py`
- Create: `backend/app/assistant/prompts.py`
- Create: `backend/app/assistant/parser.py`

**Step 1: Define a strict decision union**

Implement a discriminated Pydantic union conceptually equivalent to:

```python
AgentDecision = Annotated[
    RespondDecision | InvokeToolsDecision | HandoffDecision
    | NeedsInputDecision | CompleteDecision,
    Field(discriminator="kind"),
]
```

Every externally meaningful claim must include evidence refs. Store only `decision_summary`, never hidden reasoning.

**Step 2: Reuse the existing structured Provider**

Depend on `StructuredTextProvider`; do not add a second HTTP client for DeepSeek. Parse exactly one JSON object using existing parser conventions. Allow at most one repair attempt in Action Run and no unbounded repair loop in Fast Turn.

**Step 3: Verify imports**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app/assistant/planner.py app/assistant/prompts.py app/assistant/parser.py
```

Expected: compilation succeeds. Planner output is exercised through core tests 1–4.

## Task 6: Implement Action Grants and typed tool execution

**Implementation status (2026-08-12): Complete.** Typed Tool Specs, validated
Invocations and Results, goal-scoped Action Grants, durable ToolCalls, external
action claims, prepared-call replacement and reconciliation-only continuation are
implemented. Execution commits the write claim before awaiting a provider, consumes
Grant capacity atomically, returns a persisted exact replay, and never repeats an
unknown external mutation.

**Files:**

- Create: `backend/app/assistant/grants.py`
- Create: `backend/app/assistant/tools/__init__.py`
- Create: `backend/app/assistant/tools/contracts.py`
- Create: `backend/app/assistant/tools/registry.py`
- Create: `backend/app/assistant/tools/executor.py`
- Modify: `backend/app/assistant/repository.py`

**Step 1: Define the Port**

```python
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    version: str
    capability: str
    effect: Literal["read", "local_write", "external_write"]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    timeout_seconds: float
    supports_idempotency: bool
    supports_reconciliation: bool
```

Define `ToolResult` with succeeded/pending/failed/unknown, sanitized error fields, optional external reference, and retryable flag.

**Step 2: Implement execution ordering**

ToolExecutor performs Registry lookup, Pydantic validation, Grant validation, logical-action and argument hashing, persisted ToolCall preparation, claim reservation, transition to `requesting`, Adapter execution, result persistence, and Observation append. A replay returns the stored result if hashes match. Before `requesting`, a newer Candidate Revision may replace the prepared payload and arguments hash under the same claim; after `requesting`, the payload is immutable. A timeout or response-loss outcome becomes `unknown` and may only enter reconciliation, never a second mutation.

The implementation uses a typed `ToolInvocation` carrying candidate, resource-scope, evidence, logical-action, and idempotency context. Only external writes require and atomically consume an ActionGrant side-effect slot. ToolCall/Claim preparation is committed before awaiting an Adapter, and result/Observation persistence uses a new short transaction afterward. Explicit prepared-call replacement is guarded by both ToolCall `prepared` and Claim `reserved` compare-and-set checks. `ToolExecutor.reconcile()` is the only continuation for unknown external outcomes.

**Step 3: Verify imports**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app/assistant/grants.py app/assistant/tools
```

Expected: compilation succeeds. The successful authorized tool path and replay are covered by core test 3.

Verification completed from `backend` with an isolated temporary bytecode cache:

```powershell
$env:PYTHONPYCACHEPREFIX = 'C:\Users\GX\AppData\Local\Temp\matinier-task6-10-repair-pycache'
.\.venv\Scripts\python.exe -m compileall app/assistant/grants.py app/assistant/tools
```

Result: `app/assistant/grants.py` and every module under `app/assistant/tools`
compiled successfully. The authorized create/replay path is also covered by the
Task 9 positive test below.

## Task 7: Implement Fast Turn and atomic Handoff

**Implementation status (2026-08-11): Complete.** `FastTurnRunner` now enforces the
5-second/two-round/no-external-write budget, reads the projected Meeting State plus
the current Final tail, and may create a durable Handoff only when the request
explicitly authorizes it. `HandoffService` freezes the snapshot, copies completed
steps and observations, resolves the goal-scoped Grant, creates the queued Action
Run and immutable envelope, and transitions the Fast Turn in one transaction;
the scheduler callback runs only after commit.

**Files:**

- Create: `backend/app/assistant/fast_runner.py`
- Create: `backend/app/assistant/handoff.py`
- Create/Test: `backend/tests/test_private_meeting_agent_core.py`

**Step 1: Add core positive tests 1 and 2**

Add exactly:

- `test_final_caption_projects_and_fast_ask_uses_tail`: persist Final captions, run one projection batch, leave one newer Final in the tail, submit an Ask turn, and assert the response cites both projected and tail evidence;
- `test_fast_turn_handoff_preserves_root_parent`: run one normal Handoff and assert one target Action Run whose root is inherited and whose parent is the Fast Turn.

**Step 2: Implement Fast Turn budgets**

Defaults:

```text
timeout_seconds=5
max_model_rounds=2
max_parallel_read_tools=2
max_external_writes=0
```

Use `asyncio.timeout`; timeout produces a Handoff only when the original request authorizes a longer-running goal, otherwise return a bounded failure.

**Step 3: Implement HandoffService**

Within one SQLAlchemy transaction, freeze Context Snapshot, persist completed steps/observations, create or reference Grant, create queued Action Run, insert immutable HandoffEnvelope, transition Fast Turn to handed_off, and append events. Enqueue only after commit.

**Step 4: Run core tests 1 and 2**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_private_meeting_agent_core.py::test_final_caption_projects_and_fast_ask_uses_tail tests/test_private_meeting_agent_core.py::test_fast_turn_handoff_preserves_root_parent -q
```

Expected: two tests pass.

Verification completed from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_private_meeting_agent_core.py::test_final_caption_projects_and_fast_ask_uses_tail backend/tests/test_private_meeting_agent_core.py::test_fast_turn_handoff_preserves_root_parent -q
```

Result: `2 passed in 0.68s`.

## Task 8: Implement durable Action Run and recovery

**Implementation status (2026-08-12): Complete.** `ActionRunRunner` now persists
each lifecycle transition around bounded planning and tool work, joins durable
Evidence/Conflict/Linear Research Subagent branches through an evidence-gated
Critic, runs read tools concurrently and writes serially, and performs a relevant
context refresh before external writes. `ActionRunScheduler` provides FIFO fairness
with three configurable concurrent Runs. Startup recovery requeues safe planning
work, resumes unfinished Subagent branches, routes uncertain external calls through
reconciliation, preserves reserved claims, and never repeats an unknown mutation.
After a successful `task.search`, the Linear Research branch receives one targeted
refresh using the new typed read Observation. The Critic now blocks external writes
when any specialist is incomplete or reports a blocker. Startup recovery also
inspects active Claims and isolates a Claim without its ToolCall in `NeedsInput`
instead of admitting another writer.

**Files:**

- Create: `backend/app/assistant/action_runner.py`
- Create: `backend/app/assistant/action_scheduler.py`
- Create: `backend/app/assistant/subagents.py`
- Create: `backend/app/assistant/critic.py`
- Create: `backend/app/assistant/recovery.py`
- Create: `backend/app/assistant/bootstrap.py`
- Modify: `backend/app/settings.py`
- Modify/Test: `backend/tests/test_private_meeting_agent_core.py`

**Step 1: Add core positive test 4**

Add only `test_two_action_runs_advance_independently`: start two Action Runs, pause the first on a controllable successful tool future, allow the second to complete, release the first, and assert both complete with independent execution IDs and event streams.

**Step 2: Add settings**

```text
ASSISTANT_FAST_TIMEOUT_SECONDS=5
ASSISTANT_FAST_MAX_MODEL_ROUNDS=2
ASSISTANT_ACTION_MAX_STEPS=20
ASSISTANT_ACTION_MAX_MODEL_CALLS=8
ASSISTANT_ACTION_MAX_PLANNING_ROUNDS=4
ASSISTANT_ACTION_CONCURRENCY=3
ASSISTANT_SUBAGENTS_PER_RUN=3
ASSISTANT_SUBAGENT_GLOBAL_CONCURRENCY=6
ASSISTANT_TOOL_TIMEOUT_SECONDS=15
ASSISTANT_RECONCILIATION_DELAYS_SECONDS=1,3,10
```

**Step 3: Implement the loop**

Persist every state transition before doing the next operation. The Orchestrator creates Evidence, Conflict, and Linear Research Subagent runs with scoped read-only capabilities, joins their structured Observations, and invokes Critic before choosing the next action. Read tools may run concurrently only when their specs declare `effect=read`. External writes run serially through the main Orchestrator and require a pre-write relevant-context check plus an external action claim.

Use a fair scheduler across execution IDs. Never hold a SQLAlchemy transaction or Session open while awaiting a model, Subagent, ProcessingJob, or Linear network call.

**Step 4: Implement startup recovery**

- enqueue queued executions;
- return planning/observing to queued when no side effect is running;
- move running external ToolCalls to reconciling;
- recover unfinished Subagent branches independently and rejoin completed results;
- recover or inspect leased external action claims before allowing a new writer;
- query by external reference or provider correlation key;
- for an unknown Linear create, query the exact action-key marker after approximately 1, 3, and 10 seconds;
- one match is read back and completed, multiple matches record `duplicate_external_side_effect`, and no match after all attempts remains unknown;
- mark unverifiable effects unknown and enter NeedsInput;
- never automatically repeat an unknown write.

**Step 5: Run core test 4**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_private_meeting_agent_core.py::test_two_action_runs_advance_independently -q
```

Expected: one test passes and the second Run completes before the first future is released.

Verification completed from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_private_meeting_agent_core.py::test_two_action_runs_advance_independently -q
```

Result: `1 passed in 0.74s`. The first Run remained in `executing` while its
controllable read was paused; the second Run reached `completed` before the first
was released, and both retained separate execution IDs and event streams.

Repair verification on 2026-08-12 was run together with the Task 9 positive test:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_private_meeting_agent_core.py::test_two_action_runs_advance_independently tests/test_private_meeting_agent_core.py::test_action_run_creates_reads_back_and_reuses_fake_task -q
```

Result: `2 passed in 3.81s`. No full suite or additional negative-path test was run,
in accordance with the agreed minimal-test policy.

## Task 9: Add the platform-neutral task domain and fake adapter

**Implementation status (2026-08-12): Complete.** The new provider-neutral task
domain defines normalized drafts, external tasks, search/create/reconciliation
results and exact-first identity models. `TaskSystemService` derives the stable
Candidate-lineage action key, searches before creating, classifies deterministic
reuse/related/ambiguous cases, preserves unresolved assignees as explicit
placeholders, creates through the Adapter and verifies the result with a read-back.
`task.search`, `task.get` and grant-protected `task.create` are registered as typed
tools; Team/project configuration remains server-owned. Confirmed reuse refunds the
reserved Grant side-effect slot, while unknown writes remain reconciliation-only.
`task.create` no longer accepts Planner-supplied specialist approval booleans; its
Adapter derives the decision context from the latest persisted Evidence, Conflict
and refreshed Linear Research results. Email-like assignee text is also passed to
the exact-first identity resolver as an email candidate while unresolved text stays
an explicit placeholder.

**Files:**

- Create: `backend/app/task_system/__init__.py`
- Create: `backend/app/task_system/models.py`
- Create: `backend/app/task_system/contracts.py`
- Create: `backend/app/task_system/service.py`
- Create: `backend/app/task_system/identity.py`
- Create: `backend/app/task_system/fake.py`
- Create: `backend/app/task_system/tools.py`
- Modify/Test: `backend/tests/test_private_meeting_agent_core.py`

**Step 1: Add core positive test 3**

Add only `test_action_run_creates_reads_back_and_reuses_fake_task`: use a recordable Candidate with explicit evidence and a valid goal-scoped Grant, run the successful search→create→get path, replay the same client request/logical action, and assert FakeTaskSystemAdapter contains one task and both responses reference it.

**Step 2: Define normalized models**

```python
class TaskDraft(BaseModel):
    title: str
    description: str | None
    assignee_ref: str | None
    due_at: datetime | None
    priority: Literal["low", "normal", "high", "urgent"] | None
    source_evidence_ids: tuple[str, ...]
    related_task_refs: tuple[str, ...] = ()
```

Add normalized `ExternalTask`, `TaskSearchQuery`, `TaskSearchResult`, `TaskCreateResult`, and `TaskReconciliationResult`.

Add `PersonMention`, `ResolvedIdentity`, and `IdentityBinding` models. A placeholder preserves `spoken_text`, has no external user ID, sets `resolution` to missing/ambiguous and `is_placeholder=true`.

**Step 3: Define the Adapter**

The protocol exposes async `search`, `get`, `create`, `reconcile_create`, `search_members`, and `get_member`. Server-side connection configuration supplies workspace/project defaults; Planner inputs cannot override them.

**Step 4: Implement deterministic service behavior**

Derive the create key as `SHA256("linear.issue.create:v1" + linear_team_id + candidate_lineage_root_id)`. Candidate revisions retain the key, a merge retains the surviving Candidate key, and split children receive distinct keys. Search before create, then combine Evidence, Conflict, and Linear Research results. Exact key/marker equality is `same_action`. Classify `duplicate` only when the Team and deliverable match, the existing Issue is active, assignee/due fields do not conflict, no independent deliverable is evidenced, and the read-only agents agree. Related work is linked without blocking creation; ambiguous reuse enters NeedsInput. Resolve identities exact-first: explicit ID, unique email, confirmed Team binding, then unique normalized exact display name. Fuzzy matches never bind automatically. After create, call get and compare title, assignee, and due date.

When a responsible subject is required but cannot be resolved, omit the external assignee ID and append `Unresolved meeting assignee: <原文>` plus `matinier-identity-status: unresolved` to the Issue description. Return a partial result and surface the placeholder in the UI; never choose the closest Linear user.

**Step 5: Register typed tools**

Register `task.search`, `task.get`, and `task.create`. `task.create` declares external_write, requires idempotency and reconciliation, and requires `task.create` capability.

**Step 6: Run core test 3**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_private_meeting_agent_core.py::test_action_run_creates_reads_back_and_reuses_fake_task -q
```

Expected: the full create-and-verify loop passes against FakeTaskSystemAdapter.

Verification completed from the repository root:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_private_meeting_agent_core.py::test_action_run_creates_reads_back_and_reuses_fake_task -q
```

Result: `1 passed in 0.79s`. The Action Run completed
`task.search -> task.create -> task.get`, returned the verified Fake task URL, and
an exact client-request replay returned the same execution result. One Fake task,
one create ToolCall, one succeeded logical-action claim and one consumed side-effect
slot remained persisted.

The 2026-08-12 repair verification additionally asserts persisted specialist rounds
`evidence=[1]`, `conflict=[1]`, and `linear_research=[1,2]`, proving that the create
decision consumes the refreshed trusted research state rather than request booleans.

## Task 10: Expose Agent APIs and lifecycle wiring

**Implementation status (2026-08-12): Complete.** The Assistant API now exposes
explicit Ask/Execute turn creation, a transactionally bootstrapped Session state,
the Session-filtered global event cursor, execution detail, and versioned,
idempotent input/cancel operations. Execute mode freezes the selected Candidate
scope into its Context Snapshot, derives the fixed task Team server-side, persists
the goal-scoped Grant and Action Run, then enqueues only after commit. Ask remains a
bounded Fast Turn and may hand off to an independently scheduled read-only Action
Run. Application bootstrap composes the shared Provider, Projector, Planner, typed
task tools, three specialist branches, Fast runner, durable Action runner and
startup recovery; lifespan starts them after database connectivity and stops them
before database disposal. The runtime and factory remain injectable for tests, and
Task 11 owns replacing the temporary development Fake adapter with the configured
`disabled|fake|linear` factory.
Concurrent turn creation and client operations now use nested transactions plus
database uniqueness as the final idempotency arbiter. A committed Action Run is not
turned into a false 503 when in-memory scheduling is temporarily unavailable;
startup recovery and exact request replay can schedule the durable record again.
Event payloads are constrained to Pydantic JSON values rather than unbounded `Any`.

**Files:**

- Create: `backend/app/api/assistant.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/settings.py`

**Step 1: Add endpoints**

Implement:

```text
POST /api/sessions/{session_id}/assistant/turns
GET  /api/sessions/{session_id}/assistant/state
GET  /api/sessions/{session_id}/assistant/events?after={event_id}&limit={limit}
GET  /api/assistant/executions/{execution_id}
POST /api/assistant/executions/{execution_id}/input
POST /api/assistant/executions/{execution_id}/cancel
```

Turn creation returns 202 with the execution and current event cursor. The request must carry explicit `intent_mode=ask|execute`; mode is never inferred from message text. `ask` rejects an external-write Grant and `execute` requires one. `(session_id, client_request_id)` is idempotent and returns the original execution on replay.

The session-state endpoint returns all active executions, the 20 most recent terminal executions, unresolved NeedsInput/result summaries, root/parent links, and a `snapshot_cursor` from one read transaction. The session-events endpoint filters the global AssistantEvent sequence by Session, caps `limit` at 100, and returns `events`, `next_cursor`, and `has_more`. Events include execution/root IDs, state version, schema version, type, phase, status, bounded summary, typed payload, and timestamp; never hidden reasoning or raw provider errors.

Input and cancel require `client_operation_id` plus `expected_state_version`. Exact replay returns the stored response; an ID reused with a different request is rejected; a stale state version returns 409 with the latest execution.

**Step 2: Wire lifespan services**

Build Projector, FastTurnRunner, ActionRunRunner, Planner, Registry, and Task adapter in `assistant/bootstrap.py`. Start them after database connectivity and stop them before database disposal. Preserve dependency injection hooks in `create_app` for tests.

**Step 3: Verify imports**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app/api/assistant.py app/assistant/bootstrap.py
```

Expected: compilation succeeds. API behavior is exercised in the two manual interview flows.

Verification completed from `backend` with an isolated temporary bytecode cache
because Windows denied atomic writes to the pre-existing workspace `__pycache__`:

```powershell
$env:PYTHONPYCACHEPREFIX = 'C:\Users\GX\AppData\Local\Temp\matinier-task10-pycache'
.\.venv\Scripts\python.exe -m compileall app/api/assistant.py app/assistant/bootstrap.py
```

Result: both `app/api/assistant.py` and `app/assistant/bootstrap.py` compiled
successfully. Per the agreed minimal-test policy, no additional test or full suite
was run for this task.

The 2026-08-12 repair compile also included `repository.py`, `recovery.py`,
`subagents.py`, `critic.py`, `task_system/service.py`, and `task_system/tools.py`;
all compiled successfully with the shared temporary bytecode cache.

## Task 11: Implement the Linear task-system Adapter

**Implementation status (updated 2026-08-13): Core Adapter path complete; reconciliation
contract has one known gap and manual integration has not run.**
`LinearTaskSystemAdapter` implements one sanitized GraphQL transport boundary,
fixed-Team search, Issue create/get, exact action-marker reconciliation and exact
member reads. Issue descriptions preserve a bounded agent body plus canonical
meeting-evidence and action-key markers; optional assignee, due date, priority and
the server-owned Project are sent only when configured or grounded. The
`disabled|fake|linear` factory is wired into Assistant startup, validates the fixed
Linear Team before services start, keeps the API key in `SecretStr`, and logs only
configuration flags. Disabled mode does not register task tools and Execute mode is
rejected before an external action can be prepared.

Known gap: unknown-create reconciliation currently verifies the configured Team,
exact action-key marker and evidence marker, but does not compare the matched Issue's
normalized title with the Candidate title. Therefore Step 6 is not yet fully satisfied.

**Files:**

- Create: `backend/app/task_system/linear.py`
- Create: `backend/app/task_system/bootstrap.py`
- Modify: `backend/app/settings.py`
- Modify: `.env.example`
- Test: `backend/tests/test_linear_task_adapter.py`

**Step 1: Add positive Linear Adapter test 5**

Add only `test_linear_adapter_search_create_get_happy_path`. Mock successful `httpx` GraphQL envelopes for Team validation, issue search, `issueCreate`, and issue get. Assert the configured Team, title, evidence marker, optional fields, and normalized returned URL/identifier are mapped through the complete path.

**Step 2: Add validated settings**

Add:

```text
TASK_SYSTEM_PROVIDER=disabled|fake|linear
LINEAR_API_URL=https://api.linear.app/graphql
LINEAR_API_KEY=
LINEAR_TEAM_ID=
LINEAR_DEFAULT_PROJECT_ID=
LINEAR_REQUEST_TIMEOUT_SECONDS=10
LINEAR_MAX_SEARCH_RESULTS=20
```

Rules:

- production cannot use `fake`;
- `linear` requires API key and Team ID;
- Project ID is optional but, when configured, is fixed server-side;
- API URL must be HTTPS in production;
- Settings and startup logs expose only configured/unconfigured flags, never the API key.

**Step 3: Implement one GraphQL transport boundary**

Create a private `_execute(document, variables)` method using `httpx.AsyncClient`. It must:

1. POST JSON `{query, variables}` to the configured endpoint;
2. pass `Content-Type: application/json` and `Authorization: <API_KEY>`;
3. enforce the configured timeout;
4. parse HTTP status, then validate the GraphQL envelope;
5. reject non-empty `errors` unless the caller explicitly handles a partial read;
6. truncate and sanitize external messages before raising domain errors.

Keep GraphQL documents as module constants for:

```text
ValidateTeam
SearchIssues
GetIssue
CreateIssue
FindIssueByActionKey
SearchMembers
GetMember
```

**Step 4: Implement normalized search**

Use Linear `issues(first: $limit, filter: ...)` with a mandatory Team relation filter and case-insensitive title containment. Add optional assignee and due-date filters only when supplied. Request no more than `LINEAR_MAX_SEARCH_RESULTS`. Normalize identifier, title, description, priority, due date, assignee, team, project, state and URL.

Do not treat title containment as definitive duplication; `TaskSystemService` remains responsible for normalized duplicate scoring.

**Step 5: Implement create and evidence description**

Use `issueCreate` with configured Team ID. Render description as bounded Markdown:

```text
<agent-generated task description>

---
Matinier meeting evidence: <segment IDs or Package evidence refs>
matinier-action-key: <logical_action_key>
```

Map normalized priority explicitly to Linear's numeric values in one pure function. Do not invent an assignee or due date when absent. A placeholder assignee must never be sent as `assigneeId`; render its original text and unresolved marker in the description instead.

**Step 6: Implement get and reconciliation**

`get` queries by returned Linear issue ID. `reconcile_create` searches the configured Team for the exact action-key marker, then verifies the normalized title and evidence marker. Return:

- found: one matching Issue;
- not_found: no match for this reconciliation attempt; after the configured 1/3/10-second attempts the ToolCall remains unknown and requires user input;
- ambiguous: multiple matches, causing NeedsInput/manual reconciliation.

Never send a second `issueCreate` while the persisted ToolCall remains unknown.

**Step 7: Build the Adapter selection**

`build_task_system_adapter(settings)` returns Disabled, Fake, or Linear implementations. Inject the adapter through `assistant/bootstrap.py`; do not read environment variables inside `LinearTaskSystemAdapter`.

**Step 8: Run positive test 5**

Run from `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linear_task_adapter.py::test_linear_adapter_search_create_get_happy_path -q
```

Expected: one test passes without network access.

Verification completed from `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linear_task_adapter.py::test_linear_adapter_search_create_get_happy_path -q
```

Result: `1 passed in 1.10s`. The one positive test uses `httpx.MockTransport` and
covers `Settings -> build_task_system_adapter`, configured Team validation,
Team-scoped title/assignee/due-date search, `issueCreate`, evidence/action markers,
priority and optional field mapping, and Issue get/read-back. Pytest emitted only
the known non-functional workspace cache permission warning. No additional test or
full suite was run under the agreed minimal-test policy.

**Step 9: Run one explicitly authorized manual integration**

With a dedicated Linear test Team and user approval:

1. configure the API key and Team ID locally;
2. call search for a unique test title;
3. create one Issue through Action Run;
4. read the Issue back and verify title, due date, assignee and evidence marker;
5. open the returned Linear URL;
6. remove the single test Issue manually after recording the demo result, because delete is outside the MVP Adapter.

Do not put this network integration in the default automated test suite.

Manual integration status (2026-08-12): not run. This step requires a dedicated
Linear test Team, credentials and explicit authorization for the single external
Issue creation; none were supplied for this execution turn.

## Task 12: Add the private meeting Assistant UI

**Implementation status (2026-08-12): Complete.** The existing Room Studio now
contains a private Assistant panel without replacing the live-caption workspace.
It exposes explicit Ask and Execute-to-Linear controls, Meeting State freshness,
selectable Action Candidates, manual Final-caption highlights, automatic Mark
accept/dismiss controls and a visible goal-scoped authorization summary. Assistant
state is normalized as `executionsById`, `rootCardsById` and one Session event
cursor, so overlapping Action Runs keep independent status, NeedsInput, cancel and
result controls. Fast-to-slow Handoffs remain under one root card. Unknown external
outcomes show reconciliation without a mutation retry; confirmed Linear results
surface their external URL, and cancellation after a confirmed write states that
the Issue remains.

**Files:**

- Create: `frontend/types/assistant.ts`
- Modify: `frontend/lib/api.ts`
- Create: `frontend/components/private-meeting-assistant.tsx`
- Modify: `frontend/components/room-studio.tsx`
- Modify: `frontend/app/globals.css`

**Step 1: Add exact TypeScript contracts**

Mirror backend response discriminators for execution profile/status, root/parent linkage, session-state snapshot, versioned session events, marks, Meeting State freshness, Grant request, NeedsInput, unknown/reconciling state, cancellation-after-side-effect, and task result. Do not use `any` for payloads rendered in the UI.

**Step 2: Add API functions**

Implement create turn, bootstrap session Assistant state, list session events after the shared cursor, get execution detail, submit versioned/idempotent input, versioned/idempotent cancel, get Meeting State, create mark, and update mark. A timed-out create call must be retryable with the original `client_request_id`.

**Step 3: Build the side panel**

Include:

- private prompt input;
- separate “询问” and “执行到 Linear” submit actions that set `intent_mode` explicitly;
- manual highlight action;
- automatic candidate list with accept/dismiss;
- authorization summary for `task.create` and maximum side effects;
- Fast Turn status and result;
- Action Run background steps, NeedsInput form, cancel, and external task link.

Use normalized `executionsById`, `rootCardsById`, and one `sessionEventCursor`; never use one `activeExecutionId`. Each user request owns a root card. A handed-off Action Run is linked under the source root instead of creating a duplicate card. Different cards independently own progress, result, NeedsInput form and cancel action; submitting a new slow request must not replace or pause existing cards. Apply an event only when its execution `state_version` is newer than the stored version.

The execute control must show target system, capability, selected candidate scope, fixed Team, maximum Issue count, unresolved-identity placeholder policy, and expiration. Clicking it creates one goal-scoped Grant and does not add a second per-step confirmation. Default cards show concise user-facing phases; keep Subagent/Orchestrator details collapsed. `unknown` shows reconciliation without a direct retry action. Cancelling after a confirmed write explicitly states that the existing Linear Issue remains. Do not add standing/session-wide write authorization, TTS, or Assistant publication into the LiveKit room.

**Step 4: Implement adaptive polling**

Poll the single session event feed about every 250ms while any Fast Turn is non-terminal, then about every 2s while only Action Runs remain. Stop when no execution is active. On refresh, call the session-state bootstrap endpoint, install its execution summaries and `snapshot_cursor`, then resume incremental polling. Drain immediately while `has_more=true` before returning to the interval.

**Step 5: Verify**

Run from `frontend`:

```powershell
pnpm run typecheck
pnpm run build
```

Expected: both commands succeed. UI behavior is checked only through the two final manual demo flows.

Verification completed from `frontend`. PowerShell policy blocks the `pnpm.ps1`
shim on this workstation, so the equivalent installed `pnpm.cmd` entry point was
used:

```powershell
pnpm.cmd run typecheck
pnpm.cmd run build
```

Result: strict TypeScript checking succeeded, and Next.js 16.2.10 completed the
optimized production build, TypeScript pass, page-data collection and all three
static-page generations successfully. Per the agreed minimal-test policy, no new UI
test suite was added. The two interactive interview flows remain assigned to the
final manual-demo task.

## Task 13: Bind executions to Frozen Packages and reuse processing jobs

**Files:**

- Create: `backend/alembic/versions/20260812_0022_create_assistant_package_bindings.py`
- Modify: `backend/app/persistence/models.py`
- Create: `backend/app/assistant/package_binding.py`
- Create: `backend/app/assistant/tools/processing_job.py`
- Modify: `backend/app/packages/builder.py`
- Modify: `backend/app/main.py`

**Step 1: Add the binding table and service**

Map Snapshot Segment IDs to effective-source item IDs using each TranscriptItem's `source_segment_ids`. Persist package ID/version/hash and the complete mapping.

**Step 2: Invoke binding after Package freeze**

At `PackageBuilder`'s common validated-freeze return boundary, create bindings for unbound executions of the same Session in the caller's existing API transaction. Binding failure must roll back the new Package request rather than persist a misleading partial link. This common boundary covers baseline, revision, and legacy-script Package creation without duplicating binding calls across routes.

**Step 3: Wrap ProcessingJobRunner as a typed tool**

The tool submits a current processing job, returns pending with job ID, polls through WaitingExternal, and returns the resulting Artifact ID. It must not duplicate workflow implementations.

**Step 4: Verify imports**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app/assistant/package_binding.py app/assistant/tools/processing_job.py app/api/packages.py
```

Expected: compilation succeeds. Existing Package behavior is checked once in the final backend regression.

**Result (2026-08-12):** completed. Migration `20260812_0022` was used because
`20260811_0021` already belongs to the Meeting Mark evidence-hardening migration.
Each execution is bound once to a frozen Package with Package version/hash and a
complete Snapshot Segment ID to effective-source item ID mapping. The common
`PackageBuilder` return boundary keeps Package freeze and binding in one caller-owned
transaction across every current Package creation route.

`processing.generate_artifact` is registered as a typed local-write tool. It resolves
the Package from the current execution binding, delegates submission to the existing
`ProcessingJobRunner`, persists `pending` with the job ID, and reconciles queued or
running jobs until it can return the resulting Artifact ID. Application startup now
starts the processing queue before Assistant recovery can resume a waiting tool call.

Compilation succeeded for the two new modules and all touched integration files. The
repository's existing `__pycache__` directories were not writable in this environment,
so the same compile command used a temporary `PYTHONPYCACHEPREFIX`; source compilation
itself completed successfully. Per the agreed minimal-test policy, Package regression
coverage remains deferred to Task 14's single final backend run.

## Task 14: Observability and interview demo preparation

**Files:**

- Modify: `backend/app/logging.py`
- Modify: `backend/app/api/health.py`
- Modify: `backend/app/settings.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/private-meeting-agent-operations.md`

**Step 1: Add structured lifecycle events**

Log stable IDs and durations for turn accepted, context frozen, handoff committed, action step, tool prepared, tool reconciled, NeedsInput, and terminal result. Never log task descriptions by default.

**Step 2: Add health fields**

Report enabled/disabled, projector freshness, Fast Turn queue depth, Action Run queue depth, oldest queued age, and task adapter availability without making a live third-party request.

**Step 3: Document two interview demos**

Document exact setup and reset instructions for only:

1. realtime captions → private Ask → answer based on Meeting State Head plus current Final tail;
2. select one meeting ActionCandidate → “执行到 Linear” → search/create/get → open the returned Issue URL.

Use a dedicated Linear Team and create at most one real Issue. Include the manual cleanup step. Do not add fault, recovery, or performance demonstrations.

**Step 4: Run the five new positive tests**

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_private_meeting_agent_core.py tests/test_linear_task_adapter.py -q
```

Expected: exactly five new tests pass.

**Step 5: Run one final regression**

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all existing and new backend tests pass.

From `frontend`:

```powershell
pnpm run typecheck
pnpm run build
```

Expected: both commands succeed.

**Step 6: Run migration verification**

From `backend` against a backed-up development database:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
```

Expected: current revision is `20260812_0022` and existing Session, Segment, Package, Revision, Artifact, and ProcessingJob rows remain readable.

**Result (updated 2026-08-13):** implemented with one known telemetry gap. Content-free structured lifecycle telemetry
now covers turn accepted, context frozen, handoff committed, Action Run steps,
tool preparation/reconciliation, NeedsInput and terminal results. Records contain
stable correlation IDs, status and elapsed time; user questions and task descriptions
are not logged.

Fast Turn emits the context-frozen lifecycle event. A directly created Action Run
persists its Snapshot before execution creation but does not yet emit a separate
`assistant_context_frozen` event, so the direct Execute lifecycle is not fully covered.

`GET /health/ready` now returns an `assistant` snapshot with enabled/runtime state,
aggregate Projector freshness, Fast Turn and Action Run queue depth, active Action
Run count, oldest queued age and configuration-only task Adapter availability. The
health request performs no Linear or model-provider call. `.env.example`, README and
`docs/private-meeting-agent-operations.md` document the complete setup and cleanup
for exactly the two approved interview demonstrations, including the one-Issue
Linear limit.

Verification results:

- targeted positive suite: `5 passed`;
- complete backend regression: `314 passed, 1 skipped`;
- frontend `typecheck`: passed;
- Next.js production build: passed, including all three routes;
- migration on a SQLite consistency backup: upgraded from `20260811_0018` to
  `20260812_0022 (head)`;
- pre/post row counts were unchanged: Sessions 17, Segments 43, Packages 3,
  Revisions 2, Artifacts 9 and ProcessingJobs 8.

The real Linear interview click-through is intentionally operator-run from the
documented dedicated Team so automated verification cannot create an unapproved
external Issue. The positive core and MockTransport Linear tests cover the same
search/create/get and single-side-effect path without external writes. Pytest also
reported one dependency deprecation warning and a non-writable cache warning; both
test commands exited successfully.

The two documented manual demo flows have also not been executed as of 2026-08-13.
Accordingly, the verification list above records automated and migration evidence;
it does not satisfy the final manual-demo clause in the Definition of Done.

## Delivery checkpoints

1. **Checkpoint A — Meeting intelligence:** Tasks 1–4 complete; no external action yet.
2. **Checkpoint B — Agent core:** Tasks 5–8 complete; Fast/Slow states and Handoff proven with fakes.
3. **Checkpoint C — Task capability:** Tasks 9–11 core path implemented and verified with Fake/MockTransport; normalized-title reconciliation and dedicated real Linear Team verification remain open.
4. **Checkpoint D — Product integration:** Tasks 12–13 core path implemented; deterministic unresolved-identity UI presentation remains open.
5. **Checkpoint E — Interview-ready:** automated positive suite, full regression, frontend checks and backup migration pass; direct Execute context-frozen telemetry and both manual demo flows remain open.

## Definition of done

- Caption Final persistence/publish path contains no synchronous Agent dependency.
- Fast Turn stays within its configured budget or creates one durable Handoff.
- Durable Action Run state and the documented recovery paths are implemented; this interview MVP does not claim production-level fault validation.
- Every task field produced from the meeting cites Segment evidence or is explicitly supplied by the user/server configuration.
- User-triggered queries remain user-triggered; Projector never invokes external query tools.
- Linear supports search, create, get, and create reconciliation through the platform-neutral TaskSystemAdapter.
- Every external write is covered by a valid ActionGrant and idempotency record.
- Meeting execution history is bound to Frozen Package evidence without mutation.
- Exactly five new positive tests, the existing backend suite, frontend typecheck/build, migration check, and two manual demo flows pass.

**Current DoD assessment (2026-08-13): not fully met.** The final bullet remains open
because the two manual demos have not run. The reconciliation/title, unresolved-identity
UI, and direct Execute context-frozen gaps documented above should also be closed before
claiming the corresponding functional clauses without qualification.
