# Stage 3–6、Private Assistant API 与 LiveKit 字幕事件

## Session 历史

`GET /api/sessions` 按 `created_at DESC, id DESC` 返回 Session 历史。每项除基础字段外，还包含阶段4持久化的：

- `asr_provider`、`asr_model`
- 可选 `target_language`
- `translation_status`、`translation_provider`、`translation_model`
- `translation_error_code`、`translation_error_message`
- `final_result_count`
- `first_partial_latency_ms`、`average_final_latency_ms`
- `provider_error_count`
- `sent_audio_chunk_count`、`sent_audio_bytes`

这些字段对迁移前已经完成的 Session 可以是 `null`；阶段4 Worker 创建的新运行会保存真实 Provider/模型，并在成功结束时原子保存全部终态指标。

延迟指标从 Worker 向 ASR Provider 交付首个音频块时开始计时，不包含 HLS
解析、FFmpeg 首帧或 LiveKit 建连时间。`first_partial_latency_ms` 是首个非空
Partial 的墙钟等待时间；`average_final_latency_ms` 是非空 Final 到达时相对该
Final `audio_end_ms` 的平均滞后。空白 Partial/Final 不计数、不落库。

## Final 字幕快照

`GET /api/sessions/{session_id}/segments` 返回该 Session 已持久化的 Final 字幕数组。不存在的 Session 返回 `404`，尚无 Final 时返回 `[]`。

响应按 `audio_start_ms`、`audio_end_ms` 排序；未知时间排在已知时间之后。每项包含：

- `id`、`session_id`、`segment_id`、`track_id`
- `revision`、`language`、`status`（阶段3固定为 `final`）
- `raw_text`、`display_text`
- `audio_start_ms`、`audio_end_ms`、`confidence`
- `received_at_ms`、`finalized_at`、`created_at`、`updated_at`

Provider 原始响应、Authorization Header、API Key 和 Workspace ID 不属于此 API。

## Final 译文快照

`GET /api/sessions/{session_id}/translations` 返回该 Session 已持久化的 Final
译文。未启用翻译或尚无 Final 译文时返回 `[]`。

每项包含 `segment_id`、`revision`、`source_language`、`target_language`、
`text`、音频起止时间、`source_segment_ids`、`status`、接收/完成/创建/更新
时间。译文存放在独立的 `translation_segments` 表，不修改源字幕
`segments`。

## Session 导出

`GET /api/sessions/{session_id}/export?format=<format>` 支持 `json`、`srt`、`vtt` 和 `markdown`。不存在的 Session 返回 `404`，未知格式由参数校验返回 `422`；未预期的内部错误只向客户端返回 `Export failed`。

| `format` | Content-Type | 下载文件名 |
| --- | --- | --- |
| `json` | `application/json; charset=utf-8` | `source.json` |
| `srt` | `application/x-subrip; charset=utf-8` | `source.srt` |
| `vtt` | `text/vtt; charset=utf-8` | `source.vtt` |
| `markdown` | `text/markdown; charset=utf-8` | `source.md` |

四种格式共享同一份标准化 Final 时间线，Draft 不参与导出。标准化规则为：

1. 已知开始时间按开始、结束、创建时间和记录 ID 稳定排序；
2. 开始时间小于 0 时归零，缺失时使用上一项的标准化结束时间或 0；
3. 结束时间早于开始时间时收敛到开始时间；
4. 缺失结束时间时优先使用后续有效开始时间，否则补 2000 ms；
5. 合法重叠保留，字幕文本不被改写。

相同持久化数据产生字节级一致的重复下载。JSON 使用 UTF-8 原文和固定缩进，不加入生成时间；所有格式都排除百炼原始响应与凭据。

## 整理版台本兼容 API（deprecated）

