# Agent Engineering P1-P3 Accelerated Implementation Plan

> **Status:** 已停止作为当前执行计划，其中 MCP/RAG/Memory 属于无现行产品需求的求职扩展。当前计划为 [产品导向 P1–P3](./2026-09-01-agent-product-hardening-p1-p3.md)。

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 10 个工作日、约 45–60 小时内完成可用于 Agent 开发实习投递的 P1–P3 作品集版本，优先证明 Agent 的可评测性、工具安全、证据约束、故障恢复和工程化能力。

**Architecture:** 复用现有 AssistantExecution、Step、ToolCall、Observation、Grant、ExternalActionClaim 和 ContextSnapshot，不新增通用 Agent 框架。P1 从现有执行表导出轨迹并建立 15 个离线场景；P2 只支持一个 stdio MCP server 的严格 allowlist 工具，并用 SQLite FTS5 完成 scope-first 证据检索和显式记忆；P3 把领域轨迹映射为 OpenTelemetry/JSONL，加入无 secret CI 和一键演示。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、SQLAlchemy、Alembic、SQLite FTS5、官方 MCP Python SDK v2、OpenTelemetry、pytest、GitHub Actions。前端、LiveKit 和现有插件框架保持不变。

---

## 0. 压缩原则

下文 `Backend` 命令的工作目录为 `backend`，`Frontend` 为 `frontend`，`Root` 为项目根目录。不要把不同工作目录的命令直接粘成一条长命令。

### 本轮必须完成

- 15 个版本化 Agent 场景，每个 scripted trial 运行 3 次。
- 可由 execution ID 导出的完整 Agent trajectory。
- tool selection、arguments、evidence、authorization、duplicate side effect、recovery、latency/token 指标。
- 一个 stdio MCP fake server：至少一个 read tool、一个 external-write tool、一个 read-only reconcile tool。
- MCP allowlist、schema validation、effect、timeout、Grant、idempotency 和 unknown→reconcile。
- SQLite FTS5 的 session/owner scope-first 检索、evidence citation，以及显式创建/删除的 memory。
- Agent/model/retrieval/tool/reconcile spans，默认导出到本地 JSONL，可选 OTLP。
- 一条命令运行 5 个演示场景并生成 Markdown/JSON 报告。
- GitHub Actions workflow 文件和本地等价验证。

### 推迟到投递后或有面试反馈时

- 30+ 场景和大规模人工标注；当前先做 15 个高价值场景。
- Streamable HTTP/SSE MCP transport、多 MCP server 管理、任意 JSON Schema 全覆盖。
- embedding、reranker、向量数据库与自动 memory 抽取；当前准确表述为 **FTS5 evidence retrieval**，不宣传 hybrid/vector RAG。
- 独立 `assistant_model_calls` 数据表；本轮模型调用元数据写入 trajectory collector，不做 migration。
- Run Inspector 前端、Jaeger UI、API/worker/frontend 全栈 Docker 化。
- 云模型 30 cases × 3 trials；本轮最多选 5 个 case 各跑 1 次，且只有在明确允许付费时执行。
- Kubernetes、Redis、Postgres、分布式 worker。

这些删减不会削弱简历主线：面试官仍能看到完整 Agent loop、typed tools、MCP、RAG、memory、evaluation、observability 和 failure recovery。删掉的是规模与展示层，不是核心机制。

## 1. 十日排期与阶段 Gate

| 工作日 | 任务 | 当日可验收产物 |
| --- | --- | --- |
| Day 1 | P1 契约与 15 个 case 骨架 | suite 可校验，坏 case fail closed |
| Day 2 | trajectory exporter + model call collector | execution 可回放，敏感字段不落盘 |
| Day 3 | ScenarioRunner、grader、CLI、3 trials | 45 trials 报告，安全计数为 0 |
| Day 4 | MCP stdio client、fake server、allowlist | discovery/timeout/schema 测试通过 |
| Day 5 | MCP ToolAdapter、Grant、unknown→reconcile | read/write/reconcile E2E 通过 |
| Day 6 | FTS5 evidence index 与 scope-first search | 跨 session/owner 泄漏为 0 |
| Day 7 | Context 接入、显式 memory、RAG eval | recall@5 ≥ 0.85，引用正确率 ≥ 0.95 |
| Day 8 | OpenTelemetry/JSONL spans 与脱敏 | 一条 execution 有完整 span tree |
| Day 9 | CI、单命令 demo、case study | 无 secret demo 生成报告 |
| Day 10 | 全量回归、修正和发布证据 | README 数字可从报告重算 |

