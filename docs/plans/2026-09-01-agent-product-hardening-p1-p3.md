# Agent Product Hardening P1-P3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 7 个工作日内完善当前私人会议 Agent 已有功能，关闭 README 明确记录的产品缺口，建立覆盖完整执行链的 Trace、可靠性评测、指标结果、诊断和交付证据；不运行 MCP，但为未来会议 Agent 外部工具保留协议无关且仅允许慢 Agent 使用的 Provider 边界。

**Architecture:** 保留现有 LiveKit 媒体链路、Fast Turn / Action Run、Meeting State、ContextSnapshot、Subagents、Critic、ToolRegistry/ToolExecutor、TaskSystemAdapter、Linear、Grant、幂等和 reconciliation。ToolRegistry 增加通用 `ToolProvider` 与执行 profile 策略，未来 MCP adapter 只能注册 `action_run` 工具；Planner 可见性和 ToolExecutor 执行时双重校验，Fast Turn 永远不能发现或调用这些工具。P1 修复三个已知闭环缺口并补故障测试；P2 从现有持久化 execution/step/tool/event 导出 canonical AgentTrace，运行确定性回归并产出可重算指标；P3 加强现有结构化日志、health、CI 和本地音频到 Agent 终态的全链路验收，不引入新的运行基础设施。

**Tech Stack:** 现有 Python 3.12、FastAPI、Pydantic、SQLAlchemy、SQLite、pytest、Next.js 16、React 19、Vitest、LiveKit、Fake/Linear Task Adapter。没有新增生产依赖。

---

## 0. 产品边界

下文 `Backend` 命令的工作目录为 `backend`，`Frontend` 为 `frontend`，`Root` 为项目根目录。

当前项目运行**不需要 MCP**，但允许保留一个不依赖 MCP SDK 的扩展缝：

- Assistant 在 `backend/app/assistant/bootstrap.py` 中把现有 TaskSystem 工具注册到 `ToolRegistry`。
- Linear 通过 `backend/app/task_system/linear.py` 的项目内 Adapter 调用。
- 不可信插件通过 `backend/app/plugins/broker.py` 的 Capability Broker 使用 Host 能力。
- 仓库当前没有 MCP SDK 依赖、MCP 配置或 MCP 运行进程。

因此本计划明确：

- 不安装 MCP SDK，不创建 MCP client/server/bridge、连接配置或后台进程。
- 新增的只是协议无关 `ToolProvider` 和 `allowed_profiles` 契约。未来 `McpToolProvider` 必须作为普通 ToolProvider 接入，且 Host 强制其工具为 `action_run_only`；不能由 MCP server 自报权限或覆盖 profile。
- 不增加向量检索或通用长期 Memory。当前 ContextBuilder 和 Meeting State 已满足本产品的会议内证据输入；只有真实评测证明长会议信息召回不足时，再单独设计检索。
- 不增加 Jaeger、Run Inspector 或全栈 Docker。Trace 是由现有持久状态导出的版本化 JSON artifact，结构化日志和 health 只辅助诊断；这足以支持当前单机产品和离线评测。
- 不更改媒体/SFU 架构，不添加 TTS、电话接入或向会议广播 Agent 回答。

## 1. 七日顺序与 Gate

| 工作日 | 阶段 | 交付物 |
| --- | --- | --- |
| Day 1 | P1 | unknown reconciliation 再校验规范化标题 |
| Day 2 | P1 | 未解析负责人明确提示；直接 Action Run 记录 context_frozen |
| Day 3 | P1 | 慢 Agent 外部工具边界；crash/DB lock/429/5xx/unknown 回归 |
| Day 4 | P2 | 现有执行表导出的 canonical Agent Trace + 12 个产品场景 |
| Day 5 | P2 | 全链路 grader、3 trials、指标基线、实际结果与失败 Trace |
| Day 6 | P3 | 结构化诊断、health 指标、无 secret CI |
| Day 7 | P3 | 一键演示、全量回归、人工验收清单 |

阶段 Gate：

- **P1 Gate**：README 记录的三个细节全部关闭；unknown 结果不会错误认领不同标题的 Linear Issue；action-only Provider 对 Fast Turn 不可见且不可执行；Fast/Action/ToolCall/Claim/Grant 状态节点都有契约或故障边界测试，且外部写在任何恢复路径都不重复。
- **P2 Gate**：12 cases × 3 scripted trials 均生成完整 Trace；Trace 完整性、状态合法性、外部动作 lineage、路由/工具/证据、安全与恢复指标全部计算实际值并给出 PASS/FAIL；未授权写和重复 Linear Issue 均为 0。
- **P3 Gate**：一条命令可演示 Fast Turn、Handoff、Action Run、Linear fake write/reconcile，并生成最终 `summary.json/summary.md/metrics.json/trace-index.jsonl/traces/full-chain-summary/full-chain-traces`；全量测试和硬 Gate 通过；README 只引用报告里的实际结果。

## P1 — 三天关闭现有产品缺口

### Task 1（Day 1）: 强化 Linear unknown reconciliation

**Files:**

- Modify: `backend/app/task_system/service.py`
- Modify: `backend/app/task_system/linear.py`（仅在 Adapter 返回信息不足时修改）
- Modify: `backend/app/task_system/fake.py`
- Modify: `backend/tests/test_linear_task_adapter.py`
- Modify: `backend/tests/test_private_meeting_agent_core.py`

**Steps:**

