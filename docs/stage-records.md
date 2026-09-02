# Stage Records

## 2026-08-13 — Private Meeting Assistant 文档与验收状态对齐

### 文档更新

- README 增加 Fast Turn、Action Run、原子 Handoff、三 Subagent、ActionGrant、Linear 和
  Frozen Package 证据绑定的实际数据流，并把 Alembic head 更新为 `20260812_0022`。
- `docs/architecture.md` 和 `docs/api-events.md` 纳入 Assistant 容器边界、状态/API 合同、隐私和
  单实例约束；演示 Runbook 增加数据库预检、已知边界和人工验收记录表。
- Private Agent 设计文档与实现任务书区分“核心实现完成”和“最终人工验收完成”，不再把未运行
  的真实 Linear/两条演示描述为已通过。

### 已有验证证据

- 五个约定正向测试通过；最近记录的完整后端回归为 `314 passed, 1 skipped`。
- Frontend typecheck 与 production build 通过。
- SQLite 一致性备份从 `20260811_0018` 升级到 `20260812_0022 (head)`，关键历史表行数不变。

### 未完成验收与已知缺口

- 真实 Linear 专用 Team 集成和两条人工面试演示尚未执行。
- unknown create reconciliation 尚未复核规范化标题；unresolved identity 尚无固定 UI 提示；
  直接 Execute 尚缺单独的 `assistant_context_frozen` 生命周期记录。
- 因此当前结论是“核心实现和自动化正向路径完成，最终人工验收待完成”，不宣称生产级恢复。

## 2026-07-22 — Stage 0：项目骨架、配置与空房间连通

### 已完成

- 使用 uv 建立 Python 3.12 后端项目并生成锁文件。
- 建立统一 Pydantic Settings；LiveKit 四项必需配置缺失时立即失败，后续百炼与 DeepSeek 配置保持可选且未接入业务。
- 实现 FastAPI `GET /health`、`POST /api/livekit/token`、`POST /api/sessions`、`GET /api/sessions/{session_id}`。
- 使用 SQLAlchemy 2 + SQLite 建立 Session ORM 模型，并提供 Alembic 初始迁移。
- Token 只允许加入当前 Session 的 Room，浏览器不可发布媒体但可订阅和发送 Data。
- 使用 LiveKit Agents `WorkerOptions` 建立空 Worker；默认空 agent name 让 LiveKit 对新房间自动派发 Worker。
- 实现包含固定关联字段的 JSON 结构化日志。
- 建立 Next.js + TypeScript 页面，显示 `disconnected`、`connecting`、`connected`、`reconnecting` 和 `error`，并在卸载时主动断开 Room。
- 提供本地 LiveKit Docker Compose、环境变量样例与完整启动说明。

### 已执行验证

- `uv sync --python 3.12`：成功，使用 CPython 3.12.2。
- `pytest` 后端配置、日志、健康检查、Session API、Token 权限及 Worker 入口：10 项通过。
- `alembic upgrade head`（SQLite 内存库）：成功执行 `20260722_0001`。
- `tsc --noEmit --incremental false`：通过。
- `next build`：通过，首页静态预渲染成功。
- `docker compose up -d livekit`：成功启动 LiveKit Server 1.13.4，本地端口 7880/7881/TCP 与 7882/UDP 正常映射。
- 实际 SQLite 文件执行迁移成功；FastAPI `/health` 返回 `{"status":"ok","process":"api"}`。
- `scripts/verify_stage0.py` 实际连通成功：创建 Session、获取 Token、以 `stage-0-verifier` 加入 Room，并观察到自动派发 Worker `agent-AJ_RF8geTrZedRH`，`remote_participants=1`。
- RTC 验证参与者退出后 Worker job 正常关闭；日志出现 `worker_room_disconnected`，未出现未关闭任务警告。
- Next.js 开发页返回 HTTP 200，服务端页面包含项目标题与“创建会话并连接”控件。

### 任务书复核与补强

- 将 Docker Compose 的 LiveKit 镜像从浮动的 `latest` 固定为实际验收版本 `v1.13.4`。
- JSON 日志 formatter 现在为所有记录注入进程名；LiveKit 等依赖库日志也不会再出现 `process=null`。
- 前端将不可恢复的非主动断线归入 `error` 并显示原因，主动断开仍回到 `disconnected`。
- 完成两轮三进程启动与重启联调：两轮均成功创建、读取 Session，RTC 验证客户端均观察到自动派发 Worker，`remote_participants=1`；SQLite 记录跨重启由 1 条增至 2 条。
- 第二轮 Worker 正常记录 `worker_room_disconnected`；API、Worker、Frontend 日志中未发现未关闭任务告警，结构化日志未发现空 `process`。
- 清空四项必需 LiveKit 环境变量后实际加载 Settings，进程以非零状态立即退出，错误明确列出 `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`、`LIVEKIT_ROOM_NAME`。

### 第三方兼容差异

- 最初解析到 `livekit-agents 1.6.6`，它会强制安装并在导入阶段加载 `livekit-local-inference 0.2.6`。该原生扩展在当前 Windows 中文项目路径中触发 access violation，调用栈位于 `livekit/agents/inference/eot/transports.py`，而阶段0完全不使用本地推理。
- 已锁定 `livekit-agents 1.5.11` 并移除 `livekit-local-inference`；官方 `WorkerOptions + cli.run_app` Worker 生命周期仍满足本阶段需要，导入和测试通过。
- npm 当前最新 TypeScript 7 改变了包内路径，Next.js 16.2.10 仍检查 `typescript/lib/typescript.js`，会误报 TypeScript 缺失。前端因此锁定兼容的 TypeScript 6.0.3。

### 当前限制

- 内置浏览器控制进程因系统拒绝访问 `C:\Users\GX\AppData` 而无法启动，所以未自动点击页面按钮；同一 API Token + LiveKit Room + Worker 链路已由 `scripts/verify_stage0.py` 使用 RTC SDK 实际验证，页面本身也已完成 HTTP、类型和生产构建验证。人工打开页面即可复核最终 UI 状态。
- Compose 已固定本地验收版本；正式环境仍必须替换 `--dev` 模式与开发密钥。
- Session 状态在阶段0固定从 `created` 开始；完整状态机在任务书阶段6收口。
- 本阶段没有音频、ASR、字幕或导出能力。

### 下一阶段入口

阶段1从固定 WAV/MP3 文件实时回放开始：新增 FFmpeg 解码、20ms PCM 分帧、可注入单调时钟、Replay Participant 发布 `replay-audio` Track，以及 Worker 订阅后的帧统计。不要在阶段1接入 STT。

## 2026-07-22 — Stage 1：固定音频实时回放与 LiveKit Track

### 已完成

- 实现可注入 `FakeClock` 的绝对时间线 ReplayClock，使用 `monotonic_started_at + audio_elapsed` 计算每帧 `target_send_time`，处理耗时不会累计为长期漂移。
- 实现 FFmpeg WAV/MP3 流式解码：PCM16 little-endian、单声道、默认 16 kHz、20 ms/320 samples 分帧，保留不足 20 ms 的对齐尾帧。
- 解码器在正常、错误、取消和生成器提前关闭时均等待 FFmpeg 退出，超时后 terminate/kill，不遗留子进程。
- 提供固定 1 秒、440 Hz、16 kHz 单声道 PCM16 测试文件 `backend/tests/fixtures/demo_audio.wav`。
- `file` Session 每次创建独立 UUID/Room；Replay Token 身份由服务端固定为 `replay-{session_id}`，只允许发布 microphone source，不允许订阅或发送 Data。
- 实现 `ReplaySource`：发布 `replay-audio`、等待首个订阅、按真实节奏 capture PCM 帧、等待 playout，并在退出时取消发布、关闭 AudioSource、断开 Room。
- 实现 `scripts/run_demo_replay.py` CLI 和 `scripts/verify_stage1.py` RTC 可见性/取消验收脚本。
- Replay CLI 新增可选 `--session-id`：存在时先查询并校验 `source_type=file` Session，再向同一 Room 发布；省略时仍创建全新文件 Session。
- 前端新增“创建新 Session / 加入既有 Session”模式，可创建 `file` Session，并展示远端 Participant identity 与已发布 Track 名称。
- 前端连接生命周期防止主动断开与迟到的 `Room.connect()` 结果竞争；主动断开保持 `disconnected` 并清除旧错误。
- Worker 只订阅 `replay-*` Participant 的 `replay-audio` Audio Track；使用 16 kHz、单声道、20 ms `AudioStream` 读取。
- Worker 结构化日志记录帧数、sample rate、channels、samples per channel、累计接收时长、接收开始/结束时间；首帧、每 25 帧和结束时输出。
- Worker shutdown 移除事件监听、取消并 gather 未完成消费任务；每个消费任务负责关闭自己的 AudioStream。

### 已执行验证

- 后端全量测试：28 项通过；覆盖 ReplayClock 漂移补偿、WAV/MP3、尾帧、解码错误、FFmpeg 回收、ReplaySource 成功/取消、Token 权限、Worker 统计、AudioStream 关闭，以及 CLI 新建/复用 Session 分支。
- 前端 `tsc --noEmit --incremental false` 和 Next.js 生产构建通过；开发页 HTTP 200。
- 同一 1 秒 WAV 通过 CLI 连续运行成功，每次 Session ID 不同；发送端均为 50 帧、1.000 秒，最新实测播放阶段 1.094 秒。
- Worker 三次完整回放均观察到 16 kHz、单声道、320 samples/frame；结束日志累计收到约 1.30–1.34 秒（包含 WebRTC Track 取消发布前的尾部帧）。
- RTC viewer 使用与浏览器相同的只订阅 Token，实际观察到 Replay Participant、`replay-audio` 发布和取消发布。
- 真实取消场景在 0.25 秒触发：Track 已取消发布，Worker 完成 21 帧/0.42 秒流读取，FFmpeg 活跃进程为 0。
- 共创建 5 条 `source_type=file` 的真实 SQLite Session 记录；未发现 FFmpeg 残留或 `unclosed`、`Task was destroyed`、未 await coroutine 告警。
- 共享 Session 补完联调中，外部 CLI 使用观察者 Session ID 成功运行：`replay_participant_seen=true`、`replay_track_seen=true`、`replay_track_unpublished=true`、50 帧、1.000 秒、发送阶段 1.062 秒。
- 内置浏览器实际加载新版页面并创建 `file` Session `367f31c5-958c-4969-991a-b6a412be53bc`；真实 CLI 随后以 `--session-id` 查询并复用该 Session，完成 50 帧、1.000 秒回放，发送阶段 1.094 秒。

### 当前限制与边界

- LiveKit/WebRTC 在 Track 取消发布前可能产生少量尾部音频帧，因此 Worker 的“接收累计时长”略长于源文件；发送端 `audio_duration_seconds` 和 `playback_elapsed_seconds` 分别用于确认文件长度与发送节奏。
- 内置浏览器已完成新版页面的真实表单操作、文件 Session 创建和共享命令展示；其沙箱网络在本地 Docker LiveKit PeerConnection 阶段返回 `could not establish pc connection`。同一机器上的原生 RTC viewer 使用等同浏览器权限的 Token 已完成 Participant/Track 可见性和取消发布验证，因此这是当前内置浏览器网络环境限制，不是 Session/Token/Track 数据流失败。
- 本阶段没有 STT、字幕修订、DeepSeek 或导出逻辑。

### 下一阶段入口

阶段2在 Worker 的目标 AudioStream 上接入单条百炼实时 ASR 连接，先实现 provider 适配层、生命周期与结果日志；不要提前实现字幕修订或 DeepSeek。

## 2026-07-22 — Stage 2：阿里云百炼实时语音识别接入

### 已完成

- 建立厂商无关的 `SpeechRecognitionProvider` 抽象、标准 `ASREvent` 以及配置、认证、超时、协议、Provider、背压和状态异常体系。
- `ASREvent` 覆盖任务书要求的 `provider_event_id`、`segment_id`、文本、Final 标记、起止时间、置信度、原始响应和接收时间；生命周期事件完整覆盖 Started、Partial、Final、Completed、Error。
- 实现原生异步百炼 WebSocket Provider：Bearer 认证、区域 Endpoint、`run-task`、二进制 PCM、`result-generated`、`finish-task`、`task-finished` 和连接关闭顺序均可独立测试。
- 将百炼 `sentence_end=false/true` 映射为项目内 Partial/Final；心跳不产生字幕，非法 JSON、乱序消息、`task-failed` 和 401/403 均转换为稳定项目异常。
- 实现 16 kHz、单声道、PCM16 聚合器，把五个 20 ms/640-byte LiveKit 帧聚合为默认 100 ms/3200-byte 块，结束时冲刷对齐尾块。
- 实现 `TranscriptionSession`：默认 20 块有界队列、显式 QueueFull 异常、串行发送任务、事件消费任务、取消清理和幂等关闭。
- 启动阶段最多重建 Provider 并重试一次；发送音频后的错误不重试、不自动重放，避免生成重复结果。
- 输出 `[partial]`、`[final]` 和结构化 ASR 生命周期日志；会话摘要包含 Final 数量、首次 Partial 延迟、平均 Final 延迟、Provider 错误数、发送块数及字节数。原始响应只在 DEBUG 级别记录。
- Worker 为每条目标 Replay Track 创建独立识别 Session，在保留阶段1帧统计的同时把每个 PCM 帧送入识别层；正常、失败和取消路径都会关闭识别 Session 与 LiveKit AudioStream。
- 新增百炼 Endpoint、队列、块长、启动/结束超时和启动重试配置；Key/Workspace 在 API 导入时仍可为空，只在真实 Provider 启动时立即校验。
- 新增 `scripts/verify_stage2_local.py`：读取固定 1 秒 WAV，用 Fake Provider 验证 50 个输入帧聚合为 10 个 ASR 块、Partial/Final、指标与无后台任务残留。
- Settings 兼容读取 `DASHSCOPE_WORKSPACE_ID` 与既有环境使用的 `BAILIAN_WORKSPACE_ID`，真实凭据继续只保存在被忽略的本地 `.env`。
- Worker 在 Provider 启动期间读取 LiveKit 已缓冲帧时主动让出事件循环，避免 1.0x 回放因瞬时灌入有界队列而误触发背压。
- Replay Participant 断开后，Worker 会排空 `AudioStream` 已缓冲帧、停止等待新帧、发送 `finish-task` 并等待 `task-finished`；shutdown 为未完成任务保留有限宽限期，超时后才取消。
- 百炼任务完成等待仍使用独立的 15 秒上限，WebSocket 关闭握手限制为最多 2 秒，避免已完成识别占用完整房间退出窗口。
- Track 正常完成后 Worker 主动请求 Job shutdown；成功原因与 `error_type` 分开记录。

### 本地验证范围

- Fake WebSocket 覆盖请求头、协议消息顺序、Partial/Final 字段、认证失败、启动超时、服务端失败、非法消息和重复关闭，不访问网络。
- Fake Provider 覆盖正常流、尾块、队列溢出、启动前一次重试、发送后不重试、错误计数和取消清理。
- Worker 测试通过注入 Fake Session 验证启动、逐帧传递、结束、错误和取消路径，测试过程中不需要百炼凭据。
- 阶段0和阶段1后端测试继续纳入全量回归；前端类型检查与生产构建继续作为阶段2非 UI 变更的回归门禁。

### 已执行验证

- 后端全量测试：69 项通过；仅保留既有 Starlette `httpx` 弃用警告，无失败或资源泄漏告警。
- `scripts/verify_stage2_local.py`：`status=ok`、50 个 20 ms 输入帧、32000 bytes 源 PCM、10 个发送块、Partial/Final 各 1、Provider 错误 0、`clean_shutdown=true`。
- 前端 TypeScript 6.0.3 全量类型检查通过；Next.js 16.2.10 生产构建成功，首页与 404 页面完成静态生成。
- 额外覆盖发送任务失败且队列仍满的竞态；`finish()` 会立即传播原错误，不会因结束哨兵无法入队而挂起。
- 真实百炼直连脚本使用同一段 8.852 秒中文语音连续运行两次：每次均收到 Partial、1 个 Final 和 Completed，Provider 错误 0、89 个音频块、283256 bytes、`clean_shutdown=true`；识别 Final 与原测试文案一致。
- 最终 `Replay → LiveKit → Worker → 百炼` 全链路 Session `0b38519b-bcb8-4262-a381-392173714b57`：Replay 443 帧/8.852 秒，Worker 收到 WebRTC 尾帧后共处理 450 帧/9.0 秒，输出 14 个 Partial、1 个 Final、1 个 Completed，发送 90 块/288000 bytes，首个 Partial 5171 ms、平均 Final 10890 ms、Provider 错误 0，并正常输出 ASR 摘要及 `replay_audio_complete`。

### 真实服务验收结论与已知差异

- 真实 `fun-asr-realtime` Provider 与完整 LiveKit Worker 数据流均通过；Fake 验证仍保留为无需凭据的快速回归入口。
- 本地 LiveKit Server 1.13.4 在 Agent 调用标准 `ctx.shutdown()`、识别与资源均已正常完成后，服务端仍把 Agent Participant 离房记录为 `JS_FAILED: agent worker left the room`。Worker 自身已记录 `shutdown_reason="replay audio completed"`、`error_type=null`，且无残留任务；该服务端作业状态差异不影响阶段2的 Partial/Final、摘要和资源关闭验收，但升级 LiveKit 版本时应复核。

### 阶段边界与下一阶段入口

- 阶段2未实现 DeepSeek、字幕修订、翻译、字幕 UI、网络流或字幕导出。
- 阶段3从标准 `ASREvent` 消费接口进入字幕修订和实时页面，不依赖百炼原始消息结构。

## 2026-07-22 — Stage 3：字幕修订核心与实时页面（已完成）

### 第一批检查点：领域模型、稳定片段 ID 与 SQLite

- 新增厂商无关的 `CaptionEvent`、`CaptionStatus` 与确定性 `TranscriptReconciler`；支持按 `segment_id` 维护 revision、Partial 原位替换、Final 锁定、迟到事件/重复事件幂等以及按音频时间排序。
- 百炼未返回 `sentence_id` 时，由 Provider Adapter 使用当前 task ID 与递增片段序号生成稳定 ID；同一组 Partial/Final 共用 ID，下一片段自动换新 ID，不使用文本哈希。
- 新增与阶段4兼容的 `segments` 表、`SegmentRepository` 和 Alembic `20260722_0002`；阶段3只允许写 Final，并按 `(session_id, segment_id)` 唯一约束及 revision 做幂等 upsert。
- SQLite 启用外键、5 秒 busy timeout，文件库使用 WAL，以支持独立 API/Worker 进程并发读写。
- 现有本地数据库已升级至 `20260722_0002 (head)`，原 Session 表及记录保留。
- 第一批聚焦回归：19 项通过（Reconciler 6、百炼 Provider 8、Segment Repository 5）。

### 第二批检查点：Final 快照、可靠事件与 Worker 直写 SQLite

- 新增 `GET /api/sessions/{session_id}/segments`：仅返回按音频时间排序的 Final 快照；不存在的 Session 返回 404，响应不暴露 Provider 原始载荷或凭据。
- 固定 LiveKit 可靠 DataPacket 主题 `livecaption.events.v1` 与 schema version 1，覆盖字幕 upsert、Session 状态、进度、指标和脱敏错误事件；发布前执行严格字段及载荷大小校验。
- `TranscriptionSession` 新增逐事件 await 的异步处理钩子；指标先更新、业务处理后推进下一事件，处理器失败显式终止且不误计为 Provider 错误，取消时仍清理全部后台任务。
- 新增每条 Replay Track 独立的 `WorkerCaptionRuntime`：Partial 只发 LiveKit；Final 先按 revision 幂等写入 SQLite 并提交，再发 LiveKit；Session 的 running/completed/failed 状态和时间戳同步持久化。
- Worker 每 25 帧发布音频进度，识别结束后依次发布指标与 completed；异常先持久化 failed，再发布脱敏错误和 failed 状态，最后幂等关闭 ASR、SQLite、LiveKit 音频流等资源。
- 第二批任务书聚焦回归：24 项通过；后端全量回归在工作区 pytest 临时目录下 97 项全部通过，仅保留既有 Starlette `httpx` 弃用警告。

### 第三批检查点：浏览器状态层、字幕工作台与完整验收

- 新增严格 TypeScript 字幕事件类型、UTF-8 JSON 解码器和纯 reducer：只接受 schema version 1、精确 topic/type、当前 Session ID 与合法字段；未知或畸形数据包返回 `null`，旧 revision 被忽略，Final 覆盖并移除同片段 Draft。
- 前端连接顺序固定为“读取 Final 快照 → 注册 `RoomEvent.DataReceived` → 连接 LiveKit → 再合并一次快照”，关闭快照与实时事件之间的竞态；断线不会把已完成 Session 降级。
- 实时字幕工作台显示源文件、Session 状态、音频时钟、原位修订的浅色斜体 Partial、按音频时间排序的 Final 时间线、起止时间、置信度、首个 Partial/平均 Final 延迟、Provider 错误和可见错误信息，并支持移动端堆叠。
- 新增 `scripts/verify_stage3_local.py`，在临时 SQLite 上使用真实 `WorkerCaptionRuntime`、Fake Publisher 和 FastAPI 快照接口，一次验证 revision、Final 先落库、恢复、状态、指标和清理。
- 新增 `scripts/verify_stage3_e2e.py`，使用原生 RTC 观察者和既有 Replay CLI 验证真实 LiveKit、Worker、百炼、SQLite 与 API 链路，并发送未知 schema 测试包。
- 新增 `docs/api-events.md`，记录 Final 快照 API、事件 topic、schema、所有事件类型、revision 语义与浏览器拒绝规则；README 补充阶段3运行与验收入口。
- 真实联调时发现 `app.persistence.segments → app.captions.__init__ → app.captions.runtime → app.persistence.segments` 的冷启动循环导入；移除包级 runtime 重导出，并新增独立子进程导入 `app.main` 的回归测试，避免已加载模块掩盖问题。

### 最终自动化验证

- 后端全量回归：98 项通过；仅有既有 Starlette `httpx` 弃用警告，无失败、未关闭任务或未 await coroutine 告警。
- Alembic 已确认 `20260722_0002 (head)`；前端 TypeScript 6.0.3 类型检查和 Next.js 16.2.10 生产构建均通过，首页与 404 页面静态生成成功。
- 无凭据验证器输出：`partial_replaced=true`、`published_revisions=[1,2,3]`、`durable_final_count=1`、`snapshot_recovered=true`、`session_status=completed`、`metrics_published=true`、`error_count=0`、`clean_shutdown=true`。

### 真实中文端到端验收

- 任务书示例音频在当前工作区不可用，因此使用 Windows SAPI `Microsoft Huihui Desktop` 临时生成 13.46 秒中文 WAV；文案为“大家好，欢迎使用实时字幕工作台。今天我们将验证语音识别、字幕修订、持久化快照和页面恢复功能。”。该差异只替换验收输入，不改变链路或判定标准。
- Session `b5d4aada-2946-4c0a-bbf4-20f91b33eb1e` 完整通过：Replay 673 帧；收到 17 个 Partial、1 个 Final、1 个 Metrics；Partial 文本发生多次修订；未知 schema 包已发送且房间保持稳定；Provider 错误为 0；观察者与 Replay 均干净退出。
- SQLite/API 快照只有 1 条 Final，revision 18，音频时间 320–12760 ms；Session 最终为 `completed`，识别文本与合成文案完全一致。
- 内置浏览器重新加入同一 Session 后，即使其沙箱在 RTC 建连阶段最终断开，页面仍从 SQLite/API 恢复 `completed`、1 条 Final 和正确时间线，Final DOM 数量为 1，浏览器控制台错误为 0。
- 另以现有日语 WAV 对 Session `c67f6f51-5481-4c03-a18b-d4b3d449698a` 完成一次真实全链路复核：14 个 Partial、1 个 Final、1 条快照、1 个 Metrics，Session 为 `completed`。
- 验收后已停止 API、Worker、Frontend，移除 LiveKit 测试容器及网络；3000、8000、7880、7881 均无监听，未残留本次创建的 Python、Node 或 FFmpeg 进程。

### 已知环境差异与阶段边界

- Codex 内置浏览器沙箱不能稳定完成本地 Docker LiveKit PeerConnection；相同 Token/Room 的原生 RTC 观察者已接收全部实时事件。页面快照恢复、DOM 和控制台已由内置浏览器验证，RTC 事件链路由原生观察者验证。
- 阶段3不调用 DeepSeek，不实现翻译、历史复杂编辑或 SRT/VTT/JSON/Markdown 导出。阶段4应直接基于已持久化 Segment 建立历史时间线、编辑与导出，并保持 `livecaption.events.v1` 合同兼容。

## 2026-07-23 — Stage 4：历史时间线与确定性导出（已完成）

### 持久化元数据与统一时间线

- 新增 Alembic `20260722_0003`：Session 保存真实 `asr_provider`、`asr_model` 和六项终态指标；Segment 保存非空 `received_at_ms`，迁移时由原有 `finalized_at` 回填，既有 Stage 3 数据完整保留。
- Worker 开始运行时持久化实际 Provider/模型，成功结束时在同一事务中保存 `completed` 与全部 `TranscriptionMetrics`；失败路径不会伪造成功指标，Final 仍保持“SQLite 提交成功后再发可靠 LiveKit 事件”的顺序。
- 新增纯时间线服务：稳定按音频时间排序，处理负数、倒置和缺失时间，缺失末尾默认补 2000 ms，保留合法重叠、Unicode、换行与原始显示文本。

