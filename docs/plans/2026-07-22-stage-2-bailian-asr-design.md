# 阶段二：百炼实时语音识别设计

## 目标与边界

阶段二把 Worker 已收到的 16 kHz、单声道、PCM16 LiveKit 音频帧送入阿里云百炼 `fun-asr-realtime`，把服务端回调归一化为项目内 `ASREvent`，并在控制台输出临时与最终识别结果及会话指标。

本阶段不实现 DeepSeek、字幕修订、字幕 UI、双语翻译、网络流或字幕导出。由于当前工作区没有百炼凭据，本轮完成全部本地实现、协议模拟和回归测试；真实云端识别另行验收。

## 方案选择

采用异步原生 WebSocket 适配器，而不是把 DashScope SDK 回调直接暴露给 Worker。

- WebSocket 消息与任务书给出的 `run-task`、二进制音频、`finish-task` 生命周期一一对应，便于精确测试。
- Provider 层隔离厂商字段；Worker 和后续字幕层只依赖稳定的 `ASREvent`。
- 连接器可注入，测试中不访问百炼即可覆盖认证失败、超时、协议错误、队列溢出和清理。
- 直接依赖项目环境已有的 `websockets`，并在 `pyproject.toml` 中显式声明，避免依赖偶然来自传递依赖。

## 模块结构

`backend/app/transcription/` 新增以下模块：

- `models.py`：`ASREventType`、`ASREvent`、`TranscriptionMetrics`。
- `errors.py`：配置、认证、超时、协议、服务端和背压异常。
- `provider.py`：`SpeechRecognitionProvider` 抽象接口。
- `bailian.py`：百炼 WebSocket 生命周期与服务端事件归一化。
- `chunker.py`：把 20 ms PCM 帧聚合为默认 100 ms（3200 bytes）块，并在结束时发送剩余数据。
- `session.py`：有界队列、发送/接收任务、指标、日志和清理。

Worker 仍负责 LiveKit Track 过滤和帧统计；识别职责委托给 `TranscriptionSession`。

## 数据流

```text
LiveKit AudioFrame (20 ms)
  -> AudioChunker (默认聚合为 100 ms)
  -> asyncio.Queue(maxsize=20)
  -> BailianSpeechRecognitionProvider.send_audio(binary)
  -> 百炼 result-generated
  -> ASREvent(partial_result/final_result)
  -> 控制台 JSON 日志与 TranscriptionMetrics
```

## Provider 契约

Provider 提供异步 `start()`、`send_audio()`、`finish()`、`events()` 和 `aclose()`：

1. `start()` 校验 Key/Workspace/Region，建立 WebSocket，发送 `run-task`，等待 `task-started`。
2. `send_audio()` 只在任务开始后发送二进制 PCM。
3. 后台接收循环把 `result-generated` 的 `sentence_end=false/true` 映射为 `partial_result/final_result`。
4. `finish()` 发送 `finish-task`，有限等待 `task-finished`，随后正常关闭。
5. `task-failed`、认证错误、协议错误和超时分别转换为项目异常与 `stream_error`，不把 SDK/厂商回调对象泄漏到上层。

标准事件字段包括 `provider_event_id`、`segment_id`、`text`、`is_final`、`begin_time_ms`、`end_time_ms`、`confidence`、`raw_payload`、`received_at_ms`。生命周期事件为 `stream_started`、`partial_result`、`final_result`、`stream_completed`、`stream_error`。

## 音频、背压与重试

- 输入只接受 16 kHz、单声道、PCM16；Worker 的 `rtc.AudioStream` 已完成格式统一。
- 默认块长 100 ms，等于 1600 samples / 3200 bytes；支持配置但必须为正数。
- 队列默认最多 20 块，约 2 秒音频。队列满时抛出 `ASRBackpressureError`，绝不静默丢帧或无限缓存。
- 只允许在尚未发送任何音频时，对连接/启动失败重建 Provider 并重试一次。
- 一旦发送过音频，网络失败即终止本次识别，不自动重放，以免产生重复字幕。

## 超时、取消与清理

- 连接/启动和结束等待均使用有限超时。
- Worker Track 结束时先冲刷尾块，再结束 Provider。
- 任意异常或任务取消都会取消发送/接收任务、关闭 Provider 和 `AudioStream`。
- `aclose()` 幂等；重复运行不残留后台任务或 WebSocket。

## 日志与指标

控制台输出 `asr_stream_started`、`asr_partial_result`、`asr_final_result`、`asr_stream_completed`、`asr_stream_error` 和 `asr_summary`。摘要至少包含：最终结果数、首次临时结果延迟、平均最终结果延迟、Provider 错误数、已发送音频块/字节数。

`raw_payload` 只在 DEBUG 日志中输出；任何日志均不得包含 API Key 或 Authorization Header。

## 本地验收策略

- 单元测试事件模型、PCM 聚合和尾块冲刷。
- Fake WebSocket 验证请求头、`run-task` / 二进制音频 / `finish-task` 顺序与服务端事件映射。
- 覆盖认证失败、启动超时、`task-failed`、队列溢出、启动前一次重试、发送后不重试、取消和幂等关闭。
- Fake Provider 验证 Worker 把每个 LiveKit PCM 帧交给识别会话，同时保留阶段一帧统计和流关闭行为。
- 执行后端全量测试、前端类型检查与生产构建，确保阶段零和阶段一无回归。

## 延后验收

真实百炼验收需要用户在本地 `.env` 中配置 `DASHSCOPE_API_KEY`、`DASHSCOPE_WORKSPACE_ID` 和区域。届时用固定回放文件验证真实 Partial/Final、延迟、错误计数和连续两次干净退出。本轮不伪造该通过结论。
