# 插件开发指南

Matinier Plugin SDK v1 面向本机安装的第三方媒体助手。插件只依赖通用 `MediaSession` 和
`MediaEvent`，不应假设输入一定来自会议；直播、文件音频和视频字幕使用同一 Host API。

## 最小插件

插件包是媒体类型 `application/vnd.matinier.plugin+zip` 的 ZIP：

```text
plugin.json
image.tar
signature.json
assets/             # 可选，签名覆盖的不可变资源
```

最小 manifest 示例：

```json
{
  "schema_version": 1,
  "id": "com.example.viewer",
  "name": "Example Viewer",
  "version": "1.0.0",
  "publisher": "Example Publisher",
  "host_api": ">=1.0 <2.0",
  "image_digest": "sha256:<image.tar 的 64 位十六进制摘要>",
  "subscriptions": ["transcript.final", "session.completed"],
  "permissions": ["state.get", "state.put", "ui.publish"],
  "commands": ["refresh"],
  "resources": {
    "memory_mb": 64,
    "cpu_count": 0.25,
    "pids": 16,
    "tmpfs_mb": 16
  },
  "ui_schema_version": 1,
  "state_schema_version": 1
}
```

`id` 必须是小写反向域名，`version` 使用 SemVer。`host_api` 是插件接受的主机范围；不兼容的
包不会启动。`subscriptions`、`permissions` 和 `commands` 必须完整声明，运行时不能动态扩权。

## 传输与生命周期

容器 stdin/stdout 是双向 JSON-RPC 2.0：每行一个 UTF-8 JSON 对象。stdout **只能**写协议消息；
日志写 stderr。插件不得等待终端、输出 banner 或在协议行之间混入日志。

主机调用顺序：

1. `plugin.initialize`：校验 `plugin_id`、`version`、`protocol_version=1.0` 和 Host API；插件必须原样
   返回自己的身份、协议版本及收到的 `host_api`。
2. `session.open`：为一个 MediaSession 发放新的不透明 `session_scope`，并提供
   `after_sequence`。同一容器可同时拥有多个逻辑会话。
3. `event.batch`：只包含 manifest 已订阅的事件。插件应先把结果持久处理，再返回
   `acknowledged_sequence`；确认值不能越过批次最大 sequence。
4. `command.execute`：用户触发主机渲染视图里的已声明 command；插件应拒绝未知 command 和过期
   `expected_view_version`。
5. `plugin.heartbeat`：快速返回健康状态。
6. `session.close`、`plugin.shutdown`：通知消息；应尽快、幂等地释放逻辑资源。
7. `plugin.migrate_state`：安装新版本时接收旧/新 schema 版本及已改写为新版本 namespace 的
   `items`，返回完整新快照。失败会保留旧 preferred version 和旧会话绑定。

容器崩溃后，旧 scope 立即失效。主机启动新 generation，并再次用新的 scope 调用
`session.open(after_sequence=<durable ack>)`；插件不得缓存或猜测 scope。

## MediaEvent 与订阅

公共 schema 位于 [`../plugin-sdk/schemas`](../plugin-sdk/schemas)。事件身份由
`session_id + sequence` 排序；`event_id` 是稳定 ID，`logical_id + revision` 表达可修订来源。
首版事件族包括 `transcript.*`、`translation.*`、`session.*` 与媒体元数据。插件应：

- 忽略自己未理解但 Host API 允许的 payload 可选字段；
- 不把 Draft 当作 Final 事实；
- 用 sequence 作为消费游标，不用到达时间推断顺序；
- 对重复批次保持幂等，并只确认已经持久处理的位置。

需要主动回读时可调用 `capability.invoke` 的 `media.query`，但它要求安装权限及匹配当前
MediaSession 的短时 Grant。

## 主机 Capability

插件向主机发起：

```json
{
  "jsonrpc": "2.0",
  "id": 1001,
  "method": "capability.invoke",
  "params": {
    "capability": "state.put",
    "session_scope": "<opaque scope>",
    "input": {
      "key": "cursor",
      "value": {"sequence": 42},
      "expected_version": 0
    }
  }
}
```

当前稳定能力：

| Capability | Effect | 额外约束 |
| --- | --- | --- |
| `state.get` | read | 版本和会话 namespace 隔离 |
| `state.put` | local_write | 乐观版本；每版本总状态默认 2 MiB |
| `ui.publish` | local_write | 只接受闭合声明式组件模型 |
| `media.query` | read | 需要当前会话 Grant、事件类型和调用次数范围 |
| `model.invoke` | read | Host 代调用结构化模型；插件永远拿不到 Provider 凭据 |
| `delivery.prepare` | local_write | 从当前会话构建冻结 Package；必须提供幂等键 |
| `delivery.query` | read | 只可分页读取当前会话 Package 的交付文档 |
| `document.publish` | local_write | 发布有 Package 证据约束的版本化文档；必须提供幂等键 |
| `network.fetch` | network | 需要短时 Grant；仅公网目标、重定向重新校验、响应有界 |

安装时接受权限只是第一道门。`media.*`、`network` effect 和未来 `external_write` 还必须有身份、
版本、会话、能力、scope、调用预算和到期时间都匹配的 Grant。不要把权限提示或 Grant 流程放进
插件 UI；它们由主机管理面负责。

### 模型、交付包与文档能力