### Session 历史、四种导出与页面恢复

- 新增 `GET /api/sessions`，按创建时间及 ID 倒序返回历史，并返回 Provider、模型与持久化指标。
- 新增 `GET /api/sessions/{session_id}/export`，支持 JSON、SRT、WebVTT、Markdown；只读取 Final，统一标准化一次后交给各格式导出器，返回固定 UTF-8 Content-Type 和附件文件名。
- 四种导出均为确定性输出；JSON 不加入生成时间，SRT/WebVTT 使用各自标准时间码，Markdown 含来源、语言、时长和逐段时间戳。响应与文件不包含 Provider 原始载荷、凭据或内部异常。
- 前端新增重启安全的历史列表、刷新状态和四种下载入口。打开历史项会先断开活动 Room，再仅通过 HTTP 获取 Session 与 Final 快照，不申请 Token、不连接 RTC；原有新建、加入和实时字幕路径保持兼容。
- 历史恢复会显示已持久化的音频时长、Provider/模型和终态指标，并支持移动端堆叠布局。

### 自动化与无凭据纵向验收

- 后端全量回归：113 项通过；仅保留既有 Starlette `httpx` 弃用警告，无失败、未关闭任务或未 await coroutine 告警。
- Alembic 实际文件库已确认 `20260722_0003 (head)`；前端 TypeScript 类型检查和 Next.js 生产构建均通过。
- `scripts/verify_stage4_local.py` 使用临时 SQLite、乱序 Final 和 Draft 哨兵，实际调用历史、快照和四种导出 API；结果为 `status=ok`、`history_count=2`、`snapshot_final_count=2`、`draft_excluded=true`、`normalized_missing_end_ms=2500`、`metadata_persisted=true`、`clean_shutdown=true`。
- 无凭据验证器的重复导出 SHA-256：JSON `6e274156ef9d6d7235d6c391ab24da8937d2c597911db42b9572f6cbcdb45d43`；SRT `edaf56afb2e8d138a365f3d5042cd153342df8f873925e412bc516b7a1fa16`；WebVTT `24a9396f38e2017d027e98b5dc1fa9de3bde44c6d4282bda3a5306790ae2315c`；Markdown `1f72e10ce1a2d1e81d237031a2a3b7ec93dd3d128d41af078ae60ffe046d3679`。

### 已持久化真实数据验收

- 仅启动迁移后的 API 和前端，重新打开 Stage 3 中文 Session `b5d4aada-2946-4c0a-bbf4-20f91b33eb1e`；历史包含该记录，状态为 `completed`，快照和 JSON 各含 1 条 Final，文本与 Stage 3 中文识别结果完全一致。
- 四种格式均连续下载两次且字节一致；SHA-256：JSON `9793237d63b8fc1e9e854c208b68d64c50dd672a205d055d16f98f41924e75c1`；SRT `38e60edd87dd53ff87551d7f009f73ed48222e8ab436e4dac05eb4ed0c616f61`；WebVTT `1f29d3d3357e10e87fd138769f4a1a9f807e474d9286e8314c7fe80648571f21`；Markdown `2e7935589b1da9312598ad7994aa02123f165b90fce79d15288bd9a4e58f393d`。
- SRT 时间码、WebVTT 文件头、Markdown Final 文本和 JSON 结构均通过。内置浏览器打开历史项后显示 `completed`、`00:12.760`、1 条正确 Final 与四个下载链接；Connection 为 `disconnected`、远端参与者为 0，证明历史恢复没有建立 RTC；控制台错误为 0。
- Windows PowerShell 5.1 的 `Invoke-RestMethod` 会在无显式 `charset` 的 JSON 上按本地代码页解码，可能造成终端内中文比较假失败；验收改用原始响应字节按 UTF-8 解析后通过。浏览器、导出文件和 API 原始字节均正确。
- 该 Stage 3 Session 创建于迁移前，因此新增 Provider/模型/指标列为 `null`；这是历史数据缺少原始信息的预期结果，阶段4创建的新运行会真实写入这些字段。
- 验收结束后已关闭内置浏览器标签页并停止本次启动的 API/Frontend 精确进程树；3000、8000 端口均已释放，运行日志没有 ERROR、Traceback、未关闭任务或未 await coroutine。

### 阶段边界

- 阶段4未调用百炼或 DeepSeek；真实验收直接消费已持久化 Final，不依赖网络或 ASR 凭据。
- 阶段4不包含翻译、复杂手工编辑、Draft 持久化、上传或阶段6扩展状态机。阶段5从 DeepSeek 业务能力开始，并继续保持百炼原始消息与凭据不进入页面和导出。

## 2026-07-28 — Stage 5：DeepSeek 整理版台本（已完成）

### 配置、持久化与严格处理链路

- 新增服务端 DeepSeek 配置、`httpx` 运行依赖和兼容 `LLM_*` 别名；模型只接受 `deepseek-v4-flash` / `deepseek-v4-pro`，API Key 只存在于被忽略的根目录 `.env`，不进入 `NEXT_PUBLIC_*`、响应或日志。
- 新增 Alembic `20260723_0004` 与 `processed_scripts` 表，按 `(session_id, version)` 唯一约束保存 Provider、模型、来源快照、严格结构 JSON、Markdown 和生成时间。版本只增不改，列表按新到旧返回。
- 新增确定性双上限分块、DeepSeek Chat Completion JSON Output 适配器、有限重试和严格 Pydantic 解析。空响应、截断、非法 JSON、额外/缺失字段、外来/重复/遗漏来源、错误时间范围均拒绝；认证错误不重试，超时、429、5xx 与可重试输出错误受有限次数约束。
- 每个 Final 必须恰好引用一次，整理段落时间必须等于其来源范围；后续分块失败时不返回或保存半成品。提示词禁止翻译、虚构、删除姓名/数字/时间或改写来源 ID。

### API、页面与源字幕隔离

- 新增生成、版本列表、详情和 Markdown 导出四个 API：`POST/GET /api/sessions/{id}/scripts`、`GET /api/scripts/{id}`、`GET /api/scripts/{id}/export`。未配置、无 Final、Provider/解析失败和持久化失败均返回稳定、脱敏错误。
- 前端新增“生成整理版台本”、加载/错误/重试、版本选择、原始字幕/整理台本切换、Provider/模型/版本/时间元数据、来源片段 ID、备注/警告和独立 Markdown 下载。
- 整理流程只读取 Final 的标准化快照；Stage 3 实时事件、SQLite Segment 行及 Stage 4 的 JSON/SRT/WebVTT/Markdown 源导出均保持不变。生成失败不会清空或替换页面中的原始字幕。

### 自动化与无凭据纵向验收

- 后端全量回归：164 项通过；仅保留既有 Starlette `httpx` 弃用警告，无失败、未关闭任务或未 await coroutine 告警。
- Alembic 实际文件库确认 `20260723_0004 (head)`；`uv lock --check --offline` 成功解析 87 个包；前端 TypeScript 类型检查和 Next.js 生产构建均通过。
- `scripts/verify_stage5_local.py` 在临时 SQLite 上经真实 FastAPI 路由生成两个 Fake Provider 版本，再注入非法输出；结果为 `status=ok`、`versions=[2,1]`、`script_count=2`、`source_segment_count=2`、`referenced_source_ids=["seg-1","seg-2"]`、`source_rows_unchanged=true`、`source_exports_unchanged=true`、`invalid_output_rejected=true`、`provider_call_count=4`、`clean_shutdown=true`。

### 真实 DeepSeek 与浏览器验收

- 经用户明确授权，将既有中文 Session `b5d4aada-2946-4c0a-bbf4-20f91b33eb1e` 的 1 条持久化 Final 发送到 DeepSeek 官方 `https://api.deepseek.com/chat/completions`；HTTP 200 后创建台本 `d9929472-6ec1-4b85-bb79-4241b0278025`、版本 1，Provider `deepseek`、模型 `deepseek-v4-pro`。
- 真实验证器报告 `source_segment_count=1`、`section_count=1`、`source_references_complete=true`、`source_rows_unchanged=true`、`source_exports_unchanged=true`、`markdown_exported=true`、`clean_shutdown=true`。密钥和请求头未打印。
- 首次真实验证在调用前发现根目录执行时相对 SQLite URL 被解析到错误目录，因此没有发送数据；验证器随后将 `sqlite:///./...` 按后端目录解析，第二次运行通过。该修正只统一脚本运行路径，不改变应用数据库合同。
- 内置浏览器仅通过 HTTP 打开该历史 Session：Connection 为 `disconnected`、远端参与者为 0；显示 1 条原始 Final 和 DeepSeek v1，可在原始字幕/整理台本之间切换。整理视图显示标题、正确文本、时间范围和来源 ID；四个源导出及一个整理版 Markdown 入口均存在，浏览器控制台错误为 0，未生成第二个真实版本。
- 验收结束后已关闭内置浏览器标签页，并按已核验命令行停止本次启动的 API/Frontend 精确进程树；3000、8000 端口均已释放且 HTTP 不可达。运行日志无 Traceback、ERROR、未关闭任务或未 await coroutine；PowerShell 仅将 Uvicorn 的正常 stderr 日志包装为 `NativeCommandError` 展示，不是应用运行失败。

### 阶段边界

- 阶段5不实现翻译、复杂手工编辑、Draft 持久化、上传、后台任务队列或阶段6扩展状态机。
- Final 及其四种源导出继续是权威数据；整理版是独立的可追溯派生版本。百炼原始字段、DeepSeek 原始响应和两类凭据均不得进入浏览器或导出。

## 2026-07-28 — Stage 6：工程收口与首版验收（已完成）

### 状态机、错误与资源边界

- 新增统一 Session 状态域和 Repository，正常链固定为 `created -> room_ready -> replaying -> transcribing -> finalizing -> completed`；任意非终态可进入 `failed` 或 `cancelled`，相同状态幂等，三个终态不可回退、跳转或被晚到事件覆盖。
- Alembic `20260728_0005` 增加 `error_code` / `error_message`，旧 `running` 映射为 `transcribing`；实际 SQLite 已确认 `20260728_0005 (head)`。API、Worker、可靠事件和前端使用同一完整状态集合。
- 固定错误类别为 `configuration_error`、`media_decode_error`、`livekit_error`、`asr_auth_error`、`asr_stream_error`、`persistence_error`、`deepseek_error`、`export_error`；只持久化和发布稳定类别与预定义脱敏消息。
- 阶段6复核时修正取消态合同：`cancelled` 只持久化并发布
  `session.status`，不再使用合同外的 `"cancelled"` 错误码，也不发布
  `session.error`；HTTP/SQLite 的 `error_code` 和 `error_message` 均保持
  `null`。Repository、运行时与 API 的幂等取消测试同步覆盖。
- LiveKit 连接和 FFmpeg 启动新增有限超时并覆盖取消/失败清理；ASR 和 DeepSeek 沿用各自有限超时。Replay 失败可通过窄 API 写入终态，状态机阻止 Worker 晚到完成覆盖失败。

### 启动器、本地纵向测试与文档

- 新增 `scripts/start_dev.ps1` 和 `scripts/start_dev.sh`：检查 `.env`、后端虚拟环境、前端依赖和配置，先迁移，再监督 API/Worker/Frontend，按独立日志和记录 PID 精确清理；Replay 保持显式独立命令。
- 在工程收口后新增根目录 `start_demo.cmd` 和 PowerShell `-Demo` 模式：
  自动检查/启动 Docker Desktop、启动本地 LiveKit、复用迁移与三个应用进程
  监督器、等待 API/Frontend 就绪并打开工作台。退出时只清理本次记录的应用
  进程树；仅当 LiveKit 原本未运行时才执行 `docker compose down`，避免关闭
  用户已有服务。
- 首次实际启动发现当前 pnpm 会把 `dev -- --hostname` 中的分隔符继续传给 Next.js，导致其把 `--hostname` 误判为项目目录；已统一修正为 `dev --hostname ...` 并增加回归断言。修正后的实际冒烟中 API/Frontend 均返回 200，随后精确停止监督进程树，3000/8000 端口释放。
- 本地 Docker bridge 原先向浏览器发布容器内 `172.18.*` ICE candidate，内置浏览器无法访问并在 15 秒后关闭信令。`docker-compose.yml` 现显式
  `--node-ip 127.0.0.1`，与已发布的 7881/TCP、7882/UDP 对齐，并增加
  Compose 合同测试；修正后真实浏览器 WebRTC 连接成功。
- 新增 `scripts/verify_stage6_local.py` 和真实 SQLite/FastAPI 集成测试：两个独立 Fake ASR Session 均完成 `Partial -> Partial -> Final`，revision `[1,2,3]`，每个 Session 仅 1 条持久化 Final、无 Draft 行，状态序列完整，JSON 导出隔离，Provider/任务/数据库干净关闭。
- 新增 `docs/architecture.md`，补充 `docs/api-events.md` 的完整状态、终态错误和五类合法 JSON 示例；README 改为阶段6干净环境运行手册，包含 LiveKit、百炼、DeepSeek、数据库、一键/手动启动、Replay、导出、验证、关闭和常见错误。
- 新增 `scripts/verify_stage6_e2e.py`，复用 Stage 3/5 验收器：默认显式执行真实 LiveKit/百炼主链路，只有 `--with-deepseek` 才调用 DeepSeek；`--require-browser` 要求页面的 `browser-*` 参与者在 Replay 前已连入同一 Room，未连接页面时直接失败。输出只含脱敏摘要、计数、ID 和导出哈希。

### 最终自动化验证

- 增加演示启动器后的后端最终全量回归：201 项通过、1 项环境性跳过、1 个既有 Starlette `httpx` 弃用警告；跳过项仅为当前 Windows System32 WSL Bash 无法访问工作区，PowerShell 启动器合同和实际启动均已通过。
- 阶段6聚焦套件 50 项通过、1 项环境性跳过；无凭据验证器最终运行仍为 `status=ok`、`state_sequence=["replaying","transcribing","finalizing","completed"]`、`session_count=2`、`durable_final_count=2`、`runs_isolated=true`、`clean_shutdown=true`。
- 前端 TypeScript 类型检查和 Next.js 16.2.10 生产构建通过，首页和 404 均静态生成；页面阶段标识更新为 Stage 6。Codex 沙箱内外 pnpm store 不同曾触发依赖目录重建，最终在同一已授权环境按锁文件恢复后连续完成类型检查和构建，源码与锁文件未改变。
- `start_dev.ps1 -CheckOnly` 最终通过，不迁移、不启动进程；实际启动冒烟使用修正后的参数并通过。
- `start_demo.cmd` 真实冒烟通过：LiveKit 7880/7881、API 8000 和
  Frontend 3000 均就绪，API/Frontend 返回 200，Worker 注册成功，三个记录
  进程均存活且启动日志 0 条 ERROR/Traceback。启动器终端和工作台页面按用户
  要求保持运行，供实际体验。

### 真实中文、DeepSeek 与浏览器验收

- 使用 Windows `Microsoft Huihui Desktop` 固定生成 13.454785 秒中文 WAV；真实 Session `e1e5261f-4ceb-43af-9263-4c1d69813793` 经 673 帧 Replay 完成。原生 RTC 观察到 18 条 Partial、Partial 原位修订、1 条 Final、1 条 Metrics，以及 `replaying -> transcribing -> finalizing -> completed` 精确顺序；SQLite/API 仅有 1 条 Final。
- 四种源导出连续两次字节一致：JSON `2b258f879bd1e4fcb8386e2e8ad71f8e8f809ee28f7265b59619511c6172ec05`，SRT `38e60edd87dd53ff87551d7f009f73ed48222e8ab436e4dac05eb4ed0c616f61`，WebVTT `1f29d3d3357e10e87fd138769f4a1a9f807e474d9286e8314c7fe80648571f21`，Markdown `46d08285e7057c7812ebe1d5b87a1d231e51e12edb8cf9ace9bdfded6a826487`。
- 显式 `--with-deepseek` 创建整理版 `146ad9f9-67ff-4d0a-9d1f-1f9389274cb2`、版本 1、模型 `deepseek-v4-pro`；来源引用完整、四种源导出保持不变。
- 新增同场实时页面验收 Session
  `fb00d24f-d670-424b-9c01-77431b093841`。验收器在 Replay 前确认
  `browser-e9ac05dd26` 已在同一 Room；页面依次显示 `replaying`、
  `transcribing` 和 `completed`，直接观察到同一 Draft 的 revision
  `6 -> 11 -> 12 -> 14 -> 16` 原位替换。最终页面显示 1 条 revision 19
  Final，文本为“大家好，欢迎使用实时字幕工作台。今天我们将验证语音识别、字幕修订、持久化快照和页面恢复功能。”。
- 同一轮真实验收由 RTC 观察器确认完整
  `replaying -> transcribing -> finalizing -> completed` 顺序、18 条
  Partial、1 条 Final、1 条 Metrics、673 帧 Replay 和 1 条持久化
  Final；四种导出重复字节一致。页面刷新后从历史恢复
  `completed`、1 条 Final、revision 19 和四个导出入口，保持
  `disconnected`，说明恢复不依赖 RTC。实时和刷新后的浏览器控制台均为
  0 条 warning/error。
- 真实验收日志中无 `ERROR`、Traceback、未 await coroutine 或任务销毁警告，且无残留 FFmpeg。首次 ICE 失败留下的精确测试 Session 已按新合同收口为 `cancelled`，两个错误字段均为 `null`。验收标签页关闭，API/Worker/Frontend 精确进程树停止，真实 LiveKit compose 容器及网络已移除，3000/8000/7880/7881 均无监听。

### 首版边界

- 阶段0至阶段6形成可本地演示、可恢复、可导出、可派生整理版的首个工程闭环；Final 仍是唯一权威源字幕，LiveKit 仍只承担实时媒体与视图事件。
- 未实现翻译、复杂手工编辑、Draft 持久化、上传或后台任务队列。后续第二链路必须引用不可变 Final 快照，不得混入 `caption.upsert` 或改变 ASR Session 终态。

## 2026-07-28 — Room 与输入流调试控制台

### 已完成

- 新增持久化 `managed_rooms`，Session 增加可空 `room_id`；Alembic
  `20260728_0006` 取消 `sessions.room_name` 唯一约束，使同一 LiveKit
  Room 可按时间连续保存多个独立 CaptionRun。
- 新增 Room 创建、列表、读取、重命名、关闭、操作员 Token、CaptionRun
  创建/列表/取消和移除参与者 API。操作员 Token 同时允许 publish、
  subscribe 和 DataPacket。
- Worker 同时兼容旧 `replay-* / replay-audio` 和新
  `caption-input-{session_id}` 轨道；浏览器任务结束后 Worker 不再退出
  Room，可继续处理下一次任务。轨道取消发布和参与者退出都会触发有限收尾。
- 首页替换为三栏 Room 调试控制台：Room 管理、音频输入与实时字幕、参与者/
  轨道和任务历史。浏览器支持麦克风、标签页/系统音频和本地媒体文件三类输入，
  并保留四类源台本导出。
- 设计与实施计划分别记录在
  `docs/plans/2026-07-28-room-stream-console-design.md` 和
  `docs/plans/2026-07-28-room-stream-console.md`。

### 已执行验证

- 后端全量：208 passed、1 skipped、1 个既有 Starlette 弃用警告。
- 前端：`pnpm --dir frontend typecheck` 与生产 `next build` 均通过。
- 空库迁移从首版连续升级到 `20260728_0006`；正式
  `backend/live_caption.db` 由 `20260728_0005` 成功升级到 head。
- 一键演示重新启动，API 与 Frontend 返回 200，Worker 注册成功。
- 浏览器真实创建并进入 `调试直播间`，页面显示 `Room 已连接`，LiveKit
  运行态显示操作员和自动派发的字幕 Worker 共 2 个参与者；浏览器日志无
  error。媒体权限与实际讲话由用户在页面显式触发。
- `scripts/verify_room_input.py` 通过真实 LiveKit 发布
  `caption-input-4a563d53-7b79-42ef-8b99-969e7f4af471`，发送 50 帧/
  1.0 秒音频；对应 CaptionRun 正常进入 `completed`，Worker 保持在同一
  Room。页面刷新任务历史后显示该本地媒体任务“已完成”，控制台仍无 error。

### 当前边界

- 本批次是单用户调试版，用户同时是管理员和客户；尚未加入账号、角色与多租户。
- 尚未接入 RTMP、HLS、SRT 或 OBS Ingress；它们将在后续作为新的
  InputSource 适配器实现。

## 2026-07-30 — Docker 冷启动修复

- 修复 `start_dev.ps1 -Demo` 在 Docker 引擎未运行时的冷启动缺陷：
  `docker info` 写入 stderr 曾被全局 `ErrorActionPreference=Stop` 提升为
  `NativeCommandError`，导致脚本在执行 `docker desktop start` 前退出。
- Docker 探测现在临时使用非终止型原生命令处理、合并并丢弃预期诊断，以退出码
  判断引擎状态；Docker Desktop 启动命令同样安全收集原生输出。
- 当当前 Docker CLI 不支持或不能完成 `docker desktop start` 时，启动器回退到
  已安装的 `Docker Desktop.exe`，随后仍使用原有 120 秒引擎就绪轮询。
- 新增两项启动器回归合同；`test_dev_launchers.py` 为 9 passed、1 skipped，
  `-Demo -CheckOnly` 通过。
- 在 Docker Desktop 完全停止的真实场景重新执行 `start_demo.cmd`：Docker
  Desktop 和本地 LiveKit 自动启动，API 返回健康状态，Worker 注册成功，
  Frontend 冷编译后返回 HTTP 200，运行日志无 ERROR/Traceback。

## 2026-07-30 — 公网 M3U8 音频输入

- 新增公网 HTTP/HTTPS M3U8 URL 安全校验、服务端 FFmpeg PCM 解码、独立
  LiveKit 发布者、Room 启停 API、异常终态协调和前端 M3U8 输入控件。
- HLS 发布者使用 `hls-{session_id}` 身份和
  `caption-input-{session_id}` 音轨；Worker 继续复用既有百炼 ASR、字幕事件、
  SQLite Final 持久化和四类导出链路。
- 后端全量回归为 `216 passed, 1 skipped`；前端 `pnpm typecheck` 和生产构建
  通过。HLS 定向用例 4 项通过，启动器 UTF-8 日志合同用例通过；当前 Windows
  环境仍存在 pytest 打印完成结果后退出挂起的既有现象。
- 公开验收源为 Al Jazeera English 音频 HLS；`ffprobe` 确认唯一音轨为 AAC。
  Session `067e2531-4ad5-41b7-b14b-d73f864bb3c1` 达到 `transcribing`，
  页面实时显示 Partial，并在正常停止后进入 `completed`。
- 刷新页面后恢复 4 条 SQLite Final，其中包含非空英文字幕；验收结束时
  非终态 HLS Session 为 0、残留 FFmpeg 进程为 0，HLS 发布者已离开 Room，
  Worker 保持可用。
- 直播内容出现阿拉伯字符时暴露出 Windows LiveKit 开发日志的 GBK 输出问题；
  PowerShell 和 Bash 启动器现显式设置 `PYTHONUTF8=1` 与
  `PYTHONIOENCODING=utf-8`，避免 Unicode 日志触发 Rich
  `UnicodeEncodeError`。公开验收 URL 未写入产品代码或自动化测试。

## 2026-08-01 — Stage 1.5 子阶段 A：孤儿 HLS 与资源生命周期

### 一、完成内容

- 新增独立于 Session 业务状态的 `source_status`、`source_ended_at`、
  `cleanup_status`、`cleanup_detail`；终态 Session 仍可进入资源 stopping/stopped。
- 新增不进入 OpenAPI 的
  `POST /internal/sessions/{session_id}/source/abort`，使用
  `X-Internal-Control-Token` 鉴权，重复调用分别返回 `stopped` /
  `already_stopped`。
- Worker 在主 ASR、主字幕 runtime 不可恢复异常及强制取消时，先保留原失败，
  再通过 httpx 发出低频 abort；翻译降级与正常 EOF 不触发该控制请求。
- HLS Source 清理改为逐步执行并汇总 decoder、queue、Track、AudioSource、Room
  结果；单一步骤失败不阻断后续步骤，已退出 FFmpeg、已取消 Track、已断开 Room
  均安全处理。
- HLS Manager stop/abort 支持 `force=True`、并发锁、有限条历史结果和幂等返回；
  普通停止接口不再因为 Session 已是业务终态而跳过资源清理。
- 实际故障验收发现 HLS 最坏清理约 5.1 秒，而内部请求原默认 3 秒；已将默认值
  调整为 10 秒，并在 Settings 中强制内部超时大于 HLS stop 超时。

### 二、修改文件

- `backend/app/api/internal.py`
- `backend/app/api/rooms.py`
- `backend/app/api/sessions.py`
- `backend/app/captions/runtime.py`
- `backend/app/hls/manager.py`
- `backend/app/hls/source.py`
- `backend/app/logging.py`
- `backend/app/main.py`
- `backend/app/persistence/models.py`
- `backend/app/persistence/sessions.py`
- `backend/app/settings.py`
- `backend/app/worker/control.py`
- `backend/app/worker/entrypoint.py`
- `backend/alembic/versions/20260801_0009_add_source_lifecycle.py`
- `backend/tests/test_caption_runtime.py`
- `backend/tests/test_hls_input.py`
- `backend/tests/test_internal_control.py`
- `backend/tests/test_rooms_api.py`
- `backend/tests/test_session_repository.py`
- `backend/tests/test_settings.py`
- `backend/tests/test_worker_entrypoint.py`
- `scripts/verify_stage3_local.py`
- `scripts/verify_stage6_local.py`
- `.env`、`.env.example`、`README.md`
- `docs/plans/2026-08-01-stage1-5-stability-closure.md`

