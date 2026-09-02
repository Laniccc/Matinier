"""Readable, bounded Host history using the same closed UI vocabulary."""
import json
from urllib.parse import quote, urlsplit

UNRESOLVED_ASSIGNEE_NOTICE = (
    "负责人未能映射到 Linear 用户，已把会议中的原称呼保留在 Issue 描述；"
    "请在 Linear 中确认负责人。"
)


def _has_unresolved_assignee(value):
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
        if _has_unresolved_assignee(value.get(key)):
            return True
    return False

def history_document(display, version):
    serial = 0; budget = 48000
    def node(kind, **fields):
        nonlocal serial
        serial += 1
        return {"id": f"history-{serial}", "type": kind, **fields}
    def text(value, limit=4000):
        nonlocal budget
        value = str(value); maximum = min(limit, max(0, budget))
        rendered = value[:maximum]
        if len(value) > maximum: rendered += "…（显示已限长，完整数据保留在只读接口）"
        budget -= len(rendered)
        return node("text", text=rendered)
    def pretty(value):
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    def links(value, depth=0):
        result = []
        if depth > 5: return result
        if isinstance(value, list):
            for child in value[:10]: result.extend(links(child, depth+1))
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and len(url) <= 1000:
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                        result.append(quote(url, safe=":/?=&%#@+,-._~"))
                except ValueError: pass
            for key, child in value.items():
                if key != "url": result.extend(links(child, depth+1))
        return list(dict.fromkeys(result))[:2]
    labels = {"processing": "分析状态", "state": "会议知识与证据", "marks": "重点标记", "candidates": "待执行事项",
        "executions": "问答与执行链", "events": "事件记录", "execution": "执行详情", "steps": "执行步骤", "tool_calls": "工具与外部结果"}
    field_labels = {"execution_id": "执行", "root_execution_id": "根执行", "parent_execution_id": "父执行", "status": "状态",
        "state_version": "版本", "mark_id": "标记", "candidate_id": "事项", "goal": "目标", "title": "标题", "text": "内容",
        "summary": "摘要", "decision_summary": "步骤", "kind": "类型", "note": "备注", "origin": "来源", "result": "结果",
        "needs_input": "待补充信息", "external_effects": "已确认/未知副作用", "evidence": "证据修订", "evidence_messages": "原始证据",
        "updated_at": "更新于", "tool_name": "工具", "external_reference": "外部引用", "content": "事项内容",
        "processed_finals": "已处理原文", "pending_finals": "待处理原文", "current_finals": "当前原文", "last_error_code": "错误"}
    sections = []
    for kind, value in display.items():
        rows = value if isinstance(value, list) else [value]
        children = []
        for row in rows[:10]:
            if isinstance(row, dict):
                lines = [f"{field_labels[key]}：{pretty(child)}" for key, child in row.items() if key in field_labels and child is not None]
                if kind == "state" or not lines: lines = [f"{key}：{pretty(child)}" for key, child in row.items() if child]
                children.append(text("\n".join(lines) or "暂无内容"))
                if _has_unresolved_assignee(row):
                    children.append(node("error_state", title="负责人需要确认", message=UNRESOLVED_ASSIGNEE_NOTICE))
                coordinate = row.get("audio_start_ms")
                if type(coordinate) is int and coordinate >= 0:
                    children.append(node("media_anchor", media_time_ms=coordinate, label="原始音视频坐标"))
                for url in links(row):
                    markdown = f"[打开外部结果]({url})"
                    if len(markdown) <= budget:
                        budget -= len(markdown)
                        children.append(node("safe_markdown", markdown=markdown))
            else: children.append(text(pretty(row)))
        if not rows: children.append(text("暂无记录"))
        if len(rows) > 10: children.append(text("本视图最多展示 10 条，请按默认每页 10 条翻页查看。"))
        sections.append(node("section", title=labels[kind], children=children))
    return {"schema_version": 1, "surface": "panel", "view_id": "host-history", "view_version": max(1, version),
        "root": node("section", title="会议历史", children=sections), "actions": []}
