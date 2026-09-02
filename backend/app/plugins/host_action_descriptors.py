"""Trusted controls are defined here, never in plugin-published view JSON."""
from sqlalchemy import select
import hashlib
from app.assistant.plugin_contracts import MeetingStateQueryInput
from app.assistant.plugin_policy import MeetingPluginPolicy, LIVE_SESSION_STATUSES
from app.assistant.plugin_repository import MEETING_PLUGIN_ID, MeetingPluginDenied
from app.assistant.plugin_service import MeetingPluginReadService
from app.assistant.state_machine import is_terminal_execution_status
from app.persistence.models import PluginUIViewRecord, PluginPackageRecord, PluginSessionBindingRecord, SegmentRecord, SessionRecord
from app.plugins.repository import PluginRepository
from app.plugins.host_actions import ACTION_CAPABILITIES

def meeting_target(db, media_id, action=None, version=None, view_version=None):
    binding = db.scalar(MeetingPluginPolicy._bindings().where(
        PluginSessionBindingRecord.media_session_id == media_id))
    if binding is None:
        raise MeetingPluginDenied("会议插件未安装、未启用或尚未连接当前会话")
    package = db.get(PluginPackageRecord, binding.package_id)
    repo = PluginRepository(db)
    health = repo.get_runtime_health(plugin_id=MEETING_PLUGIN_ID, version=binding.plugin_version)
    if health is None or health.status not in {"ready", "degraded"}:
        raise MeetingPluginDenied("会议插件尚未就绪")
    if "apply_action" not in package.manifest_json.get("commands", []):
        raise MeetingPluginDenied("会议插件缺少可信命令入口")
    permissions = repo.list_base_permissions(plugin_id=MEETING_PLUGIN_ID, version=binding.plugin_version)
    if action and (ACTION_CAPABILITIES[action] not in permissions or ACTION_CAPABILITIES[action] not in package.manifest_json.get("permissions", [])):
        raise MeetingPluginDenied("会议操作权限未被接受")
    view = db.scalar(select(PluginUIViewRecord).where(PluginUIViewRecord.plugin_id == MEETING_PLUGIN_ID,
        PluginUIViewRecord.plugin_version == binding.plugin_version, PluginUIViewRecord.media_session_id == media_id,
        PluginUIViewRecord.surface == "panel", PluginUIViewRecord.view_id == "meeting-assistant"))
    if view is None:
        raise MeetingPluginDenied("会议视图尚未就绪")
    if (version is not None and version != binding.plugin_version) or (view_version is not None and view_version != view.view_version):
        raise MeetingPluginDenied("会议插件或视图版本已改变，请重新提交")
    return binding, view