### 三、关键设计

- 高频 CaptionEvent 仍直接走 LiveKit reliable DataPacket；HTTP 只承担低频资源控制。
- Worker 的持久化失败原因是主错误源，abort 的 HTTP/清理错误仅写入 cleanup 告警，
  不覆盖 `asr_stream_error` / `asr_auth_error`。
- API endpoint 在等待 Manager 前释放只读 SQLAlchemy 事务，Manager 使用自身短事务
  写资源进度，完成后 endpoint 再刷新并返回最终状态。
- Manager 最多保留 256 条停止结果，使短期重复 abort 可区分
  `already_stopped`，同时避免无限增长。

### 四、运行命令

- `backend\.venv\Scripts\python.exe -m alembic upgrade head`
- `backend\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`
- `powershell -File scripts/start_dev.ps1 -SkipMigration`
- 公网 HLS：`https://piccpndks.v.kcdnvip.com/audio/cctv13_2.m3u8`
- 使用实际 `consume_replay_audio`、`ASRProviderError` 和
  `request_source_abort` 执行确定性主链故障注入。

### 五、测试结果

- 阶段 A 聚焦回归：59 passed，1 个既有 Starlette/httpx 弃用警告。
- 最终后端全量：233 passed、1 skipped、1 warning，16.26 秒。
- 首轮全量曾有 1 项失败：旧 `verify_stage6_local.py` 未传新增的
  `source_type`；同步修正 Stage 3/6 验证脚本后重新全量通过。
- pytest 默认 cacheprovider 在当前受限 Windows 文件系统的
  `tempfile.mkdtemp` 中挂起；通过 faulthandler 精确定位后，验证命令只禁用测试
  缓存插件，不改变测试收集或断言。

### 六、手动验证

- 实际 HLS Session `4c2a89a1-7706-42c9-9d93-3987e63c7b66`：先写入
  `asr_stream_error`，首次内部 abort 返回 `stopped`，第二次返回
  `already_stopped`；Source 与 cleanup 均为 stopped/completed，已有 Final 保留。
- 确定性 Worker 主链故障 Session
  `e1c5511c-4900-41c4-a733-d59b97d37247`：实际
  `consume_replay_audio` 捕获 `ASRProviderError`，记录
  `source_abort_requested`，API 记录相同 session_id/request_id 的
  `source_abort_received` 与 `source_abort_completed`。10 秒客户端预算内返回
  `stopped`；Session 保持 failed/asr_stream_error，15 条已有源 Final 保留。
- 尝试通过临时无效 Key/本地 WebSocket 配置等待云端自然失败，两次各 30 秒均未
  进入失败，未声称通过；随后停止对应输入并改用确定性异常注入完成控制链验收。

### 七、资源检查

- FFmpeg：验收 Session 对应进程已退出；最终系统 FFmpeg 数为 0。另发现并清理
  旧代码遗留的孤儿 PID 26056，命令行确认属于同一公网 HLS 输入。
- LiveKit Room：故障验收后参与者列表为空，Publisher Track 已取消，Publisher
  Room 与字幕 Worker job 均离开；常驻 Worker 仍注册等待下一任务。
- Worker Task：最终正常启动栈无活动 Room job；LiveKit Agents 的监督/预热子进程
  继续常驻，属于设计内资源。
- SQLite：正式库 revision 为 `20260801_0009`；迁移前一致性回滚备份为
  `.runtime/backups/live_caption.pre-stage1_5-A-consistent-20260801-1720.db`，
  revision `20260730_0008`，包含 2 Room、8 Session、16 源 Final、5 译文 Final。
  最初直接复制 `.db` 的备份漏掉未 checkpoint 的 WAL（仅 12/1 条 Final），已保留
  但不作为有效回滚点；后续用 SQLite backup API + 副本 downgrade 补齐。

### 八、已知问题

- 当前 `.env` 仍使用相对 `DATABASE_URL`；数据库绝对路径、DATA_DIR 和 SQLite
  WAL/并发策略按任务书留给紧接的子阶段 B。
- 当前自然云端错误注入缺少确定性开关；阶段 C 将提供 Fake Provider 和正式故障
  注入模式，真实百炼仅保留有限 smoke。
- 仍有 1 个 Starlette TestClient 关于 httpx/httpx2 的第三方弃用警告，不影响本阶段。

### 九、下一阶段入口

- 子阶段 B：统一 DATA_DIR 与绝对 SQLite URL，确认 API/Worker 同库，补齐
  `synchronous=NORMAL`、WAL 日志、busy timeout/外键测试，并审核 Final upsert 与
  事务边界。

## 2026-08-01 — Stage 1.5 子阶段 B：SQLite 单路径与并发收口

### 一、完成内容

- 新增 `DATA_DIR`，默认解析为项目内绝对目录 `backend/data`；Settings 的 `.env`、
  数据目录和 SQLite URL 均不再依赖当前工作目录。
- `DATABASE_URL` 留空时由共享 Settings 生成绝对 SQLite URL；显式旧式相对 SQLite
  URL 固定按 `backend/` 解析。API、Worker 和 Alembic 均消费同一结果。
- Alembic 的 `script_location` 与 `prepend_sys_path` 改为基于配置文件的
  `%(here)s`，从项目根、`backend/` 或 `scripts/` 执行时均指向同一迁移目录和库。
- SQLite 每条连接统一设置 WAL、`synchronous=NORMAL`、`foreign_keys=ON` 和
  `busy_timeout=5000`；API 启动时记录实际 PRAGMA，Worker 记录同一脱敏绝对路径，
  WAL 不可用时产生明确告警。
- 审核源 Final 与翻译 Final 的幂等和事务边界：唯一约束已存在，重复/旧 revision
  不新增也不覆盖，Final 后到达的 Draft 被 reconciler 拒绝；两个 runtime 均保持
  SQLite commit 成功后才发布可靠 Final。
- 迁移前查询正式库未发现源或翻译重复键，因此无需新增 `0010` 修复迁移，也未删除
  任何历史记录。

### 二、修改文件

- `backend/app/settings.py`
- `backend/app/persistence/database.py`
- `backend/app/logging.py`
- `backend/app/main.py`
- `backend/app/worker/entrypoint.py`
- `backend/alembic/env.py`
- `backend/alembic.ini`
- `backend/tests/test_settings.py`
- `backend/tests/test_session_repository.py`
- `backend/tests/test_translation_repository.py`
- `backend/tests/test_translation_runtime.py`
- `scripts/start_dev.ps1`
- `scripts/start_dev.sh`
- `.env`、`.env.example`、`.gitignore`、`README.md`
- `docs/stage-records.md`

### 三、关键设计

- 数据目录锚定项目根，旧相对 SQLite URL 锚定 `backend/`；二者都不使用进程 cwd，
  防止 API、Worker、迁移或临时脚本静默创建第二个数据库。
- WAL 解决独立 API/Worker 的读写并行，但不把 SQLite 误当成多写者数据库；写事务
  保持短小，第二写者在 5 秒 busy timeout 内有界等待。
- Final 持久化继续使用已有 `(session_id, segment_id)` 与
  `(session_id, target_language, segment_id)` 唯一约束。仓储只接受 Final，按
  revision 更新；运行时先 commit，再通过 LiveKit 发布。
- 数据迁移使用 SQLite backup API 合并主库和未 checkpoint 的 WAL，而不是直接复制
  `.db`。只有源库、回滚副本和新库的完整性及计数一致后，才归档旧路径文件。

### 四、运行命令

- `backend\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q`
- `backend\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini current`
- 从 `scripts/` 执行 `..\backend\.venv\Scripts\python.exe -m alembic -c ..\backend\alembic.ini current`
- `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dev.ps1 -CheckOnly`
- 正式启动脚本运行 API、Worker、Frontend，并使用独立进程执行 Final、台本和并发读取验收。

### 五、测试结果

- B3 Final 聚焦回归：21 passed。
- 启动器、Settings、PRAGMA、翻译幂等聚焦回归：47 passed、1 skipped。
- 最终后端全量：237 passed、1 skipped、1 warning，17.16 秒；唯一警告仍是既有
  Starlette TestClient 对 httpx/httpx2 的第三方弃用提示。
- 三个不同 cwd（项目根、`backend/`、`scripts/`）解析出完全相同的绝对 SQLite URL：
  `backend/data/live_caption.db`。Alembic 从项目根与 `scripts/` 查询均返回
  `20260801_0009 (head)`，且未重新生成 `backend/live_caption.db`。

### 六、手动并发与重启验证

- 创建验收 Session `72c627fb-3d60-4b16-b3c0-9e809be9fcbb`。API 从
  `backend/` 启动，Worker 从项目根通过 `PYTHONPATH=backend` 启动，Frontend 从
  `frontend/` 启动；Worker 成功注册本地 LiveKit。
- 一个 Final 写进程在 flush 后有意持有写事务 0.3 秒，另一个进程并发保存版本 1
  台本；同时循环读取 Session、Final、台本列表、VTT 导出与前端首页，共 20 轮、
  100 个 HTTP 请求，全部成功，无 lock 错误。
- 同一 `segment_id` 依次写 revision 2、重复 revision 2、旧 revision 1 和 revision 3；
  最终 API 仅返回 1 行、revision 3、文本 `SQLite concurrency final`。
- 台本列表为 1 行、version 1；VTT 与台本 Markdown 导出均返回 200。停止三个进程树
  并用正式启动器重启后，Session、Final 与台本仍完整可读。

### 七、数据与资源检查

- 迁移前正式库：`quick_check=ok`、revision `20260801_0009`，2 Room、11 Session、
  42 源 Final、5 译文 Final、0 台本，源/译文重复键均为 0。
- 一致性回滚备份：
  `.runtime/backups/live_caption.pre-stage1_5-B-consistent-20260801-183157.db`；
  新库和备份均为 147456 bytes，完整性、revision 与五类记录计数一致。
- 旧路径库归档到
  `.runtime/backups/legacy-stage1_5-B-20260801-183157/live_caption.db`；
  `backend/live_caption.db` 已不存在，可从上述备份恢复。
- 验收后正式库为 2 Room、12 Session、43 源 Final、5 译文 Final、1 台本，
  `quick_check=ok`，重复键仍为 0；运行中可见预期的 WAL/SHM sidecar。
- 最终正常栈位于 `.runtime/dev-20260801-184355`：API 与前端均返回 200，Worker
  已注册，FFmpeg 进程数为 0。

### 八、已知边界

- WAL 允许读者与单个写者并行，并不适合未来的多机或高并发多写部署；需要横向扩展
  时应迁移 PostgreSQL，而不是继续提高 SQLite timeout。
- 当前仍有 1 个上游 Starlette/httpx 弃用警告，不影响 SQLite 或运行链路。
- 本阶段台本并发验收直接使用既有 repository 写入确定性合法内容，未调用 DeepSeek，
  因此没有产生云端费用；云服务长测仍留给显式授权的后续验收。

### 九、下一阶段入口

- 子阶段 C：实现 `transport-only`、Fake ASR/翻译 Provider、有限故障注入与有界长测
  报告；先执行无云费用的短预检和本地长测，真实百炼只做用户明确授权的有限 smoke。

## 2026-08-01 — Stage 1.5 已完成项对照复核（A–C3）

### 一、复核范围与结论

- 按任务书逐项复核已实施的 A、B、C1、C2、C3；A 与 B 保持完成，C1–C3 在本次
  修正后与任务书一致。
- C4 的 30 分钟 `transport-only`、Fake Provider 长测和故障清理长测尚未执行，
  因此子阶段 C 仍不能整体标记为完成。
- D–F 尚未进入本次执行范围，也未因本次复核被提前标记完成。

### 二、发现并直接修复的不匹配

- 原 Fake Provider 长稳 CLI 直接拼装 Provider/Session，未完整经过生产回放入口、
  Reconciler、SQLite 与 LiveKit 发布边界。现改为复用 `consume_replay_audio`、实际
  Caption/Translation Runtime、隔离诊断数据库和记录型 LiveKit publisher；报告中的
  Final、revision 和事件数量均来自实际链路结果。
- `real-provider-smoke` 原可被配置为常驻 Worker mode，不符合“只允许 3–10 分钟且
  显式 `--allow-cloud`”的有界要求。现仅长稳 CLI 接受该模式，Worker Settings 明确拒绝。
- M3U8 诊断路径原按解码速度消费，不能代表实时长稳。现使用 ReplayClock 按音频时钟
  节拍消费，并把输入早于请求时长结束视为明确失败。
- ASR/翻译 Session 原未暴露实际队列高水位。现统一提供当前队列与最大队列指标，长稳
  报告不再使用静态占位值。
- README 补齐 Windows `E:/LiveCaption/data` 与 Linux/容器 `/data` 映射示例，并明确
  SQLite/WAL/SHM 位于同一持久目录。
- SQLite 连接初始化先读取当前 journal mode；已经处于 WAL 时不重复切换，非 WAL 时
  仍尝试启用并保留原有明确告警。外键、5 秒 busy timeout 与
  `synchronous=NORMAL` 仍按每条连接统一设置。

### 三、验证证据

- A–C3 最小相关回归集：80 passed、1 个既有 Starlette/httpx 第三方弃用警告，
  无失败。
- Fake Provider 全路由预检报告：
  `backend/data/reports/stage1_5_longrun_20260801-122912-990492.json` 及同名 Markdown；
  18 帧、11520 bytes，源/译文 Final 各 2，LiveKit 事件 19，最大队列 1，丢帧 0，
  FFmpeg/Room/Provider 均无残留，状态为 completed。
- 上述报告使用隔离库
  `backend/data/reports/stage1_5_longrun_runtime_3fa501568631461b8cf260a1dfacb4a1.db`；
  `quick_check=ok`，源/译文各 2 条 Final 且 revision 均为 3，证明重复 Final 与过期
  Partial 未形成重复行或回退。
- 正式库 `backend/data/live_caption.db`：Alembic revision `20260801_0009`、12 个
  Session、`quick_check=ok`、`journal_mode=wal`、`synchronous=1 (NORMAL)`、
  `foreign_keys=1`、`busy_timeout=5000`，可获取并回滚 `BEGIN IMMEDIATE` 写锁。

### 四、环境说明与下一检查点

- Codex 受限命令沙箱会阻止 Python 子进程在工作区创建 SQLite/WAL 文件，表现为
  `unable to open database file`；同一精确诊断在真实本地运行权限下，正式库、同目录
  新库与两个副本库全部通过。该现象不是正式库损坏、路径错误或残留 Worker 锁库。
- 下一检查点严格为 C4：先做短预检，再执行 30 分钟本地 transport-only 和确定性
  Fake Provider 长测；真实百炼 smoke 仍需单独、显式云调用授权。

## 2026-08-02 — Stage 1.5 子阶段 C4：无云长稳验收

### 一、执行边界

- 本批只执行不调用百炼或 DeepSeek 的本地长稳验收：30 分钟 transport-only、
  10 分钟双语 Fake Provider、重复 Final、Final 后旧 Partial，以及一次 Fake 主 ASR
  失败清理。
- 真实百炼 smoke 未执行；它仍需单独、显式的云调用授权。任务书推荐的 60 分钟
  公网 M3U8 长测也未并入本次本地必做批次。

### 二、为 Fake 长测补充的诊断节奏

- 首次以每 3 个 100 ms 块形成一个 Segment 执行 10 分钟双语 Fake 长测，15 分钟
  命令上限时仅排空到源/译文各 511 条 Final、约 153.3 秒音频。隔离库
  `quick_check=ok`，证明并非死锁，而是数千个双语 Final、重复 Final 和逐条 SQLite
  提交使诊断粒度过细。
- 精确停止该次超时运行的两个 Python PID，并删除仅属于失败运行的 `.db/.wal/.shm`；
  未保留无效报告或后台进程。
- 长稳 CLI 新增 `--fake-chunks-per-segment 3..600`，命令行默认 60，即约 6 秒一个
  Fake Segment。每段仍完整产生 Partial v1、Partial v2、Final v3，并经过现有
  Reconciler、SQLite commit 和 LiveKit publisher；生产 Worker、真实百炼配置及
  Fake Provider 类的默认行为均未改变。

### 三、30 分钟 transport-only

- 报告：
  `backend/data/reports/stage1_5_longrun_20260802-030206-798674.json` 及同名 Markdown。
- 请求 30 分钟，实际音频 1800.02 秒、墙钟 1800.281 秒、实时比 0.999855；
  90,001 帧、57,600,640 bytes。
- 内存起点 142.270 MB、峰值 145.566 MB，峰值增量约 3.3 MB，结束工作集已回落；
  队列峰值 10、丢帧 0。
- 结束时 FFmpeg 0、active task 1、Room/Provider 均 false、errors 为空、
  exit_reason 为 completed。

### 四、10 分钟双语 Fake Provider

- 聚焦回归：`test_stage1_5_longrun.py`、`test_fake_providers.py`、
  `test_stage1_5_fake_pipeline.py` 共 14 passed。
- 6 秒 CLI 预检报告
  `stage1_5_longrun_20260802-032634-883739.json`：确认命令行默认
  `fake_chunks_per_segment=60`，源/译文 Final 各 2，LiveKit 事件 32，正常清理。
- 正式长测报告：
  `backend/data/reports/stage1_5_longrun_20260802-033733-488770.json` 及同名 Markdown；
  请求 10 分钟，实际音频 600.02 秒、墙钟 600.562 秒、实时比 0.999098。
- 30,001 帧、19,200,640 bytes；队列峰值 1、丢帧 0；源/译文 Final 各 101，
  LiveKit 事件 1,814。
- 隔离库
  `stage1_5_longrun_runtime_0b0196cbb828471da71563d5f2747a8c.db`：
  `quick_check=ok`，Session 与 Translation 均 completed，源/译文 revision 全为 3，
  两类重复键均为 0，证明重复 Final 未增行、旧 Partial 未回退。
- 101 条源 Final 可由现有导出器读取：JSON 60,556 bytes、SRT 5,641 bytes、
  VTT 5,353 bytes、Markdown 5,567 bytes，VTT 文件头为 `WEBVTT`。
- 内存起点 141.719 MB、峰值 148.621 MB、结束 107.918 MB；FFmpeg 0、active task 1、
  Room/Provider 均 false、errors 为空、exit_reason 为 completed。

### 五、确定性 Fake 主链失败清理

- 报告：
  `backend/data/reports/stage1_5_longrun_20260802-034042-204575.json` 及同名 Markdown。
- 第 5 个 ASR 块注入失败，处理 26 帧、0.52 秒音频后以
  `injected_asr_failure` 有界退出；错误为预期的 Fake `ASRProviderError`。
- 隔离库 `quick_check=ok`，Session 为 `failed/asr_stream_error`，无伪造 Final；
  Provider/Room 均 false、FFmpeg 0、active task 1。
- 最终系统进程复核为 `FFmpegCount=0`、`LongrunPythonCount=0`，无本批长测残留。

### 六、结论与下一入口

- C4 的无云本地验收通过；transport-only 与 Fake Provider 长测均已有可复核 JSON、
  Markdown 和隔离 SQLite 证据。
- 子阶段 C 的代码与无云验收已闭合。真实 Provider smoke 保持“待显式云授权”，
  不把 Fake 结果冒充百炼结果。
- 下一实现批次进入子阶段 D：状态模型、运行时诊断、前端状态面板和结构化日志。

## 2026-08-03 — Stage 1.5 子阶段 D1–D3：状态与运行诊断

### 一、状态契约归一

- Session 正式统一为 `created/starting/running/finalizing/completed/failed/cancelled`；
  Source 独立保持 `starting/running/stopping/stopped/failed/lost`；Translation 统一为
  `disabled/starting/running/completed/failed`。
- 新迁移 `20260801_0011` 在变更前拒绝未知历史值，并显式映射旧状态；正式 SQLite
  已从 `20260801_0009` 升级到 `20260801_0011 (head)`。
- Session 新增 `stop_reason/failure_code/failure_detail/translation_ended_at`，并继续记录
  `ended_at/source_ended_at/cleanup_status/cleanup_detail`。翻译失败只将 Translation 标为
  failed，不改变主 Session 的成功/失败语义。
- Caption/Translation LiveKit 事件、前端事件解析和旧阶段验证脚本均同步到新状态集，
  避免数据库、实时消息和 UI 使用三套不同状态名称。

### 二、运行时快照与公开诊断接口

- API 新增有界内存 `RuntimeSnapshotRegistry`；Worker 通过既有内部控制 Token 上报经过
  Pydantic 白名单验证的布尔值、计数器、队列和时间戳，不写逐帧 SQLite，也不接受
  secret、URL、traceback 或任意 detail 字段。
- `GET /api/sessions/{session_id}/runtime` 合并持久化生命周期、API 所有的 HLS/FFmpeg/
  LiveKit publisher 状态和 Worker 的订阅/ASR/翻译/音频队列快照；源/译文 Final 数量
  仍以 SQLite 为准。当前进程边界无法观测的字段通过 `unavailable` 明确返回。
- HLS source 记录 Room、音轨发布、FFmpeg、帧数、字节数和最后事件；Worker 在连接、
  首帧、每 250 帧、收尾和失败清理时更新快照。快照上报失败只记录诊断告警，不阻断
  字幕主链。

### 三、调试工作台

- 当前任务新增可折叠“运行诊断”面板，展示 Session/Source/Translation/Cleanup、
  Room、发布/订阅音轨、ASR、翻译、FFmpeg、音频帧/字节、队列与两类 Final 数量。
- 面板在选中任务时读取一次；仅非终态任务每 2 秒轮询，completed/failed/cancelled
  只保留终态快照，避免历史列表产生后台轮询。
- 不可用项、失败码或正常停止原因均显式显示；原文/译文双语字幕和任务导出入口保持
  原有交互。

### 四、验收结果与下一入口

- 后端全量：254 passed、1 skipped、1 个既有 Starlette/httpx 第三方弃用警告，
  27.99 秒；无失败。
- 前端：`pnpm typecheck` 通过；Next.js 生产 `pnpm build` 通过，首页静态生成成功。
- 状态/API/HLS/Worker 聚焦验收先通过 58 项；扩大验收发现并修复 Stage 6 脚本等待
  第一次 running 的旧竞态，修复后该验收通过，未新增冗余测试矩阵。
- D1、D2、D3 已完成；下一批进入 D4，补齐生命周期结构化日志并用成功、翻译降级、
  Source 失败三条链路按 `session_id` 重建事件顺序。

## 2026-08-03 — Stage 1.5 子阶段 D4：结构化日志闭环

### 一、稳定日志契约与脱敏

- 所有 JSON 日志固定包含任务书要求的关联字段：`timestamp/level/process/session_id/
  room_name/participant_identity/track_sid/source_type/provider/provider_request_id/event/
  status/failure_code/cleanup_status/elapsed_ms`；字段暂不可得时明确为 null，不因不同
  模块而省略键。
- `provider_request_id` 可由 Provider event id 统一映射，翻译 Provider 同时映射到通用
  `provider`，保留已有专项统计字段以兼容既有诊断。
- Formatter 对 message、exception 和嵌套 Provider payload 递归脱敏：HTTP/HTTPS/WS/WSS
  URL 查询串变为 `?[redacted]`，API key、Authorization、Token、Secret、Password 与
  Bearer 凭据不进入日志；未记录 HLS fetch URL 或内部控制 Token。
- 日志耗时使用独立单调时钟，不消费翻译节流所注入的业务时钟，不改变 Provider、
  Reconciler 或 SQLite 事务行为。

### 二、生命周期事件覆盖

- API：`session_created`、`room_ready`。
- HLS/FFmpeg/LiveKit publisher：`source_started`、`track_published`、`ffmpeg_started`、
  `source_failed`、`source_stopped`；主 Source 失败同步记录 `session_failed`，清理结果
  独立写入 `cleanup_status`。
- Worker/ASR：`worker_subscribed`、`asr_connected`、`first_partial`、`final_persisted`、
  `final_published`、`session_finalizing`、`session_completed/session_failed`、
  `worker_cleanup_completed`。
- Translation：`translation_starting/translation_connected`、翻译 Final 持久化与发布、
  `translation_completed/translation_failed`；翻译失败使用独立 failure code，不生成
  主 `session_failed`。
- 内部 HLS 中止继续使用 `source_abort_requested`，补充 source/status/failure/cleanup
  稳定字段；任务书要求的 16 个必需事件均已在生产发射点逐项定位。

### 三、三条无云验收链路

- 成功链路 `d4-success`：按 session_id 重建为 `session_running → asr_connected →
  first_partial → session_finalizing → final_persisted → final_published →
  session_completed → worker_cleanup_completed`；SQLite 主状态和翻译状态均 completed。
- 收尾时先进入 finalizing，再由 `finish()` 排空已接收的 Provider Final，因此 Final
  持久化/发布可位于 finalizing 之后；日志验收按真实异步链路记录该顺序，没有改写主链。
- 翻译降级链路 `d4-translation-degraded`：`translation_connected → translation_failed →
  session_completed → worker_cleanup_completed`；SQLite 为 Session completed、Translation
  failed，且日志中不存在主 `session_failed`。
