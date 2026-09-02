# Meeting Assistant Plugin Unification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the standalone private meeting assistant with an installable standard plugin in the common assistant sidebar, preserving every existing capability, history record, evidence link, and external-action safety boundary.

**Architecture:** A signed, network-isolated meeting plugin owns declarative UI and interaction, while typed Host capabilities reuse the existing meeting projector and execution engine. Durable per-session activation, Host-owned user intents, and short-transaction operation admission control analysis and external writes. Host history adapters keep old data accessible without starting analysis or requiring a running plugin.

**Tech Stack:** Existing FastAPI, Pydantic, SQLAlchemy, SQLite WAL, Alembic, Python 3.12+, JSON-RPC plugin SDK, Docker OCI isolation, Next.js 16, React 19, TypeScript, Vitest and jsdom. No new service or production dependency is planned.

---

## 0. 执行约定与当前状态

- 设计依据：`docs/plans/2026-08-31-meeting-assistant-plugin-design.md`，用户已确认全部设计。
- 本文件记录实施计划及分批验收；2026-09-01 已完成 Batch A–E 的全部 Task 1–18。生产数据库已一致性备份并迁移到 `20260831_0027`，生产前端已构建，会议助手已通过标准 inspect/confirm/enable 路径安装并在正常插件框架中运行；当前网页已验证通用插件视图与只读历史。真实会议分析、真实模型成功调用和真实 Linear 写入仍不在本次授权/验收范围内。
- 本机可用技能名称为 `executing-plans`。上面的标准模板名称 `superpowers:executing-plans` 指向同类执行流程，不要求安装缺失的命名空间或插件。执行时先读可用 `executing-plans/SKILL.md`。
- 用户此前要求按大批次自动执行、审核和汇报。沿用当前任务和工作区，批内完成测试与修正，批末给出结果/风险；遇到新的权限或范围选择再暂停。
- 工作区 `E:/学习文件/研究生/就业/Agent学习/Matinier` 不是有效 Git 仓库。不得初始化仓库、创建 worktree 或运行假定存在的提交命令。每个任务的“提交点”改为记录测试证据，批末更新 `docs/stage-records.md`；保留不属于本任务的现有变更。
- 只使用 `apply_patch` 编辑源文件/文档；SDK schema 再生成属于受控机械生成。不得读取或打印 `.env` 密钥。生产数据库、真实会话、已生成文档和运行中的课程插件不用于单元测试。
- 此次不迁移旧课程插件版本、不重做课程结果、不修改媒体架构，也不安装 Codex 插件。
- 架构阶段的并行审查没有完成，不能算作独立审核。后续确有独立审核时记录实际结果；否则清楚标记为主代理自审。

### 工作目录与命令

下文 `Backend:` 命令的工作目录为项目的 `backend`；`Frontend:` 为 `frontend`。不要把这些命令合并为带分隔符的长 shell 串。

```powershell
# Backend: 已核实存在的解释器
.\.venv\Scripts\python.exe -m pytest --version

# Frontend: 使用已安装 pnpm 和锁文件
pnpm exec vitest --version
```

上述版本查询仅是执行期预检。不要自动升级依赖。需要生成文件/备份时选用唯一任务目录；删除只限本次拥有且已验证绝对路径的临时产物。

### 所有任务通用的 TDD 节奏

1. 添加本任务明确列出的失败测试（单个修改步骤尽量 2–5 分钟）。
2. 运行指定聚焦命令，确认因缺失目标行为失败，而不是环境损坏。
3. 实现最小改动；避免在同一步同时重构引擎和 UI。
4. 重跑聚焦测试和相关旧测试；记录确切结果。
5. 检查 scope、隐私、幂等及兼容性后再继续下一任务。

不能把 `sleep` 当作并发测试同步机制；使用 asyncio.Event、可控 fake 时钟及明确检查点。测试失败不是理由去重启用户服务。

## 1. 批次与阶段关卡

| 批次 | 任务 | 完成后报告与关卡 |
| --- | --- | --- |
| A：会话与兼容基础 | 1–3 | 原功能基线、增量数据结构、显式会话启停及补齐；尚不切换 UI |
| B：Host 能力与安全 | 4–7 | 历史读模型、可信意图、持久操作、Broker/执行检查点；模拟 Linear 必须防重复 |
| C：标准会议插件 | 8–10 | 可签名打包/安装、多插件构建、声明式功能视图；不依赖宿主网络 |
| D：统一前端与切换 | 11–14 | 通用可信控件、历史目录、旧接口收口、独立卡片移除；音频不受影响 |
| E：验证与交付 | 15–18 | 综合 E2E、真实隔离容器冒烟、全量回归及受控上线步骤 |

每批至少核对：需求覆盖、权限/隐私、并发/幂等、异常/历史、媒体/课程回归。未通过的关卡不能用“下一批再补”掩盖。

## Batch A — 会话与兼容基础

**状态：已完成。** Task 1–3 已实现并通过本批验收；仅后端基础，不切换 UI、不迁移生产数据库或重启服务。

### Task 1: 建立旧功能与数据不变基线

**Files:**

- Create: `backend/tests/test_meeting_plugin_compatibility.py`
- Create: `backend/tests/meeting_plugin_fakes.py`
- Modify: `backend/tests/conftest.py`
- Read: `backend/tests/test_private_meeting_agent_core.py`
- Read: `frontend/components/private-meeting-assistant.tsx`
- Read: `backend/app/api/assistant.py`
- Read: `backend/app/api/meeting_state.py`

**Steps:**

1. 先运行旧后端用例，记录真实数量，不沿用此前报告的计数：
   `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_private_meeting_agent_core.py tests/test_meeting_state_extractor.py tests/test_linear_task_adapter.py -q`。
   预期：全部通过；若基线失败，隔离既有失败，不扩展修复范围。
2. 编写只使用临时 SQLite 的 fixture：两个 legacy Session、对应 MediaSession、当前 Final 与修订、自动/手动 marks、候选、Fast Turn、父子 Action Run、needs_input、unknown claim、已完成 Linear reference。复用已有 fake task adapter 的形状。
3. 为 FakeModel 增加调用列表和 `entered/release` 闸门；为 FakeLinear 分别记录 create/read/reconcile 次数，可模拟“远端已创建但响应丢失”。所有 fake 禁止真实 HTTP。
4. 先写通过旧服务建立的 characterization 测试，断言原 ID、root/parent、evidence、state_version、事件游标、输入和取消语义。不要先改旧接口来满足新插件测试。
5. 运行 `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_compatibility.py -q`，预期 PASS。记录完整功能映射，后续新增插件断言复用同一批原记录。

### Task 2: 定义严格契约与增量迁移

**Files:**

- Create: `backend/app/assistant/plugin_contracts.py`
- Create: `backend/app/assistant/plugin_repository.py`
- Create: `backend/app/plugins/host_action_contracts.py`
- Modify: `backend/app/persistence/models.py`
- Create: `backend/alembic/versions/20260831_0027_create_meeting_plugin_control.py`
- Create: `backend/tests/test_meeting_plugin_contracts.py`
- Create: `backend/tests/test_meeting_plugin_migration.py`
- Create: `backend/tests/test_meeting_plugin_repository.py`

**Steps:**

1. 添加契约失败测试：拒绝空 request ID、超长消息、额外 actor/Session/URL 字段、重复候选 ID、未知 mark operation、ask 中携带 grant，以及非法状态版本。
2. 运行 `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_contracts.py -q`。预期 RED：新模块/模型未实现。
3. 以如下完整最小模型为起点，再按设计的能力表增加其余有界模型。执行输入应分型，不用自由格式通用 payload。

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class MeetingAskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=255)
    intent_token: str = Field(min_length=32, max_length=256)
    message: str = Field(min_length=1, max_length=4000)
    mark_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=50)

class MeetingOperationAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: str = Field(min_length=1, max_length=36)
    status: Literal["accepted"] = "accepted"
```

4. 新增设计中三张表：session 激活及独立的 analysis_epoch/authority_epoch、operation 映射、intent hash。operation/intent 绑定 authority_epoch；分析控制 intent 额外绑定 analysis_epoch。operation 对 `(plugin_id, plugin_version, media_session_id, client_request_id)` 唯一；同键不同 hash 拒绝。intent 消费采用条件更新，禁止两个并发请求各消费一次。
5. migration 的 `down_revision` 应为核对到的 `20260828_0026`；开始执行时再检查 migration head，若期间有新迁移则按真实链调整，不制造分叉。旧 meeting/assistant 数据不复制、不重编号，新增表不得对插件安装使用删除级联。
6. 在临时数据库先升到旧 head、写入 fixture、再升级；验证旧行数、ID、父子链和 evidence 相同，新表为空且默认未激活。测试 `upgrade` 重复执行无额外副作用；downgrade 仅在临时测试库验证，不用于生产回滚。
7. 运行 `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_contracts.py tests/test_meeting_plugin_migration.py tests/test_meeting_plugin_repository.py -q`，预期 PASS。

### Task 3: 会话资格、补齐与 Projector 多检查点

**Files:**

- Create: `backend/app/assistant/plugin_policy.py`
- Modify: `backend/app/meeting_state/projector.py`
- Modify: `backend/app/meeting_state/bootstrap.py`
- Modify: `backend/app/assistant/context.py`
- Create: `backend/tests/test_meeting_plugin_policy.py`
- Create: `backend/tests/test_meeting_plugin_projector.py`

**Steps:**

1. 添加失败测试：只安装/打开/绑定不调用模型；会话 A 激活不处理 B；激活补上 scan cursor 之前的 Final；修订替换正确；自然结束仅 flush 已激活会话；历史读取/问答 context 不隐式 catch-up。
2. 运行 `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_policy.py tests/test_meeting_plugin_projector.py -q`，预期 RED。
3. 用显式且不可混淆的 analysis / interaction / external-write 判定。资格判断的核心应至少覆盖下列完整逻辑；final frontier 校验由 Projector 按字幕执行，不能由此函数替代：

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class AnalysisAdmission:
    plugin_enabled: bool
    binding_current: bool
    state: Literal["inactive", "active", "draining", "completed"]
    current_epoch: int

    def permits_batch(self, expected_epoch: int) -> bool:
        return (
            self.plugin_enabled
            and self.binding_current
            and self.state in {"active", "draining"}
            and self.current_epoch == expected_epoch
        )
```

4. `_scan_updates`/finalizing 查询仅选合格会话；`request_catch_up`、backlog、enqueue、worker、模型前和 `_commit_projection` 同时守卫。提交使用持久 epoch 条件，不仅比较内存快照。
5. 激活事务保存 active/analysis_epoch 并安排显式本会话 backlog；使用已有 offset/revision 去重。停止分析仅失效 analysis_epoch，不撤销已明确授权的交互执行；停用整个插件失效两类 epoch。已在模型中等待的过期分析结果回来后不得提交。
6. 用 fake 闸门测试“模型已进入→停用→模型返回”，断言旧结果不提交；另测试激活/停用/再激活时旧批次不能误用新资格。
7. 显式历史问答只使用已有状态/受限原始字幕上下文，不启动全历史投影。会话自然结束与用户停用要区分，否则会漏最后一批字幕或误补历史。
8. 重跑聚焦命令和 `tests/test_private_meeting_agent_core.py`。预期全部 PASS；旧纯引擎测试可显式注入测试资格，但生产组合禁止默认 allow-all。

**Batch A gate:** 无授权会话调用模型次数为零；backlog/修订/终止/停用竞态通过；临时迁移未损坏任何旧数据。记录并汇报，不重启生产。

## Batch B — Host 能力、意图与执行安全

**状态：已完成。** Task 4–7 已实现并通过本批验收，整体 7/18 项。新增 API 路由仍待 Task 13 注册到应用；插件包、通用前端和旧写 API 收口未切换，不应单独部署本批。

### Task 4: 只读会议服务与历史适配

**Files:**

- Create: `backend/app/assistant/plugin_service.py`
- Create: `backend/app/assistant/plugin_history.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/assistant/repository.py`
- Modify: `backend/app/meeting_state/repository.py`
- Create: `backend/tests/test_meeting_plugin_read_model.py`
- Create: `backend/tests/test_meeting_plugin_history.py`

**Steps:**

1. 为原 fixture 添加新只读服务断言：同一记录 ID/证据/执行链；分页与游标有界；scope 不匹配拒绝；停用/卸载时 Host 历史仍可读。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_read_model.py tests/test_meeting_plugin_history.py -q`，预期 RED。
3. 新只读服务复用 repositories，将 legacy Session 映射到 MediaSession；不复制正文到 plugin state，不要求运行中的 AssistantRuntime。
4. 输出 processing 状态、current-final processed/pending 数量、更新时间、标记/候选、公开 execution 摘要和 event cursor。复用已有敏感字段过滤；跨 Session execution/candidate/mark ID 返回拒绝，不先泄漏详情。
5. 返回通用历史 descriptor：`source_kind="host_history"`、plugin ID、原会话、只读视图/分页入口及 Host 注册的 cancel 控制描述。该来源由 Host 路由组装，插件 ui.publish 不能自报相同信任来源。
6. 状态与事件联动提供一致版本；计数不使用 plugin ACK。读接口与界面关闭均不能触发模型、授权或执行。
7. 重跑聚焦测试及 Task 1 基线，预期 PASS。

### Task 5: 主程序可信操作、预览和一次性意图

**Files:**

- Create: `backend/app/plugins/host_actions.py`
- Modify: `backend/app/plugins/host_action_contracts.py`
- Create: `backend/app/api/assistant_actions.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/settings.py`
- Create: `backend/tests/test_host_actions_api.py`
- Create: `backend/tests/test_host_action_intents.py`

**Steps:**

1. 编写失败测试：伪造 actor、会话、插件版本、scope、候选、团队、preview hash；不存在/过期/已用/已撤销 intent；异源请求；无可信 UI 上下文；并发双确认。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_host_actions_api.py tests/test_host_action_intents.py -q`，预期 RED。
3. 实现有限 registry，固定动作键：`meeting.analysis.activate/deactivate`、`meeting.ask`、`meeting.mark.create/accept/dismiss`、`meeting.execute`、`meeting.input`、`meeting.cancel`。动作键、schema、效果和确认要求来自 Host，而不是插件传入的说明文本。
4. 实现设计中的 prepare/confirm 路由。actor 从本地主程序身份产生；浏览器只提交输入和已知版本。普通本地操作无需重复弹确认，外部写必须确认服务端重建的候选、团队、数量、期限。
5. 可信 UI nonce 仅保留在主程序 UI 内存/Host 有限 TTL 存储，结合精确 Origin allowlist、JSON/custom-header 校验。复用现有 CORS 配置来源，但不把“通过 CORS”当作用户授权；无 Origin 的调用需明确主程序授权上下文。不要把全应用突然改为新账户登录，也不向插件传 admin token。
6. intent 使用随机值，仅持久化其 hash；绑定 actor、plugin/version、会话、authority_epoch、action、normalized payload、候选 revision/快照和 fixed team；分析开关动作额外绑定 analysis_epoch。TTL 为 15 分钟上限；检查发生在准备、确认、消费及实际写入阶段。
7. 取消确认不创建 ActionGrant、不入队；改变选项使旧 preview 失效。激活只允许正在进行的会话，历史交互不变成自动分析。
8. 重跑聚焦命令，预期 PASS；额外断言“仅伪造 plugin confirmation 组件不能签发 intent”。