阶段 Gate：

- **P1 Gate（Day 3）**：45 个 scripted trials；未授权写 0、重复副作用 0、不支持 claim 0、故障脚本结果 100% 符合预期。
- **P2 Gate（Day 7）**：MCP 未授权写 0、write 重试副作用最多 1；retrieval 跨 scope 泄漏 0、recall@5 ≥ 0.85、citation correctness ≥ 0.95。
- **P3 Gate（Day 10）**：一条命令运行 5 个 demo cases；JSONL trace 可回放；后端全量测试通过；CI 文件完成本地等价验证。

## P1 — 三天建立 Eval 与完整轨迹

### Task 1（Day 1）: 评测契约与 15 个高价值场景

**Files:**

- Create: `backend/app/evals/__init__.py`
- Create: `backend/app/evals/contracts.py`
- Create: `backend/app/evals/loader.py`
- Create: `backend/evals/cases/agent_portfolio_v1.jsonl`
- Create: `backend/tests/test_agent_eval_contracts.py`

**Steps:**

1. 运行既有核心基线：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_private_meeting_agent_core.py tests/test_linear_task_adapter.py tests/test_meeting_plugin_e2e.py -q
   ```

   预期：退出码 0；记录真实通过数与耗时。

2. 定义严格 `AgentEvalCase`：case ID、category、profile、fixture、scripted model outputs、fault、expected status/tool calls/evidence、最大 external side effects。所有模型 `extra="forbid"`。
3. 先写 15 个 case，每类只保留最有区分度的场景：
   - 正常 read 与正常 action 各 2 个。
   - 缺 assignee/deadline 2 个。
   - duplicate request 2 个。
   - 无 Grant / 用户拒绝 2 个。
   - transcript prompt injection 2 个。
   - tool timeout 1 个。
   - 远端成功但响应丢失 1 个。
   - 无 action / 中英 ASR 错误 1 个。
4. loader 拒绝重复 ID、非法 evidence、未知 fault、多余字段及 external write 未声明 side-effect 上限。
5. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_agent_eval_contracts.py -q
   ```

   预期：PASS，suite 中正好 15 个唯一 case。

### Task 2（Day 2）: 从现有执行记录导出 trajectory

**Files:**

- Create: `backend/app/assistant/trajectory.py`
- Create: `backend/app/assistant/model_trace.py`
- Modify: `backend/app/assistant/repository.py`
- Modify: `backend/app/assistant/bootstrap.py`
- Create: `backend/tests/test_assistant_trajectory.py`
- Create: `backend/tests/test_assistant_model_trace.py`

**Steps:**