阶段5的旧路由仍保留，但 Stage 2C 后已标记 deprecated。POST 内部先取得或构建 Frozen
Package，再提交 `clean_script` Job；读取与导出均从 `derived_artifacts` 生成兼容响应。
`processed_scripts` 历史已经迁移，旧表不再接受新写入。该兼容层不更新 `segments`，也不改变
四种源字幕导出或 `livecaption.events.v1`。

| 方法与路径 | 成功响应 | 用途 |
| --- | --- | --- |
| `POST /api/sessions/{session_id}/scripts` | `201` 详情 | 兼容调用 Package Job，并等待 Artifact 完成 |
| `GET /api/sessions/{session_id}/scripts` | `200` 摘要数组 | 按版本倒序列出 clean_script Artifact |
| `GET /api/scripts/{script_id}` | `200` 详情 | 从 Package/Artifact 重建旧响应 |
| `GET /api/scripts/{script_id}/export` | `200 text/markdown` | 下载 `processed-script-v{version}.md` |

摘要字段为 `id`、`session_id`、`provider`、`model`、`version`、`created_at`。详情在摘要基础上增加：

- `source_segment_snapshot`：每项含 `segment_id`、`revision`、`language`、`raw_text`、`display_text`、标准化 `audio_start_ms` / `audio_end_ms`；
- `content`：严格结构 `{title, sections, warnings}`；每个 section 含 `source_segment_ids`、`start_ms`、`end_ms`、`clean_text`、`notes`；
- `markdown_text`：由服务端从已校验结构确定性渲染。

服务端要求 Package 有效源文档的每个 item 恰好被引用一次，不接受外来、缺失、重复或时间
不一致的引用。只有全部分块均通过校验后才提交 Artifact；同一 Package 与产物类型依次生成
`artifact_version=1,2,...`，旧版本保持不变。

错误合同：

- Session 或台本不存在：`404`；
- Session 没有 Final：`409 Session has no Final captions`，不会调用 DeepSeek；
- 服务端未配置 DeepSeek：`503 DeepSeek is not configured`；
- Provider、输出或严格校验失败：`502 Script generation failed`，不保存台本；
- 持久化失败：`500 Script save failed`，事务回滚。

客户端不会收到 DeepSeek API Key、Authorization Header、完整 Provider 原始响应或内部异常。
整理版是 Frozen Package 的派生产物；权威源字幕仍是 `segments` 中的 Final。

## Stage 2A Package API

成果包只从 SQLite Final 构建，不读取 Partial、LiveKit DataPacket 或 Worker 内存：

| 方法与路径 | 成功响应 | 用途 |
| --- | --- | --- |
| `POST /api/sessions/{id}/packages` | `201` Package 详情 | 构建下一个 baseline Package 版本 |
| `GET /api/sessions/{id}/packages` | `200` 摘要数组 | 按版本倒序列出 Package |
| `GET /api/packages/{id}` | `200` Package 详情 | 读取 manifest 与全部冻结文档 |
| `GET /api/packages/{id}/manifest` | `200` manifest | 单独读取协议清单 |
| `GET /api/packages/{id}/export` | `200 application/zip` | 按需确定性渲染 ZIP |
| `POST /api/packages/{id}/validate` | `200` 校验结果 | 校验 Schema、哈希、时间线和证据索引 |

Package 详情包含 `package_id/session_id/package_version/status/content_hash`、manifest、
文档数组和生命周期时间。manifest 的 `schema` 固定为
`matinier.transcript-package`，`schema_version` 固定为 `1.0`。

Session 不存在返回 `404`；既未 completed 也没有 Final 时构建返回 `409`。重复 POST
创建新版本，不静默覆盖旧版本。ZIP 内 `checksums.json` 使用 SHA-256 覆盖其他全部条目。

## Stage 2B Revision API

Revision 是基于 Frozen Package 有效源文档的不可变校对快照，不修改 Stage 1 Final 或既有
Package 内容：

