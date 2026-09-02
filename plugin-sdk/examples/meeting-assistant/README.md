# 私人会议助手标准插件

`com.matinier.meeting-assistant` · `1.0.0` · Python 标准库 + Matinier 插件 SDK。

本目录及主程序已完成会议助手统一插件化计划 Batch C/D/E 的源码、Host 与通用侧栏接入。独立会议卡片已从源码移除，
通用 Host 操作区、旧写 API 收口和只读历史目录已接通。真实临时 Docker 构建、网络/挂载隔离、崩溃恢复、
假模型/假 Linear 幂等、前端解析、DOM 生命周期与本地浏览器侧栏均已验收。**当前运行中的本地实例尚未安装本插件，
也未迁移/重启；真实模型和真实 Linear 未获本批授权，不能把隔离验收等同于已上线或外部服务已验证。**

## 权限与职责

容器通过 stdin/stdout JSON-RPC 与 Host 通信，以非 root 运行，只复制 SDK 和本插件源码。
使用标准签名 ZIP、Publisher 信任、管理员 inspect/confirm、隔离 Supervisor；没有内置免授权路径。
manifest 只声明七种 `meeting.*` 能力及 `state.get/state.put/ui.publish`，不申请网络、模型、通用外部写入或数据库权限。
会议模型、数据和任务执行仍由 Host 原会议引擎处理。

安装、启用插件、显示侧栏和开启本次分析是不同动作。`session.open`、字幕事件和刷新都不会开启分析、发起问答或创建外部任务。
分析启停必须由主程序确认；停止分析不会取消已经单独授权的交互。取消执行不会删除已创建的 Linear 任务。

## 协议与 Host 接入

实际 SDK 方法是 `plugin.initialize`、`plugin.heartbeat`、`session.open/close`、`event.batch`、
`command.execute`、`plugin.shutdown`。设计中的 media.events/ui.command 是语义名称，不另注册一套协议。
初始化严格匹配插件 ID、版本及协议 `1.0`。

界面由六区组成：本次分析、私密问答、待办候选、重点证据、执行进度、历史详情。沿用现有 `tabs`（前端折叠展示）、
section/card、文本、列表、表格、坐标、表单和状态组件，不扩展 UI schema，不注入 HTML。
显示原 execution/root/parent ID、标记来源及修订、当前 Final 处理数量、最后更新时间、needs_input 和已确认/未知副作用。
只对 Host 返回的真实时间显示坐标，不用事件收到时间冒充视频时间。

以下两类命令必须区分：

- `analysis_activate/deactivate`、`ask/execute`、`mark_create/accept/dismiss`、`input/cancel` 是**未受信任的操作请求**。
  RPC 命令仅返回 `confirmation_required`，不调用写能力。统一前端根据 Host 注册的动作映射，引导到 HostActionPanel，
  只预填 Host 声明的输入；仍需用户在主程序操作区提交。外部写入还需确认服务端预览。
  不得把插件按钮、标签、返回字段或预填数据当作授权。
- `apply_action` 不出现在插件 view 的 actions 中。仅供确认后的主程序转交 `HostMeetingActions.confirm` 返回的
  `{status: "authorized", action, command}`，放入 Supervisor `command.execute.values`。
  `action` 使用完整的 `meeting.ask/meeting.execute/meeting.mark.create/...` 名称；短名称不是授权协议。
  `command` 必须保留原 `request_id/intent_token` 和规范化参数。插件不产生 token、不添加 actor/会话/任意 grant；
  Broker/Host 仍执行真实权限、scope、票据和最终外部写入检查。分析确认返回 `applied` 时只需刷新，不调用此命令。
- `refresh/select_candidates/select_marks/select_execution/load_more/previous_page/detail_next/detail_previous`
  仅查询或保存 UI 选择。选择使用真实 ID，表单 checkbox 名为 `selection_name` 返回的稳定散列。
  原始 ID 不从 checkbox 标签解析，也不以任务 JSON 替代候选 ID。

手动标记的“当前字幕”由 Host 根据真实字幕 ID/revision 验证，不信任插件输入坐标。
Linear 配置、固定团队、候选范围、预算和确认内容由 Host 操作区显示；缺少 Linear 不应阻断问答/本地标记。
`safe_markdown` 的 HTTPS 任务链接现由通用 renderer 输出安全的新标签页链接（拒绝凭据 URL、脚本和 HTML），
其余内容仍为转义文本。视频坐标保持只读显示，不把标签页音频伪装成可远程控制的视频播放器。
DOM 测试与本地浏览器已验证侧栏展开、收起和键盘调宽；运行实例未安装会议插件，因此实际会议卡片和 Linear 预览仍待受控上线后验收。