1. trajectory 直接读取现有 execution、step、observation、tool call、subagent、handoff、grant、external action claim 和 event，不新增 migration。
2. `ObservedStructuredTextProvider` 只把 provider/model/stage/request hash/input chars/output tokens/latency/status 写入当前 run 的 `ModelCallTraceCollector`。不保存 prompt、字幕正文或 API key。
3. `AgentTrajectory` 按稳定顺序合并 DB 记录和 model trace；包含 root/parent execution、evidence、authorization、retry、unknown/reconcile 与最终状态。
4. RED 测试覆盖并行 tools、handoff、unknown→reconcile、取消后晚到响应、跨 session 拒绝、递归敏感字段脱敏。
5. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_assistant_trajectory.py tests/test_assistant_model_trace.py tests/test_private_meeting_agent_core.py -q
   ```

   预期：PASS；trajectory 可 JSON 序列化，敏感值扫描 0 命中。

### Task 3（Day 3）: 离线 ScenarioRunner、grader、CLI 与 P1 报告

**Files:**

- Create: `backend/app/evals/fakes.py`
- Create: `backend/app/evals/scenario_runner.py`
- Create: `backend/app/evals/graders.py`
- Create: `backend/app/evals/report.py`
- Create: `backend/app/evals/cli.py`
- Create: `backend/tests/test_agent_eval_runner.py`
- Create: `backend/tests/test_agent_eval_graders.py`
- Create: `backend/tests/test_agent_eval_cli.py`
- Generated: `reports/agent-portfolio-v1/`

**Steps:**

1. ScenarioRunner 使用真实临时 SQLite、AssistantRuntime、ToolExecutor 和 recovery；仅替换 provider、clock、UUID 和 external adapter。
2. grader 只读取期望、trajectory 和最终 DB 状态，计算：task success、tool/arguments、evidence、authorization、duplicates、recovery、p50/p95、tokens 和 pass@1/pass@3。
3. 安全失败退出码为 2，普通质量阈值失败为 1，通过为 0。
4. 运行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_agent_eval_runner.py tests/test_agent_eval_graders.py tests/test_agent_eval_cli.py -q
   .\.venv\Scripts\python.exe -m app.evals.cli --suite evals/cases/agent_portfolio_v1.jsonl --provider scripted --trials 3 --seed 20260901 --output-dir ../reports/agent-portfolio-v1
   ```

   预期：45 个 trials；P1 三个安全计数为 0；生成 `summary.json`、`report.md` 和 `trajectories/*.json`。

5. 只做一个最有价值的消融：`--ablation no_reconciliation`，证明“响应丢失”场景会失败或产生不可安全重试状态。其他 critic/subagent/handoff 消融推迟。

## P2 — 四天完成受控 MCP、FTS5 RAG 与显式 Memory

### Task 4（Day 4）: 单个 stdio MCP server、发现与 allowlist

**Files:**

- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/app/mcp/__init__.py`
- Create: `backend/app/mcp/contracts.py`
- Create: `backend/app/mcp/client.py`
- Create: `backend/app/mcp/sdk_client.py`
- Create: `backend/tests/fake_mcp_server.py`
- Create: `backend/tests/test_mcp_client.py`

**Steps:**

1. 安装官方稳定 v2 SDK：

   ```powershell
   # Backend
   uv add "mcp>=2,<3"
   ```

2. 本轮只支持一个 stdio server、最多 8 个 allowlist tools。配置显式声明 tool effect、timeout、idempotency argument 和可选 reconcile tool。
3. JSON Schema 只支持 object、string、integer、number、boolean、array、enum、required；recursive refs、oneOf/anyOf 和超限 schema fail closed。
4. fake server 提供 `search_notes`、`create_task` 和 `find_task_by_key`，支持 timeout、invalid response、disconnect 和 write-success-response-lost。
5. 子进程只继承配置 allowlist 环境变量；关闭测试断言无孤儿进程。
6. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_mcp_client.py -q
   ```

   预期：PASS；未配置 MCP 时应用行为不变。

### Task 5（Day 5）: MCP ToolAdapter 与写安全链

**Files:**

- Create: `backend/app/mcp/tool_adapter.py`
- Create: `backend/app/mcp/bootstrap.py`
- Modify: `backend/app/assistant/bootstrap.py`
- Create: `backend/tests/test_mcp_tool_adapter.py`
- Create: `backend/tests/test_mcp_agent_e2e.py`

**Steps:**