| 方法与路径 | 成功响应 | 用途 |
| --- | --- | --- |
| `POST /api/packages/{id}/revisions` | `201` Revision 详情 | 从 Package 有效源文档创建首版校对稿 |
| `GET /api/sessions/{id}/revisions` | `200` 摘要数组 | 按版本倒序列出校对历史 |
| `GET /api/revisions/{id}` | `200` Revision 详情 | 读取不可变快照与父版本引用 |
| `POST /api/revisions/{id}/versions` | `201` Revision 详情 | 从任一历史版本保存一个新版本 |
| `POST /api/revisions/{id}/approve` | `200` Revision 详情 | 批准 saved 版本并 supersede 旧 approved |
| `POST /api/revisions/{id}/packages` | `201` Package 详情 | 从 approved Revision 构建 Package vNext |
| `GET /api/revisions/{id}/export?format=...` | `200` 文件 | 导出 `srt`、`vtt` 或 `markdown` |

Revision 详情包含 `revision_id/session_id/version/parent_revision_id/base_package_id/language`、
`content/content_hash/change_summary/status/created_at/approved_at`。`content.items` 每项包含 UUID
`item_id`、非空 `source_segment_ids`、`start_ms/end_ms/text`。

首次创建要求 Package 可读取且有效源文档非空。新版本请求必须保持 Package raw source 的全部
Segment ID，拒绝外来或丢失引用、空文字、重复/非 UUID item ID、负时间、`end < start` 或时间
乱序。同一 Session/源语言只允许一个 approved Revision；superseded 版本不能直接重新批准，
恢复操作应从该历史内容创建新版本。

从 Revision 构建 Package 时，未批准返回 `409`。成功结果同时包含 `source_raw` 与
`source_approved`，manifest 的 `effective_source_document_id` 指向 approved 文档，
`source_revision_id` 指向批准版本；timeline 与 evidence 由服务端从 approved 条目计算。

## Stage 2C–2F Processing Job / Artifact API

| 方法与路径 | 成功响应 | 用途 |
| --- | --- | --- |
| `POST /api/packages/{id}/jobs` | `202` Job | 创建 Package-only 后处理任务并立即返回 |
| `GET /api/packages/{id}/jobs` | `200` Job 数组 | 按时间倒序查询 Package 任务 |
| `GET /api/processing-jobs/{id}` | `200` Job | 轮询状态、进度、错误与结果 Artifact ID |
| `POST /api/processing-jobs/{id}/cancel` | `200` Job | 取消 queued/running 任务 |
| `GET /api/packages/{id}/artifacts` | `200` Artifact 数组 | 查询 Package 派生成果历史 |
| `GET /api/packages/{id}/approved-artifacts` | `200` Artifact 数组 | 查询各 identity 的 current approved |
| `GET /api/artifacts/{id}` | `200` Artifact | 读取结构化正文与 Package 证据 |
| `POST /api/artifacts/{id}/versions` | `201` Artifact | 从模型或人工版本追加人工快照 |
| `POST /api/artifacts/{id}/approve` | `200` Artifact | 批准并 supersede 同 identity 的旧批准版本 |
| `GET /api/artifacts/{id}/history` | `200` Artifact 数组 | 查询同 Package/identity 的倒序版本链 |
| `GET /api/artifacts/{id}/export?format=...` | `200` 文件 | 服务端确定性导出 Artifact |

创建请求为 `{artifact_kind, options, target_artifact_id?}`。当前可执行 `clean_script`、
`refined_translation`、`summary`、`chapter_outline` 和 `timeline_fact_review`。精译 options 包含
`target_language`，并可包含 `glossary`、`style` 和 `context_window_items`；摘要和章节可包含
`style`；事实复核必须通过 `target_artifact_id` 指向同 Package 的允许类型 Artifact，生成后的
options 固定记录目标 ID 与版本。未注册的 Workflow 返回 `409`。Job 状态为
`queued/running/completed/failed/cancelled`，进度范围 0–100；API 重启会
把遗留 queued/running 标记为 `failed` 和 `api_process_restarted`。已结束任务再次取消返回 `409`。

