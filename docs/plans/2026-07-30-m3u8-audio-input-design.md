# M3U8 音频直播流接入设计

## 背景

LiveCaption Studio 当前支持浏览器麦克风、屏幕音频和本地媒体文件。为了从真实直播场景验证 Room、LiveKit、ASR、字幕事件和 SQLite 台本链路，需要允许用户在调试控制台中填写一个公网 M3U8 地址，由服务端持续拉取其中的音频。

## 目标

- 在 Room 控制台新增“M3U8 直播流”输入。
- 后端通过 FFmpeg 拉取公开 HLS，解码为 16 kHz 单声道 PCM16。
- 将 PCM 作为 `caption-input-{session_id}` LiveKit 音轨发布。
- 完整复用现有 Worker、百炼 ASR、字幕事件和 SQLite 持久化。
- 支持启动、正常停止、取消、Room 关闭和进程退出清理。
- 能使用当前在线的公开 M3U8 完成一次端到端验收。

## 非目标

- 第一版不播放或转发视频。
- 不支持 Cookie、自定义请求头、登录态、DRM 或付费直播源。
- 不在 API 重启后恢复已经中断的直播。
- 不支持同一 Room 并行运行多个字幕输入。

## 方案决策

采用“后端 FFmpeg 拉流并作为 LiveKit 参与者发布音轨”方案。

不采用浏览器 HLS.js/Web Audio 方案，因为公开 M3U8 经常受到 CORS、自动播放和浏览器后台限速影响。不采用 FFmpeg 直连 ASR 方案，因为它会绕开现有 Room、参与者、音轨和 Worker 生命周期。

## 架构

### 前端

`RoomStudio` 增加 `hls` 输入类型、URL 输入框和简短使用提示。开始按钮调用 HLS 启动 API，停止按钮调用 HLS 停止 API。前端不直接请求 M3U8，也不创建本地 LiveKit 音轨。

界面复用现有语言选择、字幕区域、Room 参与者和历史字幕任务区域。输入状态显示为连接中、直播中、停止中、已失败或已完成。

### API

新增接口：

- `POST /api/rooms/{room_id}/hls-inputs`
  - 请求：`{"url": "...", "language": "zh-CN"}`
  - 原子完成 Room 校验、活动任务冲突检查、`source_type=hls` Session 创建和后台任务启动。
  - 返回现有 `SessionResponse`。
- `POST /api/rooms/{room_id}/hls-inputs/{session_id}/stop`
  - 请求正常停止拉流。
  - 返回当前 `SessionResponse`；前端继续轮询到终态。

现有取消接口仍表示强制取消；关闭 Room 时先停止关联 HLS 任务，再按 Room 关闭语义处理 Session。

### HLSInputManager

API 的 `app.state` 持有一个进程内 `HLSInputManager`，按 `session_id` 保存任务、停止原因和发布器。它负责：

- 防止同一 Session 重复启动。
- 创建和取消异步 HLS 输入任务。
- 在用户停止时做正常收尾。
- 在 FFmpeg/网络异常时记录明确失败原因。
- 在 API shutdown 时终止全部 FFmpeg 子进程并断开 LiveKit。
- API startup 时将无法恢复的非终态 HLS Session 标记为 `hls_process_lost`。

### HLS 音频发布器

新增面向 URL 的 FFmpeg 解码器和 LiveKit 发布器：

- FFmpeg 输入为经校验的 HTTP/HTTPS M3U8。
- 输出为 16 kHz、单声道、PCM16、20 ms 音频帧。
- 通过实时读取/节流避免把初始 HLS 缓冲突发发送给 LiveKit。
- LiveKit 参与者身份为 `hls-{session_id}`。
- Track 名称为 `caption-input-{session_id}`。

现有 Worker 已按 Track 名称解析 Session，因此不建立第二套 ASR 链路。

## 数据流

```text
公开 M3U8
  -> 后端 FFmpeg 解码
  -> LiveKit caption-input 音轨
  -> 现有 Worker
  -> 百炼 ASR
  -> 字幕事件与 SQLite
  -> Room 控制台实时显示
```

正常停止时，HLS 发布器终止 FFmpeg、卸载音轨并离开 Room；Worker 消费到音轨结束后执行现有 finalizing/completed 流程。

## 数据与日志

- Session 使用 `source_type=hls`。
- `source_name` 只保存去除用户名、密码和查询参数后的显示 URL，并限制到现有字段长度。
- 完整 URL 只存在于活动任务内存中，不写数据库或普通日志。
- 不新增数据库表；现有 Session、Segment 和 ProcessedScript 模型继续使用。

## URL 与安全边界

- 只接受 `http` 和 `https`。
- 拒绝带用户名或密码的 URL。
- 拒绝 localhost、环回、链路本地、私有、保留和不可解析地址。
- 第一版仅面向本机调试环境；URL 校验降低 SSRF 风险，但不能替代生产环境的出口代理或网络隔离。
- FFmpeg 子进程禁止弹出窗口，并设置启动、读取和关闭超时。

## 生命周期与错误处理

- FFmpeg 启动或首帧超过 15 秒：`hls_start_timeout`。
- 播放列表不可达、格式错误或持续断流：`hls_stream_error`。
- M3U8 无音频：`hls_no_audio`。
- LiveKit 连接或发布失败：沿用 LiveKit 错误分类。
- 用户点击停止：正常完成并保留已生成字幕。
- 用户强制取消或关闭 Room：Session 为 `cancelled`。
- API 异常退出后再次启动：遗留非终态 HLS Session 为 `failed/hls_process_lost`。

Manager 与 Worker 对终态写入采用幂等检查：输入端异常可以先标记失败，Worker 发现 Session 已是终态时不再覆盖为 completed。

## 精简测试策略

测试只覆盖主链路与高风险边界，不为同一行为堆叠重复参数组合。

1. URL 校验单元测试：一个合法公网 URL，以及私网/非法协议的代表性拒绝用例。
2. HLS 解码与管理器测试：覆盖首帧、正常停止、异常退出和子进程回收，每类行为一个核心用例。
3. API 生命周期测试：覆盖启动、同 Room 冲突和停止；避免为 Repository 已覆盖的状态机重复造测试。
4. 一个确定性的本地 HLS 集成测试，使用短音频验证 PCM 帧与 LiveKit 发布契约。
5. 运行一次现有后端测试套件和一次前端类型/构建检查。
6. 只进行一次公网端到端手工验收；公网 URL 不写入自动化测试。

## 公网验收标准

1. 在控制台填写一个当时可访问、包含音频的公开 M3U8。
2. 启动后 30 秒内进入识别状态并出现字幕，或得到明确的“未检测到语音”结果。
3. Room 参与者中可看到 HLS 输入端和 Worker。
4. 点击停止后 5 秒内释放 FFmpeg、音轨和 HLS 参与者。
5. 已生成字幕保存在 SQLite，刷新后仍可查看。
6. 同一 Room 随后可以启动另一个输入。