### Task 6: 持久操作受理与无锁后台执行

**Files:**

- Create: `backend/app/assistant/plugin_operations.py`
- Create: `backend/app/assistant/plugin_runtime.py`
- Modify: `backend/app/assistant/plugin_repository.py`
- Modify: `backend/app/assistant/fast_runner.py`
- Modify: `backend/app/assistant/action_scheduler.py`
- Create: `backend/tests/test_meeting_plugin_operations.py`

**Steps:**

1. 为 accepted→queued→execution 的映射添加失败测试。包括事务提交后未入内存队列、执行已创建但映射回复丢失、相同 request 重试、同键不同 payload 冲突。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_operations.py -q`，预期 RED。
3. `admit` 只在调用方事务内校验并保存 operation、绑定原 execution 幂等身份、消费 intent；不要开启嵌套写事务，也不要 await 引擎。operation 与 capability invocation 的受理映射原子提交。
4. 后台 worker 以独立短事务 claim 持久 queued operation，释放 DB Session 后运行既有 FastTurnRunner/ActionRunScheduler。使用原 `client_request_id` 和 stable operation identity 恢复，不能每次重启重新生成执行。
5. 模型成功后、响应丢失时返回同一结果；未知状态查询映射，不重复问答计费或创建任务。复用原引擎的 idempotency 查找，必要时明确持久化执行预留点。
6. 使用 Event 挂起问答/Linear，同时让另一插件 `state.get/ui.publish` 和字幕写入完成；断言无全局锁阻塞。不要通过任意放宽超时掩盖问题。
7. worker 限制队列并发，显式 stop/flush/cancel；重启恢复使用持久行，不依赖 `create_task` 的内存对象。再次运行聚焦命令，预期 PASS。

### Task 7: 注册能力、最终写入检查和受限恢复

**Files:**

- Create: `backend/app/plugins/meeting_adapter.py`
- Modify: `backend/app/plugins/capabilities.py`
- Modify: `backend/app/plugins/broker.py`
- Modify: `backend/app/plugins/permissions.py`
- Modify: `backend/app/plugins/bootstrap.py`
- Modify: `backend/app/plugins/sdk_schemas.py`
- Modify: `plugin-sdk/schemas/plugin-capabilities.schema.json`
- Modify: `backend/app/assistant/bootstrap.py`
- Modify: `backend/app/assistant/action_runner.py`
- Modify: `backend/app/assistant/recovery.py`
- Modify: `backend/app/assistant/tools/executor.py`
- Create: `backend/tests/test_meeting_plugin_broker.py`
- Create: `backend/tests/test_meeting_plugin_execution_guard.py`

**Steps:**

1. 添加 Broker 失败用例，逐一覆盖设计的七种 `meeting.*` 能力和缺少权限/错误 scope/其他插件调用/旧 generation/错误 intent。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_broker.py tests/test_meeting_plugin_execution_guard.py -q`，预期 RED。
3. 为会议能力显式注册 effect、输入输出、超时、幂等和恢复属性；不要沿用所有 capability 都 `requires_action_grant=False` 的便捷注册路径。把 AuthorizationDecision 传到受控适配层，不能只检查请求中可省略的 scope 字段。
4. `meeting.execution.submit` 必须有匹配安装权限、scope grant、可信 intent、原 ActionGrant；reconciler 只找 accepted operation，不能重新创建 execution 或 Issue。`meeting.execution.input` 即使属于 local_write，若将继续外部执行仍需写授权。
5. 在 tool 最终 admission 与 `requesting` 持久化处再次验证插件启用、operation owner/authority_epoch、grant 与预算；与停用使用共同的锁/事务边界。编写并发测试，以这个明确线性化点判断“已发出”与“停用后新请求”，并验证仅停止分析不会取消有效执行授权。
6. disable/uninstall 先关闭准入并撤权，再停插件；模型/队列在安全点终止。只读 reconciliation 不经过 planner，不得触发创建，即使未知请求后来证明确已成功。
7. startup recovery 对原有无 ownership 的活动 execution 只保留待确认/只读核对。崩溃恢复只能接续仍被授权的既有操作；升级/显式撤权不继承旧票据。
8. 将模型和 Linear 可用性分开：没有 Linear 配置时可以查询、问答和本地标记；执行控件明确不可用，不能造成整个会议 Host 启动失败。
9. 再生成公开 schema：`Backend: .\.venv\Scripts\python.exe -m app.plugins.sdk_schemas ../plugin-sdk/schemas`。检查只产生预期 schema 变化，不手工伪造生成结果。
10. 运行 `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_broker.py tests/test_meeting_plugin_execution_guard.py tests/test_plugin_broker.py tests/test_plugin_permissions.py tests/test_plugin_sdk_contract.py tests/test_private_meeting_agent_core.py -q`，预期 PASS。

**Batch B gate:** 未确认零 Linear create；重试同一 operation；停用未知结果仅 read/reconcile；并发不持全局锁等待；历史可读且无自动分析。记录授权负向矩阵与自审/独立审查实际状态。

## Batch C — 标准会议插件与多插件构建

**状态：已完成本批源码与隔离测试。** Task 8–10 通过；标准签名打包流程使用 fake Docker 验证，真实 OCI 镜像构建/运行仍按 Task 16 验收。未安装会议插件或切换前端入口。

### Task 8: 抽取有限内置构建注册表，加入会议包

**Files:**

- Create: `backend/scripts/builtin_plugin_sources.py`
- Create: `backend/scripts/package_meeting_assistant_plugin.py`
- Modify: `backend/scripts/package_course_organizer_plugin.py`
- Modify: `backend/app/plugins/builtins.py`
- Modify: `backend/app/plugins/builtin_packages.py`
- Create: `plugin-sdk/examples/meeting-assistant/plugin.json`
- Create: `plugin-sdk/examples/meeting-assistant/Dockerfile`
- Create: `plugin-sdk/examples/meeting-assistant/plugin.py`
- Create: `plugin-sdk/examples/meeting-assistant/meeting_assistant/__init__.py`
- Create: `backend/tests/test_meeting_plugin_packaging.py`
- Modify: `backend/tests/test_builtin_plugin_packages.py`
- Modify: `backend/tests/test_plugin_builtins.py`

**Steps:**

