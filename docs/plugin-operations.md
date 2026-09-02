# 插件框架运维指南

## 前置条件与配置

插件框架要求本机 Docker daemon 可用。服务启动前执行 `alembic upgrade head`；当前 head 是
`20260831_0027`。生产或共享环境必须设置随机 `PLUGIN_ADMIN_TOKEN`，该值只放服务端 `.env`。

```dotenv
PLUGIN_FRAMEWORK_ENABLED=true
PLUGIN_CONTAINER_RUNTIME=docker
PLUGIN_ADMIN_TOKEN=<随机管理令牌>
PLUGIN_ALLOW_UNSIGNED=false
PLUGIN_MODEL_MAX_CONCURRENCY=2
PLUGIN_MODEL_MAX_INPUT_CHARS=32000
PLUGIN_MODEL_MAX_OUTPUT_TOKENS=4096
PLUGIN_DOCUMENT_MAX_BYTES=196608
PLUGIN_DELIVERY_MAX_PAGE_ITEMS=100
# 仅本地开发实例启用；生产环境必须保持 false
PLUGIN_BUILTIN_BUILD_ENABLED=false
# 启用内置构建时必须是仓库外绝对目录
PLUGIN_BUILTIN_WORK_DIR=<仓库外绝对目录>
```

`PLUGIN_ALLOW_UNSIGNED=true` 只允许受控开发机临时使用，生产 Settings 会拒绝它。默认容器额度是
256 MiB 内存、0.5 CPU、64 PIDs、64 MiB `/tmp`；manifest 可以在主机允许范围内请求更小或更明确
的值。RPC 默认 30 秒，消息和单行上限均为 256 KiB。

## 安装与发布者信任

在 `/plugins` 上传 `.plugin.zip` 后先检查，不会立即启用：

1. 核对 plugin ID、版本、发布者名称、Ed25519 fingerprint、Host API 范围和全部权限。
2. 首次发布者必须逐字核对从可信渠道获得的 fingerprint，再明确固定公钥。
3. 接受的权限必须与检查结果完全一致；修改包后必须重新检查。
4. 确认安装后仍为 disabled；单独点击启用才启动容器。

同一 `plugin_id@version` 不可变；相同版本不同内容会被拒绝。管理写 API 需要
`X-Plugin-Admin-Token`，包上传 Content-Type 必须是
`application/vnd.matinier.plugin+zip`。

### 安装并使用课程整理插件

开发实例在服务端设置 `PLUGIN_BUILTIN_BUILD_ENABLED=true` 和仓库外绝对
`PLUGIN_BUILTIN_WORK_DIR` 后，可在 `/plugins` 的“项目内置插件”中检查课程整理插件；等价 API 是
`GET /api/plugins/builtins` 与带管理员令牌的 bodyless
`POST /api/plugins/builtins/com.matinier.course-organizer/packages:inspect`。该入口仍只创建二阶段检查票据，
不会绕过签名、发布者指纹、权限接受、安装或启用。检查页必须只显示并要求接受以下七项权限：
`state.get`、`state.put`、`ui.publish`、
`model.invoke`、`delivery.prepare`、`delivery.query`、`document.publish`。若出现
`network.fetch` 或任何第八项权限，停止安装并重新核对包；此插件没有直接网络权限。确认安装后还需
明确执行安装和启用，然后打开一个标签页/网页课程字幕 Session。生产环境拒绝动态内置签名；需要离线
发布时仍可用仓库外私钥执行 `backend/scripts/package_course_organizer_plugin.py` 并走普通上传检查流程。

播放期间只维护实时知识笔记。用户点击“生成最终整理”会保留一个 `interim` 文档；Session 正常结束
时自动生成 `complete`，失败/取消时生成 `partial_terminal`。语言默认优先译文、无译文时回退源语言，
显式语言各自保留版本。Host 文档历史提供可信 Markdown/JSON 下载。外部标签页坐标来自字幕音频
时间轴，不等同网页播放器 currentTime，不支持 DOM 定位或 seek。模型不可用时实时笔记以规则结果
降级并有界重试，最终任务显示 `waiting_retry`；不要清库或重启字幕主链，可等待或点击重试。

