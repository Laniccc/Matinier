import copy

from meeting_plugin_client_fakes import state_data, execution


def cases():
    base = state_data()
    updated = copy.deepcopy(base)
    updated["processing"].update(status="active", current_finals=2, processed_finals=2, updated_at="2026-08-31T12:00:00Z", analysis_epoch=1)
    updated["state"].update(highlights=[{"item_id": "h1", "text": "Preserve source evidence", "source_segment_ids": ["s1"], "source_segment_revisions": {"s1": 2}}])
    updated["marks"] = [{"mark_id": "mark-1", "session_id": "legacy-1", "title": "Release date", "note": "Confirm the date", "kind": "decision", "status": "candidate", "origin": "automatic", "source_state_version": 1,
        "audio_start_ms": 16690, "audio_end_ms": 20217, "evidence": [{"segment_id": "s1", "revision": 2}],
        "evidence_messages": [{"message_kind": "caption", "segment_id": "s1", "segment_revision": 2, "display_text": "Friday release", "audio_start_ms": 16690}]}]
    updated["candidates"] = [{"candidate_id": "candidate-1", "session_id": "legacy-1", "current_revision": 2, "readiness": "recordable", "content_status": "active", "execution_status": "not_requested", "evidence_revisions_current": True,
        "content": {"title": {"value": "Release notes"}, "deliverable": {"value": "Changelog"}, "assignee": {"value": None, "resolution": "missing"}, "due_at": {"value": None, "resolution": "missing"}, "priority": {"value": "normal"}, "evidence_messages": updated["marks"][0]["evidence_messages"]}}]
    updated["executions"] = [execution()]
    result = {"inactive": {"snapshot": base}, "updated": {"snapshot": updated}}
    for name, status, current, processed, pending in [("waiting", "active", 0, 0, 0), ("processing", "active", 3, 1, 2), ("disabled", "inactive", 2, 2, 0), ("completed", "completed", 2, 2, 0)]:
        value = copy.deepcopy(updated)
        value["processing"].update(status=status, current_finals=current, processed_finals=processed, pending_finals=pending)
        result[name] = {"snapshot": value}
    failed = copy.deepcopy(updated)
    failed["processing"]["last_error_code"] = "model_unavailable"
    result["model_failed"] = {"snapshot": failed}
    for name, status, effects in [("needs_input", "needs_input", {"confirmed": 0, "unknown": 0}), ("unknown", "reconciling", {"confirmed": 1, "unknown": 1})]:
        value = copy.deepcopy(updated)
        row = execution(status, profile="action_run", needs_input={"question": "Which release?", "choices": ["A", "B"]} if name == "needs_input" else None,
            external_effects={**effects, "existing_actions_remain": bool(effects["confirmed"])})
        value["executions"] = [row]
        result[name] = {"snapshot": value, "detail": {"execution": row, "steps": [{"kind": "tool", "status": "succeeded"}],
            "tool_calls": [{"tool_name": "task.create", "status": "succeeded", "external_reference": {"identifier": "DEMO-1", "url": "https://linear.app/demo/issue/DEMO-1"}}], "events": []}}
    result["history"] = {**copy.deepcopy(result["unknown"]), "read_only": True}
    result["query_failed"] = {"snapshot": updated, "error_code": "refresh_failed"}
    result["scope_unavailable"] = {"snapshot": updated, "error_code": "scope_unavailable", "read_only": True}
    return result


def documents():
    from meeting_assistant.view import build_meeting_view
    return {name: build_meeting_view(**params) for name, params in cases().items()}