1. 先写 RED 测试：相同 action key 但规范化标题不同；大小写/首尾空白/连续空白不同但语义标题相同；多个相同 action key；返回结果缺 title；网络响应丢失后 reconcile。
2. `TaskSystemService.reconcile_candidate` 先读取当前 candidate/revision 的预期标题，再调用 adapter 的 `reconcile_create`。只有 action key 匹配且 `_normalized(task.title) == _normalized(expected_title)` 才能确认为成功。
3. 标题不匹配或多个结果返回明确 `ambiguous/unknown`，保留 external claim，不重试 create，不把 candidate 标为 executed。
4. Fake 与 Linear Adapter 对相同输入给出一致语义；日志只记录 action key hash、candidate ID 和错误码，不记录完整任务描述。
5. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_linear_task_adapter.py tests/test_private_meeting_agent_core.py -q
   ```

   预期：退出码 0；“响应丢失但远端已创建”只认领标题一致的唯一 Issue。

### Task 2（Day 2）: 补齐身份提示与 context_frozen 审计事件

**Files:**

- Modify: `backend/app/api/assistant.py`
- Modify: `backend/app/assistant/repository.py`（若需复用统一事件 helper）
- Modify: `backend/app/assistant/plugin_service.py`
- Modify: `backend/app/assistant/plugin_history_view.py`
- Modify: `plugin-sdk/examples/meeting-assistant/meeting_assistant/view.py`
- Create/Modify: `backend/tests/test_assistant_api.py`
- Modify: `backend/tests/test_meeting_plugin_history.py`
- Modify: `backend/tests/test_meeting_plugin_views.py`
- Modify: `frontend/components/room-studio.test.tsx`（只验证通用插件 UI 能显示新提示）

**Steps:**

1. 先写后端 RED 测试：直接创建 Action Run 后，`execution.created` 之外存在单独 `action.context_frozen` 事件，payload 含 snapshot ID、meeting state version 和 evidence count；client request 重放不重复追加事件。
2. snapshot、Grant 和 execution 在同一事务完成后追加 `action.context_frozen`；事件不包含用户原文或完整证据正文。
3. Meeting plugin 当前视图和 Host history 都从 candidate/result 的结构化 `is_placeholder`、spoken text、resolution 与 partial 状态生成提示，不通过解析英文 error message 判断。
4. 声明式插件视图固定显示：“负责人未能映射到 Linear 用户，已把会议中的原称呼保留在 Issue 描述；请在 Linear 中确认负责人。”已解析负责人不显示提示。通用前端继续只渲染受支持的 paragraph/error-state 组件，不增加会议专用分支。
5. 测试当前视图、历史视图和 plugin-disabled 路径，确保查看提示不启动分析、不重连音频、不创建 Grant。
6. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_assistant_api.py tests/test_meeting_plugin_history.py tests/test_meeting_plugin_views.py tests/test_private_meeting_agent_core.py -q

   # Frontend
   pnpm exec vitest run components/room-studio.test.tsx
   pnpm run typecheck
   ```

   预期：全部退出码 0。

### Task 3（Day 3）: 预留慢 Agent 工具 Provider 边界并验证恢复合同

**Files:**

- Create: `backend/app/assistant/tools/provider.py`
- Modify: `backend/app/assistant/tools/contracts.py`
- Modify: `backend/app/assistant/tools/registry.py`
- Modify: `backend/app/assistant/planner.py`
- Modify: `backend/app/assistant/fast_runner.py`
- Modify: `backend/app/assistant/action_runner.py`
- Modify: `backend/app/assistant/tools/executor.py`
- Modify: `backend/app/assistant/state_machine.py`
- Modify: `backend/app/assistant/repository.py`
- Modify: `backend/app/task_system/tools.py`
- Modify: `docs/architecture.md`
- Create: `backend/tests/test_tool_profile_policy.py`
- Create: `backend/tests/test_execution_state_failure_policy.py`
- Create: `backend/tests/test_tool_call_state_machine.py`
- Create: `backend/tests/test_assistant_failure_recovery.py`
- Modify: `backend/tests/test_meeting_plugin_operations.py`
- Modify: `backend/tests/meeting_plugin_fakes.py`
- Modify: `backend/app/assistant/recovery.py`（仅修复测试暴露的问题）
- Modify: `backend/app/assistant/action_scheduler.py`（仅修复测试暴露的问题）

**Steps:**

1. 定义协议无关 `ToolProvider`，只负责返回有界 `ToolRegistration`；不出现 MCP import、transport、URL、command 或 server config。现有 TaskSystem tools 通过同一 Provider 注册，证明扩展缝真实可用，而不是未使用的空接口。
2. 为 `ToolSpec` 增加 `allowed_profiles: frozenset[ExecutionProfile]`，默认兼容当前工具；external-write 默认只允许 `action_run`。未来外部/MCP Provider 的 Host policy 必须覆盖为 `{"action_run"}`，不能信任 Provider 自报更宽范围。
3. 防线至少两层：FastTurnRunner 构建 available tools 时过滤；ToolExecutor 从 execution record 读取真实 profile 后再次拒绝。即使恶意/错误 planner 手工构造调用，也返回 `tool_profile_denied` 且 adapter 调用次数为 0。
4. 把当前 ToolCall 的持久状态枚举补成正式迁移合同：从实际 execute/reconcile/recovery 路径定义 `TOOL_CALL_TRANSITIONS`，由 repository 统一校验，并使用 expected status 条件更新避免并发覆盖；`succeeded/failed` 保持终态，非法倒退或跳转必须失败。同时为 Subagent 增加 `queued→running→terminal`（以及只读恢复 `running→queued`）合同；Grant 继续由额度感知的 consume/release/revoke/expire 方法管理，但补 expected-status/CAS 和终态保护，不提供绕过额度的通用状态 setter。
5. Action Run 保持 timeout、取消和并发预算。所谓“避免阻塞”是让慢工具不进入 Fast Turn 的延迟预算；adapter 仍必须使用异步 I/O 或受控线程，不能在 event loop 中执行阻塞网络调用。
6. 按下述“逐节点失败矩阵”先写 RED 测试，再做最小修复。所有故障测试遵守五条统一不变量：
   - 只有持久状态能证明尚未发出外部请求（如 `prepared/reserved`）或操作本身只读且幂等时，才允许自动重放。
   - 外部写的线性化边界是 Grant 配额消耗、ToolCall=`requesting`、Claim=`requesting` 在同一短事务提交完成；数据库 Session 不跨越 adapter `await`。
   - 进入 `requesting/unknown` 后禁止再次调用 create，只能以原 logical action key 和原参数进行只读 reconciliation。
   - 终态不可回退；重复 request、重复 enqueue、恢复器重跑都必须返回同一持久结果。
   - 每个集成测试固定断言 `execution status + lifecycle event + adapter call count + external reference count`，涉及写入时再断言 Grant/Claim。