- Source 失败链路 `d4-source-failed`：`source_started → source_failed → session_failed →
  source_stopped`；failure_code 为 `hls_stream_error`，最终 cleanup_status 为 completed，
  证明业务失败与资源清理结果可分别重建。
- 三条链路全部使用本地 Fake Provider/确定性 HLS 故障源，不调用百炼或 DeepSeek。

### 四、测试与结论

- D4 相关聚焦回归：51 passed；包含三场景的单一集成验收文件，没有增加重复组合矩阵。
- 后端最终全量：256 passed、1 skipped、1 个既有 Starlette/httpx 第三方弃用警告，
  22.46 秒；无失败。
- 子阶段 D1–D4 已闭合。下一批按既定顺序进入子阶段 E：权威 Final 导出、运行中导出
  标记、多语言编码与 RemoteMediaUrlValidator 安全收口。

## 2026-08-03 — Stage 1.5 子阶段 E：导出、多语言与 URL 安全收口

### 一、权威 Final 导出与时间轴

- 正式源字幕导出只读取 SQLite `segments.status=final`，译文通过独立
  `content=translation` 入口只读取 `translation_segments.status=final`；Draft、
  LiveKit 消息和内存态均不会混入正式文件。
- JSON、SRT、VTT、Markdown 均支持源文/译文独立文件名。非终态 Session 的格式适当
  元数据和 HTTP 头明确携带 `session_status`、`exported_at`、`partial_export=true`；
  终态输出保持确定性。
- 时间轴统一按开始时间排序；负值归零，`end < start` 收敛到 start，缺失结束时间使用
  下一条开始时间或有界回退。SRT 使用逗号毫秒，VTT 使用点号毫秒，空 Session 有明确
  空结果而不是伪造字幕。
- Stage 5 本地验证器原先在仍为 `created` 的 Session 上比较两次导出哈希，与新增的运行中
  `exported_at` 契约冲突；验证器现先把已落 Final 的样例标为 completed，再比较正式终态
  导出，未放宽源 Final 不可变断言。

### 二、多语言 UTF-8 回读

- 持久化 Final 同时包含中文、English、日本語、العربية、emoji、换行、标点和需转义字符；
  四种格式均以 UTF-8 解码并重新读取成功。
- JSON/Markdown/SRT 保留原始 Unicode；VTT 对 cue 文本执行 HTML 转义但不损坏字符；
  RTL 数据完整，不宣称本阶段已完成前端 RTL 布局优化。

### 三、RemoteMediaUrlValidator

- 独立验证器返回规范化 URL、显示 URL、最终 Host/IP、allow/rejection reason 与重定向
  次数；只接受 HTTP(S)，拒绝 credentials、localhost、loopback、RFC1918、link-local、
  元数据地址及 DNS 解析到的非公网地址。
- DNS 解析受连接超时约束；HTTP 探测使用独立连接/读取超时、禁用环境代理自动继承，
  每一跳重定向均重新执行协议、Host、端口和 IP 校验，并限制最大跳数。
- 新增 `REMOTE_MEDIA_CONNECT_TIMEOUT_SECONDS`、`REMOTE_MEDIA_READ_TIMEOUT_SECONDS`、
  `REMOTE_MEDIA_MAX_REDIRECTS`、`MAX_REMOTE_STREAM_DURATION_SECONDS`。HLS 解码器到达最大
  时长后正常收割其精确 FFmpeg 子进程，不把计划停止误报为断流。
- FFmpeg 继续仅通过 `create_subprocess_exec` 的 argv 调用；URL 即使含 `$()`、分号或管道
  字符也只占 `-i` 后一个参数，API 不接受额外 FFmpeg flags。数据库和结构化日志只记录
  无查询串显示 URL及最终 Host/IP，不记录 token/query。

### 四、E4 验收证据

- 必要 URL 矩阵覆盖正常 HTTPS、file/ftp/data、localhost、127/8、10/8、192.168/16、
  `169.254.169.254`、DNS 私网、重定向私网、重定向上限和 shell 字符；最大运行时长使用
  确定性时钟验证，无失败重试矩阵。
- 既有 CCTV 示例 `piccpndks.v.kcdnvip.com` 在本次验收时 DNS 仍可解析，但媒体探测失败，
  因而不再作为当前成功样例。改用 Apple 官方 HLS 示例
  `devstreaming-cdn.apple.com/.../bipbop_adv_example_hevc/master.m3u8`：验证器返回 allowed、
  8 个公网 IP、0 次重定向。
- 为避免展开大型 HEVC 主列表，实际媒体验收选择其官方 `a1/prog_index.m3u8` 音频
  rendition；ffprobe 识别为 AAC、48 kHz、双声道。验收结束后无 ffmpeg/ffprobe 残留。
- 导出/URL/HLS 聚焦回归：19 passed；后端最终全量：261 passed、1 skipped、1 个既有
  Starlette/httpx 第三方弃用警告，17.89 秒；无失败。
- 子阶段 E1–E4 已闭合；下一批按计划进入子阶段 F：配置与持久目录、健康检查、部署文档
  和 Stage 1 冻结验收。

## 2026-08-03 — Stage 1.5 子阶段 F1–F3：部署预留与冻结文档

### 一、统一配置与持久目录

- `.env.example` 已覆盖任务书要求的部署、LiveKit、SQLite、FFmpeg、内部控制、百炼、
  DeepSeek、远程流上限和日志配置；Frontend 示例只保留
  `NEXT_PUBLIC_API_BASE_URL`，不包含任何服务端 Secret。
- 新增 `APP_ENV/PUBLIC_API_BASE_URL/CORS_ORIGINS/FFMPEG_BIN` 校验；production 拒绝
  localhost 的公开 API、LiveKit 和 CORS 开发默认值。FastAPI CORS、HLS FFmpeg 和
  Replay CLI 均使用同一配置合同。
- `DATA_DIR` 固定包含 `live_caption.db`、`exports/`、`logs/`、`reports/` 和
  `worker-health/`；Windows 默认仍为 `backend/data`，容器预留 `/data`。开发启动器的
  新日志改写到 `DATA_DIR/logs/dev-<timestamp>`；仓库根 `.runtime` 仅保留历史验收记录，
  不再产生新运行目录。
- 删除已被 `REMOTE_MEDIA_READ_TIMEOUT_SECONDS` 取代且无调用方的旧
  `HLS_READ_TIMEOUT_SECONDS`；保留 `DATA_DIR/exports` 作为未来服务器端异步导出的明确
  预留，当前 HTTP 下载仍从 SQLite 即时生成。

### 二、健康检查

- 保留 `GET /health` 兼容入口，新增 `GET /health/live`；两者只证明 API 可响应。
- `GET /health/ready` 检查已构造 Settings、LiveKit 必需配置、SQLite SELECT 与
  `BEGIN IMMEDIATE` 写事务能力，并在每个持久目录执行创建、fsync、删除临时探针；
  任一失败返回 503 和稳定错误类型，不暴露底层路径或错误详情，也不调用百炼/DeepSeek。
- Worker 主进程通过 LiveKit Agents 的 `worker_started/worker_registered` 事件记录 alive
  与 connected；Job 子进程以独立哈希 marker 记录 active job 和最后事件时间。受内部
  Token 保护的 `GET /internal/worker/health` 跨进程返回任务书要求的四个字段。
- Worker 启停会清除自身上次遗留 Job marker；Job start/finish 和主进程状态均使用同目录
  原子替换，不把音频、字幕、URL 或 Provider payload 写入健康桥接文件。

### 三、文档与实际边界

- `docs/architecture.md` 已按当前产品链重写：三类输入、实时双语、FastAPI 所有的 HLS
  Publisher、五类部署组件、Final 数据权威、资源所有权、健康接口和安全边界均与代码一致。
- README 已补齐当前进程、本地启动、数据目录、LiveKit/百炼/DeepSeek 配置、Fake 与
  transport-only、长测和故障注入、源文/译文导出、已知限制及 Stage 2 部署入口。
- 明确 SQLite 只支持 FastAPI 单实例和单个受控 Worker；横向扩容前必须迁移 PostgreSQL
  并替换本地 Worker 健康/Job 协调。HLS Publisher 当前不拆服务。
- F1/F2 初始聚焦验证分别为 47 passed + 1 skipped、14 passed；F3 后的扩大验证和最终
  全量结果留到本批检查点收尾。F4 尚未执行，因此此处不提前标记 Stage 1 Frozen。

## 2026-08-03 — Stage 1.5 子阶段 F4：最终冻结验收

### 一、最终结果

- 20 项冻结标准全部通过，Stage 1 标记为 Frozen。逐项证据和失败命令记录在
  `backend/data/reports/stage1_5_freeze_20260803-180847.md`。
- 后端最终全量为 267 passed、1 skipped；前端 typecheck 和 Next.js 生产构建通过；
  Alembic 为 `20260801_0011 (head)`，Stage 2–6 本地验证器全部通过，Stage 0 在真实
  LiveKit 上通过。
- 正式 SQLite 为绝对路径、WAL、`busy_timeout=5000 ms`、`quick_check=ok`。服务重启后
  恢复既有源/译文 Final 各 4 条，源文和译文的四种正式导出共 8 项均成功。

### 二、真实输入与云端有限冒烟

- 麦克风数据面以 `source_type=microphone` 经真实 LiveKit Track 完成 50 帧/1 秒；本地
  媒体同样完成 50 帧/1 秒。物理麦克风权限弹窗仍保留为换机后的人工演示步骤。
- Apple 官方公开 HLS 音频经真实生产 Worker、百炼 ASR 和 LiveTranslate 连续运行约
  3 分钟，处理 9,265 帧；最终 Session/Translation completed、Source stopped、Cleanup
  completed。该示例音轨无可识别对白，本次 Final 为 0，不将其冒充字幕正确性样例；
  有声 Final 与刷新恢复证据沿用既有真实公开直播验收。

### 三、F4 暴露并修复的问题

- Worker 内部 loopback HTTP 客户端曾继承系统代理，使快照/abort 超时并间接触发百炼
  无音频超时；现固定 `trust_env=False`。修复后内部请求亚秒返回，公开 HLS 跑满 3 分钟。
- 主 ASR 失败时翻译状态可能残留 running；现先持久化 Translation failed 并关闭翻译
  会话，再请求 API 清理 Source。
- 共享 TestClient 夹具曾读取在线 Worker 的正式健康文件；现把测试 `DATA_DIR` 隔离到
  pytest 临时目录，全量测试可与开发服务并行执行。

### 四、资源终态与下一入口

- 验收结束时 LiveKit 活跃 Room 0、Worker active job 0、FFmpeg 0；待命 Worker 主进程
  保持连接，供用户继续查看本地工作台。
- Stage 1.5 到此闭合。下一入口为任务书 Stage 2 部署，不再在冻结分支扩展新业务能力。

## 2026-08-04 — Stage 2A：权威 TranscriptPackage

### 一、范围与冻结边界

- 本批只完成任务书第一部分 Stage 2A：Package 领域模型、构建器、校验器、持久化、确定性 ZIP、
  API 和 Session 后处理页。未实现 Revision、Processor Job、Artifact、DeepSeek 或通用 Processor。
- `PackageBuilder` 是 Stage 2A 唯一读取 Stage 1 SQLite Final 的模块；它只消费
  `segments.status=final` 与 `translation_segments.status=final`，不读取 Partial、LiveKit 消息或前端缓存。
- Stage 1 实时主链、Final 表结构与既有导出接口未改写。Package 生成采用独立事务，冻结后正文、文档和
  manifest 不更新；重建只新增版本，并把旧版本生命周期标记为 superseded。

### 二、权威数据、哈希与导出

- 每份 Package 固定包含源文逐条稿、按目标语言分组的直播译文、统一时间轴、证据索引、Session/
  Provider/Metrics 快照、文档哈希和 Package manifest；条目 ID 由 Session 与逻辑 Segment 标识稳定派生。
- canonical JSON 固定 UTF-8、键排序和紧凑分隔符；Package 哈希排除数据库自增 ID、存储路径和 ZIP
  时间戳。校验器检查文档哈希、Package 哈希、引用闭合、时间轴/证据一致性、必需文档与敏感查询参数。
- ZIP 固定路径顺序、时间戳、权限和压缩参数，包含 manifest、canonical documents、indexes、metadata、
  源文 SRT/VTT/Markdown、译文 SRT 及 `checksums.sha256`；相同 Package 重复导出字节一致。

### 三、数据库、API 与前端

- Alembic `20260804_0013` 新增 `result_packages` 与 `package_documents`。全新 SQLite 从空库升级成功；
  现有开发库从 `20260803_0012` 升级成功，最终 `quick_check=ok`。
- 已实现创建、列表、详情、manifest、即时 ZIP 下载和显式 validate 六组 REST 入口；Package 构建、文档写入
  与冻结在同一事务中完成，失败不会留下对外可见的半成品。
- 新增 `/sessions/[sessionId]` 后处理页，展示当前 Final、Package 版本/哈希/文档，并提供创建、校验、下载；
  Room 工作台只保留“后处理”链接，没有把后处理状态混入实时字幕状态模型。

### 四、验收结果与已知执行现象

- 后端全量：271 passed、1 skipped；前端 TypeScript 与 Next.js 生产构建通过，动态路由
  `/sessions/[sessionId]` 成功生成。
- 全新数据库：revision `20260804_0013`，两张新增表存在，`quick_check=ok`。现有数据库验收时含
  17 个 Session、43 条源文 Final、5 条译文 Final；生成 1 份冻结 Package、6 份 canonical document，
  Stage 1 Final 数量未改变。
- 本地验证器在现有数据上生成 Package `62afa8cc-3573-4e0d-8ad0-650974df1a08` v1，内容哈希
  `ebfe8627299596607bab23d46ddc5c918902b6c5b3f85bce51493358006d3ace`，ZIP 4,582 bytes，
  全文件 checksums 校验通过；验证过程未调用百炼或 DeepSeek。
- 首轮新增测试因夹具未在写入子表前 flush Session 出现 4 个外键失败，修正夹具后全部通过；直接执行
  `pnpm` 曾被 Windows PowerShell 脚本策略拦截，改用 `pnpm.cmd` 后 typecheck/build 均通过。现有库首次
  迁移因受控沙箱只读失败，授权写入后迁移成功。剩余警告仅为第三方 TestClient 弃用提示和 pytest
  缓存目录权限提示，不影响测试结果或产品数据。

### 五、阶段结论

- Stage 2A 已闭合并停在任务书规定边界。权威 Package 已可从 Stage 1 Final 构建、冻结、验证和下载；
  下一批如继续，应从 Stage 2B Revision/Processor 基础设施开始，而不是继续扩张 PackageBuilder。

## 2026-08-05 — Stage 2B：非破坏式字幕校对与 Package v2

### 一、不可变 Revision 与证据约束

- Alembic `20260804_0014` 新增 `transcript_revisions`，按 Session/源语言递增版本，并用
  `parent_revision_id` 保留来源链。SQLite 部分唯一索引保证每个 Session/源语言最多一个
  `approved`；批准新版本时旧 approved 只变为 superseded，内容和哈希不修改。
- 首版 Revision 从 Frozen Package 的有效源文档复制；后续保存、恢复旧版本均创建完整新快照。
  条目必须使用 UUID item ID、非空文字、非负且有序的时间，并保留 raw source 的全部
  `source_segment_ids`；合并继承来源 ID 联集，拆分允许共享来源 ID，外来或丢失证据会被拒绝。
- Revision 可确定性导出 SRT、VTT 与 Markdown。Stage 1 `segments.display_text`、Package 文档和
  content hash 均不被校对流程覆盖。

### 二、Package vNext 与 API

- `PackageBuilder.build_from_revision` 只读取 approved Revision 与其基础 Package，不查询
  Stage 1 Final。新 Package 复制 `source_raw`、实时译文和脱敏快照，新增 `source_approved`，
  manifest 有效源改指 approved 文档，并从校对条目重建 timeline/evidence。
- PackageValidator 同时校验 `source_revision_id`、effective source 类型、raw/approved 来源 ID
  守恒及原有文档/Package 哈希；ZIP 的 `rendered/source.*` 使用有效源文档，同时保留两个 canonical
  source JSON。Package v1 可继续读取和验证，正文与文档哈希不变。
- 新增创建、列表、详情、保存版本、批准、构建 Package、Revision 导出七类 HTTP 入口；未批准
  Revision 不能构建 Package，superseded Revision 必须先恢复为新版本而不能直接重新批准。

### 三、后处理工作台

- `/sessions/[sessionId]` 升级为 Stage 2B 工作台：继续显示原始 Final、实时译文和 Package 历史，
  新增 Revision 历史、文字/换行与起止时间编辑、相邻项合并、单项拆分、修改摘要、保存新版本、
  批准、恢复旧版本为新版本和从 approved Revision 构建 Package vNext。
- 差异面板以父版本为基准显示新增/删除/修改计数与双栏文本；Package 详情明确显示有效源文档和
  `source_revision_id`。复杂音频波形和多人协作未进入本阶段。

### 四、迁移、测试与真实纵向验收

- 聚焦后端回归 30 passed；最终后端全量 275 passed、1 skipped。前端 `pnpm.cmd typecheck` 与
  Next.js 生产构建通过，动态 `/sessions/[sessionId]` 路由成功生成。警告仍只有既有第三方
  TestClient 弃用提示和 pytest 缓存目录权限提示。
- 全新 SQLite 从空库升级到 `20260804_0014`，Revision 表、approved 唯一索引存在且
  `quick_check=ok`；现有库从 `20260804_0013` 直接升级成功。
- 现有数据纵向链：Session `72c627fb-3d60-4b16-b3c0-9e809be9fcbb` 的 Package
  `3d40818f-c1cd-4ae5-8729-213f206b5a8b` → Revision v1/v2 → approved Revision
  `33ff6ab2-7947-4d47-b228-ed101edb19e2` → Package v2
  `3eb4a9e0-d174-4d49-bc8b-3a6c02d9ea87`。新 Package 哈希为
  `86ebb12af01a679477568a44a9e76578c45f23f4bbfd141cb0696ee9104e0e04`，ZIP 5,512 bytes，
  checksums 全部通过；基础 Package 哈希/文档哈希和该 Session 的 Stage 1 Final 均未改变。
- 验收后现有库为 17 个 Session、43 条源文 Final、5 条译文 Final、3 个 Package、19 个 Package
  Document、2 个 Revision（1 saved、1 approved），`quick_check=ok`；未调用百炼或 DeepSeek。
- 数据库并行检查中的首次 `alembic current` 被受控沙箱阻止打开现有库；拆分命令并授权后确认原版本
  为 0013，升级到 0014 成功。API 文档首次补丁因上下文行不完全匹配未应用，改用稳定标题锚点后成功；
  没有测试或产品合同失败。

### 五、阶段结论与下一入口

- Stage 2B 已闭合。人工校对现在是 Package 之上的不可变、可追溯版本链，approved Revision 可生成
  新 Frozen Package，同时保留 Stage 1 证据和旧 Package。
- 下一阶段为 Stage 2C：只消费 Frozen Package 的 Processor、单进程后台 Job、Derived Artifact，
  并迁移既有 DeepSeek 整理台本；不得让 Processor 直接读取 SegmentRepository。

## 2026-08-09 — Stage 2C：Package Processor、后台 Job 与台本迁移

### 一、Package-only 处理边界

- 新增 `PackageReader`、`ArtifactWorkflow`/`PackageProcessor`、`ProcessorRegistry`、
  `EvidenceValidator`、`ArtifactService` 与 `ProcessingJobRunner`。Processor 只接收已校验的
  frozen/superseded Package、options 和可选 target Artifact，不注入或读取 Stage 1 Segment/
  Translation Repository。
- 首个注册 Workflow 为 `CleanScriptWorkflow`：读取 Package effective source，以
  `source_item_ids` 分块调用通用 Structured Provider，保留重试与严格 JSON 校验；Segment ID 和
  起止时间完全由服务端依据 Package evidence 映射。每个源 item 必须且只能被引用一次。
- DeepSeek 适配器增加通用 structured completion 能力，Provider 仅负责模型请求，不查询 Package、
  不校验证据、不保存数据库。常规测试与本地 verifier 均使用 Fake Provider，没有调用云端。

### 二、Job、Artifact、迁移与兼容

- Alembic `20260808_0015` 新增 `processing_jobs`、`derived_artifacts`；Job 保存状态、进度、脱敏
  错误与结果 Artifact，Artifact 只增版本并固定输入 Package version/content hash、Workflow、
  options、正文和证据。
- FastAPI lifespan 启停 concurrency=1 的 asyncio Queue。提交立即返回 queued Job；支持完成、失败和
  有限取消。API 启动把遗留 queued/running 统一置为 failed，错误码
  `api_process_restarted`；Workflow 或证据失败与取消均不保存半成品 Artifact。
- Alembic `20260808_0016` 把历史 `processed_scripts` 迁为 `clean_script` Artifact；可匹配时复用既有
  Package，否则生成可验证的 legacy Package，并在 Artifact 内容保留旧来源快照和 Markdown。
  旧表保留为迁移证据，但不再写入。
- 旧 `/api/sessions/{id}/scripts` 与 `/api/scripts/{id}` 路由标记 deprecated，内部统一走
  Package/Job/Artifact 或从 Artifact 生成兼容响应，不再维持第二套业务服务。

### 三、API 与后处理工作台

- 新增 Package Job 创建/列表、Job 详情/取消、Package Artifact 列表和 Artifact 详情 API。未注册
  Workflow、非 Frozen Package、外部 target Artifact 和已终态取消都有明确 4xx 合同。
- `/sessions/[sessionId]` 可基于所选 Package 创建 `clean_script` Job、轮询
  queued/running/completed/failed/cancelled、显示进度与稳定错误、取消活动 Job，并查看当前及跨
  Package 的 Artifact 历史。详情显示输入 Package 版本/哈希、结构化章节和双层 evidence，并可将
  已加载的完整 Artifact 下载为 JSON；服务端多格式 Artifact Exporter 按任务书留待 Stage 2D。
- 历史迁移台本与新 Artifact 使用同一列表；前端旧 ProcessedScript 客户端类型和未使用调用已移除。

### 四、验证结果与执行现象

- Stage 2C 聚焦回归 8 passed；最终后端全量 283 passed、1 skipped。警告仅为既有 Starlette
  TestClient 弃用提示和 pytest 缓存目录权限，不涉及业务失败。
- `tsc --noEmit --incremental false` 通过；Next.js 16.2.10 生产构建通过，`/`、
  `/_not-found` 与动态 `/sessions/[sessionId]` 路由均成功生成。
- 现有开发库确认位于 Alembic `20260808_0016 (head)`。凭据无关纵向链使用 Package
  `3eb4a9e0-d174-4d49-bc8b-3a6c02d9ea87` v2，创建 Job
  `4617752c-bb1b-4531-b642-1889afcd4637` 并完成 Artifact
  `451c063c-553b-4b27-ab94-5189369d4f3f` v1；证据校验通过，Package hash
  `86ebb12af01a679477568a44a9e76578c45f23f4bbfd141cb0696ee9104e0e04` 与 Stage 1 Final
  均未变化，Provider 明确为 fake。
- 首次在受控沙箱读取现有库返回 `unable to open database file`；授权访问后成功。两组长命令的
  执行包装层在子进程已输出最终结果后未及时回传，手动结束包装并取得了所有成功退出结果；未把该
  工具层现象记作产品通过项。项目环境没有安装 Ruff，本批以 Python compile、pytest、独立 tsc 和
  Next production build 完成静态与运行验证。补完 JSON 下载后第一次 build 被过早终止，紧接着的
  重试因旧 build 尚在收尾而返回 “Another next build process is already running”；待该进程自然退出后
  重新完整执行成功；最终代码再次生产构建，12.4 秒正常结束。

### 五、阶段结论与下一入口

- Stage 2C 已闭合：Frozen Package 可提交后台 `clean_script` Job，成功结果持久化为证据闭合的
  Derived Artifact；旧台本历史已迁移且兼容路由不再写旧模型。
- 下一阶段为 Stage 2D：在现有 Registry/Job/Artifact 框架内实现 `refined_translation` 并完善
  `clean_script`，不得回读 Stage 1 实时表或复制另一套 Provider 与任务系统。

## 2026-08-10 — Stage 2D：清稿、精译、确定性导出与对照工作台

### 一、Workflow 与证据闭合

- `CleanScriptWorkflow` 升级为 2.0，明确标点、片段合并、无意义重复清理以及事实、名称、数字、
  时间、产品名和不确定性保留规则；`RefinedTranslationWorkflow` 1.0 增加目标语言、术语表、风格
  和上下文窗口。两者复用同一分块、重试、严格 JSON/Pydantic 与 EvidenceValidator 边界。
- 精译只以 Package effective source 为事实权威；同语言 live translation 可作为非权威措辞参考，
  缺失时不阻塞。模型只返回 `source_item_ids` 与文本，Segment ID、时间和源文对照由服务器回填。
- 精译 Artifact 按目标语言隔离版本，规范化 options 完整持久化；失败、截断或证据不闭合均不落
  半成品。

### 二、导出、API 与前端

- 新增统一 `ArtifactExporter` 与 `/api/artifacts/{id}/export`。清稿提供 JSON/Markdown，精译提供
  JSON/Markdown/SRT/VTT；重复导出字节稳定，时间与 Segment ID 始终取 evidence，并拒绝语言或
  evidence 身份不一致的内容。