1. 先添加第二个 built-in 的失败用例：目录两个插件；builder 收到各自 source snapshot；缓存互不串用；manifest ID/version/permissions 一致；非法 ID 不启动 builder。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_plugin_builtins.py tests/test_meeting_plugin_packaging.py tests/test_builtin_plugin_packages.py -q`，预期新增第二插件断言 RED。
3. 采用固定 registry `{source_id: capture/digest/write_snapshot/build}`；只允许声明的源码闭包。保留原 `package_course_plugin` 等公开函数作为兼容 wrapper，课程源码 digest 不因无关会议文件变化而改变。
4. 会议 manifest 使用 `com.matinier.meeting-assistant` 与初始版本；权限严格按设计七种 meeting 能力及 state/ui。commands 明确枚举，不能让 manifest 任意扩展 Host action registry。
5. Dockerfile 只 COPY SDK 与会议插件源码，以非 root、stdout JSON-RPC 工作。不得 COPY 项目根全部文件、`.env`、数据库、输出包或签名 key；所有密钥和构建输出仍在外部受控工作目录。
6. 打包脚本提供 `--private-key/--output/--image-tag`，参数同课程脚本；唯一临时镜像，只删除自己创建的 tag。实际 Docker 构建留到 Task 16，此阶段使用 fake command runner 测试。
7. 验证 singleflight、取消 waiter、源码快照隔离、签名缓存验证和损坏缓存重建适用于两个插件。重跑聚焦命令及 `tests/test_course_plugin_packaging.py`，预期 PASS。

### Task 9: 插件控制器、查询刷新与生命周期

**Files:**

- Modify: `plugin-sdk/examples/meeting-assistant/plugin.py`
- Create: `plugin-sdk/examples/meeting-assistant/meeting_assistant/session.py`
- Create: `plugin-sdk/examples/meeting-assistant/meeting_assistant/contracts.py`
- Create: `backend/tests/test_meeting_plugin_controller.py`
- Read: `plugin-sdk/python/matinier_plugin/runtime.py`

**Steps:**

1. 添加失败测试：session.open 只查询不激活；媒体事件不发问答/外部操作；传入可信命令才调用对应能力；相同 request ID 重试不变。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_controller.py -q`，预期 RED。
3. 实现 session registry、每会话串行处理/刷新、Host response DTO 校验。回调不要阻塞 media.events ACK 等待问答完成；先受理再查询 operation 状态。
4. 有激活分析或非终结 operation 时用 SDK `create_task` 进行低频版本查询，无变化不发布新 view。最后一条媒体事件后结果仍能刷新；未激活/历史在首次和操作后读取，避免无穷轮询。
5. session.close/SDK shutdown 取消并回收任务；重连从 Host 结果重建，插件 state 只保存可恢复的 UI/游标信息，不保存授权明文、密钥或第二套 execution。
6. 错误保留上次成功结果和更新时间，并显示当前失败；退出/重连不得把错误当“空笔记”。重跑聚焦命令并检查 pending task 为零，预期 PASS。

### Task 10: 完整功能的声明式视图

**Files:**

- Create: `plugin-sdk/examples/meeting-assistant/meeting_assistant/view.py`
- Modify: `plugin-sdk/examples/meeting-assistant/meeting_assistant/session.py`
- Create: `plugin-sdk/examples/meeting-assistant/README.md`
- Create: `backend/tests/test_meeting_plugin_views.py`
- Read: `backend/app/plugins/ui_schema.py`
- Read: `frontend/lib/plugin-ui-schema.ts`

**Steps:**

1. 添加状态夹具：未激活、等待字幕、分析中、已更新、模型失败、停用、待补信息、unknown、已完成及只读历史。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_views.py -q`，预期 RED。
3. 用现有 section/card/details、text/safe_markdown、list/table/timeline/media_anchor、input/textarea/checkbox/button、badge/progress/error 组合六个区域，不注入 HTML、脚本或 iframe。
4. 所有 action ID 对应 manifest allowlist；Host 授权描述不在 plugin view 中标记为可信。候选选择保存 ID 而非任意任务 JSON；手动 mark 的“当前字幕”由 Host 验证来源而非依赖旧 React prop。
5. 显示本次分析状态与 processed/pending/current update；execution 卡保留 root/parent、needs_input、cancel、已确认/未知副作用和 Linear link。坐标使用原 evidence/time，绝不补造来源。
6. 每个 fixture 通过后端 UI schema，保存同一 JSON fixture 给前端 parser 回归；保持窄侧栏可读，不要求新组件类型即可实现的内容不要扩展 schema。
7. 重跑视图、controller、UI schema 测试，预期 PASS。

**Batch C gate:** 包是真正标准签名 OCI 插件；两个 built-in 构建/缓存独立；功能视图覆盖旧卡片；插件不自行激活或执行外部动作。仍不安装到用户运行环境。

## Batch D — 通用前端、历史与旧接口收口

**状态：已完成。** Task 11–14 源码接入、隔离测试及主代理自审通过；未部署，实际运行切换留在 E。

### Task 11: 通用主程序操作控件

**Files:**

- Create: `frontend/types/host-actions.ts`
- Create: `frontend/lib/host-action-state.ts`
- Create: `frontend/lib/host-action-state.test.ts`
- Create: `frontend/components/plugins/host-action-panel.tsx`
- Create: `frontend/components/plugins/host-action-panel.test.tsx`
- Modify: `frontend/lib/api.ts`
- Modify: `frontend/components/plugins/use-media-assistant-workspace.ts`
- Modify: `frontend/components/assistants/assistant-detail.tsx`
- Modify: `frontend/app/globals.css`

**Steps:**

1. 写 reducer/DOM 失败测试：prepare、确认、取消、过期、冲突、pending重试；假插件 confirmation 不授予权限；切换会话/插件/epoch 清除旧预览但不干扰其他插件输入。
2. `Frontend: pnpm exec vitest run lib/host-action-state.test.ts components/plugins/host-action-panel.test.tsx --reporter=dot`，预期 RED。
3. 实现闭合状态集合 `idle/preparing/awaiting_confirmation/submitting/accepted/error`。每次用户操作生成稳定 request ID，在响应不确定时复用；仅用户显式新操作才换 ID。
4. 可信控件只读取 Host action descriptor/preview，执行注册的 prepare/confirm；不从插件 safe_markdown、标签或任意 URL 构建权限。Linear 预览清晰列出候选、固定团队、上限、期限。
5. local ask/mark 在点击后通过可信通道受理，不额外重复弹窗。取消/过期清除临时 nonce/intent；不存 localStorage，不泄漏进 plugin state 或日志。
6. hook 保持 RoomStudio 的现有单实例生命周期；新增请求使用会话 generation 防止迟到响应覆盖新会话，命令并发仅禁用当前动作/视图。
7. 重跑新测试及 `lib/assistant-workspace-state.test.ts`、`lib/plugin-ui-schema.test.ts`，预期 PASS。

### Task 12: 扩展通用目录以展示 Host 历史

**Files:**

- Modify: `frontend/types/plugins.ts`
- Modify: `frontend/lib/assistant-workspace-state.ts`
- Modify: `frontend/lib/assistant-workspace-state.test.ts`
- Modify: `frontend/components/plugins/use-media-assistant-workspace.ts`
- Modify: `frontend/components/assistants/assistant-catalog.tsx`
- Modify: `frontend/components/assistants/assistant-detail.tsx`
- Modify: `frontend/components/assistants/assistant-workspace.tsx`
- Create: `frontend/components/assistants/assistant-history.test.tsx`
- Modify: `backend/app/api/assistant_actions.py`
- Modify: `backend/app/assistant/plugin_history.py`

**Steps:**

1. 新增失败用例：没有 installation/view/PluginDocument 但有旧会议历史时目录仍有会议助手；卸载后只读历史及可信取消可用；其他未知插件不误显示会议历史。
2. `Frontend: pnpm exec vitest run lib/assistant-workspace-state.test.ts components/assistants/assistant-history.test.tsx --reporter=dot`，预期新历史断言 RED。
3. 在 catalog 输入增加通用 `historySources`，按 plugin ID 合并并去重；不在 React 目录硬编码会议组件。保留课程 PluginDocument 的现有过滤、下载和语言行为。
4. Host 历史元数据通过只读 API 获取；失去插件运行时不丢历史。操作取消由 Host 描述驱动，resume/execute 在停用时不出现可用入口。
5. 保留独立 `/assistants` 历史页；侧栏打开管理/历史链接仍为新标签页和 `noopener noreferrer`，不能导航卸载当前音频组件。
6. 执行前端聚焦测试与 `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_history.py -q`，预期 PASS。

### Task 13: Host 组合、配置与旧 API 权限收口

**Files:**

- Modify: `backend/app/main.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/api/assistant.py`
- Modify: `backend/app/api/meeting_state.py`
- Modify: `backend/app/api/media.py`
- Modify: `backend/app/assistant/bootstrap.py`
- Modify: `backend/app/plugins/bootstrap.py`
- Create: `backend/tests/test_meeting_plugin_lifecycle.py`
- Create: `backend/tests/test_meeting_plugin_legacy_api.py`
- Modify: `backend/tests/test_media_api.py`

**Steps:**

1. 枚举旧 API 所有 POST/PATCH/cancel/input 入口，写表驱动失败测试：无 intent、插件停用、伪造旧 Grant、跨会话/版本必须拒绝；GET 只读且不依赖活动 runtime。
2. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_legacy_api.py tests/test_meeting_plugin_lifecycle.py -q`，预期 RED。
3. App composition 创建唯一策略/只读服务/operation runtime，供插件 Broker、Projector、旧 API 和 ToolExecutor 共用。可用性由 Host 配置上限和插件状态共同决定，`ASSISTANT_ENABLED` 不能成为独立分析的旁路。
4. 启动时 gate 默认关闭，恢复持久资格后才准入；stop 时先撤准入，收束 operation/projector，再退出容器和 DB。不得出现插件已恢复而策略尚未加载的 allow-all 窗口。
5. 旧写路由委托同一 Host 操作服务或明确返回迁移错误，绝不保留接受客户端自造 Grant 的快速路径；旧 read 复用 DTO 和 repository。scope 不合法和 intent 过期应使用明确错误，不把全部失败包装成 500。
6. 新注册动作需要检查当前 manifest/安装版本/绑定与 expected view version；旧通用 plugin-commands 不能给会议动作绕过可信流程。
7. 测试框架关闭/会议服务关闭/Linear缺失/插件未安装/停用/崩溃/自然结束/重启及两版本 owner 矩阵。重跑聚焦、原 assistant、media API 与插件 E2E，预期 PASS。