#### Fast Turn 与 Handoff 逐节点失败矩阵

| 节点 | 故障注入 | 应对方案 | 简要测试 |
| --- | --- | --- | --- |
| `received` | operation worker 在首次状态迁移前退出，或迁移事务遇到 DB lock | execution 保持 `received`，operation 可回到 `accepted`；尚未调用模型或工具，可以安全重新领取 | `test_received_requeues_before_any_model_call`：恢复两次后只执行一次 planner，事件只有一条 `contextualizing` |
| `contextualizing` | ContextBuilder 抛错、快照持久化失败或 state version 冲突 | 当前 Fast Turn 进入 `failed`，不生成 Handoff、Grant 或 ToolCall；用户可发起新 turn | `test_contextualizing_failure_has_no_partial_snapshot_or_tool_call`：事务回滚后 snapshot/tool/handoff 均为 0 |
| `deciding` | 模型 timeout、异常或 worker 在模型结果未持久化时退出 | timeout 且允许 Handoff 时原子转慢路径；未允许则 `failed`。进程中断后的未知模型结果不自动重放，记录 `model_outcome_unknown` | `test_deciding_timeout_handoffs_only_when_authorized`；`test_interrupted_fast_model_is_not_replayed` |
| `executing_reads` | 一个只读工具 timeout/500，或批处理中单个工具失败 | 将失败持久化为 observation，在剩余 Fast budget 内继续决策；不得出现 external-write ToolCall。进程中断则保守失败，不重放不确定的 local-write | `test_fast_read_failure_becomes_observation_and_other_read_completes`：一个失败、一个成功，Fast Turn 仍可响应，外部写次数 0 |
| `responding` | 生成答案后、terminal commit 前 DB lock/进程退出 | 不返回伪 `completed`；恢复时标记 `model_outcome_unknown/failed`，不再次调用模型，客户端可新建 turn | `test_response_commit_failure_never_returns_empty_completed_result` |
| `handed_off` | Handoff 已提交但 enqueue 抛错 | Fast Turn 保持终态；目标 Action Run 保持唯一 `queued`，startup recovery 再入队 | `test_handoff_commit_survives_enqueue_failure`：重放 Handoff 返回同一 target，目标数 1 |
| `completed/failed/cancelled` | 重复 client request 或 worker 再次领取 | 直接返回持久结果，planner、tool、handoff 调用次数均不增加 | 参数化 `test_fast_terminal_state_is_immutable_and_replay_safe` |
| Handoff 提交前 | snapshot/Grant/target/handoff 任一步骤失败 | 整个事务回滚；source 不得进入 `handed_off`，不留下孤立 target、Grant 或 envelope | `test_handoff_precommit_failure_rolls_back_all_children` |
| Handoff 并发重放 | 两个 worker 同时对同一 source 提交 | 由 source version/唯一 source handoff 约束只产生一个 target；失败方读取既有 commit | `test_concurrent_handoff_creates_exactly_one_action_run` |

#### Action Run 逐节点失败矩阵

| 节点 | 故障注入 | 应对方案 | 简要测试 |
| --- | --- | --- | --- |
| `queued` | DB lock 或两个 scheduler 同时领取 | 保持 `queued` 并重试领取；CAS 只允许一个 worker进入 `planning` | `test_two_workers_claim_one_queued_action_once` |
| `planning` | subagent/critic/planner timeout、无效结构输出或进程退出 | 已返回的显式错误进入 `failed`；预算耗尽进入 `partial`。恢复只能依据持久 step/budget 继续；受计费 guard 保护的未知模型调用不得自动重复计费 | `test_planning_error_fails_without_tool_call`；`test_planning_budget_exhaustion_is_partial`；复用 operation billing recovery 测试 |
| `executing` | ToolCall 提交前 DB lock；read tool 429/500；external write timeout/响应丢失 | 提交前失败时 adapter 次数 0；只读失败形成 observation 后进入 `observing`；外部写不确定时进入 `reconciling`，禁止 create retry | `test_db_lock_before_requesting_never_calls_adapter`；`test_read_failure_replans`；`test_lost_write_response_enters_reconciling_without_second_create` |
| `observing` | ToolResult 已持久化但 execution 状态迁移前退出 | recovery 复用已存 ToolCall/observation，按合法边回到 `queued/planning`；不得再次调用 adapter | `test_crash_after_tool_result_reuses_observation_without_reinvoke` |
| `waiting_external` | async job 仍 pending、回调丢失、恢复期间取消 | pending 时保持等待；可 reconciliation 时查询；取消后停止 planner 和新工具调用 | `test_pending_job_stays_waiting_until_reconciled`；`test_cancel_while_waiting_prevents_new_work` |
| `reconciling` | reconcile timeout/500、匹配结果为 0/多条、用户撤销授权 | 唯一匹配则 `observing→planning`；仍未知或歧义则 `needs_input`；撤权后仍允许只读核对既有请求，但禁止新写 | `test_reconcile_unique_match_resumes_planning`；`test_reconcile_ambiguous_requires_input`；复用 revoke/unknown 测试 |
| `needs_input` | scheduler 重复唤醒、重复用户输入、旧 state version 输入 | 无显式输入时保持静止；同一输入只产生一次 resume event；旧版本返回 conflict，不执行 planner/tool | `test_needs_input_resumes_once_with_expected_version` |
| `completed/partial/failed/cancelled` | scheduler/recovery 重跑 | 直接返回持久结果，不新增 step、model call、ToolCall 或 event | 参数化 `test_action_terminal_state_is_immutable_and_replay_safe` |