最终整理只从当前会话的 Frozen Package 读取 Stage 1 Final；Package 和逐项 evidence 校验是发布
边界。插件不会读取 Partial、Worker 内存、网页 DOM、OCR 或外部研究结果，也不生成 DOCX/PDF。

### 安装并使用会议助手插件

在 `/plugins` 对 `com.matinier.meeting-assistant` 执行标准“检查 → 核对发布者指纹与权限 → 确认安装 →
单独启用”。它请求七种有界 `meeting.*` 能力及 `state.get/state.put/ui.publish`；若出现 `network.fetch`、
`model.invoke`、通用 `action.execute` 或数据库权限，应停止安装。安装/启用只让容器可用，不会分析历史或当前会话。

用户路径为：在直播控制台展开“助手”侧栏 → 选择会议助手 → 明确开启本次会话分析 → 查看处理进度、私密问答、
重点标记与候选 → 选择待办 → 在主程序可信操作区核对固定团队、候选、数量和 15 分钟以内的授权预览 → 确认执行 →
在同一侧栏或“历史工作区”查看原 execution/root/parent 与外部引用。插件按钮和插件内 confirmation 只是请求，不能签发授权。

未配置 Linear 时，分析、问答和本地标记仍可使用；外部执行控件会明确不可用。停用分析不撤销已单独授权的交互，
停用插件会阻止新写入但不会撤回已发请求；取消不会删除已有任务。卸载不删除 Host 历史，重新启用不重放旧外部动作。
状态中的 `inactive/active/draining/completed` 是会话分析状态；`accepted/running/completed/needs_input/unknown/cancelled`
是持久操作/执行状态。`operation_unknown`、`refresh_failed`、`scope_unavailable` 和 `view_unavailable` 均要求读取原持久记录，
不得生成新 request ID 盲目重做。此次统一迁移不新增额外会议总结、多账户连接或向参会者广播能力。

`1.0.3` 的实时笔记来源使用 Host 的 canonical Segment ID；旧状态如果保存了事件别名，会在新版本
状态中从 durable events 重建，原版本状态和已发布文档不覆盖。既有 binding 仍固定原插件版本，升级
安装本身不表示所有历史 Session 已重新处理；本轮仅对用户明确授权的验收 Session 建立新版绑定。
重新处理历史字幕可能调用已配置的外部模型，应先确认数据范围与授权。

终稿读取 Package 的分页大小与模型批次大小分开：每批最多 4 项、序列化输入项最多 8,000 字符，
请求输出预算 4,096 token 且仍受 Host 上限限制。单项超限或映射后超过 500 个知识项会明确失败并
保留重试状态，不把截掉整批知识项的结果当完整文档。验收导出时应同时确认两部分内容、证据闭合和
文档 hash；只看到 `published` 不足以证明实时笔记已收入终稿。

### 使用多插件助手工作区

Room 选中或启动字幕任务后，点击最左侧活动栏“助手”或右上角“助手工作区”，就地展开助手侧栏。
“直播空间”和“助手”共用侧栏，切换、收起或调整宽度只改变显示，不卸载中央字幕和音频组件；隐藏
侧栏也不会重复 bridge 或清空插件输入。侧栏跟随当前字幕任务，不提供切换历史 Session 的下拉框，
避免把观看上下文与采集任务混淆。

默认侧栏宽 384px（含活动栏），桌面可拖动右边缘，在 320–560px 之间调整；聚焦分隔条后也可用
左右方向键、Home/End 调整。再次点击当前活动按钮、点击收起按钮或按 Esc 可收起，收起后仍保留
活动栏；窄屏使用覆盖式面板，不卸载字幕。关闭或刷新原采集标签页仍会停止采集，加载新版界面前应
先停止已有采集。

侧栏“历史工作区”在新标签页打开 `/assistants?session=<legacy-session-id>`；独立 `/assistants`
继续支持从下拉框选择运行中或历史 Session，插件管理和下载也在新标签页打开。左侧目录按 Host
返回的 plugin ID 合并安装状态、视图和文档；选择插件后只渲染该插件的闭合 UI schema 和可信文档。
若目录显示 waiting，先确认 Session 已产生 Final；若显示 degraded，先按
下节检查 Docker/runtime，已有视图和历史文档仍可只读查看。

