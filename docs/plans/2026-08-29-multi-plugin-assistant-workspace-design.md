# 多插件助手工作区与内置插件接入设计

**日期：** 2026-08-29  
**状态：** 已确认

## 目标

补齐两个实际接入缺口：把课程整理插件通过正式签名、权限确认、安装和启用链路永久接入当前开发实例；把 Room 右栏中隐蔽的通用插件面改造成能承载多个场景助手的独立工作区。

## 已确认决策

- 新增一级路由 `/assistants`。
- 默认跟随当前 Room，同时允许切换历史字幕 Session。
- 多插件使用“左侧助手目录、右侧当前助手详情”布局。
- Room 右栏只保留紧凑状态卡和工作区入口。
- 项目自带课程插件显示在 `/plugins` 的“项目内置插件”目录中。
- 内置插件仍需管理员令牌、签名检查、发布者指纹和七项权限逐项确认；不静默安装。
- 前端只渲染通用声明式 UI，不为课程、会议、直播或未来插件增加业务专属组件。

## 页面架构

`AssistantWorkspace` 由三个区域组成：

1. 顶部 Session 选择器。Room 入口使用 `/assistants?session=<legacy-session-id>`；直接访问时优先选择运行中的 Session，否则选择最近历史 Session。选择写回 URL，并可带 `plugin=<plugin-id>` 深链接。
2. 左侧助手目录。目录由已安装插件、当前 MediaSession 的 view 和历史插件文档合并，保留 ready、waiting、disabled、degraded、crashed、quarantined，以及“只有历史文档”的插件。
3. 右侧当前助手详情。上半部分渲染该插件发布的 panel/overlay view，下半部分只列出该插件的可信文档 identity、语言和不可变版本。

Room 中的 `PluginSurface` 被替换为 `RoomAssistantLauncher`：只显示已连接助手数量、异常数量、最近更新时间和“打开助手工作区”。

## 前端组件与状态

新增：

- `AssistantWorkspace`
- `AssistantSessionSelector`
- `AssistantCatalog`
- `AssistantDetail`
- `RoomAssistantLauncher`
- `useMediaAssistantWorkspace`

复用并扩展：

- `PluginComponent`
- `PluginDocumentShelf`
- 现有插件 UI schema parser
- 插件 API client

所有 view 及其字段值按 `mediaSessionId + pluginId + surface + viewId` 隔离。切换 Session 时用 generation/disposed guard 丢弃旧异步结果。view 每 2 秒刷新，文档每 4 秒刷新；一个插件的 schema、文档或命令失败不能隐藏其他插件。

## 数据流

```text
legacy Session selection
  -> resolveMediaSession
  -> parallel list installed plugins / session views / session documents
  -> merge assistant catalog
  -> select plugin from URL or first relevant entry
  -> render only selected plugin views and documents
```

命令继续携带精确 plugin version、session scope、surface、view ID、expected view version、action ID 和按 view 隔离的 values。stale view 只触发一次刷新，不自动重放可能产生副作用的命令。

## 内置插件目录与签名

后端增加只读 `BuiltinPluginRegistry`，仅包含服务端静态白名单。首个条目为 `com.matinier.course-organizer`，Registry 决定源码目录、打包器、展示元数据和精确权限；客户端不能提交路径或打包命令。

新增 HTTP：

```text
GET  /api/plugins/builtins
POST /api/plugins/builtins/{plugin_id}/packages:inspect
```

inspect 需要管理员令牌。服务端在工作线程中生成或复用实例级 Ed25519 私钥，在 plugin ID 锁下构建并缓存签名包，然后调用现有 PackageStore 的检查路径。后续安装和启用继续复用：

```text
POST /api/plugins/installations
POST /api/plugins/{plugin_id}/enable
```

开发环境允许动态构建签名；生产环境默认关闭，必须使用预签名发布包。私钥和构建缓存只保存在仓库构建上下文之外的本机应用数据目录；PackageStore 验证后才把安装包复制进 `DATA_DIR/plugins/packages`。这保持现有打包器“私钥和输出不得位于仓库内”的保护。私钥不进入浏览器、插件包、容器、普通日志或 API 响应。

## 课程插件接入

当前开发数据库从 `20260812_0022` 迁移到 `20260828_0026`。在根 `.env` 配置随机 `PLUGIN_ADMIN_TOKEN`，启动 Docker/API/Frontend，通过内置目录正式检查、确认七项权限、安装并启用课程插件。安装产物和信任状态保存在开发数据目录，不再使用 smoke 临时目录。

课程插件权限必须精确为：

```text
state.get
state.put
ui.publish
model.invoke
delivery.prepare
delivery.query
document.publish
```

任何额外权限或 `network.fetch` 都阻止内置条目的确认。

## 失败处理

- Session 缺失：从 URL 清除并回退最近 Session。
- 插件未安装：显示内置安装入口。
- disabled：显示管理入口，不创建运行 binding。
- Docker/runtime 失败：显示对应 runtime 状态，不影响字幕主链。
- 尚无 view：显示等待 Final/Session 事件。
- 非法 view：只安全降级该 view。
- 文档失败：只影响文档区域。
- 安装成功但启用失败：保留 disabled 安装并显示错误，不伪装成功。
- 内置构建失败：清理 staging，保留签名密钥和上一份有效缓存，允许显式重试。

## 验收

- 后端测试覆盖 Registry 白名单、路径拒绝、密钥不泄露、构建缓存/单飞、精确权限和现有 PackageStore 安全边界。
- 前端测试覆盖目录合并、URL 选择、每 view 状态隔离、Session 切换竞态和全部空/错状态。
- 使用课程插件和诊断插件同时绑定同一 Session，证明多插件切换、命令状态和文档过滤互不污染。
- 迁移并配置当前开发实例，永久安装并启用课程插件。
- 在实际浏览器验证 `/assistants`、Room 入口、实时笔记、手动 interim、终态 complete 和可信下载。

## 明确不做

- 不让插件提供 HTML、JavaScript、CSS、iframe 或独立前端 bundle。
- 不在启动时静默安装/启用插件。
- 不把管理员令牌、签名私钥或模型密钥下发给插件。
- 不增加网页 DOM、播放器 seek、OCR、外部研究、多租户或横向扩展能力。