#### ToolCall、ExternalActionClaim 与 Grant 逐节点失败矩阵

| 链路节点 | 故障注入 | 应对方案 | 简要测试 |
| --- | --- | --- | --- |
| ToolCall `prepared` / Claim `reserved` | 进程在远端调用前退出 | 这是唯一可证明请求未发出的写入阶段；恢复复用同一 call、claim、arguments hash 与 idempotency key，不新建第二份记录 | `test_prepared_external_call_reuses_same_claim_on_recovery` |
| ToolCall/Claim `requesting` | 远端可能已收到请求时断连 | ToolCall 转 `unknown`，Claim 转/保留 `unknown`；create 调用次数固定为 1，随后只调用 reconcile | `test_requesting_crash_never_retries_create` |
| ToolCall `pending` | 异步任务未完成或查询超时 | 保持 `pending/waiting_external`；下次只调用 reconcile | `test_pending_call_reconciles_without_execute` |
| ToolCall/Claim `unknown` | reconcile 返回 0 条、多条、标题不符或继续超时 | 0 条/超时保持 unknown，达到预算后 `needs_input`；多条/标题不符视为歧义；不能据此重新 create | 参数化 `test_unknown_reconciliation_never_executes_create` |
| ToolCall `reconciling` | 两个 recovery worker 并发核对 | expected-status CAS 只允许一个状态写入；重复只读查询可以发生，但只能得到一个 terminal result/reference | `test_concurrent_reconcilers_commit_one_terminal_result` |
| ToolCall `succeeded/failed`；Claim `succeeded/failed_safe` | 迟到响应、重复持久化、恢复器重跑 | 终态不可回退，不覆盖原 result/reference，不增加 attempt 或 Grant 使用量 | 参数化 `test_tool_and_claim_terminal_states_are_immutable` |
| Grant `active` | 调用前过期、撤销、scope/candidate/capability 不匹配，或两个写竞争最后一个额度 | 最终执行门拒绝，adapter 次数 0；CAS 只让一个调用消耗额度 | `test_expired_or_revoked_grant_blocks_adapter`；`test_one_remaining_grant_slot_has_one_winner` |
| Grant `consumed` | 响应丢失或用户随后撤权 | 不恢复额度并重试 create；由 Claim/ToolCall reconciliation 决定结果。只有确认 `confirmed_side_effects == 0` 时才可释放预留额度 | `test_consumed_grant_with_unknown_outcome_only_reconciles`；`test_confirmed_zero_side_effect_releases_slot_once` |
| Grant `revoked/expired` | recovery 或 planner 再次尝试写 | 保持终态，禁止新写；若已有 requesting/unknown，只允许只读 reconciliation | 参数化 `test_inactive_grant_allows_reconcile_but_blocks_execute` |

#### Subagent 与 Host operation 辅助链

| 节点 | 应对方案 | 简要测试 |
| --- | --- | --- |
| Subagent `queued/running` | queued 重复领取只运行一次；只读 running 分支在进程重启后可重置为 queued，并复用相同 branch ID/budget | `test_running_read_subagent_requeues_with_same_identity` |
| Subagent `completed/failed/cancelled` | 终态不重跑；失败以 observation 反馈主 Agent，由主 Agent 决定降级、继续或 partial | 参数化 `test_subagent_terminal_state_is_immutable` |
| Operation `accepted/running` | accepted 可安全重新领取；running 恢复时先同步 execution。Fast 已越过 received 且结果未知则失败而不重放模型；Action 仅按其持久状态恢复 | 扩充 `test_durable_queue_recovers_without_memory_enqueue_and_replay_does_not_rerun` 和 `test_slow_model_holds_no_database_transaction_and_crash_does_not_repeat_billing` |
| Operation `completed/failed/cancelled` | worker 重跑只同步既有结果，不重新 dispatch | `test_terminal_operation_is_not_dispatched_twice` |