- `/sessions/[sessionId]` 增加精译参数、Job 创建、按语言版本历史、服务端导出，以及源文 / Package
  实时译文 / 最终精译三栏对照。实时译文缺失时明确降级，不影响精译生成。

### 三、验证结果

- Stage 2D 聚焦回归 11 passed，覆盖双 Workflow、实时译文参考、严格失败不落库、目标语言版本
  隔离、导出器和 API；警告仅为既有 Starlette TestClient 弃用提示。
- 修复旧 `/scripts` 兼容路由提交过期 options 所导致的 3 项全量回归后，最终后端全量为
  294 passed、1 skipped；独立 TypeScript 检查与 Next.js 16.2.10 生产构建通过，`/`、
  `/_not-found`、`/sessions/[sessionId]` 路由均成功生成。
- 开发库处于 Alembic `20260808_0016 (head)`。凭据无关纵向验证使用 Package
  `3eb4a9e0-d174-4d49-bc8b-3a6c02d9ea87` v2：清稿 Job
  `c483c027-4e5c-455c-a6ed-39851a1e3018` 完成并生成 Artifact
  `6a124755-362c-4164-b2b0-08cb0ebb743c` v2；`en-US` 精译 Job
  `58bef4b1-0bcf-4705-a9b1-33534d593ad7` 完成并生成 Artifact
  `e6818508-1d72-4a72-9c2e-483f85fb5c56` v1。
- 两次 Provider 请求均为 fake；开发 Package 没有匹配的 `en-US` live translation，精译按无参考
  路径完成。清稿 JSON/Markdown 与精译 JSON/Markdown/SRT/VTT 均成功并通过重复字节及 evidence
  时间核对；Package hash 与 Stage 1 Final 未变化。首次受控沙箱访问开发库仍因权限返回
  `unable to open database file`，授权后成功，该现象不计为产品失败。

### 四、阶段结论与下一入口

- Stage 2D 已闭合：Package v2 可生成 `clean_script` vN 与按语言隔离的
  `refined_translation:<language>` vN，并可在工作台对照与按合同导出。
- 下一阶段为 Stage 2E：在同一 Processor/Job/Artifact/evidence 框架实现 `summary`、
  `chapter_outline` 与 `timeline_fact_review`。

## 2026-08-10 — Stage 2E：摘要、章节与 Package 内事实复核

### 一、事实型 Workflow 与证据边界

- 新增共享 `FactualEvidenceBuilder`：模型只提交 `evidence_item_ids`，服务器按 Package effective
  source 顺序规范引用并派生 time ranges、Segment ID、聚合起止时间和 evidence excerpts。
- `SummaryWorkflow` 生成 brief/key points/warnings；非法或重复 item ID 被拒绝，长 Package 分块后
  brief、key points 与 warnings 按输入顺序稳定合并。
- `ChapterOutlineWorkflow` 生成 title/summary/evidence；服务器计算章节时间并执行全局起始时间单调
  校验，允许相邻或重叠但拒绝倒序，失败不保存 Artifact。
- `TimelineFactReviewWorkflow` 接受同 Package 的 summary、clean_script、refined_translation 或
  chapter_outline，拆分 Claims 后输出五种支持状态。supported 必须有证据；unsupported/ambiguous
  可为空；contradicted 必须有冲突证据或明确说明。目标 ID/版本写入 options，关系写入
  `parent_artifact_id`，目标 Artifact 前后不变。该流程只做 Package 内忠实度复核，没有 Web 搜索。

### 二、注册、API 与工作台

- 默认 Processor Registry 现注册 Stage 2C–2E 五个 Workflow，共用同一 Structured Provider、分块/
  重试边界、单进程 Job Runner、ArtifactService 和仓储；没有新增迁移或第二套任务系统。
- 前端 Job 客户端支持独立 `target_artifact_id`。`/sessions/[sessionId]` 可生成摘要和章节、选择目标
  Artifact 发起复核，并显示摘要要点、章节时间轴、支持状态、时间范围和证据文本；证据链接跳转到
  当前 Frozen Package 的有效源条目。Stage 2E 类型暂不展示尚未支持的导出链接。

### 三、验证结果

- 统一凭据无关验证器扩展为五段链：clean_script、refined_translation、summary、chapter_outline、
  timeline_fact_review(summary)。隔离聚焦回归 15 passed；最终后端全量 308 passed、1 skipped，
  唯一警告为既有 Starlette TestClient 弃用提示。独立 TypeScript 检查和 Next.js 16.2.10 生产构建
  通过，`/`、`/_not-found`、`/sessions/[sessionId]` 均成功生成。
- 开发库仍为 Alembic `20260808_0016 (head)`。Package
  `3eb4a9e0-d174-4d49-bc8b-3a6c02d9ea87` v2 上的 Fake 纵向结果：summary Job
  `f1bd281f-631a-49f5-91f1-81586bdf50ab` → Artifact
  `bf2d1880-f7c2-4583-9da1-c66a7c8fe6b0` v1；chapter_outline Job
  `2bdb9f23-a3bd-4453-bf2e-c689f4f533da` → Artifact
  `ea6843ec-6538-41a9-a55d-dfc988b50de8` v1；timeline_fact_review Job
  `c45b4df5-f830-4c7b-9e27-77cce30981b7` → Artifact
  `43a27f76-b5d1-440c-80c0-857d3c290952` v1，并正确绑定上述 summary。
- 同次验证还追加 clean_script v3 与 `en-US` refined_translation v2。五次 Provider 请求均为 fake，
  没有调用百炼、DeepSeek 或 Web；Package hash
  `86ebb12af01a679477568a44a9e76578c45f23f4bbfd141cb0696ee9104e0e04` 与 Stage 1 Final 未变化。

### 四、阶段结论与下一入口

- Stage 2E 已闭合：任务书要求的三个事实型 Workflow、状态规则、目标绑定、服务器证据派生、前端
  面板及可运行 Package v2 → summary v1 / chapter_outline v1 / fact review(summary v1) 链均成立。
- 下一阶段为 Stage 2F：实现 Artifact 人工派生版本、审核/批准、history/approved API、旧 Package
  提示及工作台收口；不得原地覆盖模型 Artifact，暂不生成最终 Delivery Bundle。

## 2026-08-10 — Stage 2F：Artifact 人工版本、审批与工作台收口

### 一、稳定 identity 与不可变人工版本

- `derived_artifacts` 新增稳定 `identity_key`：清稿/摘要/章节按类型，精译附目标语言，事实复核附目标
  Artifact ID。事实复核的目标 identity 不再与表达版本父链的 `parent_artifact_id` 混用。
- 人工保存只追加 `created_by=human`、`status=reviewed`、`workflow_version=human-edit-v1` 的完整快照，
  provider/model 置空并记录父版本；模型版本和上一人工版本正文均不被覆盖。
- 服务端允许修改正文、标题、摘要、章节说明、复核状态/说明、notes 和 warnings，同时锁定 Package、
  evidence/source item、Segment、时间、claim/path 与事实复核目标，防止人工界面破坏可追溯性。
- 批准 generated/reviewed 时，同 Package/identity 的旧 approved 自动进入 superseded；superseded 不能
  直接重新批准。数据库部分唯一索引 `uq_derived_artifacts_current_approved` 提供并发最终兜底。

### 二、API、导出与工作台

- 新增 Artifact versions/approve/history 与 Package approved-artifacts 四个 API，缺失资源返回 404，
  内容/版本/审批冲突返回 409，写请求在异常时回滚。
- `/sessions/[sessionId]` 现显示 Artifact identity、模型/人工来源、父版本和 identity 版本链；可结构化
  编辑并追加版本、批准、按原 options 重新生成及导出单项成果。旧 Package 成果保留可读，并提示
  “该成果基于 Package vN；当前最新为 vM”，不删除、不自动重算。
- 清稿继续提供 JSON/Markdown，精译提供 JSON/Markdown/SRT/VTT；摘要、章节和事实复核新增完整 JSON
  单项导出。本阶段没有 Delivery Bundle、approved Artifact 聚合 ZIP、Package 导入或外部事实核验。
- 修正人工事实复核校验与 Workflow 五状态合同的一致性，并允许原合同中的 null explanation。

### 三、迁移与无云验收

- Alembic `20260810_0017` 从 `0016` 回填所有历史 identity；建唯一索引前会把同 identity 的历史重复
  approved 收敛为“最高版本保留 approved、其余 superseded”。迁移兼容 SQLite batch alter。
- 开发库已从 `20260808_0016` 原位升级到 `20260810_0017 (head)`：`PRAGMA quick_check=ok`，现有 9 个
  Artifact 的 identity 全部非空，重复 approved 槽为 0，部分唯一索引存在。首次沙箱读取仍出现历史
  已知的 `unable to open database file`，授权访问后 current、升级和只读校验均正常，不是产品故障。
- 新增 `scripts/verify_stage2_review.py`，使用临时 SQLite 和真实 FastAPI 路由完成一条确定性链：Package
  v1/v2、模型 Artifact v1、人工 v2、两次批准、旧 approved supersede、history/approved 查询、旧
  Package 读取、跨 Package 隔离和 JSON 导出；输出 `status=ok`、版本 `[2,1]`、父链/唯一性/隔离/
  Package hash/Stage 1 Final 均为 true，`cloud_calls=0`。临时库自动清理，未污染开发数据。

### 四、最终验证与边界

- 后端最终全量为 315 passed、1 skipped；skip 为既有需外部条件的用例。警告仅为既有 Starlette
  TestClient 弃用提示和 pytest cache 写权限，不涉及业务失败。
- `tsc --noEmit --incremental false` 通过；Next.js 16.2.10 生产构建通过，`/`、`/_not-found` 和动态
  `/sessions/[sessionId]` 路由均成功生成。常规回归和本地验收未调用百炼、DeepSeek 或 Web。
- Stage 2A–2F 已闭合。最终 Delivery Artifact Bundle、聚合下载、Package 导入、外部事实核验、多人
  协同与分布式任务队列保持为后续独立范围，不应从当前 Artifact approval 推断为已实现。

## 2026-08-11 — Stage 2 审查收口

- 修正 PackageBuilder 的空证据边界：任何没有 Final 字幕的 Session（包括 `completed`）均返回
  `409 Session has no Final captions`，不会创建 `building` 或空 Frozen Package。
- Alembic `20260811_0018` 为 `language IS NULL` 的 Package Document 增加部分唯一索引；升级前检查
  历史冲突，发现冲突时明确失败而不删除数据。开发库升级后 `PRAGMA quick_check=ok`、重复无语言
  document identity 为 0，索引 `uq_package_documents_identity_no_language` 已存在。
- 删除无生产调用方的旧 `ScriptService`、`ProcessedScriptRepository` 及其专项旧链测试；deprecated
  `/scripts` 路由继续通过 Package/Job/Artifact 新链提供兼容，`processed_scripts` 表与 ORM 模型仅保留
  为历史迁移证据，不再提供写入服务。
- 聚焦回归 15 passed，最终后端全量 309 passed、1 skipped；减少的用例来自删除旧并行业务链测试。
  TypeScript 类型检查和 Next.js 生产构建均通过，未调用百炼、DeepSeek 或 Web。

## 2026-08-26 — 自适应分句与歌词式悬浮字幕

### 一、屏幕字幕阅读节奏

- 新增纯函数字幕 cue composer，综合可用像素宽度、两行上限、强/弱标点、
  Unicode 词素或单词边界、最短填充比例和语言阅读速率生成显示 cue；中文、
  英文、混合文本、无标点长句、超长 token 和窄窗口均有确定性覆盖。
- Draft/Final 显示状态机以 125 ms 合并高频 Draft；同 Segment Final 原位替换，
  过渡期间只保留一个待显示候选。显示层拆句不会改变 Final 存储、时间证据或导出。
- Room 主字幕栏和悬浮字幕栏都按各自实时宽度重新排版，复用同一 cue composer。

### 二、歌词式悬浮字幕

- Room 字幕标题区新增“悬浮字幕”开关，基于 Document Picture-in-Picture 请求
  720×180 的浏览器始终置顶窗口，并把 React 字幕视图 Portal 到该窗口；重复
  开启复用既有窗口，页面开关与窗口关闭事件双向同步。
- 默认模式为译文，可切换原文/双语并调节字号和背景透明度，偏好保存在可用的
  localStorage 中；受限浏览器无存储时继续使用默认值。未启用翻译或翻译失败时
  自动显示原文并给出原因。
- 不支持、未授权、被浏览器设置禁用及其他打开失败分别映射为可操作提示。当前
  方案要求原控制台保持打开，不承诺原生无边框或鼠标穿透。

### 三、验证与边界

- 前端新增 38 个 Vitest 用例，覆盖分句、Draft/Final 稳定显示、偏好/回退和
  Document Picture-in-Picture 支持检测、复用及错误映射；TypeScript 独立检查
  和 Next.js 生产构建均通过。
- 本地控制台在 `127.0.0.1:3000` 加载成功；验收浏览器为 Codex In-app Browser
  （版本未暴露），输入类型为“未启动”，页面默认语言对为中文 → English。
  已确认悬浮字幕入口以及原文/译文空状态正常；测试复用了已在 8000 端口运行的
  API，没有停止或替换该用户进程。
- 该内置浏览器不向自动化暴露独立的 Document Picture-in-Picture 窗口，环境也
  没有可连接的桌面 Chrome/Edge，因此本轮操作系统级始终置顶、窗口控件交互和
  真实音频下视觉节奏的结果为“未验收”，需在桌面版 Chrome/Edge 手工补验。
  本轮未请求媒体权限、未发送音频，也未调用百炼、DeepSeek 或 Web。

## 2026-08-28 — 通用媒体助手插件框架：实现、修复与 Docker 复验完成

### 一、框架与兼容性结果

- 已完成通用 MediaSession/MediaEvent sidecar、签名本地插件包、Host-only Capability Broker、
  声明式 UI、持久化权限/Grant/游标/审计、Docker supervisor、管理 API 与前端插件管理/渲染面。
  本阶段没有新增会议、直播或视频等场景专用助手；后续助手应作为该框架上的插件实现。
- FakeContainerRuntime 端到端用例覆盖：签名安装和启用、Final 事件投递与 ack、UI 发布、容器崩溃后
  从 durable cursor 恢复、活动 MediaSession 版本固定、候选版本迁移失败回滚、权限撤销后默认拒绝，
  以及同一字幕 Session 仍可供私人会议助手构建上下文。
- 安全复核确认插件代码不进入 API 进程；容器启动参数固定包含 `--network none`、`--read-only`、
  `--cap-drop ALL`、`no-new-privileges` 和资源上限，不含项目、数据库、Docker socket 或其他宿主挂载；
  子进程环境只允许 PATH、SYSTEMROOT、WINDIR。插件不能注册 Host capability，外部写要求短时 scope
  Grant 和幂等键，未知结果只能经 reconciliation 恢复。前端从 `unknown` 解析闭合 UI schema，只用
  React 文本/控件渲染，不使用 raw HTML、iframe、webview、脚本或插件 CSS。

### 二、非 Docker 验证

- Python 语法：`.venv\Scripts\python.exe -m compileall -q app` 退出码 0。
- 后端聚焦回归：插件 E2E、Supervisor 与 Broker 共 `10 passed, 1 warning`；字幕、翻译、会议助手与
  插件 E2E 兼容组共 `17 passed, 1 warning`。
- 后端最终全量：`408 passed, 1 skipped, 2 warnings in 38.64s`。skip 为既有外部条件用例；warning
  为既有 Starlette TestClient 弃用提示及 `.pytest_cache` 写权限提示，不是业务失败。
- 前端：Vitest `7 files / 53 tests passed`；`pnpm typecheck` 退出码 0；Next.js 生产构建成功，生成
  `/`、`/_not-found`、`/plugins`、`/sessions/[sessionId]`。
- 临时空 SQLite 已从首个迁移升级到 `20260828_0025 (head)`；显式临时数据库及 WAL/SHM 均已确认
  位于 `backend/data` 后删除。`20260828_0025` 在计划草案的 `0024` 之后增加 runtime image ref，
  因此以实际 Alembic head 为准。

### 三、Docker 主机阻塞与保护措施

- 两条真实运行时冒烟没有得到通过结果，也没有降级为同进程或普通子进程。Docker Desktop 在 daemon
  可用前崩溃，核心错误为 Windows AF_UNIX listener 无法访问
  `C:\Users\GX\AppData\Local\Docker\run\dockerInference`，即
  `The file cannot be accessed by the system`。在隔离证明缺失前，本阶段不得标记为可发布。
- 排障只触及可恢复的 Docker Desktop 临时状态：配置备份位于
  `C:\Users\GX\AppData\Roaming\Docker\settings-store.codex-backup-20260828-1115.json`；当前配置只把
  `EnableDockerAI` 改为 `false`。旧临时目录保留为
  `C:\Users\GX\AppData\Local\Docker\run.stale-20260828-1118` 和
  `C:\Users\GX\AppData\Local\docker-secrets-engine.stale-20260828-1120`。未重置 Docker，未删除镜像、
  WSL 数据、项目文件或用户配置备份。
- 重新创建全新 run 目录后，同一 AF_UNIX 错误仍立即复现，说明当前 Windows 会话的 socket 子系统
  需要通过系统重启恢复；继续删除 socket 或恢复出厂设置没有证据支持，故已停止破坏性排障。

#### Windows 重启后的复验

- Windows 已重启，并在 `2026-08-28 11:42` 重新启动 Docker Desktop 4.74.0（build 227015）。
  `docker-desktop` WSL distribution 仍保持 stopped，Docker API named pipe 未建立，daemon 没有进入可用状态。
- Docker 官方设置中 Model Runner 的实际键是 `enableInference`，不是先前修改的 Gordon 键
  `EnableDockerAI`。已停止崩溃进程并新增完整设置备份
  `C:\Users\GX\AppData\Roaming\Docker\settings-store.codex-backup-20260828-1148.json`，然后把
  `enableInference`、`enableInferenceGPUVariant`、`enableInferenceTCP` 精确设为 `false`，并把无关的
  `EnableDockerAI` 恢复原值 `true`。
- 修正设置后于 `11:46` 再次启动，backend 仍在 `initializing Inference manager` 阶段创建
  `dockerInference` 时以相同 AF_UNIX 错误崩溃。因此 Windows 重启和官方禁用配置均未解除故障；
  真实隔离冒烟仍未进入容器构建/运行阶段。下一步需要明确授权后原位升级 Docker Desktop，或由用户
  自行修复 Docker 主机；仍不应执行 factory reset 或静默改用较弱沙箱。

### 四、重启后的发布检查点

Docker Desktop 修复并确认 daemon 正常后，在 `backend` 执行：

```powershell
.\.venv\Scripts\python.exe smoke/plugin_sandbox_smoke.py
.\.venv\Scripts\python.exe smoke/plugin_framework_smoke.py
```

第一条必须证明无宿主 Secret、根文件系统只读、无直接网络和资源限制；第二条必须精确报告
`plugin_ready=true`、`cursor_resumed=true`、`network_isolated=true`、`caption_regression=false`。
随后人工检查真实 `docker inspect` 无 Mounts、NetworkMode 为 `none`，再更新本记录并决定是否发布。

### 五、Docker 主机恢复与最终收口

- 用户原位升级 Docker Desktop 至 4.88.1；最终 Docker CLI 与 Engine 均为 `29.7.2`。升级后发现
  `dockerInference` 是遗留在 Docker 临时 run 目录中的失效 Windows AF_UNIX reparse point。停止已崩溃的
  Docker Desktop 后，将经解析确认位于 `C:\Users\GX\AppData\Local\Docker` 内的整个 run 目录保留性
  重命名为 `run.stale-20260828-1612`，再启动 Docker；WSL backend 与 daemon 随后正常就绪。没有执行
  factory reset，也没有删除镜像、volume、WSL 或项目数据；两份设置备份和其他 stale 目录继续保留。
- 真实沙箱冒烟通过，原始结果为
  `{"sandbox_ok":true,"sentinel_blocked":true,"secret_blocked":true,"network_blocked":true,"root_write_blocked":true,"private_state_ok":true,"rpc_ok":true}`。
  该结果证明插件不能读取未挂载宿主哨兵或继承宿主密钥，不能直接联网或写容器根文件系统，同时私有
  state 与 JSON-RPC 仍可工作。容器参数专项测试继续验证无宿主 mounts、`--network none`、只读根、
  capability 全删除及 `no-new-privileges`。
- 真实框架冒烟首次强杀容器后暴露一个 supervisor 清理阻塞：已退出 peer 的 `aclose()` 可能无限等待，
  从而阻止重启。Supervisor 现以既有 shutdown timeout 约束 best-effort peer cleanup；新增悬挂关闭回归
  后专项组为 `7 passed`。再次运行真实框架冒烟得到
  `{"plugin_ready":true,"cursor_resumed":true,"network_isolated":true,"caption_regression":false}`，证明真实
  Docker 插件可启动、NetworkMode 为 `none` 且直连失败、强杀后 generation 增长并按 durable cursor
  续传，同时私人会议助手仍能读取同一字幕证据。
- 最终全量回归又暴露并修复 MediaEvent projector 竞态：后台轮询与 API catch-up 可同时进入由“事件写入
  + offset 推进”组成的多事务投影，SQLite 曾返回 `no such savepoint` 并使媒体会话 API 成为 500。投影
  临界区现在使用工作线程级互斥，协程取消也不会提前释放锁；新增确定性并发回归先复现失败再通过。
  原插件 E2E 单独连续运行 5 次全部通过，投影器、插件 E2E 与 Supervisor 聚焦组为 `12 passed`。
- 最终后端全量为 `410 passed, 1 skipped, 1 warning in 51.51s`；唯一 warning 是既有 Starlette
  TestClient 弃用提示。`compileall` 退出码 0，Alembic 为 `20260828_0025 (head)`。前端 Vitest 为
  `7 files / 53 tests passed`，TypeScript 独立检查退出码 0，Next.js 16.2.10 生产构建成功并生成
  `/`、`/_not-found`、`/plugins`、`/sessions/[sessionId]`。
- 至此本阶段计划的代码、文档、迁移、兼容性、崩溃恢复和真实 Docker 隔离检查均已完成。发布边界仍是
  单 FastAPI 实例、单 SQLite 与受信任且保持更新的本机 Docker；不得外推为多租户、横向扩展或抵御
  被管理员、daemon 或内核控制者攻击的生产级强隔离。

## 2026-08-29 — 课程内容整理插件：完整实现与真实容器验收

### 一、行为与边界闭合

- `com.matinier.course-organizer` 已作为通用 MediaSession 框架上的独立签名 OCI 插件实现；Host 没有
  增加课程场景特判。插件精确请求 `state.get`、`state.put`、`ui.publish`、`model.invoke`、
  `delivery.prepare`、`delivery.query`、`document.publish` 七项权限，不请求 `network.fetch`。
- 播放期间只从 Final 原文/译文形成较散的实时知识笔记和字幕音频坐标，不持续执行最终整理。手动命令
  发布 `interim` v1；`session.completed` 从新的 Frozen Package 自动发布 `complete` v2；失败/取消
  使用 `partial_terminal`。模型不可用时实时链保存 `rule_fallback` 并有界重试，终稿链持久化
  `waiting_retry`，不伪造完成状态或回写字幕。
- 默认文档语言为首个已观测译文语言，无译文时回退源语言；显式语言使用独立 identity/version。
  “源语言”选择已修正为实际观测语言而非字面 `source`，对应回归包含在课程实时测试中。
- 终稿唯一事实边界仍是 `PackageBuilder` 从 Stage 1 Final 生成的 Frozen Package；Markdown/JSON 和
  evidence refs 由 Host 校验并下载。外部标签页坐标不读取 DOM，不等同播放器权威 currentTime，
  不提供 seek、OCR、DOCX/PDF 或外部研究。

### 二、迁移与非 Docker 回归

- 显式全新 `backend/data/course-plugin-migration-verify.db` 已从空库升级到
  `20260828_0026 (head)`；`current` 与 `heads` 一致。解析绝对路径确认仍在 `backend/data` 后，数据库
  及存在时的 `-wal`/`-shm` 仅按精确文件名清理。
- `compileall` 对 backend app/tests 和课程插件退出码 0。后端全量最终结果为
  `514 passed, 1 skipped, 1 warning in 72.01s`；skip 是既有外部条件项，唯一 warning 是既有
  Starlette TestClient 弃用提示。
- 课程 Fake Host E2E 独立顺序运行 5 次全部通过：耗时分别为 3.34、3.22、3.14、3.31、4.15 秒，
  每次均为 `1 passed, 1 warning`。该链覆盖安装/启用、实时笔记、手动/终态文档、语言隔离、证据、
  导出、规则降级、崩溃续跑和会议助手字幕兼容。
- 前端 Vitest 为 `8 files / 57 tests passed`；独立 TypeScript 检查退出码 0；Next.js 16.2.10 生产
  构建成功，路由为 `/`、`/_not-found`、`/plugins`、`/sessions/[sessionId]`。

### 三、真实 Docker 验收证据

- Docker Client 与 Engine 均为 `29.7.2`。三条 smoke 严格顺序执行，均退出 0。隔离基线原始 JSON：
  `{"sandbox_ok":true,"sentinel_blocked":true,"secret_blocked":true,"network_blocked":true,"root_write_blocked":true,"private_state_ok":true,"rpc_ok":true}`。