### Task 14: 删除独立挂载，验证媒体生命周期

**Files:**

- Modify: `frontend/components/room-studio.tsx`
- Modify: `frontend/components/room-studio.test.tsx`
- Modify: `frontend/components/assistants/room-assistant-sidebar.tsx`
- Modify: `frontend/components/assistants/studio-sidebar.test.tsx`
- Remove after parity: `frontend/components/private-meeting-assistant.tsx`
- Modify if orphaned: `frontend/app/globals.css`

**Steps:**

1. 先在真实 RoomStudio DOM 测试中挂两个 fake 插件（课程/会议），启动 fake tab track，再执行会议侧栏交互；新行为断言在移除旧卡片前应显示存在重复入口或缺少统一操作。
2. `Frontend: pnpm exec vitest run components/room-studio.test.tsx components/assistants/studio-sidebar.test.tsx --reporter=dot`，记录目标失败。
3. Task 10–13 功能等价通过后才删除旧 import/挂载；所有控制留在统一工作区。确认没有其他引用再删除旧组件；原 API 类型若仍用于历史必须保留。
4. 折叠/展开、切换插件、改变侧栏宽度、问答/确认/取消不得调用 track.stop、unpublishTrack、disconnect，也不得重新创建字幕 DOM。整个 RoomStudio 真正 unmount 时原清理仍执行。
5. 查看历史、打开插件管理用原有安全导航方式；不新建会议专属 route 来承载当前会话交互。
6. 重跑聚焦命令并执行 `Frontend: pnpm typecheck`，预期 PASS。检查页面不再出现原独立卡片，但插件未安装时有明确安装入口而非静默消失。

**Batch D gate:** 统一入口唯一；旧写 API 无旁路；七类旧功能/历史可用；两个插件切换时音频 cleanup 调用数为零；真正卸载时 cleanup 正常。

## Batch E — 综合验证、受控上线与记录

### Task 15: 端到端回归和故障注入

**Files:**

- Create: `backend/tests/test_meeting_plugin_e2e.py`
- Modify: `backend/tests/test_plugin_event_delivery.py`
- Modify: `backend/tests/test_plugin_framework_e2e.py`
- Modify: `backend/tests/test_course_organizer_e2e.py`
- Modify: `frontend/lib/plugin-ui-schema.test.ts`

**Steps:**

1. 编写完整场景：临时 Host 安装/启用会议和课程插件→同一会话→会议未激活→课程继续消费→激活会议并补齐→新 Final/修订→问答/mark→准备/取消授权→重新确认→FakeLinear create→needs_input/cancel→自然结束→查看历史。
2. 注入 RPC 响应丢失、写成功响应超时、插件 crash、Host restart、duplicate command、停用与排队并发、旧版本迟到回调；断言不重复执行和不跨会话。
3. 即使没有新 MediaEvent，挂起后完成的问答仍刷新视图。验证当前课程实时笔记和最终文档未被会议路径修改。
4. 前后端同时验证 Task 10 视图 fixture；不只测试生产者认为合法，而要测试实际前端 parser 接受。
5. `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_e2e.py tests/test_plugin_event_delivery.py tests/test_plugin_framework_e2e.py tests/test_course_organizer_e2e.py -q`，预期 PASS；`Frontend: pnpm exec vitest run lib/plugin-ui-schema.test.ts --reporter=dot`，预期 PASS。
6. 执行五次并发回归（每次作为单独命令，不用长阻塞 sleep）：
   `Backend: .\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_projector.py tests/test_meeting_plugin_operations.py tests/test_meeting_plugin_execution_guard.py -q`。
   预期五次均 PASS，无遗留 asyncio task、DB lock 或重复 write。

### Task 16: 真实隔离容器冒烟，外部服务保持模拟

**Files:**

- Create: `backend/smoke/meeting_assistant_plugin_smoke.py`
- Modify: `backend/tests/test_meeting_plugin_packaging.py`

**Steps:**

1. 参考现有 framework/course smoke，使用唯一临时工作目录、临时 SQLite、专用签名 key、唯一镜像 tag、FakeModel/FakeLinear。启动前只检查 Docker 是否可用，不重置、不修复或重启 Docker Desktop。
2. 构建→签名检查→确认安装→启动→绑定→显式激活→投递字幕→查询 view→可信确认→模拟执行→停用→读取历史→重启恢复。容器不得有网络或 Host 数据挂载。
3. 默认运行：`Backend: .\.venv\Scripts\python.exe smoke/meeting_assistant_plugin_smoke.py`。脚本不得因本机 `.env` 设置而切换到真实模型/Linear。
4. 输出仅安全布尔值和计数，例如：

```json
{
  "signed_package": true,
  "network_isolated": true,
  "activation_required": true,
  "backlog_processed": true,
  "view_valid": true,
  "external_create_count": 1,
  "disable_blocks_new_writes": true,
  "history_preserved": true,
  "recovery_no_duplicate": true
}
```

5. 退出码仅在所有断言满足时为 0。cleanup 只针对脚本记录的容器 ID、镜像 tag 和临时目录；清理前验证所有权和最终绝对路径。不删除其他课程或 LiveKit 容器。
6. 若 Docker 不可用，记录“真实容器验证未完成”，继续不依赖 Docker 的检查；不得把单元测试当作容器通过证明。

### Task 17: 全量检查、迁移演练与验收证据

**Files:**

- Modify: `backend/tests/test_meeting_plugin_migration.py`
- Modify: `frontend/components/room-studio.test.tsx`
- Modify: `docs/stage-records.md`

**Steps:**

1. `Backend: .\.venv\Scripts\python.exe -m pytest -q`。预期全部通过；逐项说明任何 skip/warning，不复用旧计数。
2. `Frontend: pnpm exec vitest run --reporter=dot`。预期全部通过。重复 Task 14 DOM/媒体测试五次，均不在侧栏交互中清理音轨。
3. `Frontend: pnpm typecheck`，预期退出 0；然后 `Frontend: pnpm build`，预期构建成功且 `/`、`/assistants`、`/plugins` 保留。命令不要与运行服务的终端混用。
4. 对临时旧版 fixture 库再次验证增量迁移、旧行数/ID/引用、完整性与外键检查；停用/卸载后的 API 历史读取仍正常。生产备份副本的只读校验与迁移演练需先确认范围，不能直接拿生产库练习。
5. 人工/可用浏览器工具验证侧栏窄宽度、键盘确认/取消、状态文案、history 与 Linear 预览。若当前浏览器工具对 URL 有安全拒绝，不换工具绕过；记录真实 UI 验证未完成，保留 DOM/API 证据的准确边界。
6. 按设计验收矩阵逐项填写证据。模型/真实 Linear 未调用写清楚，不宣称“全链路真实外部执行已验证”。