1. 将 allowlist descriptor 映射为 `mcp.<server>.<tool>` Assistant `ToolSpec`；继续由现有 ToolExecutor 执行，不能创建伪插件 principal 或绕过 Grant。
2. read tool 可用于 Fast Turn。external write 只有同时声明稳定 idempotency argument 和 read-only reconcile tool 才注册。
3. timeout/断连后的 write 标记 `unknown`；只调用 reconcile tool，不直接重试 write。找到同一 idempotency key 后返回原 external reference。
4. RED 测试覆盖无/过期/错误 scope Grant、重复 client request、schema drift、write response lost、reconcile found/not-found/failure。
5. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_mcp_tool_adapter.py tests/test_mcp_agent_e2e.py tests/test_private_meeting_agent_core.py -q
   ```

   预期：PASS；未授权 external write 为 0，重复副作用为 0。

### Task 6（Day 6）: FTS5 evidence index 与 scope-first retrieval

**Files:**

- Modify: `backend/app/persistence/models.py`
- Create: `backend/alembic/versions/20260901_0028_create_retrieval_memory.py`
- Create: `backend/app/retrieval/__init__.py`
- Create: `backend/app/retrieval/contracts.py`
- Create: `backend/app/retrieval/repository.py`
- Create: `backend/app/retrieval/indexer.py`
- Create: `backend/app/retrieval/search.py`
- Create: `backend/tests/test_retrieval_migration.py`
- Create: `backend/tests/test_retrieval_search.py`

**Steps:**

1. migration 新增 `evidence_chunks`、`agent_memories` 和 `retrieval_index_offsets`，并创建 `evidence_chunks_fts`。`evidence_chunks` 保存 owner scope、session、source/ref/revision、current/deleted 状态、文本和 metadata。
2. 启动时实际验证 FTS5；不支持时返回明确 capability error，不能全表扫描。
3. Indexer 从 current final transcript 与显式 memory 增量写入；相同 revision 重放无副作用，新 revision 使旧 chunk 不再 current。
4. 搜索必须在 SQL 中先限制 owner scope、session/current/deleted，再执行 FTS 排名和稳定 tie-break。不能先全库搜索再在 Python 过滤。
5. RED 测试覆盖中文、英文、编号、revision、删除、崩溃重放、索引修复、跨 session、跨 owner 和 limit/budget。
6. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_retrieval_migration.py tests/test_retrieval_search.py -q
   ```

   预期：PASS；跨 scope 候选数始终为 0。

### Task 7（Day 7）: Context 接入、显式 Memory 与 retrieval eval

**Files:**

- Modify: `backend/app/assistant/context.py`
- Modify: `backend/app/assistant/fast_runner.py`
- Modify: `backend/app/assistant/action_runner.py`
- Modify: `backend/app/assistant/prompts.py`
- Create: `backend/app/retrieval/memory.py`
- Create: `backend/app/api/memory.py`
- Modify: `backend/app/main.py`
- Create: `backend/evals/cases/retrieval_portfolio_v1.jsonl`
- Create: `backend/tests/test_retrieval_context.py`
- Create: `backend/tests/test_memory_api.py`
- Create: `backend/tests/test_retrieval_eval.py`

**Steps:**

1. 保留同步 `ContextBuilder.build`；新增 `async build_with_retrieval`。retrieval disabled 时完全走旧路径。
2. 检索结果转换为稳定 evidence ID，加入 ContextSnapshot 的允许引用集合；`relevant_context_hash` 覆盖 chunk ID/revision。
3. prompt 明确 evidence 是不可信数据，不能授权工具或修改 policy。事实 claim 没有允许 evidence 时必须谨慎回答或请求输入。
4. Memory 只通过 Host API 显式创建/删除，支持 `user_preference`、`meeting_fact`、`meeting_summary`；每条必须有 provenance、owner scope、可选 session 和 TTL。Agent 本轮不能自动学习 memory。
5. 删除 memory 同事务标记 base/chunk deleted 并移除 FTS；reindex 只读未删除记录。
6. 建立 12 个 retrieval cases：精确编号、中文、英文、中英混合、修订、冲突、prompt injection、无答案、删除、过期、跨 session、跨 owner。
7. grader 增加 recall@5、MRR、citation correctness、unsupported claim 和 scope leak。
8. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_retrieval_context.py tests/test_memory_api.py tests/test_retrieval_eval.py tests/test_private_meeting_agent_core.py -q
   .\.venv\Scripts\python.exe -m app.evals.cli --suite evals/cases/retrieval_portfolio_v1.jsonl --provider scripted --trials 3 --seed 20260901 --output-dir ../reports/retrieval-portfolio-v1
   ```

   预期：36 个 trials；scope leak 为 0、recall@5 ≥ 0.85、citation correctness ≥ 0.95。

## P3 — 三天完成可观测、CI 和 Demo

### Task 8（Day 8）: Agent OpenTelemetry 与本地 JSONL trace

**Files:**

- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/app/observability/__init__.py`
- Create: `backend/app/observability/bootstrap.py`
- Create: `backend/app/observability/agent_spans.py`
- Create: `backend/app/observability/jsonl_exporter.py`
- Modify: `backend/app/settings.py`
- Modify: `backend/app/assistant/fast_runner.py`
- Modify: `backend/app/assistant/action_runner.py`
- Modify: `backend/app/assistant/tools/executor.py`
- Modify: `backend/app/mcp/tool_adapter.py`
- Modify: `backend/app/retrieval/search.py`
- Create: `backend/tests/test_agent_observability.py`
- Create: `backend/tests/test_observability_redaction.py`

