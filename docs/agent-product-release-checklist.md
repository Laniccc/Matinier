# Meeting Agent 发布与面试验收清单

## 自动门禁

- [x] `meeting_agent_demo.cmd` 生成 12-case × 3 trials，`summary.json.overall_result=PASS`。
- [x] 36 条 AgentTrace 全部存在并通过完整性、状态边、版本连续性、唯一终态和敏感内容检查。
- [x] 两个 `local_media_diagnostic` 场景从 WAV 解码走到 Action terminal，Final→evidence→Trace join 为 100%。
- [x] 丢响应/崩溃场景 create 次数为 1，恢复器通过 reconcile 收敛，没有直接重试 create。
- [x] Fast 路径看不到且不能伪造执行 action-only 工具。
- [x] 未解析负责人保持 placeholder，Host UI 有固定提示。
- [x] `/health/ready` 只读报告 Fast/Action 活跃与排队、unknown/reconciling、恢复 backlog 和 Adapter 可用性。
- [x] 结构化日志可用 trace/root execution/execution/tool call ID 定位 durable Trace，且不输出 goal、prompt 或 secret。
- [x] Frontend test、typecheck、production build 通过。
- [x] `.github/workflows/agent-ci.yml` 运行后端/前端门禁、12-case Trace smoke 和 2-case 本地媒体全链路 Trace smoke，不需要 secret、Docker、MCP 或真实 Linear。

自动结果以 [`reports/meeting-agent-product-v1/summary.json`](../reports/meeting-agent-product-v1/summary.json) 和 [`reports/meeting-agent-product-v1/full-chain-summary.json`](../reports/meeting-agent-product-v1/full-chain-summary.json) 为准。

## 面试前人工检查

- [ ] 在页面上完成一条“实时字幕 → Fast 私密问答”演示，并记录 Session ID、MediaSession ID 和对应 trace_id。
- [ ] 完成一条“ActionCandidate → 显式授权 → Linear 链接”演示。
- [ ] 持续播放浏览器标签页音频，核对 WebRTC/LiveKit Track、Final 字幕与 AgentTrace input anchor。
- [ ] 检查当前 `/health/ready`，确认 projector、Fast/Action、recovery backlog 和 Task Adapter 状态符合预期。
- [ ] 演示后撤销 Grant，关闭会议分析，确认不会继续生成 Meeting State 或外部动作。

## 真实 Linear 专项

- [ ] 获得用户对一次真实外部写的明确授权，并取得专用演示 Team ID。
- [ ] 确认 Team 内没有同 action key 的遗留 Issue。
- [ ] 执行一次 create，记录 execution_id、tool_call_id、action key 和 Issue URL。
- [ ] 在可控条件下注入一次响应丢失，确认只 reconcile、不第二次 create。
- [ ] 清理演示 Issue，并记录真实 provider scope 的结果；不要与 `scripted_local` 或 `local_media_diagnostic` 混算。

## 声明边界

- 不写“实现 Google A2A 协议”；准确表述为同一 Host 内的持久化 Fast→Slow handoff。
- 不写“已接入 MCP”；准确表述为预留协议无关 ToolProvider，外部工具只能由慢 Agent 调用。
- 不把 scripted/local-media 延迟写成真实 WebRTC、云 ASR、模型或 Linear SLA。
- 远端 workflow 未实际运行完成前，不写“CI passing”。
