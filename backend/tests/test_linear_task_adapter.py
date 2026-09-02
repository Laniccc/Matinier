from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from app.settings import Settings
from app.task_system.bootstrap import build_task_system_adapter
from app.task_system.linear import LinearTaskSystemAdapter, LinearTaskSystemError
from app.task_system.models import TaskDraft, TaskSearchQuery


def test_linear_adapter_search_create_get_happy_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        team_id = "team-linear-test"
        project_id = "project-linear-test"
        assignee_id = "user-linear-test"
        action_key = "a" * 64
        due_at = dt.datetime(2026, 8, 20, 15, 30, tzinfo=dt.UTC)
        operations: list[str] = []
        create_input: dict[str, object] = {}
        issue_node: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "linear-test-key"
            assert request.headers["Content-Type"] == "application/json"
            payload = json.loads(request.content)
            document = payload["query"]
            variables = payload["variables"]
            if "query ValidateTeam" in document:
                operations.append("validate")
                assert variables == {"teamId": team_id}
                data = {
                    "team": {
                        "id": team_id,
                        "organization": {"id": "workspace-linear-test"},
                    }
                }
            elif "query SearchIssues" in document:
                operations.append("search")
                issue_filter = variables["filter"]
                assert variables["limit"] == 7
                assert issue_filter["team"] == {"id": {"eq": team_id}}
                assert issue_filter["title"] == {
                    "containsIgnoreCase": "Release checklist"
                }
                assert issue_filter["assignee"] == {
                    "id": {"eq": assignee_id}
                }
                assert issue_filter["dueDate"] == {"eq": "2026-08-20"}
                data = {"issues": {"nodes": []}}
            elif "mutation CreateIssue" in document:
                operations.append("create")
                create_input.update(variables["input"])
                issue_node.update(
                    {
                        "id": "issue-linear-test",
                        "identifier": "ENG-42",
                        "url": "https://linear.app/acme/issue/ENG-42/release-checklist",
                        "title": create_input["title"],
                        "description": create_input["description"],
                        "priority": create_input["priority"],
                        "dueDate": create_input["dueDate"],
                        "createdAt": "2026-08-12T08:00:00.000Z",
                        "updatedAt": "2026-08-12T08:00:01.000Z",
                        "completedAt": None,
                        "canceledAt": None,
                        "archivedAt": None,
                        "assignee": {
                            "id": assignee_id,
                            "name": "Lin User",
                            "email": "lin@example.com",
                        },
                        "team": {"id": team_id},
                        "project": {"id": project_id},
                        "state": {"type": "started"},
                    }
                )
                data = {
                    "issueCreate": {
                        "success": True,
                        "issue": dict(issue_node),
                    }
                }
            elif "query GetIssue" in document:
                operations.append("get")
                assert variables == {"issueId": "issue-linear-test"}
                data = {"issue": dict(issue_node)}
            else:
                raise AssertionError("Unexpected Linear GraphQL operation")
            return httpx.Response(200, json={"data": data})

        settings = Settings(
            _env_file=None,
            livekit_url="ws://127.0.0.1:7880",
            livekit_api_key="test-key",
            livekit_api_secret="test-secret-that-is-at-least-32-bytes",
            livekit_room_name="test-room",
            data_dir=tmp_path / "data",
            database_url="sqlite://",
            task_system_provider="linear",
            linear_api_url="https://api.linear.app/graphql",
            linear_api_key="linear-test-key",
            linear_team_id=team_id,
            linear_default_project_id=project_id,
            linear_request_timeout_seconds=3,
            linear_max_search_results=20,
        )
        adapter = build_task_system_adapter(
            settings,
            transport=httpx.MockTransport(handler),
        )
        assert isinstance(adapter, LinearTaskSystemAdapter)
        connection = await adapter.validate_connection()
        search = await adapter.search(
            TaskSearchQuery(
                title="Release checklist",
                assignee_ref=assignee_id,
                due_at=due_at,
                limit=7,
            )
        )
        assert search.tasks == ()

        created = await adapter.create(
            TaskDraft(
                title="Release checklist",
                description="Prepare and publish the release checklist.",
                assignee_ref=assignee_id,
                due_at=due_at,
                priority="high",
                source_evidence_ids=("caption:segment-1:r1",),
            ),
            action_key=action_key,
        )
        read_back = await adapter.get(created.task.external_id)

        assert connection.team_id == team_id
        assert connection.workspace_id == "workspace-linear-test"
        assert create_input["teamId"] == team_id
        assert create_input["projectId"] == project_id
        assert create_input["assigneeId"] == assignee_id
        assert create_input["dueDate"] == "2026-08-20"
        assert create_input["priority"] == 2
        assert "Matinier meeting evidence: caption:segment-1:r1" in str(
            create_input["description"]
        )
        assert f"matinier-action-key: {action_key}" in str(
            create_input["description"]
        )
        assert created.created is True
        assert created.task.identifier == "ENG-42"
        assert read_back.external_id == "issue-linear-test"
        assert read_back.identifier == "ENG-42"
        assert read_back.title == "Release checklist"
        assert read_back.url.endswith("/ENG-42/release-checklist")
        assert read_back.due_at is not None
        assert read_back.due_at.date() == due_at.date()
        assert read_back.assignee_ref == assignee_id
        assert read_back.project_id == project_id
        assert read_back.action_key == action_key
        assert operations == ["validate", "search", "create", "get"]

    asyncio.run(scenario())


def test_linear_reconciliation_rejects_match_without_title() -> None:
    async def scenario() -> None:
        action_key = "b" * 64

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert "query FindIssueByActionKey" in payload["query"]
            issue = {
                "id": "issue-missing-title",
                "identifier": "ENG-404",
                "url": "https://linear.app/acme/issue/ENG-404/missing-title",
                "description": (
                    "Matinier meeting evidence: caption:segment-1:r1\n"
                    f"matinier-action-key: {action_key}"
                ),
                "createdAt": "2026-08-12T08:00:00.000Z",
                "updatedAt": "2026-08-12T08:00:01.000Z",
                "completedAt": None,
                "canceledAt": None,
                "archivedAt": None,
                "assignee": None,
                "team": {"id": "team-linear-test"},
                "project": None,
                "state": {"type": "started"},
            }
            return httpx.Response(
                200,
                json={"data": {"issues": {"nodes": [issue]}}},
            )

        adapter = LinearTaskSystemAdapter(
            api_url="https://api.linear.app/graphql",
            api_key="linear-test-key",
            team_id="team-linear-test",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(LinearTaskSystemError, match="Issue title"):
            await adapter.reconcile_create(action_key=action_key)

    asyncio.run(scenario())