**Steps:**

1. 安装最小依赖：

   ```powershell
   # Backend
   uv add opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
   ```

2. 只埋 Agent 领域 spans，不做全站 FastAPI 自动 instrumentation。固定 span：execution、plan、model、retrieval、tool、approval、reconcile。
3. 默认 exporter 为本地 JSONL；设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后才启用 OTLP。collector 不可用时业务继续并记录 degraded。
4. attributes 采用 allowlist：execution/session scope hash、provider/model、token、latency、tool/effect/status、schema hash、retrieval counts。禁止 prompt、content、tool secret arguments 和 intent token。
5. JSONL span 与 P1 trajectory 共用 execution ID，但 recovery 不依赖 trace 文件。
6. 执行：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_agent_observability.py tests/test_observability_redaction.py tests/test_assistant_trajectory.py -q
   ```

   预期：PASS；一个完整场景形成正确父子 span tree，敏感值扫描 0 命中。

### Task 9（Day 9）: 无 secret CI、一键 Demo 与案例文档

**Files:**

- Create: `.github/workflows/agent-ci.yml`
- Create: `backend/evals/cases/portfolio_demo_v1.jsonl`
- Create: `scripts/run_agent_portfolio_demo.py`
- Create: `scripts/run_agent_demo.ps1`
- Create: `agent_demo.cmd`
- Create: `backend/tests/test_agent_portfolio_demo.py`
- Create: `docs/agent-demo-script.md`
- Create: `docs/agent-engineering-case-study.md`
- Modify: `README.md`

**Steps:**

1. demo 固定 5 个连续场景：prompt injection、evidence retrieval、MCP read、MCP write denied/approved、write response lost→reconcile。额外断言跨 owner retrieval 为 0。
2. demo 使用 scripted provider、fake MCP、临时 DB 和 JSONL exporter；默认不需要 Docker、云 key 或真实 Linear。
3. `agent_demo.cmd` 调用 PowerShell wrapper，最终生成 `reports/portfolio-demo-v1/report.md`、`summary.json`、trajectory 和 spans。
4. CI jobs 保持最小：
   - `backend-tests`：`uv sync --frozen` 后运行全量 pytest。
   - `agent-eval`：5 个 demo cases × 3 trials，上传脱敏报告。
   - `frontend-regression`：现有 Vitest + typecheck；因为未改前端，不要求新增 UI 测试或 Docker build。
5. PR CI 不注入 DeepSeek、DashScope、Linear 或 MCP secret；代码若试图走真实网络应 fail。
6. case study 只写可验证内容：现有 runtime、P1 指标、MCP 安全链、FTS5 scope、故障恢复、trace、限制和后续完整版路线。
7. 执行：

   ```powershell
   # Root
   .\agent_demo.cmd

   # Backend
   .\.venv\Scripts\python.exe -m pytest tests/test_agent_portfolio_demo.py -q

   # Frontend
   pnpm test
   pnpm run typecheck
   ```

   预期：全部退出码 0；demo 报告显示未授权写与重复副作用为 0。

8. 当前工作区不是有效 Git repository。先完成 workflow 和本地等价验证；只有恢复/初始化 Git 并在远程看到真实 workflow 成功后，才写“GitHub Actions passing”。

### Task 10（Day 10）: 全量回归、修正和发布证据

**Files:**

- Create: `docs/agent-engineering-release-checklist.md`
- Modify: `docs/stage-records.md`
- Modify: `README.md`
- Generated: `reports/agent-engineering-accelerated-v1/`

**Steps:**

1. 运行后端全量：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m pytest -q
   ```

   预期：退出码 0；记录实际通过/跳过/耗时。