首次使用 Session 时，先确认 `GET /api/plugins/{id}` 为 `enabled/ready`，再由页面请求 Session 的
`media-session` bridge。Host 随后持续向已打开的插件 scope 派发新字幕及结束事件，不需要刷新页面、
再次连接或点击命令。API 重启恢复的已绑定会话也会继续补发；不会仅因历史 Session 被投影而自动
创建新的插件绑定。恢复完成时各 binding 的 acknowledged sequence 应追上事件流；不要手工改游标。
诊断插件可作为本地多插件隔离检查，但它不是业务场景助手。

后台派发复用 `MEDIA_EVENT_PROJECTOR_POLL_INTERVAL_MS`（默认 500ms），每个插件/Session 每轮处理
一个有界批次；首次补发和后台派发共用游标锁。某个插件慢或失败不会阻塞其他插件的派发，失败从已确认
游标重试，停止时先取消派发再关闭容器。页面视图查询仍只读，`ready` 表示运行时可用，不表示游标已追平。
实时笔记自身仍按 60 秒字幕跨度或 1,600 字符触发，短窗口在收到结束事件时处理，不保证逐句出笔记。

侧栏交互回归可在 `frontend` 中运行：

```powershell
pnpm exec vitest run components/room-studio.test.tsx components/assistants/studio-sidebar.test.tsx
```

测试挂载真实 RoomStudio 与声明式插件渲染器，在 LiveKit/API 边界使用合成数据；检查切换后字幕事件
仍更新，音轨未 stop/unpublish、Room 未 disconnect，真正卸载时清理仍执行。这不替代浏览器共享音频
权限和视觉验收。人工复验时保持课程播放，依次展开、折叠、切换插件和拖动宽度，确认仍有新字幕，
且不再次弹出音频共享选择器。

## 运行状态与健康

`GET /health/ready` 的字幕 readiness 与插件详情相互独立；Docker 不可用会使已启用插件 degraded，
不会让字幕写入和发布主链同步失败。`GET /api/plugins` 和 `/api/plugins/{id}` 显示：

- `installed`/`disabled`：包存在但容器未运行；
- `starting`：容器启动和协议协商中；
- `ready`：初始化和最近心跳成功；
- `degraded`：容器 runtime 或心跳异常，主机仍保持有界恢复；
- `crashed`：观察到非预期退出；
- `quarantined`：60 秒窗口内超过默认 5 次重启；
- `incompatible`：Host API 或协议身份不匹配。

排障顺序：Docker daemon → 插件详情/health → API 结构化日志中的 plugin ID/version/status → 容器
stderr。不要把 stdout 当日志读取，它是协议专用通道。审计表记录 capability requested/completed/
denied/failed、grant 和调用身份；公开 API 不返回隐藏异常正文或敏感 input。

启动 API 的进程必须能访问本机 Docker named pipe；若以受限沙箱启动，字幕 API 可以正常工作，但
已启用插件会明确进入 `degraded` 并标记 container runtime unavailable。Windows 上重启开发栈前还要
确认 3000/8000 没有本项目遗留进程。Docker Desktop 的 AF_UNIX 临时 socket 故障应先停止 Desktop，
只对已解析确认位于 Docker 本地临时目录内的目标做保留性重命名；不要直接 factory reset 或删除
镜像、volume、WSL 数据。

## 崩溃恢复、游标与隔离检查

主机在 event.batch 前记录 delivered sequence，在插件返回后记录 acknowledged sequence。容器退出会
销毁旧 session scope；重启后用新 scope 和 durable ack 重新打开会话。若插件重复处理事件，应先
检查自己的幂等写入，不要手工向前修改数据库游标。

本地验证命令：

