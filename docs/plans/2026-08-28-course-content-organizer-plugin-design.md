# 课程内容整理插件设计

日期：2026-08-28  
状态：已确认  
插件 ID：`com.matinier.course-organizer`

## 1. 目标

在通用媒体助手插件框架上实现第一个场景插件，服务于浏览器标签页或网页播放课程的场景。插件不接触
原始音频、模型密钥、数据库或宿主文件，只消费通用 `MediaEvent`、调用受控 Host Capability，并通过
声明式 UI 和版本化文档向用户交付结果。

插件输出一份包含两部分的课程文档：

1. 实时知识笔记：课程播放期间按有界窗口生成较为松散的笔记，判断内容是否属于课程知识，并保存
   对应视频时间坐标和字幕证据。
2. 课程知识点整理：只在用户手动触发或 LiveKit 会话结束后生成，使用不可变 Frozen Package 的 Final
   原文、Final 译文、时间线和证据索引作为更准确的输入，形成分层知识体系。

文档支持原文语言、字幕译文语言或自定义目标语言，并可导出 Markdown 与 JSON。

## 2. 设计原则与范围

- 播放期间只做实时笔记，不持续运行最终整理。
- 用户可在课程进行中生成阶段版；实时笔记继续运行，阶段版不可变。
- LiveKit 结束后自动生成完整版本，保留所有阶段版。
- 最终知识结论必须闭合到 Frozen Package evidence；没有来源的结论不得发布。
- 宿主只增加可复用的模型、交付快照和文档能力，不包含课程分类规则。
- 插件继续运行在无网络、无宿主挂载、只读根文件系统的 Docker 沙箱内。
- 首版不控制被捕获的外部网页播放器，不进行网页 DOM 注入，也不提供任意 HTML、脚本或 URL。
- 首版不依赖清稿、精译或其他可选 Artifact；最终整理只依赖 Frozen Package。

## 3. 总体架构

采用“薄宿主能力 + 独立课程插件”方案。

### 3.1 插件订阅

插件订阅：

- `transcript.final`
- `translation.final`
- `session.completed`
- 为失败或取消后的有界收口保留必要的 `session.failed` / `session.cancelled`

插件用 `session_id + sequence` 消费事件，用 Host durable ack 恢复游标；`logical_id + revision` 用于处理
字幕修订和幂等合并。

### 3.2 通用 Host Capability 扩展

宿主新增或正式启用以下通用能力：

| Capability | Effect | 用途 |
| --- | --- | --- |
| `model.invoke` | read | 通过宿主配置的模型执行有界、可审计的结构化推理，不向容器暴露凭据 |
| `delivery.prepare` | local_write | 将当前已落库 Final 冻结为不可变 Package；支持阶段快照和终态快照 |
| `delivery.query` | read | 按 Package ID、文档种类和 cursor 分页读取内容、时间线与 evidence |
| `document.publish` | local_write | 发布经过 Host Schema 校验的版本化插件文档 |

宿主同时提供只读文档查询、版本历史以及 Markdown/JSON 导出 API。导出链接由可信前端根据宿主文档
记录生成，插件不能提供任意本地路径或下载 URL。

`delivery.prepare` 必须支持幂等键。终态自动生成按 Session、Final watermark 和语言去重；手动生成按
用户命令 ID 去重。Package 不原地修改，后续 Final 产生新 Package 版本。

### 3.3 组件边界

- Media Event Projector：继续异步投影 Final 和会话终态，不进入字幕热路径。
- Delivery Adapter：复用现有 `PackageBuilder`，负责 Package 创建/查找、分页和权限收口。
- Model Adapter：把 `model.invoke` 映射到宿主已有模型 Provider，实施输入分类、token、超时、输出和审计
  限制。
- Document Store：保存插件文档身份、版本、语言、触发方式、Package 绑定、结构化正文和状态。
- Course Organizer Plugin：实现窗口聚合、规则筛选、模型分类、知识合并、语言本地化和声明式视图。
- Trusted Frontend：渲染插件视图、宿主文档版本与可信下载按钮，不执行插件代码。

## 4. 数据流

### 4.1 实时知识笔记

```text
transcript.final / translation.final
  → 按 sequence 去重和修订
  → 累积 60–90 秒或达到文本阈值
  → 确定性预筛选与窗口合并
  → model.invoke 分类并生成松散笔记
  → Schema/evidence 校验
  → CAS 保存增量插件状态
  → ui.publish 更新时间线视图
```

实时批次失败不能阻塞 Event ack 之前的持久处理。插件只有在批次已安全记录或明确进入可重试状态后才
确认 sequence。状态不保存完整音频、模型密钥或无限增长的原始字幕副本。

### 4.2 手动阶段版