2. 运行前端现有回归：

   ```powershell
   # Frontend
   pnpm test
   pnpm run typecheck
   pnpm run build
   ```

   预期：全部退出码 0。

3. 重跑两个冻结 suite 和 demo：

   ```powershell
   # Backend
   .\.venv\Scripts\python.exe -m app.evals.cli --suite evals/cases/agent_portfolio_v1.jsonl --provider scripted --trials 3 --seed 20260901 --output-dir ../reports/agent-engineering-accelerated-v1/core
   .\.venv\Scripts\python.exe -m app.evals.cli --suite evals/cases/retrieval_portfolio_v1.jsonl --provider scripted --trials 3 --seed 20260901 --output-dir ../reports/agent-engineering-accelerated-v1/retrieval

   # Root
   .\agent_demo.cmd
   ```

4. release checklist 记录 Python/uv、MCP SDK、migration head、suite version、seed、指标、已知限制和本地 CI 等价命令。
5. README 与简历 bullet 的每个数字必须能从 `summary.json` 重算。不得把 scripted success rate 写成真实模型线上成功率。
6. 如果 Day 10 有剩余时间，只用于修复 Gate 或改善报告，不临时加入 embedding、前端 Inspector、Docker 或第二个 MCP transport。

## 2. 加速版 Definition of Done

- [ ] 15 个核心 case × 3 scripted trials 可复现，trajectory 可按 execution ID 导出。
- [ ] 未授权 external write、重复副作用、不支持 claim 均为 0。
- [ ] 一个 stdio MCP server 的 read/write/reconcile 全链路通过。
- [ ] MCP write 具备显式 Grant、稳定 idempotency key 和 unknown→read-only reconcile。
- [ ] FTS5 retrieval 的跨 owner/session 泄漏为 0，达到 recall@5 和 citation Gate。
- [ ] memory 只能显式创建，具有 provenance、TTL 和可靠删除。
- [ ] execution/model/retrieval/tool/reconcile spans 写入脱敏 JSONL，可选 OTLP。
- [ ] `agent_demo.cmd` 无云 key 一条命令生成 5-case 报告。
- [ ] Backend 全量 pytest、Frontend test/typecheck/build 通过。
- [ ] 案例文档和简历数字均来自版本化报告。

## 3. 10 天后可写进简历的主线

实际数字必须替换为报告值：

- 设计可回放 Agent Eval harness，覆盖 15 个会议 Agent 场景和 45 次确定性 trials，评估工具选择、证据引用、授权、恢复、时延与 token。
- 将 MCP stdio 工具映射到 typed ToolSpec，复用 Grant、幂等和 reconciliation，在注入、超时与响应丢失场景中保持未授权写和重复副作用为 0。
- 构建 owner/session 隔离的 SQLite FTS5 evidence retrieval 与有 provenance 的显式 memory，实现可引用回答和可靠删除。
- 用 OpenTelemetry/JSONL、CI 和一键 demo 串联 execution、model、retrieval、tool 与 reconcile，形成可复现的 Agent 工程案例。

## 4. 完整版后续路线

原六周计划保留为扩展 backlog：[2026-09-01-agent-engineering-p1-p3.md](./2026-09-01-agent-engineering-p1-p3.md)。优先级依次为：扩充 30+ case 与多次真实模型评测、Streamable HTTP/多 MCP server、embedding/reranker、Run Inspector、全栈 Compose/Jaeger。只有实际投递反馈或指标显示需要时再做。