Artifact 记录 `package_id/package_version/package_content_hash`、类型、稳定 `identity_key` 与版本、
Provider/模型、Workflow 版本、options、结构化 content 以及 evidence。每条 evidence 包含
`source_item_ids`、服务端映射的 `source_segment_ids` 和时间范围。只有 Workflow 与全部 evidence
验证成功才会落库。

摘要 key points、章节和事实复核 reviews 使用 `evidence_item_ids`；API 返回由服务器补齐的
`time_ranges`、`evidence_excerpts`、Segment ID 和聚合起止时间。事实复核的 unsupported/ambiguous
可返回空证据和空时间，supported 不可为空；该接口只做 Package 内忠实度复核，不调用 Web 搜索。

人工版本请求为 `{content}`；服务端继承 Package、identity、evidence 和目标语言，设置
`parent_artifact_id`、`created_by=human`、`status=reviewed`，并拒绝证据拓扑、时间、claim 或目标
被更改。同一 Package/identity 只有一个 approved；superseded 版本不能直接重新批准，可从其内容
创建新版本。缺失 Artifact/Package 返回 `404`，编辑或批准冲突返回 `409`。

`format=json|markdown` 对清稿有效；精译还支持 `srt|vtt`，摘要/章节/事实复核支持 JSON。不支持的格式返回 `409`，Artifact
不存在返回 `404`。响应文件名包含 Artifact 类型、精译目标语言与版本；JSON、SRT、VTT 中的时间
和 Segment ID 由服务端 evidence 生成，不信任模型正文携带的对应字段。

## LiveKit 可靠事件

Worker 使用可靠 DataPacket 发布实时事件：

- LiveKit topic：`livecaption.events.v1`
- `reliable=true`
- UTF-8 JSON
- schema version：`1`
- 单条载荷默认不得超过 12000 bytes

统一信封：

```json
{
  "schema_version": 1,
  "topic": "caption",
  "type": "caption.upsert",
  "session_id": "session-uuid",
  "sent_at_ms": 1750000000000,
  "payload": {}
}
```

支持的精确组合如下：

| `topic` | `type` | `payload` |
| --- | --- | --- |
| `caption` | `caption.upsert` | `segment_id`、`revision`、`status`、`text`、音频起止时间、置信度、Provider event ID、接收时间 |
| `translation` | `translation.upsert` | `segment_id`、`revision`、`status`、源/目标语言、译文、音频起止时间、来源 Segment ID、Provider event ID、接收时间 |
| `translation` | `translation.status` | `status`（`disabled` / `pending` / `translating` / `completed` / `failed`）、源/目标语言及可选稳定错误 |
| `session` | `session.status` | `status`: 完整 Stage 6 Session 状态之一 |
| `session` | `session.progress` | `audio_time_ms` |
| `session` | `session.metrics` | Final 数、首次 Partial 延迟、平均 Final 延迟、Provider 错误数、音频块数和字节数 |
| `session` | `session.error` | 稳定 `error_code` 与脱敏后的用户可见 `message` |

## Session 状态机

正常状态只允许逐步前进：

```text
created -> room_ready -> replaying -> transcribing -> finalizing -> completed
```

- API 创建 Session 时为 `created`，首次签发合法 LiveKit Token 后为 `room_ready`；
- Worker 订阅目标 Replay Track 后发布 `replaying`；
- 百炼流已启动后发布 `transcribing`；
- 音频输入结束、等待 ASR 收尾前发布 `finalizing`；
- Final、终态指标和状态提交成功后发布 `completed`；
- 任意非终态可转为 `failed` 或 `cancelled`；
- 相同状态更新幂等，不能跳过正常状态或倒退；
- `completed`、`failed`、`cancelled` 是不可变终态，晚到事件不得覆盖；
- `failed` 持久化并发布稳定错误类别与脱敏消息；`cancelled` 只发布
  `session.status`，不发布 `session.error`，且持久化的 `error_code` /
  `error_message` 均为 `null`。