7. 在 `test_execution_state_failure_policy.py` 参数化遍历 Fast/Action 的全部声明边：所有合法边成功，所有未声明边和终态回退抛 `ValueError`；在 `test_tool_call_state_machine.py` 对 ToolCall/Claim 做同样的全矩阵测试。集成测试不重复验证每一条纯枚举边，只验证上表的事务和异步边界。
8. 使用 fake clock、`asyncio.Event`、可编程 fault injector 和临时 SQLite；不得通过扩大 timeout 或真实 `sleep` 修测试。429/500/timeout、DB lock、响应丢失和并发顺序全部由事件闸门控制。
9. 在 `docs/architecture.md` 记录未来 MCP adapter 的映射点：discovery/schema validation 属于 adapter，profile/effect/Grant/idempotency/reconciliation 属于 Host。当前不实现 discovery 或连接。
10. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_tool_profile_policy.py tests/test_execution_state_failure_policy.py tests/test_tool_call_state_machine.py tests/test_assistant_failure_recovery.py tests/test_meeting_plugin_operations.py tests/test_private_meeting_agent_core.py tests/test_meeting_plugin_execution_guard.py -q
   ```

   预期：退出码 0，P1 Gate 达成。

## P2 — 两天给现有功能建立评测证据

### Task 4（Day 4）: 导出并校验 canonical Agent 全链路 Trace

**Files:**

- Create: `backend/app/assistant/trace.py`
- Modify: `backend/app/assistant/repository.py`
- Create: `backend/tests/test_assistant_trace.py`
- Create: `backend/app/evals/__init__.py`
- Create: `backend/app/evals/contracts.py`
- Create: `backend/app/evals/trace_validator.py`
- Create: `backend/evals/cases/meeting_agent_product_v1.jsonl`
- Create: `backend/tests/test_agent_eval_contracts.py`
- Create: `backend/tests/test_agent_trace_validator.py`

**Steps:**

1. 定义版本化 `AgentTrace`。Trace 边界从已经持久化的会议输入锚点开始：`session_id/media_session_id`、caption/mark/candidate 的 ID 与 revision；随后覆盖 ContextSnapshot、Fast Turn、Handoff、Action Run、Subagent/Critic/Planner、ToolCall、Grant、ExternalActionClaim、reconciliation 和终态。原始 RTP/PCM、完整字幕、prompt 和工具 secret 不复制进 Trace，只保留 ID、hash、长度、计数、状态与时间。
2. 每个 trial 只有一个 `trace_id == root_execution_id`。节点至少包含 `input_anchor/context/execution/model_stage/subagent/handoff/tool/grant/claim/recovery/terminal`；边至少包含 `derived_from/parent_of/handed_off_to/invoked/authorized_by/claimed_by/reconciled_to/terminated_as`。节点和事件按 `(timestamp, state_version, id)` 稳定排序。
3. Trace 必须能够从最终结果反向追到输入证据：terminal → ToolResult/response claim → ToolCall 或 model stage → execution → ContextSnapshot → evidence revision；每次 external write 还必须连接 Grant、candidate、Claim、logical action key、arguments hash、idempotency key 和 external reference。
4. `trace_validator.py` 不读取自然语言日志，直接验证：唯一 root、无孤儿节点、root/parent 闭合、state version 连续且单调、状态边合法、Handoff 双端配对、终态唯一、ToolCall 属于正确 execution、外部写 lineage 完整、unknown 无 create retry、敏感字段扫描为 0。任何结构错误列出 `trace_id + node_id + error_code`。
5. 建立 12 个直接对应产品行为的 case：Fast 回答、Fast tail、Fast 不可见 action-only tool、Handoff 后慢 Agent 调用工具、Action 正常执行、缺负责人、用户拒绝、重复 request、prompt injection、429/5xx、unknown reconcile、并发相同 action。每个 case 声明 expected route、terminal status、evidence/tool/side-effect 上限和必须出现的 Trace node/edge。
6. 契约拒绝重复 case ID、未知 evidence、非法故障类型、external write 缺 side-effect 上限，以及无法从输入 evidence 连到期望结果的 case。
7. 用一条正常 Fast Trace、一条 Fast→Slow→Tool Trace、一条 unknown→reconcile Trace 做 golden tests；再分别破坏 parent、state version、Grant edge、evidence edge、terminal 和 redaction，断言 validator 给出稳定错误码。
8. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_assistant_trace.py tests/test_agent_trace_validator.py tests/test_agent_eval_contracts.py -q
   ```

   预期：PASS；suite 恰好 12 个 case；三条 golden Trace 均可从 terminal 回溯到 evidence，六类损坏 Trace 均被拒绝，敏感内容扫描为 0。

### Task 5（Day 5）: ScenarioRunner、确定性 grader 与产品基线

**Files:**

- Create: `backend/app/evals/fakes.py`
- Create: `backend/app/evals/scenario_runner.py`
- Create: `backend/app/evals/graders.py`
- Create: `backend/app/evals/metrics.py`
- Create: `backend/app/evals/report.py`
- Create: `backend/app/evals/cli.py`
- Create: `backend/tests/test_agent_eval_runner.py`
- Create: `backend/tests/test_agent_eval_graders.py`
- Create: `backend/tests/test_agent_eval_metrics.py`
- Create: `backend/tests/test_agent_eval_cli.py`
- Generated: `reports/meeting-agent-product-v1/`

**Steps:**

1. ScenarioRunner 使用真实临时数据库、Assistant runtime、HandoffService、Action scheduler、ToolExecutor 和 recovery；只替换结构化模型、task adapter、clock 和 UUID。每个 trial 完成或停止在预期 `needs_input` 后，必须先导出并验证一条 AgentTrace，grader 才能计算分数。
2. grader 只读取结构化 case expectation、terminal result 和已验证 Trace，不解析自然语言日志猜状态。每个指标同时输出 `actual`、`target`、`unit`、`sample_count`、`scope`、`result=PASS|FAIL|N/A`；聚合报告必须能从 `traces/` 重算，禁止手填数字。
3. 指标与 Gate 固定如下：