```powershell
cd backend
.venv\Scripts\python.exe smoke/plugin_sandbox_smoke.py
.venv\Scripts\python.exe smoke/course_organizer_plugin_smoke.py
.venv\Scripts\python.exe smoke/plugin_framework_smoke.py
.venv\Scripts\python.exe smoke/meeting_assistant_plugin_smoke.py
```

课程插件 smoke 使用临时仓库外工作根和临时 SQLite，通过项目内置检查端点动态构建、签名、检查、
明确接受权限、安装并启用真实镜像，再验证实时笔记、手动临时版、终态完整版、证据闭合、可信导出、
语言版本和崩溃游标恢复。私钥、源码快照与插件包都位于系统临时目录，不写入仓库；smoke 会按记录的
容器 ID 和导入 image ref 精确清理资源。

课程 smoke 还要求 `continuous_delivery=true`：新增字幕后不再次请求 bridge，仅轮询视图即得到新的
实时笔记；Session 结束后也仅轮询文档即得到 complete。只通过手动回放或再次连接的测试不能证明实时
派发正常。最终文档第一部分必须非空（`realtime_notes_in_final=true`）。

通用框架 smoke 必须报告：

会议 smoke 使用唯一临时 SQLite、Ed25519 key 与镜像标签，真实构建并启动 `--network none`、只读根文件系统、
无 Host 数据挂载的容器；模型和 Linear 均为进程内 fake。成功输出必须包含 `signed_package`、`network_isolated`、
`activation_required`、`backlog_processed`、`view_valid`、`disable_blocks_new_writes`、`history_preserved`、
`recovery_no_duplicate` 全为 true，且 `external_create_count=1`。这证明隔离和幂等，不证明真实模型/Linear 可用。

```json
{"plugin_ready":true,"cursor_resumed":true,"network_isolated":true,"caption_regression":false}
```

任一 Docker 冒烟不能证明无网络、只读根文件系统或 scope/游标恢复时，应停止发布；不能改用同进程
或普通子进程作为降级沙箱。

## 更新与回滚

安装新版本会先作为候选启动并调用 `plugin.migrate_state`。只有初始化、迁移结果校验和新版本状态
快照事务都成功后，preferred version 才切换：

- 已打开 MediaSession 继续固定在旧版本，避免观看过程行为突变；
- 新 MediaSession 使用新的 preferred version；
- 旧状态不原地修改，旧包仍可服务已固定会话；
- 迁移失败返回冲突，preferred version 和旧状态保持不变，候选容器停止。

回滚应重新选择/安装经信任的旧版本并验证其 state schema；当前管理 UI 不提供任意数据库级强制
切换按钮。不要删除仍有 open binding 的旧镜像或包目录。

## 权限撤销、禁用与卸载

撤销安装权限或 Grant 后，Capability Broker 在下一次调用时重新读取并默认拒绝。禁用会停止该
plugin ID 的所有受监督版本，向逻辑会话发送 close，随后关闭 RPC 和容器；再次启用从 durable
binding 恢复。

卸载先执行禁用，再删除插件持久记录和受 `PLUGIN_PACKAGES_DIR` 控制的包目录。该操作会失去插件
状态、视图和审计关联，不等于可回滚；需要可恢复卸载时，先备份 SQLite、包目录和发布者 fingerprint，
并在副本上验证 `alembic current` 与 `PRAGMA quick_check`。

## 单实例限制

当前 Supervisor、scope generation 和 Projector 调度属于单 FastAPI 实例；SQLite 使用 WAL，但不
支持多主机共享卷、多个 API 实例同时监督同一插件或分布式 exactly-once。横向扩展前需要把数据库
迁移到 PostgreSQL，并增加共享租约、队列和容器所有权协调。本阶段不宣称多租户隔离。
同一 API 进程内的 capability 数据库事务、runtime 状态、binding 持久化和投递/确认游标写入会串行
进入 SQLite，以避免恢复时的 writer 竞争。`model.invoke` 在远程等待前先提交 pending 调用与审计，
释放数据库事务和插件写入锁；返回后重新获取锁并保存结果。模型成功、失败和取消均不会把长时间
等待传递给字幕 Worker；取消时保留 pending 记录而不伪造完成。模型并发仍受 Host semaphore 限制。