HTTP Session 响应包含 `error_code` 和 `error_message`。除 `failed` 外二者均为
`null`；失败时只能出现以下稳定类别之一：

`configuration_error`、`media_decode_error`、`livekit_error`、`asr_auth_error`、`asr_stream_error`、`persistence_error`、`deepseek_error`、`export_error`。

## 事件示例

下面的数组中每一项都是可单独发布的合法信封；实际 DataPacket 每次只包含一个对象。

```json
[
  {
    "schema_version": 1,
    "topic": "caption",
    "type": "caption.upsert",
    "session_id": "31c4f4cb-6684-42cc-85ca-f2594387aecf",
    "sent_at_ms": 1785200000123,
    "payload": {
      "segment_id": "track-a:result-1",
      "revision": 3,
      "status": "final",
      "text": "欢迎使用实时字幕工作台。",
      "audio_start_ms": 320,
      "audio_end_ms": 2760,
      "confidence": 0.96,
      "provider_event_id": "result-1-final",
      "received_at_ms": 1785200000100
    }
  },
  {
    "schema_version": 1,
    "topic": "session",
    "type": "session.status",
    "session_id": "31c4f4cb-6684-42cc-85ca-f2594387aecf",
    "sent_at_ms": 1785200000200,
    "payload": {
      "status": "finalizing"
    }
  },
  {
    "schema_version": 1,
    "topic": "session",
    "type": "session.progress",
    "session_id": "31c4f4cb-6684-42cc-85ca-f2594387aecf",
    "sent_at_ms": 1785200000300,
    "payload": {
      "audio_time_ms": 5000
    }
  },
  {
    "schema_version": 1,
    "topic": "session",
    "type": "session.metrics",
    "session_id": "31c4f4cb-6684-42cc-85ca-f2594387aecf",
    "sent_at_ms": 1785200000400,
    "payload": {
      "final_result_count": 1,
      "first_partial_latency_ms": 410.5,
      "average_final_latency_ms": 820.0,
      "provider_error_count": 0,
      "sent_audio_chunk_count": 90,
      "sent_audio_bytes": 288000
    }
  },
  {
    "schema_version": 1,
    "topic": "session",
    "type": "session.error",
    "session_id": "31c4f4cb-6684-42cc-85ca-f2594387aecf",
    "sent_at_ms": 1785200000500,
    "payload": {
      "error_code": "asr_stream_error",
      "message": "Speech recognition stopped before completion."
    }
  }
]
```

## Revision 与恢复规则

- 同一 `segment_id` 的首条消息从 revision 1 开始，后续有效修订递增。
- Partial 只通过 LiveKit 发布，不写 SQLite；新的 Partial 原位替换旧版本。
- Final 先幂等提交 SQLite，再通过 LiveKit 发布；Final 会移除同片段 Draft。
- 译文使用与源字幕相同的 revision / Final 规则，但通过
  `translation.upsert` 和 `translation_segments` 独立存储。
- 翻译状态通过 `translation.status` 发布；翻译失败仅把翻译链路置为
  `failed`，原文 ASR 和源字幕事件继续运行。
- 浏览器拒绝相同或更低 revision，重复 Final 不会生成第二行，Final 后的 Partial 不会覆盖 Final。
- 页面连接前读取一次原文和译文快照，注册 DataPacket 监听后连接 Room，再读取一次双快照，以关闭快照与实时事件之间的竞态。

## Private Meeting State 与 Assistant API

Assistant 默认由 `ASSISTANT_ENABLED=false` 关闭。Meeting State/Mark 路由可以读取已持久化状态；
需要 Assistant Runtime 的 turn/state/event/execution 路由在关闭时返回 `503`。

### Meeting State 与 Mark

