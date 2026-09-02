# Meeting Agent 工程案例

## 问题与边界

Matinier 的会议助手同时面对两类请求：用户希望即时得到私密回答，也希望把会议中的明确事项安全地执行到外部任务系统。前者要求低延迟，后者需要较长的分析、显式授权、幂等控制和失败恢复。项目因此采用 Fast Turn 与 Action Run 两条路径，并通过持久化 HandoffEnvelope 传递 root/parent execution、冻结 ContextSnapshot、证据、剩余预算、Grant 和幂等范围。

Fast Turn 最多两轮决策，只看到允许其使用的只读或本地工具；`task.create` 的 ToolSpec 只允许 `action_run`，Registry 展示和 ToolExecutor 执行时会各检查一次。Action Run 编排 evidence、conflict、linear research 三类只读 Subagent 与 Critic；外部写由 ActionGrant、Candidate revision、稳定 action key、幂等 ToolCall 和 ExternalActionClaim 共同约束。当前 ToolProvider 边界与协议无关，现有任务系统直接注册本地 Adapter；没有安装 MCP runtime，未来若接入 MCP 也只能映射到慢路径的同一安全合同。

## 失败恢复

Execution、ToolCall、Subagent、Grant 和 ExternalActionClaim 都持久化并使用集中状态迁移合同。Linear create 返回未知时不会再次直接调用 create：系统将 ToolCall 标记为 `unknown/reconciling`，按 action key 查询候选结果，并再次核对规范化标题。唯一匹配才收敛；零个、多个或标题不一致都保持安全未知或请求人工输入。未解析负责人不会被猜测为 Linear 用户，原称呼进入 Issue 描述，UI 显示固定警告。

## Trace 与评测结果

可导出的 AgentTrace 从音频产生的 Final evidence revision 连接到 ContextSnapshot、Fast/Handoff、Action/Subagent、ToolCall/Grant/Claim/reconciliation 和唯一叶子终态。Trace 只保存 ID、hash、状态、时间和计数，不保存会议正文、prompt 或密钥。

自动产品评测运行 12 个场景、每个 3 次，共 36 次。`quality.scenario_pass_rate`、`quality.route_accuracy` 和 `trace.integrity_pass_rate` 均为 100%，`safety.unauthorized_writes`、`reliability.duplicate_side_effects` 与 `reliability.unknown_create_retries` 均为 0；这些数字的 scope 均为 `scripted_local`，来源是 [`reports/meeting-agent-product-v1/summary.json`](../reports/meeting-agent-product-v1/summary.json)。同一报告中的观察性耗时为 `efficiency.wall_time_p50_ms=50.8769`、`efficiency.wall_time_p95_ms=117.2229`，它们不代表真实模型或 WebRTC SLA。

本地媒体诊断另外运行两个场景，将 `demo_audio.wav` 通过现有 FFmpeg decoder、音频帧统计、fake ASR Partial/Final、TranscriptReconciler、SegmentRepository、MeetingStateProjector、ContextBuilder、Fast/Handoff、Action/Subagent/Critic、Fake Linear、ToolExecutor 和 ActionRunRecovery 串成一条链。`input.decoded_frame_count=50`、`input.audio_duration_seconds=1.0`、`input.dropped_frames=0`，`connection.evidence_to_agent_trace_join_rate_percent=100`，`agent.trace_integrity_percent=100`；丢响应场景的 `recovery.response_lost_create_call_count=1`、`recovery.response_lost_reconcile_call_count=2`。这些结果 scope 为 `local_media_diagnostic`，来源是 [`reports/meeting-agent-product-v1/full-chain-summary.json`](../reports/meeting-agent-product-v1/full-chain-summary.json)。

## 可复现命令

在 Windows 项目根目录执行：

```powershell
.\meeting_agent_demo.cmd
```

命令会重新生成 36 条产品评测 Trace、2 条本地媒体全链路 Trace 和 5 条面试故事 Trace，并验证全部硬 Gate。它只使用临时 SQLite、scripted provider 和 Fake Task Adapter，不需要云密钥、LiveKit 服务或真实 Linear。

## 尚未验证

真实浏览器标签页音频经 LiveKit/WebRTC 的持续性、真实 DeepSeek/百炼调用和真实 Linear 专用 Team 的 create/reconcile/cleanup 不纳入上述数字。真实 Linear 会产生外部写，只有在明确授权和提供专用 Team 后才能执行。新增 workflow 在远端实际完成前只能声称“本地等价门禁通过”，不能声称远端 CI 已通过。