- 课程插件原始 JSON：
  `{"plugin_ready":true,"realtime_notes":true,"manual_interim":true,"terminal_complete":true,"evidence_closed":true,"exports_match":true,"language_selected":true,"cursor_resumed":true,"network_isolated":true,"secret_blocked":true,"caption_regression":false,"interim_document_id":"fb2aa875-b7f2-4937-8706-6be39471df41","interim_document_hash":"95f74c52470daa5521b18f7a5092285c19ea84c6637f16d19bcf4d9a4d31f2f7","interim_package_hash":"aaeeef2df31dca122ebf4f88c1d923b40d1bee66ebba40b7e766fc409ec7c5ac","complete_document_id":"475d8e43-80ee-4882-a7cb-129f1edfaeab","complete_document_hash":"8c36a60cd890d3038ab9e2a491f03a5386a04ec9670844583c100b9330b97ff9","complete_package_id":"4a4feef5-2303-4683-8131-23e7322ab7c4","complete_package_hash":"a56b6314e667cf70812551754847cb979c4767c0b7dffd8bd34a4d0b3d699e3a"}`。
- `manual_interim=true` 精确要求 interim 文档版本为 v1，`terminal_complete=true` 精确要求同一中文
  identity 的 complete 为 v2；不同 document ID/hash 与新的 complete Package ID/hash 证明两版共存
  且终态输入重新冻结。`evidence_closed`、`exports_match`、`language_selected`、`cursor_resumed` 均为真。
- 通用框架原始 JSON：
  `{"plugin_ready":true,"cursor_resumed":true,"network_isolated":true,"caption_regression":false}`。
  课程 smoke 同时检查容器 `NetworkMode=none`、空 Mounts、只读根、`CapDrop=ALL`、
  `no-new-privileges`、直连失败、宿主 Secret 缺失以及强杀后的 generation/cursor 恢复。

### 四、验收环境与后续扩展边界

- 自动验收的应用链使用 Host 内确定性 Structured Provider，不需要云模型、模型 API key 或应用级
  外部 Web 调用；插件容器本身全程无网络。Docker 构建仍以本地/Registry 基础镜像元数据可用为基础
  设施前提，这不构成插件的网络 capability。
- 当前只承诺单 FastAPI 实例、单 SQLite 和受测试 Docker boundary；不承诺多租户、横向扩展、
  管理员/daemon/内核失陷后的隔离，也没有网页 DOM/播放器控制。后续场景助手应继续作为新插件使用
  通用事件和 Capability；若需要精确播放器定位、OCR、新文档格式或研究数据，应先设计新的 Host-owned
  有界 capability/可信 exporter 和对应 Grant，而不是让插件获得挂载、数据库或直接网络。

## 2026-08-29 — 多插件助手工作台：Batch 1 内置插件安装链检查点

### 一、内置目录与安全构建链

- Host 新增固定白名单内置插件目录，目前只包含 `com.matinier.course-organizer`；名称、版本和精确七项
  权限从受验证 Manifest 派生，ID 或权限漂移时 fail closed。公开目录只返回 ID、名称、说明、版本和
  动态构建可用性，不暴露源码目录、签名材料、镜像命令或服务器路径。
- 动态构建默认关闭，生产环境禁止开启。签名工作根已通过配置校验确保位于仓库外；本记录不保存该根、
  私钥或管理员令牌的具体路径和值。
- 包服务从 Docker 实际 COPY 闭包捕获最小只读源码快照并计算摘要，再以快照构建、签名和缓存；单进程
  同一插件构建采用共享任务与线程锁，调用方取消不会提前清理仍在写入的 partial/snapshot。每次真实构建
  使用唯一镜像标签并只清理该标签；损坏 ZIP、Manifest、权限、image digest、签名或公钥不匹配均重建。

### 二、既有二阶段安装流接入

- 新增 `GET /api/plugins/builtins` 和受既有管理员令牌保护的 bodyless
  `POST /api/plugins/builtins/{plugin_id}/packages:inspect`。静态路由位于动态插件详情路由之前；未知 ID
  返回 404，已知但禁用返回 409，隐藏构建失败返回不含内部路径的稳定 500。
- 内置包检查仍复用既有 `PackageStore.inspect`，后续确认、信任发布者、安装和启用继续走原二阶段 API，
  没有绕过签名、权限精确接受、publisher fingerprint 或容器导入边界。阻塞的清理、构建和 ZIP 检查已
  卸载到工作线程。

### 三、Batch 1 验证与审查

- 计划规定的六组联合测试：`69 passed, 3 skipped, 1 warning`；扩大到全部插件/设置相关回归：
  `201 passed, 3 skipped, 344 deselected, 1 warning`。独立只读审查复验：
  `83 passed, 3 skipped, 1 warning`。相关 Python `compileall` 退出码 0。
- 3 个 skip 均是当前 Windows 账户不能创建符号链接的环境条件；符号链接拒绝路径已由实现和静态审查
  覆盖。唯一 warning 是既有 Starlette TestClient/httpx 弃用提示。
- 独立审查未发现 P0/P1，确认取消安全、唯一标签、源码摘要/快照、公开 DTO、路由/鉴权和既有二阶段流
  三组核心风险已闭合。保留的 P2 是：全局 singleflight 的跨事件循环亲和性、有效期内重复 inspect 的
  staging 放大、进程崩溃遗留/历史缓存的有界回收，以及浮动基础镜像和打包器版本未进入缓存盐。这些不
  阻止本批次，但应在真实 Docker 验收前后按部署边界继续收口或明确记录。

## 2026-08-29 — 多插件助手工作台：Batch 2 通用前端接入检查点

### 一、通用助手工作区与状态隔离

- 新增 `/assistants` 主从式工作区，可通过 URL 中的 `session` 与 `plugin` 深链恢复选择；默认优先当前或
  运行中的字幕 Session，再回退到最近历史。已安装但暂无视图、仅有历史文档、未安装的显式深链插件、
  disabled、waiting、degraded、crashed、quarantined 和框架关闭均有独立状态，不把课程插件写死在页面。
- 插件目录由 installed plugin、视图和文档按 plugin ID 合并。表单值使用
  `mediaSessionId:pluginId:surface:viewId` 复合键；切换 Session 只清理旧 Session 的输入和已验证视图。
  文档始终按当前插件过滤，下载链接只采用 Host 返回并规范化的可信字段，不信任插件视图附带的 URL。
- Session 切换、初始加载、轮询和命令后刷新均受 generation 与共享请求序列保护；陈旧响应不能覆盖新
  Session 或更新视图。无效视图只可保留上次已验证内容用于安全降级展示，渲染层和命令层都禁止执行。
  health、plugins、views、documents 与各插件 command error 分开记录；命令成功后的视图网络错误不会
  误报为命令失败。轮询间隔为视图 2 秒、插件/文档/健康状态 4 秒，不自动重放用户命令。

### 二、Room 入口与可扩展插件管理

- Room 原展开式插件区已替换为紧凑助手启动器，显示连接数、异常数、最近更新和明确的加载/连接错误，
  并提供携带当前 Session 的工作区入口；Room、助手工作区和插件管理页之间均有顶层导航。
- `/plugins` 在上传区之前新增“项目内置插件”目录。内置和上传来源在 reducer 中显式区分，内置流程复用
  同一检查、发布者指纹、精确权限接受、安装与启用链；权限永不自动勾选，发布者信任绑定当前 inspection
  并在重新选择/检查时重置。重复操作被禁用；安装成功但启用失败会保留并展示 installed/disabled 警告。
  管理员令牌只存在页面内存，不写 URL、本地存储、日志或本文档。

### 三、验证与阶段边界

- 前端最终回归为 `9 files / 72 tests passed`；独立 TypeScript 检查退出码 0；Next.js 16.2.10 生产构建
  成功并生成 `/`、`/_not-found`、`/assistants`、`/plugins`、`/sessions/[sessionId]`。
- 浏览器桌面与 390px 窄屏检查确认 `/assistants`、`/plugins` 正常渲染、单列折叠且无横向溢出；Room
  导航、Session 选择、插件目录和内置插件卡片均可见。独立终审复验同一 72 项测试、类型检查与构建，
  未发现剩余 P0/P1；终审提出的命令成功后刷新错误误报 P2 也已拆分并再次通过全量前端验证。
- 当前已运行的本地开发后端仍是迁移和配置前的旧实例，因此浏览器中插件 API 暂显示连接/操作失败，
  这不是本批次用前端代码掩盖的假成功。真实数据库备份迁移、开发配置、内置插件持久化安装、API/Worker
  重启及多插件真实浏览器验收严格留给 Batch 3（计划 Task 8–9）执行。

## 2026-08-31 — 多插件助手工作台：Batch 3 本地验收通过，真实模型验收待授权

### 一、迁移、安装与重启恢复

- 实际开发数据库已升级至 `20260828_0026 (head)`；2026-08-31 再次只读检查 `PRAGMA quick_check`
  为 `ok`，Session/Segment 数量仍为 `28/89`，与迁移前一致。保留的备份为
  `E:\学习文件\研究生\就业\Agent学习\Matinier\backend\data\backups\live_caption-pre-20260828_0026-20260830-102750.db`
  （946176 字节），未删除或覆盖。
- 开发实例已经通过 Host 内置检查与原二阶段授权安装课程插件，精确接受七项权限。签名工作根经解析
  确认位于仓库外；本记录不包含管理员令牌、私钥及签名工作根的路径或值。
- 课程插件升级为 `com.matinier.course-organizer@1.0.1`，补齐实时/终稿 JSON 字段契约及同 schema
  的状态迁移入口。新绑定使用 preferred version；既有绑定继续固定旧版本，不暗中改变历史处理语义。
  `com.matinier.diagnostic@1.0.0` 同时安装，仅有 `state.get/state.put/ui.publish` 三项权限。
- 2026-08-31 重启开发服务后，本地 `GET /api/plugins` 确认课程 preferred version 为 `1.0.1`，两个
  插件均为 `enabled/ready`，证明安装状态、包与运行时可恢复。后续为等待历史字幕外发授权，主动停止
  此次启动的 API/Worker/Frontend；没有禁用或卸载插件，Docker/LiveKit 保留运行。

### 二、真实运行暴露的问题与修复

- 历史标签页 Session 打开时的 `500 Media bridge failed` 已在日志定位到插件游标确认的
  `sqlite3.OperationalError: database is locked`。Host 现在把 capability 数据库事务、runtime 状态、
  binding 以及投递/确认游标写入纳入同一进程内写入通道；公开错误保持稳定，服务端增加受控异常日志。
- 针对远程模型请求新增文件型 SQLite 并发回归，先稳定复现模型等待时字幕连接无法写入，再修复为：
  先提交 pending 调用与审计，模型等待时释放数据库事务和插件写入锁，返回后重新获取锁保存结果。
  成功、失败、取消分别保留 `completed/failed/pending` 状态，并验证字幕写入与插件状态写入持续可用，
  锁不会泄漏。非模型 capability 仍在失败时回滚，Frozen Package/文档原子发布边界未放宽。
- 插件声明的 `max_output_tokens` 原先只在结果返回后校验，没有传给实际 Provider。现经可选结构化请求
  字段传递，Provider 使用请求预算与 Host 配置的较小值；其他工作流未指定时保持原默认预算。使用
  MockTransport 覆盖默认值、较小请求值和 Host 上限，不需要发送私人数据。
- Docker Desktop 4.88.1 再次被 Windows 失效 AF_UNIX reparse point 阻塞，错误对象为
  `sailor-ingest.sock`。确认 Docker 已退出且目标目录不是 reparse point 后，仅保留性重命名
  `C:\Users\GX\AppData\Local\Docker\run.stale-20260831-102240` 与
  `C:\Users\GX\AppData\Local\docker-secrets-engine.stale-20260831-102240`，随后 Engine 29.7.2 恢复。
  这是本次启动恢复证据，不宣称根治 Docker Desktop 的跨重启套接字问题；未执行 factory reset，也未
  删除镜像、卷、WSL、项目数据或先前备份。

### 三、最终本地回归与真实 Docker 隔离

- 后端最终 `compileall` 退出 0；完整 pytest 为 `555 passed, 4 skipped, 1 warning in 96.00s`。
  跳过项为 Windows 符号链接/Bash 环境条件，warning 为既有 Starlette TestClient/httpx 弃用提示。
  新增模型/事务/课程聚焦组为 `34 passed`；Task 8 课程与通用框架 E2E 此前已顺序重复五次通过。
- 前端最终为 `9 files / 72 tests passed`；独立 TypeScript 检查与 Next.js 16.2.10 生产构建均退出 0，
  路由包含 `/`、`/_not-found`、`/assistants`、`/plugins`、`/sessions/[sessionId]`。
- 三项 Docker smoke 按计划顺序重跑，全部退出 0，原始结果如下。使用临时 SQLite、合成字幕和
  Host 内确定性模型，不把私人课程发送至外部模型。
  - 沙箱：`{"sandbox_ok":true,"sentinel_blocked":true,"secret_blocked":true,"network_blocked":true,"root_write_blocked":true,"private_state_ok":true,"rpc_ok":true}`。
  - 课程：`{"plugin_ready":true,"realtime_notes":true,"manual_interim":true,"terminal_complete":true,"evidence_closed":true,"exports_match":true,"language_selected":true,"cursor_resumed":true,"network_isolated":true,"secret_blocked":true,"caption_regression":false,"interim_document_id":"240820f8-200e-41f5-b871-4aef58b3473f","interim_document_hash":"48ba58375b4b50c8e9dc8d4f4c09716577d397cccad0138d5bb95ee53f4ff88f","interim_package_hash":"5b4b6f306fdee97db944617017ee6e7a86817f747b90d2222323a2a30d9280f8","complete_document_id":"eb54a6ef-2a92-4a7f-9778-df1b0f9a4add","complete_document_hash":"766e0a1c30a1c16834add42ca5896bd1c4d73a7250071a3f81b6622454f56213","complete_package_id":"dfd86aed-9de6-47c7-8a54-9c55768429de","complete_package_hash":"5025ce42de8e90bd3db597ebf061f98b465422b6b196991b5dc0b07e8e18064e"}`。
  - 通用框架：`{"plugin_ready":true,"cursor_resumed":true,"network_isolated":true,"caption_regression":false}`。
- 结束后核对 Docker 容器仅保留既有 LiveKit/Redis；smoke 容器及其精确镜像标签已清理，真实安装使用的
  两个课程版本镜像和 acceptance 诊断插件镜像保留。单 FastAPI/SQLite/本机受信任 Docker 边界不变，
  不外推为横向扩展或生产多租户隔离。

### 四、验收范围与待完成项（Task 9 尚未关闭）

- 先前浏览器验收已观察到 Room 顶层“助手工作区”、历史 Session 选择及课程/诊断插件切换，同一
  MediaSession `96668af0-0ac9-414e-aee1-1ad393de6774` 下两个视图共存，两个 durable ACK 均为 17。
  课程视图中出现历史标签页字幕形成的带时间坐标笔记，但当时存在规则降级，不能作为真实模型终稿成功。
- 当前只读数据库确认新测试 MediaSession `585eb5e0-aa28-4dd2-a3d4-a22a922d3b89` 下两个视图共存，
  课程 `1.0.1` 的投递/ACK 均为 36；诊断绑定的最终 catch-up 尚待重新打开会话验证。实际开发库当前
  没有已发布课程文档，不能把确定性 smoke 的文档证据冒充实际课程交付。
- 浏览器后续 reload 受到工具 URL 安全策略限制，未绕过限制或改用其他浏览器自动化。最终真实课程
  API 验收又因历史字幕可能发往 DeepSeek 而被权限审核拒绝；未重试该被拒操作或通过间接执行绕过。
  已向用户明确说明目的地和字幕数据范围并请求授权，同时停止会自动续跑旧模型任务的开发服务。
- 待用户允许 Session `8e6cfe75-cdf3-4291-8ef8-e5e356d7381e` 的课程字幕发送至已配置 DeepSeek 后，
  再启动最终代码，验证桥接无 500、两插件 ACK、真实模型实时笔记和终稿、可信 Markdown/JSON 导出及
  重启恢复。如果真实模型仍失败，继续基于受控错误码定位，不伪造终稿、不回写字幕。至此仅 Task 8
  及 Task 9 的迁移、安装、代码修复和本地回归完成，尚未宣告整个计划闭合。

## 2026-08-31 — Batch 3 授权后收尾：课程 1.0.3 真实文档与重启验收

### 一、授权范围与真实运行修复

- 用户明确允许将历史字幕 Session `8e6cfe75-cdf3-4291-8ef8-e5e356d7381e` 的课程内容发送至已配置
  DeepSeek。受控验收仅恢复其 MediaSession `585eb5e0-aa28-4dd2-a3d4-a22a922d3b89`；binding 加载、
  会话打开及模型 adapter 均限制为这一会话，未为其他历史会话触发模型处理。普通开发服务保持停止，
  避免后台自动恢复未获本次授权的旧模型任务。
- `1.0.1` 的真实终稿返回多次达到 2,048 token 上限而截断；`1.0.2` 将交付分页与模型 map/reduce
  批次分离，按最多 4 项/8,000 字符分批，请求 4,096 token 并保留 Host 上限。批内模型 ID 采用
  确定性命名空间合并；超出单项或总知识项上限会显式失败，不再静默丢弃整批尾项。终态重试继续保持
  原终态 completeness，升级后的 job identity 避免命中旧版本失败调用。
- `1.0.2` 曾发布 `e4ab9f06-737e-4da3-a779-c0962dfb64a2`，但导出审核发现第一部分为空，故该文档
  不作为完整功能验收成功证据。根因是实时事件的 `transcript:`/`translation:` 别名与 Frozen Package
  的真实源 Segment ID 不一致。`1.0.3` 使用 Host 明确提供的源 ID，支持多源译文对齐且不猜测未对齐
  译文来源；旧错误状态在新版本中由 durable events 重建。旧包、旧状态和旧文档仍保留可查。

### 二、实际课程交付与恢复证据

- 实际开发实例 preferred package 为 `com.matinier.course-organizer@1.0.3`，安装状态 `enabled`、
  签名 `verified`；诊断插件 `com.matinier.diagnostic@1.0.0` 同样启用且签名有效。课程历史包
  `1.0.0/1.0.1/1.0.2` 均保留。本次只关闭并保留授权会话的旧版本 binding，不批量迁移其他历史会话。
- 最终文档 `845f867a-de0a-4071-92f5-b71050eed127`：`course-notes:zh-CN` v2，
  `trigger=session_completed`、`completeness=complete`、`status=published`。第一部分包含 3 条
  真实模型知识笔记（`rule_fallback=0`），第二部分包含 17 条分类知识点；17 个 evidence refs 有效，
  实时笔记来源均为 canonical ID，闭合成功，warnings 为空。
- 终稿 source Package 为 `3eb848ba-868d-4e75-a6ea-11a451847945` v4，hash
  `c240461d0b1d43448c51a104402b4b2fe071576ecba58960d5182ab8aa0d190a`；document hash 为
  `8765f556416f9f1b808d50fbf02c5ccec294a0fe4d708ec21ea5278df8ed7703`。
- 关闭并重新启动受控 Host 后，bridge 返回 200，课程状态恢复 `ready` 且仍有 3 条笔记和同一文档；
  课程与诊断两个视图共存，投递/ACK 均为 `36/36`，按诊断插件过滤的文档列表为空。恢复验证没有出现
  新的模型请求。Markdown/JSON 路由返回内容与文档详情一致，导出响应 hash 与存储 hash 一致。
- 已通过 Host exporter 导出至
  `backend/data/acceptance/845f867a-de0a-4071-92f5-b71050eed127/course-notes-zh-CN-v2.md` 和
  同目录 `.json`；文件只在不存在时创建，已存在则校验内容一致，不覆盖历史导出。

### 三、最终回归、Docker 与数据库

- 最终 Python compileall 退出 0；完整后端 pytest 为
  `559 passed, 4 skipped, 1 warning in 103.21s`。4 个 skip 为 Windows 符号链接/Bash 环境条件；
  warning 仍为既有 Starlette TestClient/httpx 弃用提示。新增回归明确要求真实源 ID 对齐、未对齐译文
  不捏造来源、迁移保留旧状态，并将 E2E 的终稿第一部分非空设为断言。
- 前端未再改动功能代码，最终既有结果为 `9 files / 72 tests passed`，typecheck 与 Next.js 16.2.10
  production build 均通过，包含 `/assistants` 路由。
- 当前 Docker Client/Engine 为 `29.7.2/29.7.2`，Desktop 4.88.1。通用沙箱和框架 smoke 在上节
  已验证，本次对最终 `1.0.3` 重跑真实 Docker 课程 smoke，退出 0，完整 JSON：
  `{"plugin_ready":true,"realtime_notes":true,"manual_interim":true,"terminal_complete":true,"evidence_closed":true,"exports_match":true,"language_selected":true,"cursor_resumed":true,"network_isolated":true,"secret_blocked":true,"caption_regression":false,"interim_document_id":"742557fd-9fb3-4653-9af9-bb4693306bd8","interim_document_hash":"f2132e68124e59427dddf6502cc7189aaf9baca3f8c177c111bb456a522b3e73","interim_package_hash":"e5e035de4513ca64886e2ed14c5fefccdf0ccf184cc9f0b0001ce5c9decfbc00","complete_document_id":"a0228f6d-9e20-43d2-9ead-c5ffcad90630","complete_document_hash":"6658cccbd537e9b9e50715932a57ddf4a02fc0f0a22f44faa0c9a0049aebb4d3","complete_package_id":"126b6640-fe77-4635-8636-1140c509faaa","complete_package_hash":"107358879b5e90e5741f555cd905e7093dbabccf49319406c317cec037543caf","realtime_notes_in_final":true}`。
- 数据库仍为 `20260828_0026 (head)`，`PRAGMA quick_check=ok`，Session/Segment 仍为 `28/89`。
  在原迁移备份之外，保留新增备份 `backend/data/backups/live_caption-pre-course-1.0.2-20260831-105144.db`
  （3489792 字节）和 `live_caption-pre-course-1.0.3-20260831-110450.db`（3948544 字节）。
- 验收结束仅保留既有 LiveKit/Redis 容器；临时 smoke 容器和精确镜像标签已清理，实际安装镜像、数据、
  Docker stale 目录及备份保留。3000/8000 无监听，未把完整开发服务留在后台运行。

### 四、交付范围与尚需人工复验的项目

- 插件安装、通用工作区接入、真实模型双部分文档、证据/导出和 Host 重启恢复已取得成功证据。先前
  浏览器已验证 Room 入口、Session 选择与课程/诊断切换；后续浏览器 reload 被工具 URL 安全策略阻止，
  没有绕过。最终版本的真实播放、页面点击生成与页面重载仍需用户在浏览器复验，不能把本次受控 API
  和 Docker 验收称为全套最终浏览器验收。
- `executing-plans` 所要求的分支收尾技能当前不可用，且工作区不是有效 Git 仓库；以全量测试、
  导出产物和本阶段记录交付，没有伪造 commit 或执行仓库重置。单 FastAPI/SQLite/本机 Docker 以及
  字幕时间轴不等于外部播放器精确 seek 的边界不变。

## 2026-08-31 — 实时课程笔记缺失：持续事件投递修复（运行实例待重启授权）

### 一、根因与验收缺口

- 用户最新播放 Session `ae4a31f0-b9ba-45e6-8791-2b8c8f567541` 已保存 3 条媒体事件（原文、结束、
  译文），但课程与诊断插件的 delivered/ACK 均为 0，仅发布了初始空视图，没有模型调用。此问题不是
  模型筛选或页面隐藏笔记，而是首次连接之后的事件没有继续派发。
- 原 Host 只有 `resolve_media_session → _open_ready_bindings → _deliver_pending_events` 这一条派发
  入口；投影器持续写入事件，前端只轮询只读视图，缺少连接后持续派发。此前 E2E/smoke 在新增字幕和
  结束后主动调用 bridge，掩盖了断点；历史回放生成文档不能代替真实增量投递验收。

### 二、修复与安全边界

- `PluginHostRuntime` 增加独立后台 dispatcher，随 Host 启停，目标仅来自当前已打开的运行时 scope，
  不扫描历史媒体会话自动创建绑定。每个插件/会话每轮最多一个事件批次、一个在途后台任务，持续派发
  新字幕和结束事件；慢/失败插件独立重试，不串行阻塞其他插件。
- 前台首次补发与后台派发共用 per-plugin/session 锁，在锁内重新读取 durable ACK，防止并发读取旧游标
  导致重复交错投递。保留原订阅过滤与 ACK 校验；失败不伪造确认，下轮从旧 ACK 重试。scope 撤销取消
  在途任务；shutdown 先取消并等待派发，再关闭 supervisor/projector，日志不包含正文或异常秘密。
- 此次只改 Host、测试与文档，插件仍为 `1.0.3`，不需重新打包或安装。不改变 60 秒/1,600 字符窗口、
  插件权限、最终整理触发规则、前端轮询或字幕主链。

### 三、失败复现与最终验证

- 新增“空会话先连接、随后写入字幕、后续只读取视图”的 E2E；修复前稳定失败于
  `timed out waiting for realtime notes without reconnecting`。新增生命周期/并发单测修复前 5 项失败，
  确认断言覆盖了缺失功能而非依赖设置错误。