```text
GET   /api/sessions/{session_id}/meeting-state
GET   /api/sessions/{session_id}/marks
POST  /api/sessions/{session_id}/marks
PATCH /api/sessions/{session_id}/marks/{mark_id}
```

Meeting State 响应包含 `state`、规范化 `state_hash`、`source_frontier` 和 `freshness`。每个
Candidate/State Item 保存源 Segment ID 与 revision；`freshness` 明确区分 ready、lagging、stale
和 rebuilding，并报告未投影 Final tail。手动 Mark 创建必须引用当前 Session 的 Final Segment
revision；自动候选 Mark 只有在提交相同证据 revision 时才能 accepted。

### 创建 Ask 或 Execute Turn

```text
POST /api/sessions/{session_id}/assistant/turns
```

请求必须显式指定 `intent_mode=ask|execute`，服务端不会从自然语言推断模式：

- `ask` 不允许携带 Grant，可创建 Fast Turn，并可用 `allow_handoff` 控制是否转入慢通道；
- `execute` 必须携带 Grant，至少包含 `task.create`、一个或多个 Candidate、最大副作用数和带时区
  的过期时间；Team 由服务端 `LINEAR_TEAM_ID` 固定。
- `(session_id, client_request_id)` 是幂等键；相同 ID 绑定不同目标或模式返回 `409`。

成功返回 `202`、Execution 摘要和当前事件 cursor。Fast Turn 当前在请求内完成有界运行后返回，
因此响应中的 Execution 可能已经是 completed/handed_off；Action Run 提交后由后台 Scheduler 推进。

### 状态、事件和执行详情

```text
GET /api/sessions/{session_id}/assistant/state
GET /api/sessions/{session_id}/assistant/events?after={cursor}&limit={1..100}
GET /api/assistant/executions/{execution_id}
```

Session state 返回全部活动执行、最近 20 个终态执行和同一读事务取得的 `snapshot_cursor`。事件
接口按 Session 过滤全局递增 cursor，返回 `events/next_cursor/has_more`；客户端只应用比本地
`state_version` 更新的事件。Execution detail 返回公开 Step 摘要和 ToolCall 状态/外部引用，不返回
隐藏推理、原始 Provider 错误或 Tool 参数正文。

### NeedsInput 与取消

```text
POST /api/assistant/executions/{execution_id}/input
POST /api/assistant/executions/{execution_id}/cancel
```

两类请求都携带 `client_operation_id` 和 `expected_state_version`，以乐观并发和幂等记录防止双击
重复推进。Input 仅接受处于 `needs_input` 的 Action Run。Cancel 不回滚已经确认的 Linear Issue；
响应的 `external_effects.existing_actions_remain` 会明确这一点。unknown/reconciling 状态不得由前端
提供直接重试创建按钮。

Assistant 事件和响应只包含有界摘要、稳定 ID、状态、证据引用及安全外部链接。当前执行详情尚未
把 unresolved identity 的类型化 ToolResult 直接返回给 UI，直接 Execute 也尚未单独发出
`assistant_context_frozen` 生命周期事件；这些边界记录在实现任务书中。

## MediaSession、插件管理与 Host RPC

### Media sidecar HTTP

```text
GET  /api/sessions/{session_id}/media-session
GET  /api/media-sessions/{media_session_id}/events?after={sequence}&limit={1..1000}
GET  /api/media-sessions/{media_session_id}/plugin-views
POST /api/media-sessions/{media_session_id}/plugin-commands
GET  /api/media-sessions/{media_session_id}/plugin-documents
GET  /api/plugin-documents/{document_id}
GET  /api/plugin-documents/{document_id}/export?format=markdown|json
```

