"""Closed, bounded UI projection. Nothing in this document is trusted authority."""
from __future__ import annotations

import hashlib
from urllib.parse import quote, urlsplit

from .contracts import QUIET_STATUSES


UNRESOLVED_ASSIGNEE_NOTICE = (
    "负责人未能映射到 Linear 用户，已把会议中的原称呼保留在 Issue 描述；"
    "请在 Linear 中确认负责人。"
)


def has_unresolved_assignee(value):
    """Recognize identity placeholders only from the typed result/candidate shape."""
    if not isinstance(value, dict):
        return False
    assignee = value.get("assignee")
    if isinstance(assignee, dict):
        identity = assignee.get("value")
        if (isinstance(identity, dict) and identity.get("is_placeholder") is True
                and assignee.get("resolution") in {"missing", "ambiguous", "conflicting"}
                and isinstance(identity.get("spoken_text"), str)):
            return True
    identity = value.get("unresolved_identity")
    if (value.get("partial") is True and isinstance(identity, dict)
            and identity.get("is_placeholder") is True
            and identity.get("resolution") in {"missing", "ambiguous"}
            and isinstance(identity.get("spoken_text"), str)):
        return True
    for key in ("content", "outcome", "result", "output"):
        if has_unresolved_assignee(value.get(key)):
            return True
    return False


def selection_name(kind, value):
    return kind + "-" + hashlib.sha256(value.encode()).hexdigest()[:16]