- 修复后 E2E 覆盖连续两个实时窗口、期间无自动终稿、结束自动 complete、第一部分非空以及
  delivered/ACK `9/9`；参数化另覆盖中途 API 重启后不重新连接仍接收后续字幕。单测覆盖空 Host 后新增
  scope、禁用/离线目标、慢/失败插件隔离、scope 撤销、取消顺序、并发游标锁及失败事件不丢失重试。
- 聚焦组（派发、课程 E2E、通用框架 E2E）连续五次均为 `13 passed, 1 warning`，耗时分别
  11.55、11.63、11.71、12.48、12.82 秒。完整 compileall 退出 0；全量后端为
  `567 passed, 4 skipped, 1 warning in 110.34s`。skip 为既有 Windows 符号链接/Bash 条件，warning 为
  既有 Starlette TestClient/httpx 弃用提示。前端本次未修改或重测，不以先前测试冒充新浏览器验收。
- 真实 Docker 课程 smoke 删除终态的额外 bridge 调用，并等待新实时窗口后才结束会话，退出 0：
  `{"plugin_ready":true,"realtime_notes":true,"continuous_delivery":true,"manual_interim":true,"terminal_complete":true,"evidence_closed":true,"exports_match":true,"language_selected":true,"cursor_resumed":true,"network_isolated":true,"secret_blocked":true,"caption_regression":false,"interim_document_id":"803e2874-4a11-4863-90c1-9494336aa60b","interim_document_hash":"bf303b80d2ab291d0abbdd87ae302d755549e1c1c21c97e36238f1b43fde32b5","interim_package_hash":"ea15aa1e91671c1f4476211826d429d13227688222f0b82b012b53239ed5fa6b","complete_document_id":"ec7780e6-105e-441f-8932-3aa24cfc24b1","complete_document_hash":"583b16361e12536ee4d852f2bd81eb14b0735fc6859d51fe9de925cb027a03fb","complete_package_id":"b0099426-516c-4996-824f-783e9de26c29","complete_package_hash":"85daf89e41b7049aa2f751506ea3718e349486ccffafe135cf27f721599fd5a8","realtime_notes_in_final":true}`。
- 通用沙箱与框架 smoke 也均退出 0：
  `{"sandbox_ok":true,"sentinel_blocked":true,"secret_blocked":true,"network_blocked":true,"root_write_blocked":true,"private_state_ok":true,"rpc_ok":true}`；
  `{"plugin_ready":true,"cursor_resumed":true,"network_isolated":true,"caption_regression":false}`。
  测试只使用临时数据库、合成字幕和确定性模型，没有发送私人历史课程到外部模型。临时容器及测试标签
  已清理，原运行的课程/诊断容器和 LiveKit/Redis 保留。

### 四、运行实例交付状态

- 当前 8000 端口仍为原 API PID 24120，3000 为原前端 PID 28800；确认 API 未启用自动重载，所以
  代码修复尚未加载进当前网页使用的进程。未擅自重启、修改业务数据库、补发私人历史字幕或触发整理。
- 已询问用户是否允许重启后端并处理已连接会话的积压字幕（可能调用配置的 DeepSeek）。在收到该授权
  前，代码和合成实时验收已完成，但实际运行实例仍待切换，不宣称网页问题已现场复验解决。
- 使用 writing-plans/executing-plans 记录测试先行步骤；没有有效 Git 仓库，分支收尾技能不可用，按
  测试、代码审查与阶段记录交付，不伪造提交。计划位于
  `docs/plans/2026-08-31-continuous-plugin-event-delivery.md`。

## 2026-08-31 — 常驻字幕的多插件助手侧栏

### 一、问题与实现

- 原 Room 的“助手工作区”入口通过同标签页链接跳转到 `/assistants`，卸载 RoomStudio 后触发现有
  `releaseInput` 和 `room.disconnect` 清理，导致正在共享的标签页音频及字幕停止。清理本身正确，
  不能通过删除清理来规避；修复将入口改为仅更新 UI 状态的按钮。
- 用户确认左侧活动栏加可折叠共用侧栏。活动栏提供“直播空间”和“助手”，顶栏入口明确展开助手，
  再次点击活动按钮可折叠；默认宽度 384px、320–560px 边界，支持拖动及方向键/Home/End，Esc 或
  收起按钮归还焦点到活动项。窄屏覆盖式面板用 CSS 切换，不卸载中央音频/字幕。
- 新增 `StudioSidebar`、`RoomAssistantSidebar` 及纯状态 reducer；两个面板始终挂载，隐藏后仍保留
  插件连接、轮询、选择与输入。RoomStudio 仍持有 Room/音轨，媒体获取、发布和卸载清理没有改动。
- 复用 `useMediaAssistantWorkspace`、通用目录/详情/声明式渲染器，删除原右侧 launcher 的重复数据
  加载职责，当前 Session 只保留一份助手工作区 hook。没有新增课程特判或改变最终整理触发规则。
- 侧栏历史工作区、管理、文档下载及当前台本“后处理”链接使用新标签页和 `noopener noreferrer`。
  独立 `/assistants` 保留历史 Session 选择。原采集标签页被刷新、关闭或真正离开时仍正常停止音轨。

### 二、测试先行与最终验证

- 新增开发依赖 `jsdom=30.0.1`，Vitest 配置 JSX 与 `@/` 路径解析，默认环境仍为 node；未变更生产
  依赖。状态测试先因实现缺失失败；实际 RoomStudio DOM 回归在旧代码上因“助手工作区”不是按钮
  而失败，确认捕获的是页面跳转入口。
- 全量前端 `pnpm exec vitest run --reporter=dot`：`12 files / 87 tests passed`，耗时 2.60 秒。
  新增 4 项纯状态测试、8 项 RoomStudio/助手 DOM 测试、3 项侧栏交互测试。
- 两份 DOM 测试连续五轮均 `2 files / 11 tests passed`，耗时分别 2.27、2.48、2.34、2.35、2.41 秒。
  覆盖共享音轨发布后展开/折叠/切换/调宽，中央节点身份不变，stop/unpublish/disconnect 均未调用；
  切换后注入新的字幕事件仍更新 DOM，真正卸载则停止音轨并断开 Room。
- 另覆盖隐藏时继续轮询且不重复 bridge、输入保持、多插件命令与错误隔离、文档过滤/新标签页导出、
  Session 改变后不携带旧输入，以及拖动取消、丢失 pointer capture、其他指针忽略、键盘边界和焦点。
  LiveKit/API 均为合成边界，测试禁止真实 fetch，不访问生产数据库或外部模型。
- `pnpm typecheck` 退出 0；Next.js 16.2.10 production build 退出 0，保留 `/`、`/assistants`、
  `/plugins`、`/sessions/[sessionId]`。自审确认媒体清理 effect 无侧栏依赖；窄屏和中央区域采用响应式
  样式，插件文档元数据在窄侧栏内改为两列，避免横向溢出。

### 三、交付与边界

- 更新 README 和插件运维说明，记录使用方法、首次更新界面前先停止旧采集、人工复验步骤。
  设计与执行计划为 `docs/plans/2026-08-31-assistant-sidebar-design.md`、
  `docs/plans/2026-08-31-assistant-sidebar.md`。
- 浏览器工具先前被 URL 安全策略限制，本次没有重试或绕过；DOM 合成验收不是浏览器实际共享音频、
  页面视觉或整套课程链路验收，最终实际播放仍需人工复验。
- 未重启 API/Worker/Docker，未重新打包插件，未补发私人历史字幕。上一阶段持续投递修复仍需单独
  授权重启并处理已绑定会话的积压事件（可能调用 DeepSeek）；本次侧栏确认不替代该授权。
- 使用 writing-plans/executing-plans 完成已批准的自动分阶段实施；工作区无有效 Git 仓库且分支
  收尾技能不可用，以全量测试、自审和阶段记录交付，没有伪造提交。

## 2026-08-31 — 用户授权后加载持续投递修复并验收真实课程

### 一、授权与切换

- 用户针对 Session `f5703db6-f633-4d5e-ae2d-76d4ca92f524` 再次询问助手无反应。只读诊断确认其
  7 条原文、4 条译文和结束事件均已持久化，MediaSession
  `665f68e0-79db-407e-9c5d-835e9a45ded7` 共 12 个事件，但课程/诊断插件 delivered/ACK 都为 0。
  API PID 24120 在 12:47:56 启动，未启用热重载，早于持续派发修复。用户随后明确允许重启 API
  并处理已绑定会话积压字幕，包括可能调用 DeepSeek。
- 切换前无当前音频采集任务或新在途模型调用；一个 `1.0.1` 的 pending 调用为上午遗留记录，
  未改写它的状态。其他已绑定会话的 ACK 已追平；处理任务无排队/运行项，未人为触发外部执行。
- SQLite online backup 保存为
  `backend/data/backups/live_caption-pre-event-dispatch-20260831-142631.db`，5,169,152 字节，
  `quick_check=ok`。备份基线为 Session 32、原文 Segment 98、译文 47、媒体事件 174、插件文档 6。
- 核实旧 API 的独立控制台只包含 API/venv wrapper 及其三个 Docker 传输子进程后发送 Ctrl+C，
  API 优雅退出，旧插件容器停止。没有对前端、Worker、LiveKit 或 Redis 发送停止信号。