| 类别 | 指标与计算 | P2 scripted Gate | 结果字段 |
| --- | --- | --- | --- |
| Trace | `trace_export_rate = exported_trials / total_trials` | `100%` | `trace.export_rate` |
| Trace | `trace_integrity_pass_rate = valid_traces / exported_traces` | `100%` | `trace.integrity_pass_rate` |
| Trace | orphan node、非法 transition、state-version gap、多个 terminal 的总数 | 每项 `0` | `trace.orphan_nodes`, `trace.illegal_transitions`, `trace.version_gaps`, `trace.multiple_terminals` |
| Trace | `external_lineage_complete_rate = writes with evidence+execution+grant+claim+idempotency+reference / writes` | `100%`；无写 case 记 `N/A`，不混入分母 | `trace.external_lineage_complete_rate` |
| 路由 | Fast/Slow/Handoff/NeedsInput route 与 case expectation 一致的 trial 比例 | `100%` | `quality.route_accuracy` |
| 工具 | tool name/version/effect 选择正确率；参数按 case 声明的结构化 subset 比较 | 两项均 `100%` | `quality.tool_selection_accuracy`, `quality.tool_argument_accuracy` |
| 证据 | factual claim 的 evidence coverage；未知 evidence 和 unsupported claim 数 | coverage `100%`，两个计数 `0` | `quality.evidence_coverage`, `safety.unknown_evidence_count`, `safety.unsupported_claim_count` |
| 安全 | 未授权外部写、Fast 暴露/执行 action-only tool、超 Grant side-effect budget | 每项 `0` | `safety.unauthorized_writes`, `safety.fast_tool_policy_violations`, `safety.grant_budget_violations` |
| 幂等 | 重复 external side effect、unknown 后直接 create retry、终态被修改 | 每项 `0` | `reliability.duplicate_side_effects`, `reliability.unknown_create_retries`, `reliability.terminal_mutations` |
| 恢复 | `recovery_convergence_rate = fault trials reaching expected terminal/needs_input without duplicate write / fault trials` | `100%` | `reliability.recovery_convergence_rate` |
| 场景 | `scenario_pass_rate = trials passing every case assertion / total_trials` | `100%`，即 `36/36` | `quality.scenario_pass_rate` |
| 效率 | Fast/Action/Handoff/Reconcile 的 p50/p95 wall time；model calls、planning rounds、tool attempts 的 mean/p95 | 必须输出，无绝对 PASS 阈值；`scope=scripted_local`，仅作回归基线 | `efficiency.*` |
| 成本 | input/output token 与估算成本 | provider 提供可靠 usage 时输出；否则 `actual=N/A, result=N/A, reason=provider_usage_unavailable` | `cost.*` |
| 隐私 | Trace/报告敏感字段命中数 | `0` | `safety.sensitive_value_hits` |

4. 结果分级固定：任何 safety、Trace integrity 或 duplicate-side-effect Gate 失败时 `overall_result=FAIL_SAFE`、CLI 退出码 2；其余质量/恢复 Gate 失败时 `overall_result=FAIL_QUALITY`、退出码 1；全部硬 Gate 通过时 `overall_result=PASS`、退出码 0。效率和 provider usage 为观察指标，不得单独把结果变为 PASS，也不得把 `N/A` 当 0。
5. 输出目录必须包含：
   - `manifest.json`：suite hash、代码/配置标识、provider=`scripted`、seed、trial 数和生成时间。
   - `summary.json`：overall result、每个 Gate 的 target/actual/result、失败 case ID。
   - `summary.md`：与 JSON 同值的人类可读表格，明确区分 scripted 指标与真实 provider 指标。
   - `metrics.json`：逐 trial 原始指标和聚合 p50/p95。
   - `trace-index.jsonl`：case/trial/trace/root/terminal/result/path 索引。
   - `traces/<case-id>/trial-<n>.json`：36 条完整且已脱敏的 AgentTrace。
   - `failures/<case-id>-trial-<n>.json`：失败 grader、错误码、相关 node IDs；通过时目录为空。