```text
用户点击“生成最终整理”
  → command.execute
  → delivery.prepare 冻结当前 Final
  → delivery.query 分页读取 Package
  → 分块提取知识点（Map）
  → 跨块归并、去重和分层（Reduce）
  → document.publish 创建阶段版
  → 实时笔记继续消费后续事件
```

同一时刻一个 MediaSession/语言只允许一个最终生成任务。重复命令返回现有任务状态，不启动并行重复
调用。

### 4.3 LiveKit 结束后的完整版本

收到 `session.completed` 后，插件先刷新末尾尚未处理的实时窗口，再请求终态 Package。最终知识整理只
读取 Package，不依赖实时笔记是否全部成功。完整文档发布后保留既有阶段版。失败或取消会在存在 Final
字幕时允许生成“非完整终态版本”，并明确记录终态原因，不冒充完整课程。

## 5. 实时筛选规则

实时筛选采用“确定性规则预筛选 + 模型语义分类”，避免纯关键词方案，也避免把所有判断交给模型。

1. 合并同一语义窗口中的重复、口吃和更高 revision 字幕。
2. 去除纯静音占位、无意义语气词和无法形成语义的短碎片。
3. 寒暄、平台操作、广告和课程管理说明标记为“过渡/背景”。
4. 定义、因果、比较、步骤、公式、结论或教师强调内容进入“知识点候选”。
5. 演示、故事、题目和情境进入“案例”，并与候选知识点建立关系。
6. 低置信度、语义残缺或前后矛盾内容进入“待确认”。
7. 模型输出必须引用当前窗口内的 Segment 和时间范围；越界引用或无 evidence 输出被拒绝。

实时笔记不追求过早构建完整层级。每条笔记至少包含：稳定 ID、类型、标题、正文、开始/结束毫秒、
Segment/evidence ID、置信状态、语言、来源窗口和关联笔记 ID。

可发布类型为：

- `knowledge_candidate`
- `example`
- `background`
- `transition`
- `needs_confirmation`

不属于知识点的有价值内容仍可追溯，但只有通过最终证据复核的候选内容能进入确定知识分类。

## 6. 最终文档模型

最终文档固定包含两个顶级部分。

### 6.1 第一部分：实时知识笔记

按视频时间排序，保留实时分类、标题、正文、置信状态、语言和坐标。阶段版只包含 Package watermark
之前的笔记；完整版包含终态 Package 范围内的笔记。

### 6.2 第二部分：课程知识点整理

固定一级分类：

1. 核心概念
2. 原理/机制
3. 方法/步骤
4. 案例
5. 公式/数据
6. 易错点与待确认问题

每个一级分类下面由模型生成课程主题和子主题。知识项包含标题、结论、解释、相关知识、案例关系、
证据时间范围、Package item、Segment/evidence ID 以及确认状态。语义重复项合并，但所有有效坐标均保留。
`needs_confirmation` 不得改写为确定事实。

### 6.3 文档身份与版本

文档稳定身份至少由 `plugin_id + media_session_id + document_kind + language` 构成。每次成功发布追加新
版本，不覆盖旧记录。版本元数据包括：

- `trigger=manual|session_completed|session_failed|session_cancelled`
- `completeness=interim|complete|partial_terminal`
- Package ID、版本、content hash 和 Final watermark
- 输出语言和来源语言
- 模型/provider 标识（不含凭据）
- 状态、创建时间、父版本和生成统计

## 7. 文档语言

- 默认优先使用当前字幕译文语言；没有译文时使用原文语言。
- 用户可选择原文语言、字幕译文语言或受 Host 校验的自定义目标语言。
- 输出正文使用所选语言；evidence ID 和坐标不随语言变化。
- 中途切换语言时，插件分块本地化已有实时笔记，全部成功后原子替换显示，避免单一文档混用语言。
- 切换语言不覆盖既有最终文档；不同语言拥有独立版本历史。
- 自定义语言生成失败时保留当前语言视图，不发布半翻译文档。

## 8. 模型使用与有界处理

实时路径使用短窗口分类和笔记生成；最终路径使用分块 Map/Reduce：

1. Package 按文档 item 和时间范围分页。
2. 每块提取候选知识、类型、关系和 evidence。
3. Host/插件校验引用只能指向该块输入。
4. Reduce 阶段按固定一级分类合并主题、重复项和跨块关系。
5. 最终再次执行 evidence closure 和 Schema 校验。

模型输出使用闭合 JSON Schema。首次不合法时允许一次有界修复调用；仍不合法则任务失败，不发布不完整
版本。单次 prompt、输出 token、超时、总调用数和重试数都必须有配置上限。

## 9. UI 与交互

插件 panel 提供：