def describe_actions(actions, media_id):
    with actions.database.session() as db:
        service = MeetingPluginReadService(db)
        data = service.read_session(media_id, MeetingStateQueryInput(limit=50))
        live = bool(actions.accepting and actions.settings.assistant_enabled and actions.settings.plugin_framework_enabled)
        try:
            binding, view = meeting_target(db, media_id)
        except MeetingPluginDenied:
            live = False; binding = view = None
        marks = data["marks"]
        executions = data["executions"]
        candidates = [item for item in data["candidates"] if item["content_status"] == "active" and item["evidence_revisions_current"]
            and item["execution_status"] == "not_requested" and item["readiness"] != "detected"]
        options = lambda rows, id_key, label_key: [{"value": row[id_key], "label": str(row.get(label_key) or row[id_key])[:500]} for row in rows]
        mark_options = options([m for m in marks if m["status"] == "accepted"], "mark_id", "title")
        def selectable_input(kind, value):
            return kind + "-" + hashlib.sha256(value.encode()).hexdigest()[:16]
        for option in mark_options: option["input_name"] = selectable_input("mark_id", option["value"])
        def field(name, label, kind="text", required=False, **kwargs):
            return {"name": name, "label": label, "type": kind, "required": required, **kwargs}
        def execution_options(rows):
            return [{"value": row["execution_id"], "label": f'{row["goal"][:160]} · {row["status"]}', "arguments": {"expected_state_version": row["state_version"]}} for row in rows]
        cancellable = [e for e in executions if not is_terminal_execution_status(e["profile"], e["status"])]
        controls = []
        def add(action, label, fields=(), fixed=None, enabled=True, reason=None):
            if live:
                try: meeting_target(db, media_id, action)
                except MeetingPluginDenied as error: enabled = False; reason = str(error)
            controls.append({"id": action, "action": action, "label": label, "fields": list(fields), "fixed_arguments": fixed or {},
                "enabled": bool(enabled and actions.accepting), "reason": reason, "trigger_command": action.removeprefix("meeting.").replace(".", "_")})
        if live:
            status = data["processing"]["status"]
            current = db.get(SessionRecord, data["legacy_session_id"])
            add("meeting.analysis.activate", "开始会议分析", enabled=current.status in LIVE_SESSION_STATUSES and status != "active", reason=None if current.status in LIVE_SESSION_STATUSES else "历史会话不可启动自动分析")
            add("meeting.analysis.deactivate", "停止会议分析", enabled=status in {"active", "draining"})
            context = field("mark_ids", "引用标记", "multi_select", options=mark_options)
            add("meeting.ask", "询问", [field("message", "问题", "textarea", True), context])
            candidate_options = [{"value": c["candidate_id"], "label": c["content"]["title"]["value"] or "待确认事项",
                "input_name": selectable_input("candidate_id", c["candidate_id"])} for c in candidates]
            external = actions.settings.task_system_provider != "disabled"
            add("meeting.execute", "执行到 Linear", [field("message", "执行目标", "textarea", True), field("candidate_ids", "待执行事项", "multi_select", True, options=candidate_options), context],
                enabled=external and bool(candidate_options), reason=None if external else "未配置任务系统，不能执行外部写入")
            final = db.scalar(select(SegmentRecord).where(SegmentRecord.session_id == data["legacy_session_id"], SegmentRecord.status == "final").order_by(SegmentRecord.audio_end_ms.desc(), SegmentRecord.updated_at.desc()).limit(1))
            add("meeting.mark.create", "标记当前字幕", [field("title", "标记标题", required=True, max_length=500, input_name="mark_title"), field("note", "备注", "textarea", max_length=2000, input_name="mark_note")],
                fixed={"evidence": [{"segment_id": final.segment_id, "revision": final.revision}]} if final else {}, enabled=final is not None, reason=None if final else "等待已确认的原文字幕")
            for operation, label in (("accept", "接受标记"), ("dismiss", "忽略标记")):
                choices = [{"value": m["mark_id"], "label": m["title"], "arguments": {"expected_state_version": m["source_state_version"], "evidence": m["evidence"]}} for m in marks if m["status"] == "candidate"]
                add(f"meeting.mark.{operation}", label, [field("mark_id", "待确认标记", "select", True, options=choices)], enabled=bool(choices))
            waiting = execution_options([e for e in executions if e["status"] == "needs_input"])
            add("meeting.input", "补充输入", [field("execution_id", "待补充执行", "select", True, options=waiting), field("input", "补充内容", "textarea", True)], enabled=bool(waiting))
        add("meeting.cancel", "取消执行", [field("execution_id", "待取消执行", "select", True, options=execution_options(cancellable))], enabled=bool(cancellable), reason=None if cancellable else "没有可取消执行")
        return [{"source_kind": "host_actions" if live else "host_history", "plugin_id": MEETING_PLUGIN_ID, "media_session_id": media_id,
            "plugin_version": binding.plugin_version if live else None, "view_version": view.view_version if live else None,
            "authority_epoch": data["processing"]["authority_epoch"], "analysis_epoch": data["processing"]["analysis_epoch"], "actions": controls}]
