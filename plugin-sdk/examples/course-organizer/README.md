# 课程内容整理插件

`com.matinier.course-organizer` 面向浏览器标签页课程、直播课程和普通课程音频。播放期间，它从
Final 字幕形成带视频坐标的实时知识笔记；用户手动触发时发布 interim 文档，媒体会话结束后从
Frozen Package 自动发布 evidence-closed 的 complete 或 partial-terminal 文档。

默认文档语言取首个已观测译文语言，没有译文时回退源语言；显式选择中文、英文或源语言会保留独立
版本。“源语言”在收到源 Final 后解析为实际语言码。标签页坐标来自捕获音频/字幕时间轴，不读取网页
DOM、不控制播放器或 seek，也不保证与外部视频 currentTime 逐帧一致。模型暂不可用时实时笔记使用
规则结果并有界重试，最终整理保持 waiting-retry，等待自动或用户重试。

插件没有直接网络权限，也不会收到数据库地址、模型密钥、Docker socket 或宿主目录。模型调用、
Package 分页、状态、UI 和文档发布都通过 Host capability 完成。最终 Markdown/JSON 由同一结构
确定性渲染，历史版本和下载链接由可信前端区域提供。

1.0.2 将 Package 的读取分页与模型批次分开：map/reduce 每次最多 4 条、8,000 个序列化字符，
输出预算为 4,096 tokens（仍受 Host 上限约束）。批次合并时重新命名局部 ID，避免不同批次使用相同
模型 ID；保留各批次证据和层级分类，不再把整个课程塞进一次 reduce。单条过大或超过总量上限时
明确失败，不静默截掉课程后半部分。实时窗口最多请求 3 条简短笔记。

终态失败后的“重试”保留 complete/partial-terminal 语义。升级迁移保留笔记和历史，但给未完成的
终态任务分配新版本任务标识，避免复用旧版本的 capability 幂等键。现有 Session 默认仍固定原插件
版本；运维显式切换版本前必须备份，并保留旧绑定和状态，不覆盖历史成果。

1.0.3 修复实时笔记与成果包的来源映射：`logical_id` 是事件身份，不是原文片段 ID。笔记使用 Host
提供的原文证据 ID 和译文 `source_segment_ids`，支持一条译文对应多个原文片段；已对齐译文优先，
未对齐译文不猜测来源，而由原文供模型生成指定语言的笔记。迁移检测到旧的事件别名笔记时，在新版本
状态中从持久化事件重建；旧状态及历史文档保留。课程 E2E 和 Docker smoke 均强制验证第一部分非空。

`plugin.json` 中的 image digest 是占位值。不要直接压缩本目录。使用仓库外部的 Ed25519 私钥，
并将输出写到仓库之外：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\package_course_organizer_plugin.py `
  --private-key C:\safe\course-plugin-development-key.pem `
  --output C:\safe\matinier-course-organizer-1.0.3.plugin.zip
```

构建上下文是仓库根目录，但 Dockerfile 只复制公共 Python runtime、插件入口和
`course_organizer` 包；镜像没有 pip 或系统包下载步骤，并以非特权用户运行。
