# Private Meeting Assistant：面试演示手册

本文只定义两条演示：一次私密实时问答，以及一次授权后的 Linear 任务动作。不要在面试
现场增加故障、恢复或性能演示。

> 状态（2026-09-02）：scripted Agent Eval、local-media 全链路和一键离线演示已通过；真实
> 浏览器 WebRTC、真实模型和真实 Linear 专用 Team 仍待操作者执行。离线结果不能标记为
> “真实 Provider/人工演示已验收”。

## 面试前离线预检

在项目根目录执行：

```powershell
.\meeting_agent_demo.cmd
```

命令不读取云密钥，不要求 LiveKit、DeepSeek、百炼或 Linear 在线。它会重跑 12-case × 3 trials、
两个 `local_media_diagnostic` 场景和五个演示故事；成功时打印 `overall_result=PASS`、硬 Gate
通过数、36 条产品 Trace 和 2 条全链路 Trace 路径。报告写入
`reports/meeting-agent-product-v1/`，重复执行使用新的临时 SQLite，不会累积外部副作用。

## 一次性准备

1. 在 Linear 新建专用 Team，例如 `Matinier Interview Demo`，确认其中没有上次演示遗留的
   Matinier Issue。创建仅能访问该 Workspace/Team 的 API key，并记录 Team ID。可选的
   Project ID 也必须属于该 Team。
2. 从项目根目录复制 `.env.example` 为 `.env`，至少设置：

   ```dotenv
   ASSISTANT_ENABLED=true
   DEEPSEEK_API_KEY=<demo-key>
   TASK_SYSTEM_PROVIDER=linear
   LINEAR_API_KEY=<demo-key>
   LINEAR_TEAM_ID=<dedicated-demo-team-id>
   LINEAR_DEFAULT_PROJECT_ID=
   ```

3. 升级数据库并启动应用：

   ```powershell
   Set-Location backend
    .\.venv\Scripts\alembic.exe upgrade head
    .\.venv\Scripts\alembic.exe current
    Set-Location ..
    .\start_demo.cmd
    ```

   `alembic current` 必须显示 `20260828_0025 (head)`。`start_demo.cmd` 也会默认执行
   `upgrade head`；上面的显式命令用于在演示前单独确认数据库，而不是要求重复迁移。

4. 打开 `http://127.0.0.1:8000/health/ready`，确认：

   - 顶层 `status` 为 `ready`；
   - `assistant.enabled` 与 `assistant.runtime_started` 为 `true`；
   - `assistant.task_adapter_provider` 为 `linear`；
   - `assistant.task_adapter_available` 为 `true`；
   - `assistant.tool_call_unknown_count`、`assistant.tool_call_reconciling_count` 和
     `assistant.recovery_backlog_count` 在演示前均为 `0`；
   - `assistant.fast_turn_active_count`、Action queue/active 与最老等待时间没有异常积压。

   readiness 不会发起 Linear 或模型请求；它只能证明本地配置和运行时已装配。真实 Team 权限与
   Issue 写入必须由演示二实际确认。

## Trace 诊断

结构化日志中的 `trace_id` 等于 root execution ID；使用 `execution_id` 可定位 Fast 或 Action
分支，使用 `tool_call_id` 可定位工具、Grant、Claim 与 reconcile。日志字段包含 `phase`、
`duration_ms`、`retry_count` 和 `error_code`，不会包含会议问题、任务描述或密钥。离线报告中的
Trace 路径可直接用于复盘；真实页面验收时需另外记录 Session ID、MediaSession ID 和 trace_id，
用于核对本次 Final segment/revision 是否成为 Trace 的 input anchor。

5. 打开前端，新建一个专用于本次演示的 Room 与字幕 Session。使用麦克风或本地媒体说出
   一段包含明确事项的话，例如：

   > 发布准备清单需要在周五前完成，交付物是经过评审的上线检查表。

   等待至少一条 Final 字幕出现，并等待私人助手的新鲜度显示为 `ready`。这一步的 Session
   同时供下面两条演示使用。

## 演示一：实时字幕到私密问答

1. 保持字幕输入进行中，确保页面上同时可见已有 Final 和刚出现的最新 Final 尾部。
2. 在 `Private assistant / 私人会议助手` 输入：

   > 刚才明确的交付物和截止时间是什么？只依据会议内容回答。

3. 点击“询问”，不要选择“执行到 Linear”。
4. 展示结果卡：它应在 Fast Turn 时限内给出私密回答。说明服务端要求每条 claim 绑定冻结的
   会议证据，回答使用 Meeting State Head 加当前尚未投影完成的 Final tail，而字幕链路没有
   等待它。当前默认卡片只展示回答文本，不单独展开 claim 的证据 ID。

重置：清空输入框即可再次讲解；不要删除字幕。若必须从空状态重演，结束当前字幕 Session，
在同一 Room 新建 Session。此演示不会创建任何 Linear 数据。

## 演示二：ActionCandidate 到 Linear

1. 在同一 Session 等待私人助手显示一个由上述会议内容提取的有效 ActionCandidate。只选择
   这一个 Candidate；不要同时选择其他事项。
2. 输入：

   > 将选中的事项执行到 Linear；先检查是否已有同一事项，完成后读回并给我链接。

3. 点击“执行到 Linear”一次。该操作会创建一个 15 分钟 ActionGrant；不要重复点击，也不要
   新建第二个执行请求。
4. 展示 Action Run 卡片的状态变化。服务端会用 Candidate lineage 构造幂等 action key，依次
   搜索 Linear、在没有同一事项时创建，并通过 `get` 读回验证。三个只读子代理分别检查会议
   证据、冲突和 Linear 相关项；外部写入仍受 Grant 限制。
5. 完成后点击结果卡中的 Linear URL，在专用 Team 中确认至多出现一个 Issue。检查标题、截止
   日期（若会议已明确）、会议证据标记和 `matinier-action-key` 均存在。

重置与清理：演示结束后，在 Linear 网页中手动取消或归档刚创建的唯一 Issue，并确认专用
Team 中不再有活动的本次演示 Issue。随后结束字幕 Session。若因页面刷新需要重试，必须复用
同一 Session/Candidate，让相同 action key 命中已有 Issue；不要用新 Session 重试，否则会形成
新的 Candidate lineage。在清理完成前不要再次运行第二条演示。

## 当前已知边界

- 未知 `issueCreate` 的恢复路径当前会校验 action key 和 evidence marker，但尚未再次核对规范化
  标题；不要把本演示描述为生产级 exactly-once 外部事务。
- 未解析负责人会保留会议原文且不会自动选择相似 Linear 用户，但当前 UI 没有固定的专用占位
  提示。演示二优先使用不要求负责人或负责人能唯一解析的 Candidate。
- 直接 Action Run 的 Snapshot 已持久化，但其生命周期日志尚未单独发出 `context_frozen` 记录。
- 演示仅允许创建一个真实 Issue，不演示故障注入、删除 API、批量动作或长期授权。

## 人工验收记录

执行后填写；未执行项保持“待执行”。

| 项目 | 状态 | 时间 / 备注 |
|---|---|---|
| 数据库为 `20260828_0025 (head)` | 待执行 | |
| `/health/ready` Assistant 状态符合预期 | 待执行 | |
| 演示一：实时字幕 → 私密 Fast Turn | 待执行 | |
| 演示二：Candidate → Linear create/get/link | 待执行 | |
| 专用 Team 中至多创建一个 Issue | 待执行 | |
| 演示 Issue 已手动归档或取消 | 待执行 | |