def build_meeting_view(*, snapshot=None, detail=None, error_code=None, notice=None,
                       selected_candidates=(), selected_marks=(), pending_operations=(), detail_offset=0, live_executions=(), read_only=False):
    snapshot = snapshot or {}
    detail = detail or {}
    processing = snapshot.get("processing", {})
    actions = {}
    budget = 36_000
    serial = 0

    def text(value, limit=600):
        nonlocal budget
        result = str(value if value is not None else "未提供")[:min(limit, max(0, budget))]
        budget -= len(result)
        return result or "…"

    def node(kind, **fields):
        nonlocal serial
        serial += 1
        return {"id": f"n-{serial}", "type": kind, **fields}

    def paragraph(value, limit=600):
        return node("text", text=text(value, limit))

    def button(command, label):
        actions[command] = {"id": command, "kind": "command", "command": command}
        return node("button", label=label, action_id=command)

    def section(key, title, children):
        return {"type": "section", "id": key, "title": title, "children": children}

    def unique(items, key, limit=10):
        result, seen = [], set()
        for item in items:
            identity = item.get(key)
            if not isinstance(identity, str) or identity in seen:
                continue
            seen.add(identity)
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def evidence(messages, refs=(), start=None):
        children = []
        if type(start) is int and start >= 0:
            children.append(node("media_anchor", media_time_ms=start, label="来源视频坐标"))
        for message in messages[:1]:
            segment = message.get("segment_id")
            revision = message.get("segment_revision")
            label = f"{segment} · r{revision}" if segment else "用户输入"
            children.append(paragraph(label + "：" + str(message.get("display_text", "")), 280))
            coordinate = message.get("audio_start_ms")
            if start is None and type(coordinate) is int and coordinate >= 0:
                children.append(node("media_anchor", media_time_ms=coordinate, label=text(label, 200)))
        if refs and not messages:
            children.append(paragraph("证据：" + "；".join(f"{r.get('segment_id')} · r{r.get('revision')}" for r in refs[:3]), 300))
        return children

    def links(value):
        children = []
        if not isinstance(value, dict):
            return children
        url = value.get("url")
        if isinstance(url, str) and len(url) <= 1000:
            try:
                parsed = urlsplit(url)
                safe = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                safe = False
            if safe:
                children.append(node("safe_markdown", markdown="[Linear 任务](" + quote(url, safe=":/?=&%#@+,-._~") + ")"))
        if value.get("identifier"):
            children.append(paragraph(value["identifier"], 200))
        return children

    status = processing.get("status", "inactive")
    current, done, pending = (processing.get(k, 0) for k in ("current_finals", "processed_finals", "pending_finals"))
    label = "未启用本次分析"
    tone = "neutral"
    if read_only:
        label = "只读历史"
    elif error_code == "scope_unavailable":
        label, tone = "插件或会话权限不可用", "warning"
    elif processing.get("last_error_code"):
        label, tone = "分析失败，可刷新检查", "danger"
    elif status in {"active", "draining"}:
        label, tone = ("等待字幕", "info") if current == 0 else (("分析中", "info") if pending else ("已更新", "success"))
    elif status == "completed":
        label, tone = "本次分析已结束", "success"
    elif processing.get("analysis_epoch", 0) > 0:
        label = "已停止分析"
    analysis = [node("badge", text=label, tone=tone),
        node("metric", label="当前 Final", value=f"已处理 {done} / {current}", detail=f"待处理 {pending}（不使用插件 ACK 计数）"),
        node("progress", label="当前字幕处理进度", value=min(100, max(0, round(done / current * 100))) if current else 0),
        paragraph("最近更新：" + str(processing.get("updated_at") or "尚无成功更新")), button("refresh", "刷新状态")]
    if error_code:
        analysis.append(node("error_state", title="当前读取失败", message="已保留上次成功结果和更新时间。检查插件启用及会话权限后刷新。"))
    if processing.get("last_error_code"):
        analysis.append(node("error_state", title="会议分析暂不可用", message="已有内容保留；请检查主程序模型配置和分析错误状态。"))
    if not read_only:
        if status in {"active", "draining"}:
            analysis.append(button("analysis_deactivate", "停止本次分析（主程序确认）"))
        elif status != "completed":
            analysis.append(button("analysis_activate", "开启本次分析（主程序确认）"))
    analysis.append(paragraph("打开插件不会自动分析。问答、标记和执行均需主程序确认；停止分析不会撤销已有任务。"))
    if notice:
        analysis.append(node("error_state" if notice == "operation_unknown" else "empty_state", title="操作受理状态未知" if notice == "operation_unknown" else "请在主程序操作区确认",
            message="请用原请求查询或重试，不要新建同一操作。" if notice == "operation_unknown" else "此按钮未发放授权，也未执行操作。"))

    executions = unique(snapshot.get("executions", []), "execution_id")
    replies = [e for e in executions if e.get("profile") == "fast_turn"][:3]
    ask = [paragraph("私密回复仅在本会话展示，不广播给其他插件。")]
    if not read_only:
        ask += [node("textarea", name="message", label="私密问题或执行目标", placeholder="结合会议重点与选中的证据提问", rows=3), button("ask", "询问（主程序确认）")]
    for reply in replies:
        ask.append(node("card", title=text(reply.get("goal"), 200), children=[paragraph((reply.get("result") or {}).get("answer") or (reply.get("result") or {}).get("summary") or "回复尚未完成", 1600)]))
    if not replies:
        ask.append(node("empty_state", title="暂无私密回复", message="已完成的问答会保存在原执行历史中。"))

    candidates = []
    for candidate in unique(snapshot.get("candidates", []), "candidate_id"):
        content = candidate.get("content", {})
        title = (content.get("title") or {}).get("value") or "待确认事项"
        children = [paragraph(f"ID {candidate['candidate_id']} · r{candidate.get('current_revision')} · {candidate.get('readiness')} · {candidate.get('content_status')} / {candidate.get('execution_status')}", 350)]
        fields = []
        for field, label_field in (("deliverable", "交付物"), ("assignee", "负责人"), ("due_at", "截止"), ("priority", "优先级")):
            grounded = content.get(field) or {}
            item = grounded.get("value")
            if isinstance(item, dict):
                item = item.get("spoken_text")
            fields.append(f"{label_field}：{item if item is not None else '待补充'}")
        children.append(paragraph("；".join(fields), 500))
        if has_unresolved_assignee(content):
            children.append(node("error_state", title="负责人需要确认", message=UNRESOLVED_ASSIGNEE_NOTICE))
        children.extend(evidence(content.get("evidence_messages", [])))
        if candidate.get("evidence_revisions_current") is False:
            children.append(node("badge", text="证据已修订，需重新确认", tone="warning"))
        if not read_only:
            children.append(node("checkbox", name=selection_name("candidate_id", candidate["candidate_id"]), label="选择此候选", checked=candidate["candidate_id"] in selected_candidates))
        candidates.append(node("card", title=text(title, 180), children=children))
    if not candidates:
        candidates.append(node("empty_state", title="暂无待办候选", message="开启分析后从已确认字幕提取；也可查看历史。"))
    if not read_only:
        candidates += [button("select_candidates", "保存候选选择"), button("execute", "执行到 Linear（主程序确认）"), paragraph("可用性、固定团队、候选范围和写入额度由主程序显示并验证。缺少 Linear 配置不影响问答和本地标记。")]

    marks = []
    for mark in unique(snapshot.get("marks", []), "mark_id"):
        children = [paragraph(f"ID {mark['mark_id']} · {mark.get('origin')} · {mark.get('kind')} · {mark.get('status')}", 300), paragraph(mark.get("note") or "", 400)]
        children.extend(evidence(mark.get("evidence_messages", []), mark.get("evidence", []), mark.get("audio_start_ms")))
        if not read_only:
            children.append(node("checkbox", name=selection_name("mark_id", mark["mark_id"]), label="选作问题证据", checked=mark["mark_id"] in selected_marks))
        marks.append(node("card", title=text(mark.get("title"), 180), children=children))
    state = snapshot.get("state", {})
    for key, title in (("topics", "主题"), ("decisions", "决策"), ("highlights", "重点"), ("conflicts", "冲突"), ("user_concerns", "关注点"), ("entities", "实体")):
        items = state.get(key, [])[:3]
        if items:
            marks.append(node("list", items=[{"id": f"{key}-{i}", "primary": text(title + "：" + str(item.get("text", "")), 180),
                "secondary": text("来源：" + "；".join(f"{s} · r{item.get('source_segment_revisions', {}).get(s, '未知')}" for s in item.get("source_segment_ids", [])[:3]), 150)} for i, item in enumerate(items)]))
    if not marks:
        marks.append(node("empty_state", title="暂无重点标记", message="此处保留自动候选、已接受和手动标记。"))
    if not read_only:
        marks += [button("select_marks", "保存证据选择"),
            node("input", name="mark_title", label="手动重点标题"), node("textarea", name="mark_note", label="补充说明", rows=2),
            button("mark_create", "添加当前字幕重点（主程序确认）"),
            paragraph("当前字幕的 ID、修订和时间由主程序取证；不能仅凭插件表单指定来源。")]
        available_marks = unique(snapshot.get("marks", []), "mark_id")
        if available_marks:
            marks += [node("select", name="mark_id", label="要处理的重点", options=[{"id": f"mark-option-{i}", "label": text(m.get("title"), 100), "value": m["mark_id"]} for i, m in enumerate(available_marks)]),
                button("mark_accept", "接受重点（主程序确认）"), button("mark_dismiss", "忽略重点（主程序确认）")]

    run = detail.get("execution")
    if not run:
        current_executions = unique([*live_executions, *executions], "execution_id")
        run = next((e for e in current_executions if e.get("profile") == "action_run"), current_executions[0] if current_executions else None)
    progress = []
    if pending_operations:
        progress.append(node("badge", text=f"已受理 {len(pending_operations)} 项，等待后台完成", tone="info"))
        progress.append(paragraph("操作：" + "、".join(pending_operations[:3]), 200))
    if run:
        progress += [node("badge", text=text(run.get("status"), 100), tone="warning" if run.get("status") == "needs_input" else "info"),
            paragraph(f"执行 {run['execution_id']}\n根执行 {run.get('root_execution_id')}\n父执行 {run.get('parent_execution_id') or '无'}\n版本 {run.get('state_version')} · 步骤 {run.get('step_count', 0)}", 600), paragraph(run.get("goal"), 250)]
        effects = run.get("external_effects", {})
        progress.append(paragraph(f"外部副作用：已确认 {effects.get('confirmed', 0)}，未知 {effects.get('unknown', 0)}。取消不会删除已创建任务。"))
        if effects.get("unknown", 0):
            progress.append(node("error_state", title="存在未知结果", message="仅核对已发出的请求；不要再次创建相同任务。"))
        needs_input = run.get("needs_input")
        if needs_input:
            progress += [paragraph(needs_input.get("question"), 1000), paragraph("选项：" + "；".join(needs_input.get("choices", [])), 500)]
            if not read_only:
                progress += [node("textarea", name="input", label="补充信息", rows=2), button("input", "提交补充信息（主程序确认）")]
        if not read_only and (run.get("status") not in QUIET_STATUSES or needs_input):
            progress.append(button("cancel", "取消执行（主程序确认）"))
        result = run.get("result") or {}
        if has_unresolved_assignee(result):
            progress.append(node("error_state", title="负责人需要确认", message=UNRESOLVED_ASSIGNEE_NOTICE))
        for key in ("response_text", "answer", "summary"):
            if result.get(key):
                progress.append(paragraph(result[key], 2000))
        progress.extend(links(result.get("external_reference")))
        for call in detail.get("tool_calls", [])[:5]:
            progress.append(paragraph(f"{call.get('tool_name')} · {call.get('status')}", 200))
            progress.extend(links(call.get("external_reference")))
            if has_unresolved_assignee(call):
                progress.append(node("error_state", title="负责人需要确认", message=UNRESOLVED_ASSIGNEE_NOTICE))
        if run.get("error_code"):
            progress.append(node("error_state", title="执行未正常完成", message=text(run["error_code"], 200)))
    else:
        progress.append(node("empty_state", title="暂无执行", message="问答和任务在受理后展示进度；不会随字幕自动执行。"))
    if detail.get("operation") and detail["operation"].get("error_code"):
        progress.append(node("error_state", title="操作失败", message=text(detail["operation"]["error_code"], 200)))

    history = [paragraph(f"原会话：{snapshot.get('legacy_session_id', '尚未读取')} · 当前分页偏移 {snapshot.get('offset', 0)}"),
        node("list", items=[{"id": f"execution-{i}", "primary": text(e.get("goal"), 200),
            "secondary": text(f"{e['execution_id']} · {e.get('status')} · 根 {e.get('root_execution_id')} · {e.get('updated_at')}", 300)} for i, e in enumerate(executions)])]
    if executions:
        history += [node("select", name="execution_id", label="执行详情", options=[{"id": f"execution-option-{i}", "label": text(str(e.get("goal")) + " · " + str(e.get("status")), 150), "value": e["execution_id"]} for i, e in enumerate(executions)]), button("select_execution", "查看执行详情")]
    detail_rows = [{"kind": text(s.get("kind"), 100), "status": text(s.get("status"), 100)} for s in detail.get("steps", [])[:10]]
    if detail_rows:
        history.append(node("table", columns=[{"id": "kind", "label": "步骤"}, {"id": "status", "label": "状态"}], rows=detail_rows))
    if detail.get("execution"):
        history.append(paragraph(f"执行详情分页偏移：{detail_offset}"))
        if (len(detail.get("steps", [])) >= 10 or len(detail.get("tool_calls", [])) >= 10
                or detail.get("next_cursor", 0) < detail.get("event_cursor", 0)):
            history.append(button("detail_next", "下一页执行详情"))
        if detail_offset:
            history.append(button("detail_previous", "上一页执行详情"))
    events = detail.get("events", []) or snapshot.get("events", [])
    if events:
        history.append(node("list", items=[{"id": f"event-{i}", "primary": text(e.get("summary"), 200), "secondary": text(e.get("created_at"), 100)} for i, e in enumerate(events[:10])]))
    if snapshot.get("has_more") or snapshot.get("has_more_events"):
        history.append(button("load_more", "下一页历史"))
    if snapshot.get("offset", 0):
        history.append(button("previous_page", "上一页历史"))
    history.append(paragraph("历史沿用原记录 ID 与父子链。插件停用或卸载后，请从主程序历史目录只读查看。"))

    regions = [("ask", "私密问答", ask), ("candidates", "待办候选", candidates),
        ("marks", "重点与证据", marks), ("executions", "执行进度", progress), ("history", "历史记录", history)]
    return {"schema_version": 1, "surface": "panel", "view_id": "meeting-assistant", "view_version": 1,
        "root": {"type": "section", "id": "meeting", "title": "私人会议助手", "children": [section("analysis", "本次会议分析", analysis),
            node("tabs", tabs=[{"id": f"tab-{key}", "label": title, "children": [section(key, title, children)]} for key, title, children in regions])]},
        "actions": [actions[key] for key in sorted(actions)]}