`model.invoke` 输入为 `input_category`、`system_prompt`、`user_prompt`、`input_payload`、固定
`response_format=json_object`、`max_output_tokens` 和 `timeout_seconds`；输出为经校验的 JSON
`output` 及 `provider/model/finish_reason/output_tokens` 元数据。默认总输入上限 32000 字符、输出
4096 tokens/192 KiB，且所有插件共享 Host 并发额度。模型 API key、Authorization header、原始
Provider 响应和完整 prompt/payload 都不会进入容器或审计正文。

`delivery.prepare` 输入 `{trigger, output_language, final_sequence}`，输出 Package ID/version/hash、
源/目标语言和 final sequence。`delivery.query` 输入 Package ID、文档类型、可选语言、游标和 limit，
输出 Package identity、当前页 items 和下一游标；Host 默认将单页收敛到最多 100 项。两者都绑定
当前 `session_scope` 对应的 MediaSession 及其 legacy Session bridge，不能读取另一会话 Package。

`document.publish` 输入包含稳定 `identity_key`、schema name/version、文档语言、trigger、
completeness、`source_package_id`、结构化 `content`、安全 Markdown 和 evidence refs；输出文档
ID/version、identity/hash、语言、trigger 和 completeness。默认请求上限 192 KiB；Markdown 禁止
raw HTML 和不安全协议。每条证据必须逐字段匹配同一会话 Package 的 evidence index，文档按
`plugin + MediaSession + identity + language` 追加不可变版本。

`delivery.prepare` 和 `document.publish` 的 `capability.invoke` 必须携带不超过 255 bytes 的
`idempotency_key`。同一插件用相同 key 和相同请求重试会取得既有结果；key 绑定不同请求会被拒绝。
Host 在每次调用时重新检查当前安装权限和 session scope，撤销权限从下一次调用立即生效。安装时
出现 Host Registry 未知权限也会在包写入前失败。

插件可稳定处理的公开 RPC 错误码为 `plugin.protocol.invalid`、`plugin.permission.denied`、
`plugin.protocol.method_not_found`、`plugin.protocol.timeout`、`plugin.scope.invalid` 和
`plugin.capability.failed`。内部路径、异常、凭据与源 payload 不跨越 RPC 信任边界。

## 状态、UI 与命令

`state.put` 使用 compare-and-swap：新 key 的 `expected_version=0`，更新时使用最近一次
`state.get`/`state.put` 返回的版本。不要把密钥、令牌或完整敏感音频内容写入状态。

`ui.publish` 只能发布 SDK schema 定义的 `panel` 或 `overlay`。不接受 HTML、脚本、样式表、
iframe、webview 或任意 URL 执行。每个组件需要稳定 `id`；action 只能引用 manifest 中的 command。
视图更新必须递增 `view_version`，主机会拒绝旧版本覆盖和过期用户命令。

Host 在调用 `command.execute` 前生成唯一 `command_id` 并原样随参数发送，HTTP 响应返回同一 ID。
浏览器重复点击会产生不同 ID；插件应根据自己的运行中任务状态决定是否合并，而不是把 command ID
当作跨请求幂等键。长任务应在插件后台继续，`command.execute` 只需在 RPC timeout 内返回已接受。

## 课程整理参考实现

[`../plugin-sdk/examples/course-organizer`](../plugin-sdk/examples/course-organizer) 展示了一个完整的
双阶段助手：播放期间把 Final 事件筛选成较散的实时知识笔记，只有用户命令或 Session 终态才从
Frozen Package 执行分层 Map/Reduce 整理。其 manifest 精确请求七项权限：
`state.get`、`state.put`、`ui.publish`、`model.invoke`、`delivery.prepare`、`delivery.query`、
`document.publish`。它没有 `network.fetch`，容器仍固定 `--network none`。

实时模型失败时实现先提交带 `rule_fallback` 置信状态的规则笔记，再按 2/10/30 秒有界重试；最终
模型失败时持久化 `waiting_retry`，不会伪造 complete 文档。默认语言取首个译文，否则源语言；每种
语言使用独立 identity/version。外部标签页输入只具备 Host 字幕时间轴，不得把坐标描述为网页 DOM
定位或播放器 seek。打包命令：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\package_course_organizer_plugin.py `
  --private-key C:\safe\course-plugin-development-key.pem `
  --output C:\safe\matinier-course-organizer-1.0.0.plugin.zip
```

## 打包、签名与一致性测试

仓库自带无第三方运行依赖的诊断插件和打包器：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\package_diagnostic_plugin.py `
  --private-key C:\safe\diagnostic-development-key.pem `
  --output C:\safe\matinier-diagnostic-1.0.0.plugin.zip
```

私钥必须位于仓库和插件包之外。`signature.json` 使用 Ed25519，对规范化 manifest、image digest
和 assets digest 签名。Host 会拒绝路径穿越、链接、重复成员、ZIP bomb、摘要不符、签名不符及
检查/确认之间权限变化。

提交前运行：

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  tests/test_plugin_sdk_schemas.py tests/test_plugin_sdk_contract.py -q
.venv\Scripts\python.exe -m app.plugins.sdk_schemas ..\plugin-sdk\schemas
```

Schema 生成只应在有意改变 Host API 时执行，并与插件 SDK 同次审查。Host API v1 内新增可选字段
应保持向后兼容；删除/改义字段、收紧已公开取值或改变 RPC 语义需要新的主版本。