### Task 18: 操作文档和有边界的运行环境切换

**Files:**

- Modify: `README.md`
- Modify: `docs/plugin-operations.md`
- Modify: `docs/stage-records.md`
- Modify: `plugin-sdk/README.md`
- Modify: `plugin-sdk/examples/meeting-assistant/README.md`
- Modify: `docs/plans/2026-08-31-meeting-assistant-plugin.md`

**Steps:**

1. 写清用户路径：插件管理安装会议助手→启用插件→在助手侧栏选择它→本次会话启用会议分析→查看进度/问答/标记→选择待办并通过主程序确认→在同一入口查看历史。
2. 说明“安装/启用”“会话分析”“侧栏显示”的差别；未配置 Linear 不影响问答；停用不撤回已创建任务；卸载不删除历史；重新启用不会重放旧外部执行。
3. 记录 SDK 能力、可信动作 API、状态字段和错误码；声明 plugin confirmation 不是授权。列出不支持项，不把此次迁移包装成新增会议总结或多账户系统。
4. 如果用户尚未授权运行环境修改，停止在“实现与隔离验证完成、待上线”，明确需要的安装/迁移/重启影响。不能从设计确认推断真实模型回放或 Linear 写入授权。
5. 运行环境切换前：只读核对当前 PID/服务、活跃音频/执行、数据库位置和 migration head；用 SQLite 一致性备份并验证完整性；记录文件位置/摘要与旧记录基线，不打印密钥或私密正文。
6. 在无活跃音频或用户接受影响的窗口，运行受控增量迁移和必要 API/前端重启；只操作已验证的本项目进程。标准 inspect/confirm 路径安装会议包并展示权限，不能绕过管理员确认。
7. 先验证 health、安装状态、未激活零分析、历史可见；用户明确激活测试会话后才验证实际字幕处理。真实模型调用和真实 Linear 创建按各自授权范围执行，缺少授权就保留对应待验项。
8. 如果切换失败，停止新增任务并记录错误，保留增量表及旧数据；不自动降级或覆盖运行中 DB。恢复备份需要确认具体目标、停止写入及新的恢复授权。
9. 最终报告已完成批次、实际测试数量、运行环境是否已切换、未验证/待授权事项。完成项逐一打勾，不将计划编写或代码测试冒充生产验收。

**Batch E gate:** 功能和安全矩阵有真实证据；Docker/浏览器/模型/Linear 各自验证边界准确；部署和外部副作用不越权。仍有必要验收或部署步骤时，不标记整体“全部完成”。

### Batch E 实施记录（2026-09-01）

- Task 15 完成：新增真实 Host/Broker/controller/worker 的临时端到端场景，课程与会议同会话隔离；覆盖显式激活补齐、修订、问答、标记、外部预览取消/确认、needs_input/input/cancel、自然结束和历史。注入 RPC 回复丢失、远端写入后超时、插件崩溃、Host 重启、重复确认、停用/排队竞态与旧 scope 回调，外部 create 始终不重复。综合组 `17 passed`；前端 parser `11 passed`；并发组五轮均 `36 passed`。
- Task 16 完成：真实构建、签名、检查/确认、安装、启动、绑定、崩溃恢复和重启恢复均在唯一临时 Host 中执行；容器 `network none`、只读根、无 Host bind/mount。安全输出八项均为 true，`external_create_count=1`。脚本忽略 `.env`/环境服务配置，模型与 Linear 仅使用 fake，清理只碰本次记录的容器和唯一镜像标签。
- Task 17 完成源码与隔离验收：后端 `769 passed, 4 skipped, 1 warning`；前端 `119 passed`；typecheck 退出 0；独立目录 production build 生成 `/`、`/assistants`、`/plugins` 和既有动态路由。临时旧 head→`20260831_0027` 的重复升级、旧行/ID/外键/完整性与降级演练通过。RoomStudio/侧栏媒体测试五轮均 `14 passed`。
- 浏览器验证当前 `127.0.0.1:3000`：助手在同页侧栏展开，不导航离开字幕页面；键盘 ArrowRight 将宽度从 384 调到 408，可收起；插件管理显示课程、会议和诊断插件。会议助手当前运行实例显示完整状态、私密问答、待办候选、重点与证据、执行进度及历史区域，未点击任何分析或外部执行动作。
- E2E 揭示并修复两项真实缺口：插件视图未显示 FastTurnRunner 的 `response_text`；外部结果核对成功后 Action Run 返回 `planning` 却未在同轮继续，导致执行停滞。均有防回归测试并纳入全量回归。
- Task 18 完成。切换前 API `ready`；3 条非终态旧会话均无房间、音轨、ASR、FFmpeg 或事件活动。SQLite 一致性备份位于 `.local-backups/meeting-plugin-deploy-20260901-final/live_caption.pre-20260831_0027.db`，SHA-256 为 `C1F1754F4361E2EEE68DD4A3CDC179411CDB9E93A675209873B6562698139936`；备份 `integrity_check=ok`、外键违规 0、head `20260828_0026`。
- 生产迁移已到 `20260831_0027`；迁移后 `integrity_check=ok`、外键违规 0、旧表行数与备份无差异，三张新控制表为空。生产前端 `pnpm run build` 成功，生成 `/`、`/assistants`、`/plugins`、`/sessions/[sessionId]`。第一次普通沙箱迁移因写权限失败，确认没有半迁移表后以授权权限执行同一迁移成功。
- 首次 OCI inspection 因 Docker 拉取 `python:3.12-alpine` 遇到 CloudFront EOF；首次正常重启还恢复了一条历史课程 `1.0.0` completed-but-open 绑定并产生 4 次 DeepSeek HTTP 400。发现后立即停机并进入安全模式，没有成功模型结果或 Linear 写入。经用户明确授权，先备份数据库，再只关闭该绑定；预拉基础镜像成功，digest 为 `sha256:d81968c559557b881aa557ff6d1200acec8e72a2c85fcb4ad1806e8d13e09f0b`。
- 恢复正常插件框架后，会议插件 `com.matinier.meeting-assistant` 1.0.0 通过标准 inspect/confirm/enable 安装；publisher trusted、signature verified，安装状态 enabled、runtime ready、crash 0、无 quarantine。请求权限严格为 10 项 meeting/state/ui 能力，没有任意网络或通用外部写能力。
- 真实 UI 首验发现 Pydantic 持久化把可选 `placeholder` 重新序列化为 `null`，前端严格 parser 因而安全降级。新增通用回归测试并将 Host 视图持久化改为 `exclude_none=True`；在一致性备份 `.local-backups/meeting-plugin-deploy-20260901-final/live_caption.pre-ui-normalize.db`（SHA-256 `CE9D8F3C2564A5BB047CA9C014B0F9EA065A81BE2C4036AFD4CB097C54A07A30`）后，仅规范化 1 条会议派生视图。数据库完整性与外键检查保持正常。
- 最终全量后端 `769 passed, 4 skipped, 2 warnings`，前端 `119 passed`，typecheck 与 compileall 退出 0；浏览器重载后会议视图完整可用。未激活验证中 `meeting_plugin_sessions`、`meeting_plugin_operations`、`assistant_action_intents` 均为 0，`model.invoke` 为 0；只有状态读取/写入和 UI 发布。已有旧会议历史以 `host_history` 只读来源可见，最终后端日志无 DeepSeek、事件投递失败、quarantine 或 ERROR。
- 真实会议字幕分析、真实模型成功调用和真实 Linear 创建仍未执行；它们属于用户后续明确激活/外部写入授权，不影响 Task 18 的有边界切换验收。