6. `test_agent_eval_metrics.py` 用手工构造的三条 Trace 验证分母、N/A、p50/p95 和 Gate 优先级；`test_agent_eval_cli.py` 删除一个 Grant edge、制造重复 reference、注入一个普通质量错误，分别断言退出码 2、2、1，并核对 Markdown/JSON 数字一致。
7. 运行 12 cases × 3 trials：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_agent_eval_runner.py tests/test_agent_eval_graders.py tests/test_agent_eval_metrics.py tests/test_agent_eval_cli.py -q
   .\.venv\Scripts\python.exe -m app.evals.cli --suite evals/cases/meeting_agent_product_v1.jsonl --provider scripted --trials 3 --seed 20260901 --require-complete-trace --output-dir ../reports/meeting-agent-product-v1
   ```

   预期：36 trials/36 traces。命令完成后才记录实际结果；目标是 `overall_result=PASS`、scenario/route/tool/evidence/recovery/Trace 完整率均为 `100%`，所有安全与重复副作用计数均为 `0`。若实际不满足，保留 FAIL 报告和失败 Trace，不修改报告伪装通过。

## P3 — 两天完成运行诊断和交付

### Task 6（Day 6）: 加强现有结构化日志、health 与 CI

**Files:**

- Modify: `backend/app/logging.py`
- Modify: `backend/app/api/health.py`
- Modify: `backend/app/assistant/fast_runner.py`
- Modify: `backend/app/assistant/action_runner.py`
- Modify: `backend/app/assistant/tools/executor.py`
- Create: `backend/tests/test_assistant_diagnostics.py`
- Modify: `backend/tests/test_health.py`
- Create: `.github/workflows/agent-ci.yml`

**Steps:**

1. 不引入 OpenTelemetry。canonical Trace 来自持久状态；现有 JSON log 只作为运行诊断，增加稳定的 `trace_id/root_execution_id`、`execution_id`、`phase`、`tool_call_id`、`duration_ms`、`retry_count` 和 `error_code`，使日志可跳转到对应 Trace；继续禁止原始 goal/prompt/tool secret。
2. health 增加聚合字段：active Fast/Action 数、oldest queued age、unknown/reconciling tool calls、recovery backlog、task adapter availability。readiness 不调用 Linear 或模型。
3. 测试 execution 的日志关联完整，异常也有 terminal record；任意日志里的 trace/execution/tool ID 都能在导出 Trace 中找到；敏感字段扫描为 0；health 查询不改变执行状态。
4. CI 只做项目真实依赖：backend 全量 pytest、12-case × 1 trial scripted Trace smoke、frontend test/typecheck/build。无 secret、无真实 Linear、无 Docker/MCP/RAG job；包含 `test_tool_profile_policy.py`，防止以后把慢工具误暴露给 Fast Turn。CI smoke 必须生成 12 条 Trace、通过全部硬 Gate，并上传 `summary.json/summary.md/metrics.json/trace-index.jsonl/traces/`；任一 Trace 缺失或不合法都使 job 失败。
5. 本地等价验证：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_assistant_diagnostics.py tests/test_health.py -q
   .\.venv\Scripts\python.exe -m app.evals.cli --suite evals/cases/meeting_agent_product_v1.jsonl --provider scripted --trials 1 --seed 20260901 --require-complete-trace --output-dir ../reports/ci-smoke

   # Frontend
   pnpm test
   pnpm run typecheck
   pnpm run build
   ```

   预期：全部退出码 0；`reports/ci-smoke/summary.json` 为 `overall_result=PASS`，Trace 数为 12，实际指标、target 和 Gate result 均非缺失。

6. 当前工作区不是有效 Git repository；workflow 文件和本地验证可完成，但只有恢复/初始化 Git 并看到真实远程 run 成功后才写“CI passing”。

### Task 7（Day 7）: 一键现有功能演示与最终验收

**Files:**

- Create: `scripts/run_meeting_agent_demo.py`
- Create: `scripts/run_meeting_agent_demo.ps1`
- Create: `scripts/run_meeting_agent_full_chain_eval.py`
- Create: `meeting_agent_demo.cmd`
- Create: `backend/tests/test_meeting_agent_demo.py`
- Create: `backend/tests/test_meeting_agent_full_chain_eval.py`
- Modify: `docs/private-meeting-agent-operations.md`
- Create: `docs/meeting-agent-case-study.md`
- Create: `docs/agent-product-release-checklist.md`
- Modify: `README.md`
- Modify: `docs/stage-records.md`

**Steps:**

1. 离线一键 demo 只使用现有功能：
   1. Fast Turn 依据最新 evidence 回答。
   2. 复杂目标触发 Fast→Slow Handoff。
   3. Action Run 使用 subagents/critic 和 fake Linear。
   4. 未解析负责人产生明确 partial 提示。
   5. Linear 响应丢失后按 action key + 规范化标题 reconcile，不重复创建。
2. demo 使用临时数据库、scripted provider 和 FakeTaskAdapter，不要求云 key、真实 Linear 或正在运行的 LiveKit。每个演示场景输出 AgentTrace，并在终端打印 `trace_id、terminal status、关键指标结果、trace path`。
3. `run_meeting_agent_full_chain_eval.py` 增加两个有界全链路场景：正常执行、Linear 成功但响应丢失后 reconcile。链路固定为 `backend/tests/fixtures/demo_audio.wav → 现有 decoder/音频帧消费 → fake ASR Partial/Final → Segment/Meeting State/evidence → ContextSnapshot → Fast/Handoff → Action/Subagent/Critic → Fake Linear ToolCall/Grant/Claim → terminal`。只替换 ASR、结构化模型和任务系统三个外部 provider；不得绕过项目的 Reconciler、数据库、ContextBuilder、Runner、ToolExecutor 或 recovery。
4. 全链路结果写入 `reports/meeting-agent-product-v1/full-chain-summary.json`、`full-chain-summary.md` 和 `full-chain-traces/`。必须记录并给出 actual/target/result：
   - 输入：decoded frame count `>0`、audio duration `>0`、dropped frames `=0`。
   - 字幕：Final count `>=1`、provider error count `=0`、Final→evidence 转换率 `100%`。
   - 连接：`evidence_to_agent_trace_join_rate=100%`，即每个 Agent factual claim 可追到本次音频产生的 Final segment/revision。
   - Agent：两个场景 route/terminal 正确率 `100%`，Trace integrity `100%`。
   - 外部动作：unauthorized/duplicate/unknown-direct-retry 均为 `0`；丢响应场景 create 次数 `1` 且 reconcile 次数 `>=1`。
   - 时延：audio→first Partial、audio→Final、Final→snapshot、snapshot→Fast terminal/Handoff、Handoff→Action terminal、reconcile duration 输出实际毫秒值；`scope=local_media_diagnostic`，只作回归观察，不作为真实 WebRTC/云 provider SLA。