第一个路由幂等创建/读取 legacy Session bridge，并请求 Projector catch-up；返回通用
`media_session_id`、legacy ID、mode、source kind 和 status。事件列表严格按递增 sequence 返回。
插件 view 响应同时包含 plugin ID/version、当前 scope、surface、view/version、allowed commands
和已校验 document。command 请求必须回传完全匹配的 plugin version、scope、surface、view ID、
expected view version、action ID 和 values；scope 错误为 403，目标缺失为 404，旧 view/非法 action
为 409。Host 在调用插件前生成 `command_id`，`command.execute` 参数与成功 HTTP 响应返回同一值；
每次浏览器请求都会得到新 ID。

前端 `/assistants?session={legacy_session_id}&plugin={plugin_id}` 只把 URL 参数作为选择提示：Session
必须来自 `GET /api/sessions`，plugin 必须与 Host 返回的安装、视图或文档条目合并后才能选中。切换
Session/plugin 不复用其他复合 scope 的表单值或上次校验视图；页面不会把 URL 参数当作能力授权。

插件文档列表可按 plugin ID、identity key 和 language 筛选，按最新版本优先返回不可变摘要；详情
包含结构化 content、安全 Markdown 和 Package evidence refs。导出仅支持 `markdown` 与 `json`，
响应携带文档哈希；文档不存在为 404，不支持的格式为 400。发布不经公共 HTTP 写接口，只能由已
授权插件通过 session-bound `document.publish` 完成。

MediaEvent schema version 1 的稳定字段：

```json
{
  "event_id": "uuid",
  "session_id": "media-session-uuid",
  "sequence": 42,
  "schema_version": 1,
  "event_type": "transcript.final",
  "media_time_ms": 1200,
  "duration_ms": 800,
  "logical_id": "segment-id",
  "revision": 1,
  "finality": "final",
  "source": "caption.projector",
  "payload": {},
  "created_at": "2026-08-28T03:00:00Z"
}
```

sequence 是消费顺序和 ack 基准；`logical_id + revision` 表达源对象修订。插件不能从 event 到达时间
推断顺序，也不能把 Draft 当作 Final 证据。

### 插件管理 HTTP

```text
GET    /api/plugins/builtins
POST   /api/plugins/builtins/{plugin_id}/packages:inspect
POST   /api/plugins/packages:inspect
POST   /api/plugins/installations
GET    /api/plugins
GET    /api/plugins/{plugin_id}
POST   /api/plugins/{plugin_id}/enable
POST   /api/plugins/{plugin_id}/disable
POST   /api/plugins/{plugin_id}/grants
DELETE /api/plugins/{plugin_id}
```

内置目录由服务端固定 allowlist 产生，只返回公开 ID、名称、说明、版本和动态构建可用性；检查端点
不接受请求体、源码路径、Dockerfile、镜像标签、输出目录或签名路径。内置检查同样返回短时 ticket，
后续必须走相同的指纹确认、精确权限接受、安装和启用 API。动态内置构建默认关闭，生产环境拒绝开启。

除只读列表/详情外，管理操作要求 `X-Plugin-Admin-Token`。包检查请求 Content-Type 必须是
`application/vnd.matinier.plugin+zip`，上传流与声明长度都受压缩字节上限约束。检查返回短时 ticket、
manifest identity、publisher/fingerprint/trust、签名状态、权限、摘要和过期时间；确认安装必须提交
完全相同的 accepted permissions，并在首次 publisher 时显式信任相同 fingerprint。

安装/迁移冲突为 409；生命周期 Lookup 为 404；无效媒体类型为 415；超限上传为 413。服务端错误
只返回稳定通用 detail，不回显内部路径、Docker stderr、签名材料或 traceback。

Grant 创建参数包含可选 `media_session_id`、capability、effect、scope 和 30..86400 秒 TTL。
capability 必须已在对应 preferred package 的安装权限中，effect 必须匹配 Host Registry。实际授权
仍按 plugin ID/version、会话、scope、状态、撤销、到期和调用次数逐次评估。

### JSON-RPC 1.0 lifecycle

容器 stdin/stdout 每行一个 JSON-RPC 2.0 对象。Host → plugin：