## 2. 完成检查表

- [x] A：原能力基线、增量迁移、会话资格与字幕补齐
- [x] B：只读历史、可信意图、持久操作、外部执行守卫
- [x] C：通用构建注册表、会议插件、完整声明式视图（源码/模拟打包验证，真实 Docker 见 E）
- [x] D：通用可信控件、历史目录、旧 API 收口、独立卡片移除（源码与隔离测试）
- [x] E：E2E、Docker、全量回归、操作文档、生产备份/迁移、标准会议插件安装、正常插件运行与当前浏览器视图验收（整体 18/18）

## 3. 计划阶段验证记录

2026-08-31：设计分段已获用户确认；代码路径、旧 API、Projector 全局扫描、Broker 写锁、内置课程专用构建和通用目录来源已只读核对。计划编写期间未修改业务代码、数据库、依赖或服务，未调用模型或 Linear，未安装会议插件。上述任务及测试命令均为待执行，不代表已通过。

文档静态检查：5 个批次、18 个任务；128 处文件条目中，所有标为现有文件的路径都存在或已在前置任务声明创建；49 个路径明确标为拟新增。两份文档代码围栏成对，无 Unicode 替换字符。该检查不是代码测试、独立架构审核或运行环境验收。

## 4. Batch A 执行与验收记录（2026-08-31）

- Task 1：旧功能指定基线在业务改动前为 `12 passed in 1.67s`；新增临时 SQLite 历史夹具及 4 项兼容测试，保留原 ID、Fast Turn/Action Run 父子链、候选、手动/自动标记、证据修订和坐标、输入/取消版本、unknown claim 与已完成的外部引用。模型/Linear 使用可控 fake，不访问真实服务。
- Task 2：新增有界、禁止额外字段的会议输入及 Host intent 契约；新增三张控制表与 `20260831_0027` 增量迁移。操作同键同 hash 返回原记录、不同 hash 冲突；一次性票据使用条件更新消费，只存 hash。临时库升级、重复升级、降级演练前后 9 张旧表数据完全一致，外键和完整性检查通过。
- Task 3：默认拒绝未激活会话；安装/绑定/展示不等于启用分析。按会话记录独立 analysis/authority epoch；扫描、补齐、队列、每次模型调用、失败记录和提交均检查资格。提交使用持久条件更新取得写入屏障；停止后旧结果不可写回。激活补齐游标以前的 Final，按 revision 去重；自然结束只处理已激活会话的冻结 frontier。运行中的历史/未激活问答不隐式补算。
- 主代理自审增加确定性竞态回归，并修正四处边界：终止切换时被丢弃的在途批次可能被游标遗漏；模型调用前也须检查终止 frontier；会议撤销不得影响其他插件票据；新一轮分析不得完成旧 epoch 的 catch-up 等待者。没有以额度受限的旧并行审查冒充本批独立审核。

最终验证（工作目录 `backend`）：

1. 本批全部新测试与计划指定旧基线：`65 passed in 12.78s`，其中本批新增 53 项，旧基线 12 项。
2. `python -m pytest -q -rs`：`620 passed, 4 skipped, 1 warning in 71.30s`。
3. policy/projector/repository 聚焦组连续五轮：每轮 `32 passed`，耗时依次 5.46、5.56、4.92、4.88、4.95 秒。
4. `python -m compileall -q app alembic tests`：退出 0。

4 项 skip 为 Windows 平台的三项符号链接条件与一项 WSL Bash 工作区路径条件；warning 为既有 Starlette TestClient/httpx 弃用提示，未为此升级依赖。前端没有修改或重测，未做浏览器、Docker 或真实外部服务验收。

边界：只执行 Batch A；Batch B 的可信操作 API/worker/执行守卫、Batch C 的标准会议插件包、Batch D 的目录/前端/旧 API 收口及生命周期组合均未接入。新的会话控制目前为内部 Host 基础接口，不是网页已可使用的插件入口。生产数据库未迁移，服务未重启，课程插件、现有字幕与历史文档未改动；不要单独将本批作为完整迁移版本上线。按执行技能停在批次检查点，等待下一批指示。详细记录见 `docs/stage-records.md` 同日“会议助手统一插件化 Batch A”。

## 5. Batch B 执行与验收记录（2026-08-31）

- Task 4：新增原记录只读服务与 Host 历史适配，不复制正文到 plugin state。保留原 Session/Execution/父子链、证据与坐标；显式 SQLite 读快照使状态和事件游标一致；分页有界，计数使用当前 Final/revision 和投影 offset。查询不启动模型或分析；未安装、停用或移除安装后仍可由 Host 读取历史。
- Task 5：闭合 Host 动作注册表、prepare/confirm、精确 Origin/JSON/custom-header 与短期 UI nonce；无 Origin 需要单独 Host 凭据。客户端不能指定 actor、Session 或写授权。确认后才发放一次性 intent；外部操作额外绑定 Host 生成的 capability grant、候选修订、固定团队和预算。只存票据 hash，双确认返回同一票据；历史取消走受限 Host 通道。
- Task 6：受理在调用方短事务内消费 intent、写 operation、预留原 execution 幂等身份及 ActionGrant；与 Broker invocation 原子提交。固定并发 worker 从持久表恢复，释放 DB Session 后才等待引擎。Fast Turn 中断且模型结果未知时不自动重做；队列停止有界。模型和外部请求等待期间不持有全局插件锁或 SQLite writer。
- Task 7：显式注册七种 meeting 能力；适配器接收 AuthorizationDecision，检查当前进程 generation、owner、scope、安装权限及确认范围。最终 ToolExecutor 再检查 authority epoch、原 ActionGrant、capability grant、团队、候选/证据、期限及预算，检查与 requesting 持久化和撤权共用写事务边界。停用先撤权再等容器；旧无 ownership、撤权或结果不确定的恢复只读核对/待确认，不重新创建任务。模型、修复重试、子分析及结果提交增加授权检查点。禁用 Linear 集成时仍可问答/本地操作。
- 公开 schema 从模型重新生成：仅 `plugin-capabilities.schema.json` 改变，其余四份字节 hash 不变。
- 主代理自审修正：`deactivate` 被 `endswith("activate")` 误匹配；scope 不能由可省略的客户端字段导出；明文 intent 不进入 Broker ledger；模型修复重试/子分析结果也需要检查授权；状态列表分页不能漏报 has_more；公开输出继续过滤凭据字段。新增 fake/临时数据库回归证明以上边界，没有独立代理审核。

最终验证（工作目录 `backend`）：

1. Batch B 七个新增测试文件：收集到 65 项；连同计划指定旧用例、契约/API 回归：`107 passed, 1 warning in 19.65s`。
2. 最终 `python -m pytest -q -rs`：`685 passed, 4 skipped, 1 warning in 110.16s`。此前首轮为 678 passed，不能替代最终结果。
3. 重复并发组为 projector / operations / execution_guard：每轮 36 项，全部五轮结果记录于 `docs/stage-records.md`。
4. `python -m compileall -q app alembic tests`：退出 0；SDK 生成与一致性测试通过。

四项 skip 仍为三项 Windows 符号链接条件和一项 WSL Bash 工作区路径条件；warning 为既有 Starlette TestClient/httpx 弃用提示，没有升级依赖。模型和 Linear 均用 fake，含模拟响应丢失后只核对一次已有任务；这不是实际 Linear 写入或真实容器验收。