## 刷新、错误与数据边界

- 同会话 Host 读/命令串行；不同会话不持共同锁等待 Host。媒体 ACK 只收取刷新提示，不等待查询、模型或任务结果。
- 已激活/收尾分析或未完成操作约每 2 秒查询一次；无变化不发布新 view_version。未激活和终结历史首次及用户操作后查询。
- 受理即返回 operation ID，后续独立轮询，不依赖下一条字幕。操作恰好在状态读取后完成时补读一次，防止遗漏最终标记。
- 浏览旧页时另读有界最新页，继续追踪当前执行。列表和详情每页 10 条，事件游标与数据 offset 一同前后翻页；
  保留最多 1000 页回退游标。只展示受限摘要，原始完整历史仍在 Host。
- 查询/发布错误保留上次成功内容；最多连续失败 3 轮后停止自动重试，可手动刷新或重连。异常正文和 Host 路径不展示给用户。
- 受理响应丢失时返回 unknown，绝不自动生成新 request ID 重做。Host 重试须使用原命令/同一 ID；票据失效须重新经过主程序确认。
- `meeting-ui` state 仅保存选择、分页/版本和待查 operation ID，不保存字幕、回答、第二套执行状态、授权明文或密钥。
  发布前预留递增 view_version，重连从 Host 原记录重建。session.close、重复 open、SDK transport shutdown 回收所有自有任务。
- `read_only` 只控制展示，不构成可信来源；插件不能伪造 Host history。停用/卸载或框架关闭后仍可从 Host 目录读取历史。
  没有 MediaSession 映射的老记录使用只读 legacy 别名；只有用户明确准备取消时才创建真实映射。历史不提供恢复/外部执行入口。

## 主程序 HTTP 通道

- `POST /api/assistant-actions/ui-context`：可信页面 nonce，仅保留内存，要求精确 Origin 和 `X-Assistant-UI: 1`。
- `GET /api/media-sessions/{id}/assistant-actions`：Host 生成的控件、输入映射、当前版本、epoch 和可用性。
- `POST /api/media-sessions/{id}/assistant-actions/prepare`：提交稳定 request ID、当前插件/视图版本和有界输入。
- `POST /api/media-sessions/{id}/assistant-actions/confirm`：提交预览 ID/hash 和确认结果。Host 内部转交 capsule，浏览器只获得 applied/accepted/unknown。
- `GET /api/sessions/{id}/assistant-history-sources`、`GET /api/media-sessions/{id}/assistant-history`：主程序只读目录与分页历史。
- `POST /api/media-sessions/{id}/assistant-history/prepare-cancel`：历史只允许取消；随后走相同 confirm。

写请求还要求 `X-Assistant-UI-Nonce`。无 Origin 的自动化调用需要显式 Host 授权上下文；插件 RPC 不拥有这个通道。
旧 turn/input/cancel/mark POST/PATCH 返回 HTTP 410 `meeting_plugin_migration_required`；旧 GET 保留只读兼容。
响应丢失或无法解析时重试原请求，Host 优先查持久受理记录，不能凭插件声称的 operation ID 判定成功。

## 打包与测试

`backend/scripts/builtin_plugin_sources.py` 维护固定 source registry。课程和会议只共享 SDK 源码，各自 source digest、
snapshot、缓存和临时 image tag 独立。课程 wrapper 的公开函数与原 digest 算法保留。
输出和签名私钥必须位于仓库之外；签名密钥不进入 Docker COPY 上下文。

```powershell
# 在 backend；打包/安装仍需管理员按目标环境授权。
.\.venv\Scripts\python.exe scripts/package_meeting_assistant_plugin.py --help
# 标准参数：--private-key <外部路径> --output <外部路径> --image-tag <唯一标签>

.\.venv\Scripts\python.exe -m pytest tests/test_meeting_plugin_packaging.py tests/test_meeting_plugin_controller.py tests/test_meeting_plugin_views.py -q
.\.venv\Scripts\python.exe smoke/meeting_assistant_plugin_smoke.py
```

单元打包测试继续使用 fake Docker；独立 smoke 使用真实临时 OCI 镜像和标准签名/安装链路，且仅清理本次记录的容器和唯一标签。
12 种 Python 生成视图保存在 `frontend/lib/__fixtures__/meeting-plugin-views.json`，由后端校验相等，并由真实前端 parser 读取回归。