- 新 API 于 14:28:09 启动，监听 PID 29388，venv wrapper PID 32680；仍使用
  `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。
  日志位于 `backend/data/logs/api-event-dispatch-20260831-142809/`。
  前端始终为 PID 28800，启动时间仍为 12:48:04；LiveKit/Redis 容器身份及运行时间不变。

### 二、真实补发与文档结果

- 新 Host 自动恢复已绑定 scope。目标会话课程插件和诊断插件的 delivered/ACK 均推进至 `12/12`；
  验收过程只轮询只读视图/文档端点，没有再次 bridge、没有追加“生成最终整理”命令。
- 课程视图从 v3 推进到 v7，显示 3 条实时知识笔记（规则降级笔记 0）。结束事件自动生成
  `session_completed / complete` 文档 v2：`30538e7d-d2f8-4f9d-be9d-cf8c001a2748`，第一部分
  包含全部 3 条笔记，第二部分包含 7 条分类知识点；7 个 evidence refs 闭合、warnings 为空。
- 文档 hash：`5234a654491f035e7903e10f48bb01da82b25e21db888d779e85df7eea7e7f21`。
  绑定 Frozen Package `18a376a9-bd4a-4dad-a88d-004d93480646` v2，hash
  `ac92a8379a86643859f843dab26ad426feba0d7fb0c6963b4549c51de13980ea`。
- 用户此前手动生成的 v1 `0301a006-c291-40dd-b3ac-defda9927249` 仍保留，未覆盖；该临时版本的
  第一部分为空、第二部分有 7 条知识点，与本次补发完成后的完整 v2 明确区分。
- 已验证 Markdown 与详情正文一致、JSON 与响应头 hash 一致，知识点和实时笔记的 evidence ID 均
  闭合到对应 Package，笔记来源 ID 非空；按诊断插件过滤的文档仍为空，未跨插件泄露文档。
- 另用前端真实 `parsePluginUIView` 解析当前 API 响应：课程 v7、诊断 v2 均
  `frontend_schema_valid=true`，排除本次视图被前端安全 schema 拒绝的情况；该检查不等同于浏览器
  视觉验收。Node 仅提示既有 package 未声明模块类型，本次未为诊断调整项目模块配置。
- 使用既有 Host exporter 导出 Markdown（3,594 字节）和 JSON（11,830 字节）至
  `backend/data/acceptance/30538e7d-d2f8-4f9d-be9d-cf8c001a2748/course-notes-zh-CN-v2.md` 及
  同目录 `.json`，已有文件只校验一致性，不覆盖。

### 三、健康、数据不变与历史边界

- `/health/ready` 为 ready；插件框架和 Docker 可用，projector running、lag 0、RPC pending 0，
  没有剩余未确认事件。Worker alive、LiveKit connected；Worker 原有 job 未被重启或人为取消。
- 数据库 `quick_check=ok`；Session/原文/译文/媒体事件仍为 `32/98/47/174`，文档数从 6 增为 7。
  本次没有改写原始字幕、注入假课程、迁移数据库、重新打包插件或手工改 ACK。
- 新 API 启动后，目标 `1.0.3` 会话共 9 次模型调用完成且无 error_code。另一个绑定 `1.0.0` 的
  历史会话 `96668af0-0ac9-414e-aee1-1ad393de6774` 恢复旧状态时出现 4 次 `capability_failed`
  模型调用（截至 14:29:06），保留模型降级/未完成整理提示。其 ACK 原本已为 17/17；这是旧版本
  历史处理错误，不是目标会话持续派发失败。未擅自迁移其版本或清空历史状态，也不宣称所有历史
  版本都已修复；当前 `1.0.3` 目标的文档及隔离检查均通过。
- 本次是已授权真实历史补发/API/导出验收；没有冒充新的实时浏览器采集验收。原有后端 567 项和
  前端 87 项测试结果仍为先前代码回归，本次未修改业务代码，不把它们称作本次重新运行的结果。
- 先前“API 未加载持续投递修复、等待授权重启”的交付限制至此解除。浏览器仍需用户实际播放新
  课程复验；已有页面应由轮询读到 v7 与完整文档，无需为本次补发再次开始采集。

## 2026-08-31 — 会议助手统一插件化 Batch A

### 一、范围与完成状态

- 用户要求完成第一批，按已确认的 `docs/plans/2026-08-31-meeting-assistant-plugin.md` 执行
  Task 1–3：旧功能/历史兼容基线、严格契约与增量迁移、显式会话资格和字幕补齐。
- 使用 `executing-plans` 批内测试与修正、批末验收和暂停；本批 3/3 项完成，整体 3/18 项。
  主代理完成自审，没有独立子代理审核。工作区无有效 Git 仓库，不初始化、不伪造提交或 worktree。
- 不执行后续 Host 操作 API/worker、标准会议插件包、统一前端及旧入口移除；不把后端基础完成
  视为会议插件已经可从网页安装使用。

### 二、兼容基线与实现

| 原能力/数据 | 本批保留与验证方式 |
| --- | --- |
| 私密问答、字幕上下文 | 复用旧 ContextBuilder；原 Final 修订与证据不变；运行中的 Projector 对未激活/历史问答不发起 catch-up |
| Fast Turn / Action Run、执行详情 | 临时库保留原 ID、root/parent、needs_input 问题、事件与 state_version |
| 候选、手动/自动重点、视频坐标 | 使用原仓储和响应适配器；验证候选 revision、mark ID、caption/user_input 证据及起止坐标 |
| 补充信息、取消及重放 | 直接调用原服务，版本 3→4→5、取消重放结果稳定、事件 ID 不重复 |
| Linear 引用和未知结果 | 已完成引用与 unknown claim 不变；fake 模拟创建成功但响应丢失，reconcile 找回同一任务 |

- 新增会议命令、查询、证据、操作受理及 Host intent scope 契约：有界长度/列表、严格状态版本、
  唯一候选/证据、拒绝额外 actor/Session/URL/grant。客户端不能通过这些输入选择服务器身份或团队。
- 新增 `meeting_plugin_sessions`、`meeting_plugin_operations`、`assistant_action_intents`，
  migration 为 `20260831_0027`，接在 `20260828_0026` 后。旧会议和执行数据不复制、不重编号；
  新表不对插件安装建立删除级联。操作唯一键绑定 plugin/version/media/request ID，hash 不同则冲突；
  票据仅持久化 hash/作用域，消费使用单条条件更新，支持事务回滚和并发单次消费。
- 新增默认拒绝的 MeetingPluginPolicy：实际启用安装、签名/版本、当前绑定、接受的权限、
  MediaSession/legacy Session 映射及持久激活均符合才可分析。仅安装或打开插件不会激活会话。
- 停止会话分析只递增 analysis_epoch；撤销会议插件同时递增 authority_epoch 并撤销该插件票据，
  不影响其他插件。自然结束与撤销分开处理，已激活会话按冻结终止 frontier 完成最后一批后停止。
- Projector 的扫描、backlog、排队、每次模型调用、错误写入和提交均受资格/epoch 保护；提交用
  SQLite 持久条件更新形成写入屏障，与撤销串行化。激活补齐全局游标之前的 Final，按 offset/revision
  去重；长字幕分片之间重新检查，旧结果不能覆盖重新激活后的状态；停止服务取消并清理 worker/等待者。

### 三、测试先行、自审修正和最终证据

- 业务修改前指定旧测试基线：`12 passed in 1.67s`。新增契约/模块缺失测试先失败，再实现；
  测试夹具按既有 API/字段语义校准，未通过削弱生产默认授权来恢复旧引擎测试。
- 自审补测先复现再修正四处边界：终止期间丢弃的在途字幕被扫描游标遗漏；旧修订超出终止 frontier
  仍先调用模型；撤销范围错误覆盖其他插件 intent；重新激活后的处理结果误完成旧 epoch 等待者。
- 并发测试使用 asyncio.Event、线程 Barrier/Event 和数据库执行检查点，没有用 sleep 碰时序。
  覆盖排队后停用、模型等待/失败后撤销、分片间撤销、重新激活、条件写入与并发撤销、并发激活、
  权限/绑定/签名/版本改变、空/非法终止输入、旧游标补齐、字幕修订、运行中历史问答和 shutdown。
- 最终计划聚焦组（全部本批新测试及旧基线）：`65 passed in 12.78s`，新增 53 项、旧基线 12 项。
- 最终全量后端 `python -m pytest -q -rs`：`620 passed, 4 skipped, 1 warning in 71.30s`。
  三项 skip 是本机无法创建符号链接，一项是 Windows subsystem Bash 无法访问工作区；warning 为
  既有 Starlette TestClient/httpx 弃用提示。本批没有升级依赖。
- 最终 policy/projector/repository 聚焦组连续五轮均 `32 passed`，耗时依次
  5.46、5.56、4.92、4.88、4.95 秒。`python -m compileall -q app alembic tests` 退出 0。
- 迁移只在临时库演练：旧 head 写入夹具→升级新 head→重复升级→临时降级；9 张旧表逐行快照不变，
  新表为空、唯一约束及非安装级联成立，`foreign_key_check` 为空、`integrity_check=ok`。

### 四、交付边界与后续

- 没有修改生产数据库、重启 API/Worker/前端/Docker、重新安装课程插件、补发真实历史字幕或调用
  真实模型/Linear。当前运行服务没有加载这一批改动；原字幕、课程文档及独立会议入口保持原状。
- 本批未改前端，也未重跑前端或做浏览器/真实隔离容器验收；不使用此前的 UI/Docker 结果冒充本批验证。
- 激活/停用目前是内部 Host 基础接口。可信动作受理与 worker、停用/卸载生命周期组合、历史目录、
  会议插件包、前端入口及旧 API 收口仍按 Batch B–E 完成。尤其不能在这些接入完成前直接重启上线
  当作完整插件迁移；后续受控运行切换还需要增量迁移及相应验收。
- Batch A gate 已通过，计划只勾选 A；依执行技能在此汇报并等待下一批指示。

## 2026-08-31 — 会议助手统一插件化 Batch B

### 一、范围与完成状态

- 用户“继续”后按已确认计划执行 Task 4–7：只读历史、可信操作、持久队列、会议能力与最终写入守卫。
  本批 4/4 项完成，整体 7/18 项（约 39%）。使用 `executing-plans`，主代理自审；没有独立代理审核。
- 无有效 Git 仓库，未初始化仓库或伪造提交。保持批内测试/修正、批末汇报；不提前执行 Batch C–E。

### 二、后端实现与授权边界

- 新增 `MeetingPluginReadService` 与 `MeetingPluginHistory`，读取原 Session、执行链、标记、候选及
  evidence/视频坐标；不将正文复制进插件 state。用显式 SQLite 读快照关联状态与会话事件游标；
  分页和 JSON 有界，processing 统计基于当前 Final/revision 与 offset，不使用插件 ACK。
- Host 有限动作注册表覆盖分析开关、问答、三种标记动作、执行、补充输入和取消。
  `prepare/confirm` 使用精确 Origin、JSON/custom-header 和短期 UI nonce；无 Origin 需单独 Host 凭据。
  actor 和真实会话/安装版本由 Host 决定，不信任插件的 confirmation/trusted 字段。
- 确认前不创建 intent、ActionGrant 或任务。确认后生成一次性 intent，只持久化 hash；外部操作还绑定
  确切 capability grant、候选 revision、固定 team、预算、过期时间与 authority epoch。
  重复确认返回同一结果，参数变化须重新准备。Host 历史取消不要求插件运行，不能扩展为 resume/execute。
- `MeetingPluginOperations` 在调用方短事务消费票据、保存 operation、预留原 execution ID/幂等 request、
  冻结上下文及原 ActionGrant；与 Broker invocation 一起原子提交。固定并发 worker 从数据库接续，
  不在 Broker 锁或 DB Session 内等待模型/引擎；停止有界。中断后无持久结果的问答不会自动重复计费。
- 注册七种 `meeting.*` 能力，显式声明 effect、幂等、恢复及执行写授权要求。受控适配器接收
  AuthorizationDecision；请求 scope 来自 Host 票据而非客户端可省略的 JSON。旧进程 generation、
  其他插件、错误会话、缺失权限/票据被拒绝。Broker ledger 不保存可重放的明文 intent。
- 最终 ToolExecutor 使用与撤权共用的 SQLite writer 事务边界，检查 owner/authority epoch、
  capability grant、原 ActionGrant、候选/证据、固定团队和剩余预算，再持久化 requesting、提交、发请求。
  停用先关闭准入/撤销权限，再等待容器退出；已发请求可完成，新请求被拒绝。停止分析不撤销有效交互授权。
- 撤权或无 ownership 的旧活动执行只核对已有 tool call 并保留待确认，禁止自动重启 planner。
  未知外部结果保留原 claim；fake 模拟远端已创建但响应丢失，撤权后的核对只返回该结果，不重复创建。
  合法已授权操作仍复用原 Action Run 状态机。模型调用/修复重试、子分析及结果提交都有检查点。
- Linear 集成设为 disabled 时，实际 FastTurnRunner 仍可通过 fake 模型完成问答；执行准备明确拒绝。
  此证明不代表真实 Linear 连通性、凭据有效性或真实模型服务已验证。
- 从后端契约机械生成公开 SDK schema，仅 `plugin-capabilities.schema.json` 的 hash 改变；
  JSON-RPC、MediaEvent、manifest 和 UI schema 四份文件的 hash 不变。

### 三、测试、自审与最终证据

- 按 TDD 先确认目标模块/行为缺失的失败，再补实现；自审补充最终工具与生命周期测试。
  测试夹具的候选 projection head 和 ui.publish root 按原契约校准，未放宽生产校验来迁就测试。
- 自审修正 `deactivate` 后缀误判、可省略 scope 的授权隐患、明文票据进入 ledger 的风险、
  模型修复重试与子分析的检查点、状态分页 has_more、公开结果凭据过滤及临时 DB claim 错误处理。

| 负向/并发场景 | 实际验证 |
| --- | --- |
| 伪造 actor/Session/版本/团队、异源或缺少 UI nonce | API/prepare 拒绝，未签发有效票据 |
| 准备后取消、双确认、过期/撤权票据、同键不同参数 | 不入队；单次签发/消费；参数冲突拒绝 |
| 七种能力、错误 owner/scope/generation、缺失权限或 intent | 正向受理及负向拒绝，原 operation 重试不新增 |
| 问答已进入模型 | 另一插件 state.get/ui.publish 和字幕写入完成，全局锁未持有 |
| 外部请求已进入 fake | requesting 已提交；字幕仍可写；撤权阻断第二次调用，已发请求可完成 |
| 未确认、scope grant 被篡改、预算用尽、团队/证据变化 | 最终工具不调用 fake create |
| 响应丢失后撤权/启动恢复 | fake create=1、reconcile=1、planner 入队=0；原执行 needs_input、原调用结果可读 |
| 容器退出被 Event 挂起 | 撤权已提交、capability grant revoked，新写调用被拒绝 |
| Linear disabled、模型返回前撤权、旧无 owner 的活动执行 | 问答可用；撤权答案不提交；旧执行不自动运行 |
| 停用/移除安装后的历史 | 原 ID/执行链/证据保留，不创建分析资格或调用模型 |

最终命令均在 `backend`，不使用生产数据库：

1. 七个 Batch B 新测试文件：`65 tests collected`。
2. 本批全部新测试及计划指定兼容/契约/API 组：`107 passed, 1 warning in 19.65s`。
3. 最终全量 `python -m pytest -q -rs`：`685 passed, 4 skipped, 1 warning in 110.16s`。
   自审补测前首轮 678 passed 仅作过程记录，不替代最终结果。
4. projector / operations / execution_guard 连续五轮均 `36 passed`，耗时依次为
   9.12、9.18、8.60、8.44、8.45 秒，无遗留 task、数据库锁定或重复 create 的测试失败。
5. `python -m compileall -q app alembic tests` 退出 0；SDK 生成及一致性检查通过。

四项 skip 为 Windows 的三项符号链接条件和一项 WSL Bash 工作区路径条件；warning 是原有
Starlette TestClient/httpx 弃用提示，没有为此升级依赖。全部外部服务使用 fake。

### 四、交付边界与下一批

- 没有进行生产数据库迁移、服务/Docker 重启、插件安装或真实字幕补发，没有调用真实模型/Linear。
  未改前端、未做浏览器或真实隔离容器验收，不将本批后端通过称作网页新功能已可使用。
- 新 Host action/history 路由目前只在测试应用注册；唯一 Host 实例组合及主应用路由注册、旧写 API
  收口仍属 Task 13。旧 Fast Turn 的只读入口暂保留，生产组合中的最终写入/启动恢复已有守卫。
- 会议插件包/控制器/声明式视图为 Batch C；通用可信控件、历史目录、独立卡片移除为 Batch D；
  整体验收及授权范围内受控上线为 Batch E。不能单独重启部署当前不完整迁移版本。
- 计划勾选 A/B，按执行技能停在本批检查点，等待下一批指示。

## 2026-08-31 — 会议助手统一插件化 Batch C

### 一、范围和完成状态

- 依用户“继续”和已确认计划执行 Task 8–10，使用 `executing-plans`，完成源码、隔离回归与主代理自审。
  本批 3/3，整体 10/18（约 56%）；未进入 Batch D/E。没有独立代理审核，也没有 Git 初始化/提交。
- 新增会议插件 manifest、Dockerfile、SDK entrypoint、contracts/session/view 和目录 README；
  修改通用内置构建、课程兼容 wrapper；前端只增加共享 JSON fixture 与 parser 测试，不修改正在使用的 UI 入口。

### 二、实现和安全边界

- 内置构建从课程专用逻辑改为固定 course-organizer / meeting-assistant source registry。
  只捕获本插件 Python/manifest/Dockerfile 与共享 SDK 源码，不含其他插件、字节码、数据、密钥或根目录。
  校验整个源码闭包后才写快照；路径越界、其他插件源码和额外文件拒绝。
- 课程原公开打包函数和摘要算法保留；会议文件变化不使课程缓存失效。两插件各自缓存/快照/唯一镜像标签，
  共享测试签名 key 的首次创建加短锁，防止并发读取半写入 PEM。仅清理本次成功构建的指定 tag。
- 会议权限仅七种 meeting 能力和 state/ui。容器设计为非 root、网络隔离，重用标准签名安装链路；
  不申请任意模型调用、网络、数据库或通用 action.execute。
- session.open 首次读取、媒体事件提示刷新均不激活、不提问、不发外部动作。普通 UI 操作只是未受信任请求；
  `apply_action` 只接收主程序确认后转交的有界 capsule，保留原 request ID，真实权限和 intent 仍由 Broker/Host 检查。
  不在插件 view 中声明 trusted/source_kind，不把确认按钮升级为授权，也不将 intent 持久化或展示。
- 每会话串行 Host 读/命令，不同会话没有跨 Host I/O 的共同生命周期锁；媒体 ACK 不等待查询。
  活跃分析/未完成操作约 2 秒刷新，无变化不增 view_version；闲置/终结历史不无限轮询。
  错误保留上次内容，连续失败三轮停自动重试；受理结果未知不自动生成新请求。
- state 仅 UI 选择、分页游标、view_version 和待查 operation 指针；Host 保留唯一执行与内容记录。
  发布前预留递增版本，重连读取真实 Host 内容。关闭、重复 open、SDK shutdown 取消并回收自有任务。
- 六区保留原标记/候选/执行 ID、根/父执行链、真实 evidence/revision/time、当前 Final 处理计数、更新时间、
  needs_input、取消提示、已确认/未知副作用、HTTPS 任务链接数据和历史详情。使用现有折叠 tabs，不新增组件 schema。
- 分页每页 10 条，列表事件游标及执行详情游标支持前后导航；最多 1000 页回退信息，不复制正文。
  查看旧页另读有界最新页，继续跟踪新执行；操作在初始状态读之后完成则补读一次，避免刚完成的标记漏刷新。

### 三、测试与自审

- TDD 先确认源注册表/controller/view 缺失失败，再实现。自审针对发布失败、真实 wire values、关闭竞态、
  查询/操作完成时序和分页游标添加失败测试，再修复；不降低 Host 权限或 schema 校验来迁就测试。
- 新专题测试共 45 项，旧打包用例另增加 6 个会议插件参数化分支。
- 聚焦组：`84 passed, 3 skipped in 11.68s`。
- 最终后端：`736 passed, 4 skipped, 1 warning in 111.91s`；首轮 726 passed 仅作过程记录。
- controller / meeting packaging 三轮重复：各 `29 passed`，1.32 / 1.32 / 1.23 秒。
- 前端共享夹具：12 类状态、13 个断言；全部前端测试 `100 passed`（13 文件），typecheck 退出 0。
- Python compileall 退出 0。最初普通沙箱无法写入已有测试缓存，相同命令经审批后通过；没有删除或改写缓存权限。
- 四项 skip 仍为三项 Windows 符号链接条件和一项 WSL Bash 工作区路径条件；warning 为原 Starlette/httpx 弃用提示。
  没有升级依赖；外部模型、Linear 和 Docker 均没有真实调用。

### 四、未上线与下一批

- 打包用 fake Docker 字节 + 真临时 Ed25519 验证标准包结构、签名、摘要与缓存；尚未真实构建/运行 OCI，
  更没有安装到当前用户环境。Task 16 的真实 Docker gate 仍待执行。
- 未迁移生产数据库、重启 API/Worker/前端/Docker、修改课程已安装版本、补发字幕、重做文档或切换旧会议卡片。
- 下一批 D 接通用可信操作区和历史目录、注册唯一 Host 实例与路由、收口旧写 API、移除独立卡片。
  现有 safe_markdown renderer 的链接尚是纯文本，D/E 需验证点击与全部交互；schema/parser 通过不代表浏览器已可用。
- 运行中的网页仍是原入口；不要单独部署本批不完整迁移。计划勾选 A/B/C，按执行技能在批次检查点汇报并暂停。

## 2026-08-31 — 会议助手统一插件化 Batch D

### 一、范围与完成状态

- 依用户“继续下一批”执行 Task 11–14，使用 `executing-plans`，完成源码接入、隔离回归和主代理自审。
  本批 4/4，整体 14/18（约 78%）；未进入 Batch E。没有独立代理审核，没有 Git 初始化或提交。
- 通用 Host 操作区、历史目录、应用组合、旧接口收口、独立卡片替换均已完成；不将源码测试当作运行环境验收。

### 二、实现与自审修正

- Host 描述符定义可用动作、版本/epoch、输入及预填映射。前端不硬编码会议组件；插件按钮只可引导到主程序区域并预填已声明数据，不能自动授权。
- 本地问答/标记经可信控件点击后受理，外部执行另确认 Host 生成的候选、固定团队、预算和有效期。取消、过期、切换会话/插件/epoch 清除临时权限。
- 响应丢失、无法解析或缺少受理标识时保留原请求/预览重试；重试 prepare 不绕过外部确认。nonce 仅在前端内存，intent 不返回浏览器、不写入插件 state 或日志。
- 主程序内部转交完整 `meeting.*` 授权 capsule 到 `apply_action`，修正前批短名称协议差异。普通 plugin-commands 即使伪造视图也不能调用此入口。
  插件返回 accepted 后核对持久 operation；重复确认先读原受理记录，不新增执行。
- Host history 作为通用 catalog 来源合并去重；没有安装、插件视图或文档时仍显示原会议记录。框架/会议服务关闭也可读历史，只保留可信取消，无恢复/执行入口。
- 没有 MediaSession 的旧会议通过只读 legacy 别名查看，不因读取建立映射或开始分析；用户明确准备取消时才建立真实映射，intent/operation 不使用虚拟 owner。
- 历史保留原根/父执行 ID、结果、补充信息、确认/未知副作用、证据/坐标、步骤、工具、事件分页和 HTTPS 结果链接；长内容有明确展示限长，完整原字段仍在只读响应中。
- 主应用注册动作和历史 API、补齐 CORS 头，共享唯一 HostActions、operations 和生产 policy。启动恢复前关闭 gate；关闭时先撤准入再收束 operation/projector/container。
  ready/degraded 才可分析，crashed/quarantined/stopped 被拒；分析确认主动唤醒插件刷新，激活请求 catch-up。
- 原 turn/input/cancel/mark 五类写入口返回 410 `meeting_plugin_migration_required`；旧 GET 仍可读且不要求活动 runtime。
- RoomStudio 移除旧独立会议组件及无引用 CSS。原组件备份于 `.local-backups/meeting-plugin-batch-d-20260831/private-meeting-assistant.tsx`，删除前哈希验证一致。
  未删除或改写会议记录、字幕、课程文档；课程插件及其他插件输入保持隔离，管理/历史链接继续新标签页打开。

### 三、最终验证

- 后端新增 27 项；完整回归 **763 passed、4 skipped、1 warning，124.14 秒**。
- 前端新增 19 项；完整回归 **119 passed，17 文件，3.70 秒**。
- Host bridge / lifecycle / controller 连续三轮均 **40 passed**，6.35 / 7.27 / 7.28 秒。
- `pnpm typecheck` 与 Python compileall 均退出 0；使用项目已安装的 PostCSS 解析最终 CSS 通过。
  补充 CSS 检查最初无法从顶层解析间接依赖，定位 pnpm 内既有包后通过，没有安装/升级依赖。
- RoomStudio DOM：问答、确认、取消、切换课程/会议、折叠/展开、调宽均不调用 track.stop/unpublish/disconnect，不重建字幕 DOM；真正 unmount 正常清理。
- 关闭整个框架仍显示 Host 历史；会话切换后的迟到 Host 响应被丢弃。安全链接拒绝脚本、HTML 和带凭据 URL。
- 四项 skip 仍为 Windows 符号链接及 WSL 路径条件；warning 是既有 Starlette/httpx 弃用提示。
  首轮 749/761 与前端 113/117/118 为过程计数，最终结果以上述完整回归为准。

### 四、未上线与下一批

- 本轮没有生产数据库迁移、服务/Docker 重启、真实 OCI 构建/安装、模型回放、Linear 写入或真实浏览器验收。
- 源码已统一接入；运行环境是否完成切换尚未验证，不能宣称网页已可使用。
- 下一批 E 执行综合故障验证、真实隔离容器和浏览器验收，再按明确授权进行备份、迁移、安装及必要重启。
- 更新计划为 A/B/C/D 完成，按执行技能在批末汇报并暂停。

## 2026-09-01 — 会议助手统一插件化 Batch E：隔离交付验收

### 完成范围

- 完成 Task 15–17，Task 18 的使用/SDK/运维文档已更新；整体 17/18。运行环境切换仍待明确的生产数据库范围、停机、安装与重启授权。
- 新增临时 Host E2E，真实组合 HostActions、capability Broker、插件 controller、Projector 和持久 operation worker；容器/模型/Linear 边界分别替换为可控 fake，所有数据写入唯一临时 SQLite。
- 同会话会议/课程共存，会议安装和绑定不触发分析；显式激活后补齐现有 Final，新 Final/修订继续处理，其他会话不处理。问答在没有新媒体事件时完成并刷新，会议操作不改课程实时笔记或终稿。
- 可信操作覆盖问答、手动标记、执行预览取消/重新确认、needs_input/input/cancel、自然结束和停用后历史；重复确认、回复丢失、插件崩溃、Host 重启、旧 scope 回调和排队停用均不重复或跨会话。
- 修复插件视图漏读引擎实际 `response_text`；修复外部 create 响应丢失后核对成功返回 `planning` 却无人续跑。远端 fake create 精确一次，reconcile 后完成且重启不重放。

### 实际验证

- 综合 E2E/框架/事件/课程组：`17 passed`；前端实际 parser：`11 passed`。
- projector/operations/execution_guard 五个独立命令：每轮 `36 passed`。
- RoomStudio/助手侧栏 DOM 与媒体生命周期五个独立命令：每轮 `14 passed`；助手展开、折叠、切换、确认/取消和调宽不 stop/unpublish/disconnect 音轨。
- 真实 Docker smoke 输出：`signed_package/network_isolated/activation_required/backlog_processed/view_valid/disable_blocks_new_writes/history_preserved/recovery_no_duplicate=true`，`external_create_count=1`。容器为无网络、只读根、无 Host 数据挂载；测试未重置/修复/restart Docker Desktop。
- 临时旧版 SQLite 迁移：`1 passed`，旧表行数/ID/引用不变，重复升级无副作用，`foreign_key_check` 空且 `integrity_check=ok`。
- 最终后端：`769 passed, 4 skipped, 1 warning in 151.09s`。四项 skip 仍为三项 Windows 符号链接条件和一项 WSL Bash 路径条件；warning 仍为既有 Starlette TestClient/httpx 弃用提示。
- 最终前端：17 文件 `119 passed`；`pnpm typecheck` 退出 0。为避免覆盖运行中 3000 端口服务的 `.next`，在本项目唯一临时副本做 production build；成功生成 `/`、`/assistants`、`/plugins`、`/sessions/[sessionId]`。
- 浏览器只读实测当前 `127.0.0.1:3000`：同页助手侧栏可展开/收起，宽度键盘从 384 调至 408；中央字幕输入和悬浮字幕仍在同一 DOM。`/plugins` 当前仅显示已安装课程/诊断插件，会议插件未安装，因此当前实例的会议卡片与 Linear 预览未冒称通过。

### 权限与交付边界

- 没有读取或打印 `.env`、私密字幕、数据库正文或密钥；独立 Harness 明确忽略环境与 dotenv，真实容器也不获得 Host 配置或网络。
- 没有对生产数据库迁移/备份，没有安装会议插件，没有重启 3000/8000/LiveKit 或中断音频，没有调用真实模型或真实 Linear。
- Task 18 运行切换需下一步明确授权：先确认生产数据库路径和一致性备份目标、当前活跃音频/执行、允许停机窗口，再执行增量迁移、标准 inspect/confirm/enable 和精确服务重启。真实字幕分析和真实 Linear 分别需要单独范围授权。

## 2026-09-01 — 会议助手统一插件化 Batch E：Task 18 受控切换（部分完成）

### 已完成的生产切换

- 用户明确授权数据库一致性备份、增量迁移、会议插件标准安装和 3000/8000 精确重启；真实模型回放和真实 Linear 不在授权内。
- 切换前后端 `/health/ready` 为 ready。数据库中有 3 条历史非终态 Session，但运行快照的 room/publisher/track/ASR/FFmpeg/last_event 全为空；没有活动音频。旧 `needs_input` 执行及 pending `model.invoke` 均为历史记录，不当作实时活动。
- 只停止了已核对命令行和端口归属的本项目 Next/uvicorn 启动链；3 个本项目插件容器随后端停止，LiveKit compose 容器和无关 Redis 未停止。
- 使用 SQLite backup API 生成一致性备份 `.local-backups/meeting-plugin-deploy-20260901-final/live_caption.pre-20260831_0027.db`，大小 5,791,744 字节，SHA-256 `C1F1754F4361E2EEE68DD4A3CDC179411CDB9E93A675209873B6562698139936`。备份 head 为 `20260828_0026`，`integrity_check=ok`、外键违规 0，并记录了所有旧表行数基线。
- 第一次普通沙箱迁移因数据库写权限被拒绝；检查确认 head 仍为 `20260828_0026`、三张新表均不存在、完整性正常后，按授权在沙箱外重试同一 `alembic upgrade head`。生产 head 现为 `20260831_0027`；旧表行数与备份无差异，新增 `assistant_action_intents`、`meeting_plugin_operations`、`meeting_plugin_sessions` 均为 0，外键违规 0，完整性正常。没有回滚或覆盖数据库。
- 前端真实工作目录 `pnpm run build` 成功；Next production build 生成 `/`、`/assistants`、`/plugins` 和 `/sessions/[sessionId]`。生产前端已在 3000 启动且 `/plugins` 返回 200。

### 安装失败、安全处置与当前状态

- 内置目录已公开列出 `com.matinier.meeting-assistant` 1.0.0，dynamic build 可用。标准 admin inspection 发起真实 OCI 构建，但本机没有 `python:3.12-alpine`；Docker 从 CloudFront 拉取 layer 时 EOF，后端按公共错误边界返回 `Built-in plugin inspection failed`。inspection 未成功，因此没有 ticket 确认、安装、启用或会议插件容器。
- 正常后端重启恢复了历史 completed-but-open 的课程插件绑定；旧 `com.matinier.course-organizer` 1.0.0 控制器生成 4 次新的 `model.invoke`，DeepSeek 均返回 HTTP 400 并被记录为 failed。发现后立即停止后端；没有成功模型结果或 Linear 写入。原 1.0.1 pending 调用未被改写。
- 为避免继续发生未授权模型请求，后端已用进程级 `PLUGIN_FRAMEWORK_ENABLED=false` 安全模式恢复。readiness 为 ready，插件 runtime disabled、RPC pending 0；8 秒观察期未出现 DeepSeek/chat completion 日志。数据库里的 2 个原安装仍保持 enabled 状态，但当前进程不启动插件，会议插件也尚未安装。前端保持 production 服务。
- Task 18 因验证关卡失败保持未完成，整体仍为 17/18。下一步需要用户授权如何处理那条历史课程 1.0.0 completed-but-open 绑定及 pending 模型工作，并允许重试/预拉 `python:3.12-alpine`。之后才能恢复正常插件框架，重走标准 inspect/permission/confirm/enable 并验证未激活零分析和历史可见。
- 真实会议分析、真实模型成功调用和真实 Linear 创建仍未执行。恢复备份或数据库降级仍需另行明确授权。

## 2026-09-01 — 会议助手统一插件化 Batch E：Task 18 完成

### 恢复、安装与运行态

- 用户授权关闭精确的一条历史课程 `1.0.0` completed-but-open 绑定、预拉基础镜像并继续标准安装。操作前备份
  `.local-backups/meeting-plugin-deploy-20260901-final/live_caption.pre-course-binding-close.db`，大小 6,131,712 字节，
  SHA-256 `FBA7623005B10353C1A4FD0E4A012F7D3FF2084269733A6D90CFD351DDBFFD4E`；只更新目标绑定 1 行，
  其余 pending 历史记录未改写，完整性与外键检查通过。
- `python:3.12-alpine` 预拉成功，digest `sha256:d81968c559557b881aa557ff6d1200acec8e72a2c85fcb4ad1806e8d13e09f0b`。
  正常插件框架恢复后无新增 DeepSeek 请求。会议插件通过标准 admin inspect/confirm/enable 安装：
  `com.matinier.meeting-assistant` 1.0.0，publisher trusted、signature verified、status enabled、runtime ready、crash 0、无 quarantine。
- 安装确认展示并授予 10 项有限能力：`meeting.execution.cancel`、`meeting.execution.input`、
  `meeting.execution.submit`、`meeting.mark.write`、`meeting.operation.query`、`meeting.state.query`、
  `meeting.turn.submit`、`state.get`、`state.put`、`ui.publish`。没有任意网络、数据库或通用外部写权限。

### 真实 UI 缺口与修复

- `/plugins` 实测会议插件已安装、启用并 ready；`/assistants` 目录同时显示课程、会议、诊断三个助手。
  首次会议卡片安全降级为 `Plugin view unavailable`，浏览器控制台无异常。结构化检查定位为 Host 持久化
  `PluginUIViewDocument` 时把两个未提供的可选 `placeholder` 字段重新序列化成 JSON `null`，而前端严格 schema 只接受字符串或缺省。
- 先增加持久化回归测试并确认 RED，再将 `RepositoryUIViewPublisher` 改为
  `document.model_dump(mode="json", exclude_none=True)`。为避免同一 view version 内容冲突，停后端后创建一致性备份
  `.local-backups/meeting-plugin-deploy-20260901-final/live_caption.pre-ui-normalize.db`，大小 6,307,840 字节，
  SHA-256 `CE9D8F3C2564A5BB047CA9C014B0F9EA065A81BE2C4036AFD4CB097C54A07A30`；只规范化 meeting-assistant 的
  1 条派生 UI 视图，移除 2 个空字段，剩余 null 0。完整性与外键检查均通过。
- 正常配置重启后 `/health/ready` 为 ready，插件框架启用、3 个安装均 enabled、RPC pending 0。
  浏览器刷新后会议卡片显示分析状态、私密问答、待办候选、重点与证据、执行进度和历史记录，不再降级。
  页面显示“未启用本次分析”；验证期间没有点击开始、询问、标记、确认、取消或 Linear 动作。

### 最终验证与边界

- 聚焦后端 UI/会议桥接组 `30 passed`；前端 UI schema/会议视图组 `24 passed`。
  最终后端全量 `769 passed, 4 skipped, 2 warnings in 141.72s`；前端 17 文件 `119 passed`；
  TypeScript typecheck 与 Python compileall 退出 0。新增的第二个 warning 是只读 `.pytest_cache` 无法写 nodeids，
  另一个仍是既有 Starlette TestClient/httpx 弃用提示，不影响测试结果。
- 运行数据库中会议插件 `meeting_plugin_sessions=0`、`meeting_plugin_operations=0`、
  `assistant_action_intents=0`、`model.invoke=0`；只有 `meeting.state.query`、`state.get/put`、`ui.publish` 的完成记录。
  已有旧会议记录通过 `host_history` 只读来源可见。数据库 `integrity_check=ok`、外键违规 0。
- 最终后端观察期内 DeepSeek 匹配 0、event delivery failure 0、quarantine 0、ERROR 0。
  先前已记录的 4 次课程 HTTP 400 没有成功结果或 Linear 写入，也没有在恢复后重放。
- Batch E 与整个 18 项计划完成。真实会议字幕分析、真实模型成功调用和真实 Linear 创建仍未执行；
  它们需要用户后续明确启用会话及外部写入授权，不能从本次安装/未激活验收推断为已验证。

## 2026-09-02 — Meeting Agent P1–P3 产品化补强

### 安全与状态链

- Linear 未知 create 的恢复同时核对稳定 action key 和规范化标题；零匹配、多匹配和标题不一致均不直接重试 create。未解析负责人保留会议原称呼，Host UI 显示固定提示。
- 直接 Action Run 追加独立 `action.context_frozen`；Fast/Action ToolSpec 增加执行 profile，Registry 展示和 ToolExecutor 执行层都拒绝 Fast 调用 action-only 工具。
- 引入协议无关 ToolProvider 并由现有 TaskSystem 使用。没有添加 MCP SDK/runtime、RAG、Memory、OTel 或新生产服务；未来 MCP 仅保留为慢 Agent Provider 的扩展缝。
- Execution、ToolCall、Subagent、Grant 和 ExternalActionClaim 使用集中状态迁移与条件更新；新增 crash、数据库锁、429/5xx、unknown、Grant 取消/过期和并发同 action 的节点级测试。

### Trace、评测与演示

- 新增 versioned AgentTrace，从 Final evidence revision 连接到 ContextSnapshot、Fast/Handoff、Action/Subagent、ToolCall/Grant/Claim/reconcile 和叶子终态；导出只含 ID、hash、状态、时间和计数。
- 12-case × 3 trials 生成 36 条 Trace，`scripted_local` 报告为 36/36 PASS；路由、场景、证据和 Trace integrity 均为 100%，未授权写、重复副作用和 unknown create 直接重试均为 0。报告位于 `reports/meeting-agent-product-v1/summary.json`。
- 两个 `local_media_diagnostic` 场景从 `demo_audio.wav` 经过 FFmpeg decoder、fake ASR、TranscriptReconciler、Segment、Meeting State、Context、Fast→Slow、Subagent/Critic、Fake Linear、ToolExecutor 和 recovery 到 terminal。2/2 PASS；每场解码 50 帧/1 秒音频、丢帧 0，Final→evidence→Trace join 与 Trace integrity 均为 100%。丢响应/崩溃场景 create 1 次、reconcile 2 次、无直接 create 重试。
- `meeting_agent_demo.cmd` 一条命令重跑 36 条产品 Trace、2 条全链路 Trace和 5 个演示故事；最终输出 42/42 硬 Gate。重复运行使用临时数据库，不调用真实 Provider。

### 诊断、CI 与验证

- JSON 生命周期日志补充 trace/root execution、execution、phase、tool call、duration、retry 和 error code；`/health/ready` 只读报告 Fast/Action 活跃数、Action 队列、最老等待、unknown/reconciling、恢复 backlog 和 Adapter 可用性。
- 新增 `.github/workflows/agent-ci.yml`，包含 backend 全量 pytest、12-case × 1 Trace smoke、2-case 本地媒体全链路 Trace smoke、Fast/Action profile guard、frontend test/typecheck/build 和报告上传；不需要 secret、Docker、MCP 或真实 Linear。当前工作区不是有效 Git repository，因此只完成本地等价验证，没有宣称远端 CI passing。
- 最终后端全量为 `815 passed, 4 skipped, 1 warning in 109.66s`；四项 skip 仍是三项 Windows 符号链接条件和一项 WSL 路径条件，warning 为既有 Starlette TestClient/httpx 弃用提示。
- 前端为 17 个测试文件、`120 passed`；TypeScript typecheck 与 Next production build 均退出 0。一键离线演示和两个 full-chain 测试均退出 0。
- 首次全量回归人为指定了项目内 `--basetemp`，触发 24 个要求插件构建目录位于 PROJECT_ROOT 外的安全测试失败；改用系统临时目录后全量通过。CI workflow 未使用该错误参数。

### 未完成的人工项

- 真实浏览器标签页音频经 WebRTC/LiveKit 到 Final/AgentTrace 的持续性、两条页面面试演示、真实 DeepSeek/百炼和真实 Linear 专用 Team create/reconcile/cleanup 仍待人工执行。
- 真实 Linear 会产生外部写，只有取得明确授权和专用 Team 后才能执行；其结果必须标记 `real_provider`，不能与 `scripted_local` 或 `local_media_diagnostic` 混算。