```text
plugin.initialize
plugin.heartbeat
plugin.migrate_state
session.open
session.close          # notification
event.batch
command.execute
plugin.shutdown        # notification
rpc.cancel             # notification
```

plugin → Host 仅开放 `capability.invoke`。Host 从连接上下文确定 principal，忽略任何插件尝试自报的
其他 identity。`session.open` 参数包含 `media_session_id`、随机 `session_scope` 和
`after_sequence`。`event.batch` 包含同一 binding 的事件；响应：

```json
{
  "acknowledged_sequence": 42
}
```

ack 不能低于当前 durable ack，也不能大于本批最大 sequence。崩溃后旧 scope 失效，新 generation
使用新 scope 并从 ack 继续。

`capability.invoke` 参数只允许 `capability`、`input`、可选 `session_scope` 和
`idempotency_key`。当前能力输入/输出详见 [`plugin-authoring.md`](plugin-authoring.md) 和 SDK schema。
权限拒绝、scope 错误、方法未知、timeout、协议错误和 adapter 失败只使用公开错误码；未知异常不会
跨信任边界返回。

`command.execute` 参数除媒体会话、scope、command、values 和 expected view version 外还包含
Host 预生成的 `command_id`。`delivery.prepare`/`document.publish` 必须携带幂等键；Package 交付与
文档发布都在 capability invocation 的同一数据库事务中完成，失败不会留下已提交的半成品。

当前部署使用单 FastAPI 实例和 SQLite。插件可以并发发送 RPC，但 Host 会在进程内串行化插件
capability 数据库事务、runtime 状态、binding/scope 与投递/确认游标持久化，避免多个 SQLite writer
互相等待。`model.invoke` 的 pending 调用先落盘，远程等待时不持有数据库事务或插件写入锁，结果
返回后重新获取锁；取消不会伪造完成状态。交付和文档发布的原子事务边界保持不变。这不是分布式锁，
也不允许多个 API 实例共同监督同一批插件。

### 插件 UI 文档

`ui.publish` 的 envelope 指定 `surface=panel|overlay`、稳定 `view_id`、递增 `view_version`、根组件和
最多 32 个 actions。文档从不可信 JSON 解析，禁止额外字段和 raw HTML。Host 只接受 SDK schema
中的 text/safe_markdown/section/list/table/badge/metric/progress/button/select 等闭合组件；overlay 使用
更小的 allowlist。action command 必须在 manifest 中预声明，媒体锚点不能超出已知 duration。

### 课程整理插件的 API 语义

`com.matinier.course-organizer` 只订阅 Final 字幕/译文和 Session 终态事件。播放中 view 展示实时知识
笔记，最终文档不会随窗口持续重算。`generate_final`/`retry_final` 是异步命令：前者在非终态会话发布
`completeness=interim`；`session.completed` 自动发布 `complete`，失败/取消发布
`partial_terminal`。模型暂不可用时 view 可返回 `degraded` 或 `waiting_retry`，不改变字幕 API 状态。

语言默认取第一个译文语言，没有译文则取源语言；`set_language` 的 `source` 选择会解析为实际观测
到的源语言。文档 identity 按语言隔离，同一语言版本递增，interim 与 complete 因此可同时从
`plugin-documents` 列出。详情与 `export?format=markdown|json` 是可信 Host 输出，并返回内容哈希。
文档证据必须属于当前 MediaSession 对应的 Frozen Package。`media_time_ms` 是捕获音频/字幕坐标；
对于外部标签页，它不是 DOM 视频元素的权威 currentTime，也不提供 seek API。

## 浏览器拒绝规则

浏览器只接受：schema version 1、精确 LiveKit topic、当前 Session ID、已知的 `topic/type` 组合及类型正确且无额外字段的 payload。未知版本、错误 Session、未知消息、非 UTF-8、非法 JSON 或字段不完整的包会被安静忽略，不改变页面状态。