边界：本轮只完成 Batch B。API 路由尚未注册到主应用，Host action/operation 的唯一实例组合、旧 POST/PATCH 权限收口仍在 Task 13；Fast Turn 旧只读路径暂保留，最终外部写与启动恢复已有守卫。标准会议插件包、声明式视图、通用可信控件、历史目录及独立卡片移除留在 Batch C/D。未进行生产数据库迁移、安装、服务重启或历史回放；未改前端、未做浏览器/Docker 验收。后续须完成接入再受控上线，不能将本批后端通过当作网页已可使用。按执行技能在批末暂停，等待下一批指示。

## 6. Batch C 执行与验收记录（2026-08-31）

- Task 8：固定 `BuiltinPluginSource` 注册表分别捕获课程/会议源码闭包，复用 digest/snapshot/build；课程公开 wrapper 和 digest 算法兼容。
  新增会议 manifest、非 root Dockerfile、CLI 打包入口与 built-in descriptor。缓存、快照及临时 image tag 按插件隔离；共享签名密钥只在创建/读取时使用短锁，两个插件仍可同时构建。
  原课程包保持 1.0.3，未修改课程源代码、SDK、已安装版本或生成文档。
- Task 9：SDK 生命周期、每会话串行 controller、Host DTO 边界验证、先受理后查询 operation、默认 2 秒有界轮询、内容去重、重连及关闭回收。
  `event.batch` ACK 不等待 Host 刷新；事件不触发分析或外部动作。原 request ID 生成稳定幂等 key；票据不进 state/view。
  state 仅保存 UI/游标/待查 operation ID。受理响应未知不自动重做；查询或 UI 发布连续失败三轮暂停自动重试，保留成功内容。
- Task 10：六区声明式视图及 12 类状态夹具，复用 tabs/section/card/列表/表格/坐标/表单，无新 schema 类型。
  保留原 ID、root/parent、候选修订、手动/自动标记、needs_input、unknown、已确认副作用及 HTTPS 任务链接数据。
  同一批 Python 生成 JSON 由后端校验相等并由前端真实 parser 回归。补齐列表事件游标、执行详情翻页、最新执行独立跟踪。
- 主代理自审修正：跨插件首次构建密钥竞态；Supervisor 实际使用 `command.execute.values`；同会话关闭取消挂起命令；
  SDK transport shutdown 回收 poller；UI 发布失败不能杀死刷新任务；操作在状态读取之后完成时须补读一次；旧页不能掩盖最新执行。
  没有独立代理审核；并发测试使用 Event/可控 clock，不靠 sleep 碰时序。

最终证据：

1. 三个新增专题测试文件：`45 tests collected`；既有打包测试增加两插件参数化覆盖 6 项，后端总数较 Batch B 增加 51 项。
2. Task 8–10 + 课程打包/内置缓存/UI schema 聚焦组：`84 passed, 3 skipped in 11.68s`。
3. 后端最终 `python -m pytest -q -rs`：`736 passed, 4 skipped, 1 warning in 111.91s`。
4. controller + meeting packaging 连续三轮均 `29 passed`，耗时 1.32、1.32、1.23 秒。
5. 前端 `pnpm test`：13 个文件、`100 passed`（含 13 个新增会议共享视图断言）；`pnpm run typecheck` 退出 0。
6. `python -m compileall -q app scripts tests ../plugin-sdk/examples/meeting-assistant` 退出 0。
   首次普通沙箱因已有缓存写权限失败，随后相同命令经审批通过；没有以改权限/删目录绕过。

后端四项 skip 是三项 Windows 符号链接条件和一项 WSL Bash 路径条件；warning 为既有 Starlette/httpx 弃用提示，未升级依赖。
模拟 Docker 仅提供测试镜像字节，真实临时 Ed25519 验证签名/摘要/缓存；**未生成或运行真实可安装 OCI 镜像**，不将其计作 Task 16。

交付边界：本轮只完成 C，整体 10/18。尚未安装会议插件、注册主应用可信操作路由、迁移生产 DB、重启服务或移除旧卡片。
插件普通按钮仅请求 Host 确认；`apply_action` 不在 view 的 actions 中，由 Batch D 主程序在确认后转交。
现有 safe_markdown renderer 仍以纯文本呈现，链接可点击性及完整交互需在 D/E 接入并浏览器验收；本批不冒称网页可用。
详细契约见 `plugin-sdk/examples/meeting-assistant/README.md`。按执行技能停在本批检查点，等待下一批指示。

## 7. Batch D 执行与验收记录（2026-08-31）

完成 Task 11–14，整体 14/18（约 78%）。沿用无 Git 的原工作区，使用 `executing-plans`；本批只有主代理自审，没有独立代理审核。

- 通用 HostActionPanel 从主程序 descriptor 构建控件；prepare/confirm、取消、过期、冲突、响应未知重试均按稳定 request ID 处理。
  plugin view 只可引导和预填 Host 声明的数据，不可自动签发意图；外部写入必须确认候选、固定团队、上限和期限。
- nonce 只保留在 UI 内存；intent capsule 由 Host 内部经 Supervisor 转交。HTTP 不输出 token；受理结果必须匹配持久 operation。
  修正 C 的短动作名称，实际 wire 使用完整 `meeting.*`；普通 plugin-commands 明确拒绝 apply_action 旁路。
- Host 历史加入通用目录，不依赖 installation/view/PluginDocument 或活动 runtime。框架关闭也可查看；只提供可信取消，不提供 resume/execute。
  未建立媒体映射的老记录只读可见；显式取消才创建映射。页/事件游标、执行详情、根父链、证据坐标与安全 HTTPS 引用保留。
- 主应用注册动作/历史路由和 CORS 自定义头，共享 HostActions/operations/policy；启动恢复前 gate 关闭，退出先关闭 gate，再收束后台任务。
  生产分析资格检查 runtime ready/degraded；崩溃、隔离或停止不能继续分析。分析启停确认后主动刷新插件，激活安排真实本会话 catch-up。
- 旧 5 类写入口返回 410 迁移提示，旧 GET 脱离活动 runtime。原独立组件及无引用样式移除；课程插件输入和文档逻辑保留。
  原组件已逐字节核验并备份至 `.local-backups/meeting-plugin-batch-d-20260831/private-meeting-assistant.tsx`，没有删除会议/字幕/课程数据。

最终验证（全部使用测试数据/fake 服务）：

| 验证 | 结果 |
| --- | --- |
| 后端全量 `pytest -q --tb=short --show-capture=no` | 763 passed、4 skipped、1 warning，124.14s |
| 前端全量 Vitest | 119 passed，17 文件，3.70s |
| Host bridge / lifecycle / controller 连续 3 轮 | 每轮 40 passed，6.35 / 7.27 / 7.28s |
| TypeScript typecheck / Python compileall | 均退出 0 |
| CSS 语法检查 | 使用已安装 PostCSS 解析通过；未安装或升级依赖 |
| RoomStudio DOM 音轨检查 | 问答/确认/取消/切换助手/折叠/调宽时 cleanup=0；真正 unmount 清理正常 |

新增后端 27 项、前端 19 项；此前 749/761、113/117/118 通过为过程记录，不替代最终结果。
四项 skip 和 Starlette/httpx 弃用 warning 沿用既有平台条件；没有放宽权限或跳过失败来通过测试。
补充 CSS 检查首次因 pnpm 非顶层依赖解析失败，定位现有包路径后通过，没有修改依赖。

边界：本轮未运行生产迁移、服务/Docker 重启、真实构建/安装、模型回放、Linear 写入或浏览器实测。
源码接通不等于运行环境已切换。下一批 E 为综合故障验收、真实隔离容器、浏览器及授权范围内的受控上线；按执行技能在批末汇报并暂停。