5. `meeting_agent_demo.cmd` 一条命令依次运行 12 cases × 3 trials Agent Eval、两个 full-chain local-media 场景和离线 demo，并生成全部结果/Trace；重复运行不得产生重复副作用。命令最后打印实际 `overall_result`、硬 Gate 通过数/总数、36 条 Agent Trace 路径、2 条 full-chain Trace 路径；任一硬 Gate 失败返回非零。
6. 运行全量验证：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest -q

   # Frontend
   pnpm test
   pnpm run typecheck
   pnpm run build

   # Root
   .\meeting_agent_demo.cmd
   ```

7. 人工验收清单分开记录，不与上述 scripted/local-media 指标混算：
   - 本地浏览器标签页音频经 LiveKit/WebRTC 到字幕的持续性，并记录 session/media_session ID，使其能与 AgentTrace 的输入 anchor 对照。
   - 两条已准备的面试演示。
   - 真实 Linear 专用 Team 的一次 create/reconcile/cleanup。此项会产生真实外部写，只有用户明确授权并提供专用 Team 后执行。
8. README 删除已经修复的限制，保留未验证项；case study 的所有数字都引用 `reports/meeting-agent-product-v1/summary.json` 或 `full-chain-summary.json`，并紧邻数字标注 `scripted_local`、`local_media_diagnostic` 或 `real_provider` scope。没有报告路径和 metric key 的数字不得写入 README/简历。

## 2. Definition of Done

- [ ] reconciliation 同时核对 action key 与规范化标题，未知结果不直接重试 create。
- [ ] unresolved assignee 在 Host UI 有固定、可测试的提示。
- [ ] 直接 Action Run 有独立 `action.context_frozen` 事件，重放不重复。
- [ ] crash、DB lock、429、5xx、unknown、cancel/expired Grant 和并发相同 action 测试通过。
- [ ] 协议无关 ToolProvider 已被现有 TaskSystem 使用；action-only tool 对 Fast Turn 不可见且执行层拒绝伪造调用。
- [ ] Execution、ToolCall、Subagent 与 ExternalActionClaim 都由集中迁移合同约束；Grant 由额度感知的条件更新约束；终态不可回退，并发更新不会静默覆盖。
- [ ] Fast Turn、Handoff、Action Run、ToolCall/Claim/Grant 和辅助运行链的每个状态节点，都至少映射到一个参数化契约测试或一个事务/异步故障测试。
- [ ] 自动重放只发生在可证明未发出外部请求或只读幂等的节点；`requesting/unknown` 路径的 external create 调用次数恒为 1。
- [ ] 故障测试使用 fake clock、事件闸门和 fault injector，不依赖真实网络、真实 Linear、扩大 timeout 或非零 `sleep`。
- [ ] 12 个产品场景 × 3 trials 生成 36 条完整 AgentTrace；scenario/route/tool/evidence/recovery/Trace 指标都有 actual、target、sample count、scope 和 PASS/FAIL，未授权写和重复 Issue 为 0。
- [ ] 每条 Trace 从 terminal 闭合到 execution/Handoff/ToolCall/Grant/Claim 和输入 evidence revision；无孤儿节点、非法状态边、version gap、多个 terminal 或敏感正文。
- [ ] 两个 local-media full-chain 场景从 `demo_audio.wav` 运行到 Agent terminal；Final→evidence→AgentTrace join 为 100%，丢响应场景只 create 一次并完成 reconciliation。
- [ ] `summary.json/summary.md/metrics.json/trace-index.jsonl/traces/full-chain-summary` 均存在且可由 Trace 重算；失败时保留失败结果和相关 node IDs。
- [ ] 结构化日志和 health 能按 trace/execution ID 定位失败；scripted/local-media/real-provider 指标 scope 分离，未知 token/cost 显式为 N/A。
- [ ] Backend 全量 pytest、Frontend test/typecheck/build、离线 demo 全部通过。
- [ ] README、运维文档、案例文档与实际报告一致。
- [ ] 未添加 MCP SDK/runtime、RAG、Memory、OTel、Jaeger 或新的生产服务；仅保留 Host 控制的慢 Agent ToolProvider 扩展缝。

## 3. 完成后适合写进简历的内容

不要写未实现的 MCP/RAG。围绕项目实际能力表述：

- 设计 Fast Turn / Action Run 双路径会议 Agent，使用不可变 ContextSnapshot 和持久 HandoffEnvelope 在低延迟问答与可恢复任务执行间切换，并将外部工具限制在慢路径。
- 通过 ActionGrant、稳定 action key、幂等 ToolCall 和 title-aware reconciliation，防止未授权或重复创建 Linear Issue。
- 建立基于真实持久化执行链的 12-case × 3 trials Agent Eval，导出从 meeting evidence、Fast/Slow Handoff、Subagent 到 Tool/Grant/reconciliation/terminal 的可回放 Trace，并对路由、证据、工具、安全、恢复和副作用指标给出可重算结果。
- 完善结构化诊断、health、CI 与一键离线 demo，使 Agent 故障能够按 execution/tool/evidence 链路定位和复现。

### 关于“A2A”的简历措辞

当前实现值得重点呈现，但不要写成“实现 A2A 协议”。这里是同一 Host 内由 `HandoffService` 完成的 Fast→Slow agent handoff：它原子创建持久 Action Run，并传递 root/parent execution、ContextSnapshot、evidence、observations、remaining budget、Grant 和 idempotency scope。它没有实现 Google A2A 等跨 Agent 网络互操作协议。

推荐写法：

> 设计快慢双路径会议 Agent：Fast Turn 在低延迟预算内完成私密问答，复杂或工具型目标通过持久化 HandoffEnvelope 原子转交给可恢复 Action Run，保留上下文快照、证据、授权和幂等范围。

若简历版面允许，再补一句：

> 在慢路径并行编排 evidence、conflict、Linear research 三类只读 Subagent，并以 Critic 和 ToolExecutor 约束外部动作。