- 顶部：运行状态、实时处理进度、文档语言选择、“生成最终整理”按钮。
- “实时笔记”：按时间排列的卡片、分类 badge、置信状态和 `HH:MM:SS.mmm` 坐标。
- “最终整理”：两部分文档、生成/重试状态及当前版本信息。
- “历史版本”：阶段版/完整版、语言、触发方式、Package hash、创建时间和版本选择。
- 可信宿主区域：Markdown/JSON 下载按钮。

外部捕获标签页不向宿主暴露播放器控制，因此首版坐标用于阅读、复制和导出，不承诺点击后控制外部
网页跳转。未来受控播放器可直接复用 `media_time_ms` 增加 seek adapter。

## 10. 导出

Markdown 使用确定性结构：

```markdown
# 课程内容整理

## 第一部分：实时知识笔记
### [00:12:35.200] 知识点候选：标题
正文……

## 第二部分：课程知识点整理
### 核心概念
#### 主题
知识点……
```

JSON 保存 Schema 版本、插件/MediaSession、文档身份与版本、语言、触发方式、Package 绑定、全部笔记、
知识层级、毫秒坐标、Segment/evidence ID 和生成统计。Markdown 和 JSON 必须来自同一已发布结构化文档，
不得各自重新调用模型。

## 11. 故障处理

- 模型不可用：实时部分退化为规则笔记并标记 degraded；最终任务进入 waiting_retry，执行有界自动退避，
  同时允许用户手动重试。
- 没有 Final 字幕：拒绝创建空 Package 或空文档，UI 显示稳定原因。
- 译文缺失：回退原文；自定义语言通过模型本地化并明确标记。
- 非法模型输出：最多一次修复，失败后保留旧版本。
- 插件崩溃：旧 scope 失效，从 durable cursor 和 CAS state 恢复；已发布宿主文档保持不变。
- 终态竞争：按 Package content hash、language 和 trigger idempotency key 去重。
- 课程过长：分页读取、分块处理、分层归并；任何 RPC、状态、视图或文档大小不得绕过 Host 上限。
- 部分终态：失败/取消但有 Final 时可生成明确标记的 partial terminal 版本；没有 Final 时只报告原因。

## 12. 安全与权限

插件 manifest 只声明实际需要的订阅、命令和能力。模型密钥、Provider 客户端、Package 数据库访问和
文档文件渲染全部留在 Host。插件不得申请 `network.fetch`，首版不需要直接网络。所有 capability 调用
重新校验 plugin ID/version、generation、MediaSession scope、安装权限、大小和调用预算，并写入审计。

插件容器保持：`--network none`、只读根、无 Linux capability、`no-new-privileges`、无项目/数据库/
Docker socket/Secret 挂载，以及 CPU、内存、PID、tmpfs 上限。

## 13. 测试与验收

### 13.1 测试层次

1. 纯规则：重复、寒暄、广告、操作提示、定义、因果、步骤、公式、案例、残句、低置信度及多语言。
2. Host 合同：新 capability 的权限、scope、分页、上限、幂等、审计、错误净化和越权拒绝。
3. 插件协议：Fake Host/Fake Model 下的批处理、CAS、ack、语言切换、Schema 修复、命令和恢复。
4. 端到端：运行中手动阶段版、实时继续、终态自动完整版、版本/语言/Package/evidence 闭合。
5. 真实 Docker：签名安装、隔离、模型密钥不泄漏、强杀恢复及字幕/会议助手兼容。
6. 前端：声明式视图、状态、语言、命令、历史版本、可信下载和无障碍反馈。

### 13.2 验收标准

- 达到时间或文本阈值后产生带分类和视频坐标的实时笔记。
- 非知识内容不混入确定知识点；有价值背景、案例和待确认内容仍可追溯。
- 运行中手动生成阶段版不停止实时笔记。
- LiveKit 结束后只自动生成一次绑定完整 Package 的完整版本。
- 文档严格包含实时知识笔记和课程知识点整理两部分。
- 所有确定知识点具有 Frozen Package evidence closure。
- 原文、译文和自定义语言可用，语言切换不覆盖旧版本。
- Markdown/JSON 的版本、内容和坐标一致。
- 模型故障时规则降级可用，最终任务可恢复重试。
- 后端、前端、迁移、SDK schema、插件端到端和真实 Docker 冒烟全部通过。

## 14. 明确不做

- 不直接抓取网页文字、幻灯片 DOM 或视频画面 OCR。
- 不控制外部网页播放器跳转。
- 不生成 DOCX/PDF。
- 不把课程插件逻辑写入 FastAPI 核心业务。
- 不依赖已批准 Artifact 或外部 Web 知识补充。
- 不把模型 Provider 或密钥放入插件镜像。
- 不实现多租户、分布式队列或远程插件市场。
