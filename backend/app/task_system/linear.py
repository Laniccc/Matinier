from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.task_system.models import (
    ExternalMember,
    ExternalTask,
    TaskCreateResult,
    TaskDraft,
    TaskPriority,
    TaskReconciliationResult,
    TaskSearchQuery,
    TaskSearchResult,
    TaskStatus,
    TaskSystemConnection,
)


_ISSUE_FIELDS = """
    id
    identifier
    url
    title
    description
    priority
    dueDate
    createdAt
    updatedAt
    completedAt
    canceledAt
    archivedAt
    assignee { id name email }
    team { id }
    project { id }
    state { type }
"""

VALIDATE_TEAM = """
query ValidateTeam($teamId: String!) {
  team(id: $teamId) {
    id
    organization { id }
  }
}
"""

SEARCH_ISSUES = f"""
query SearchIssues($limit: Int!, $filter: IssueFilter!) {{
  issues(first: $limit, filter: $filter, orderBy: updatedAt) {{
    nodes {{
      {_ISSUE_FIELDS}
    }}
  }}
}}
"""

GET_ISSUE = f"""
query GetIssue($issueId: String!) {{
  issue(id: $issueId) {{
    {_ISSUE_FIELDS}
  }}
}}
"""

CREATE_ISSUE = f"""
mutation CreateIssue($input: IssueCreateInput!) {{
  issueCreate(input: $input) {{
    success
    issue {{
      {_ISSUE_FIELDS}
    }}
  }}
}}
"""

FIND_ISSUE_BY_ACTION_KEY = f"""
query FindIssueByActionKey($limit: Int!, $filter: IssueFilter!) {{
  issues(first: $limit, filter: $filter, orderBy: updatedAt) {{
    nodes {{
      {_ISSUE_FIELDS}
    }}
  }}
}}
"""

SEARCH_MEMBERS = """
query SearchMembers($limit: Int!, $filter: UserFilter!) {
  users(first: $limit, filter: $filter) {
    nodes {
      id
      name
      email
      active
      teams { nodes { id } }
    }
  }
}
"""

GET_MEMBER = """
query GetMember($userId: String!) {
  user(id: $userId) {
    id
    name
    email
    active
    teams { nodes { id } }
  }
}
"""

_ACTION_KEY_PATTERN = re.compile(
    r"(?im)^matinier-action-key:\s*([0-9a-f]{64})\s*$"
)
_EVIDENCE_MARKER = "Matinier meeting evidence:"
_MAX_DESCRIPTION_CHARS = 20_000
_LINEAR_PRIORITY = {
    "urgent": 1,
    "high": 2,
    "normal": 3,
    "low": 4,
}
_TASK_PRIORITY = {value: key for key, value in _LINEAR_PRIORITY.items()}


class LinearTaskSystemError(RuntimeError):
    """A bounded, sanitized Linear boundary failure."""


class _GraphQLError(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    message: str = Field(default="Linear GraphQL request failed", max_length=2_000)


class _GraphQLEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    data: dict[str, Any] | None = None
    errors: tuple[_GraphQLError, ...] = ()


def linear_priority(priority: TaskPriority | None) -> int | None:
    return _LINEAR_PRIORITY.get(priority) if priority is not None else None


class LinearTaskSystemAdapter:
    provider_name = "linear"

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        team_id: str,
        default_project_id: str | None = None,
        request_timeout_seconds: float = 10.0,
        max_search_results: int = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_url.strip():
            raise ValueError("Linear API URL is required")
        if not api_key.strip():
            raise ValueError("Linear API key is required")
        if not team_id.strip():
            raise ValueError("Linear Team ID is required")
        if request_timeout_seconds <= 0:
            raise ValueError("Linear request timeout must be positive")
        if not 1 <= max_search_results <= 20:
            raise ValueError("Linear search limit must be between one and twenty")
        self._api_url = api_url.strip()
        self._api_key = api_key.strip()
        self._timeout = request_timeout_seconds
        self._max_search_results = max_search_results
        self._transport = transport
        self._connection = TaskSystemConnection(
            provider=self.provider_name,
            team_id=team_id.strip(),
            default_project_id=(
                default_project_id.strip()
                if default_project_id is not None and default_project_id.strip()
                else None
            ),
        )

    @property
    def connection(self) -> TaskSystemConnection:
        return self._connection

    async def validate_connection(self) -> TaskSystemConnection:
        data = await self._execute(
            VALIDATE_TEAM,
            {"teamId": self.connection.team_id},
        )
        team = _mapping(data.get("team"), "Linear Team")
        team_id = _required_text(team.get("id"), "Linear Team ID")
        if team_id != self.connection.team_id:
            raise LinearTaskSystemError("Linear returned a different configured Team")
        organization = team.get("organization")
        workspace_id = (
            _required_text(organization.get("id"), "Linear workspace ID")
            if isinstance(organization, Mapping)
            else None
        )
        self._connection = self.connection.model_copy(
            update={"workspace_id": workspace_id}
        )
        return self.connection

    async def search(self, query: TaskSearchQuery) -> TaskSearchResult:
        if query.action_key is not None:
            return await self._search_by_action_key(
                query.action_key,
                limit=query.limit,
            )
        issue_filter: dict[str, object] = {
            "team": {"id": {"eq": self.connection.team_id}}
        }
        if query.title is not None:
            issue_filter["title"] = {
                "containsIgnoreCase": " ".join(query.title.split())
            }
        if query.assignee_ref is not None:
            issue_filter["assignee"] = {
                "id": {"eq": query.assignee_ref}
            }
        if query.due_at is not None:
            issue_filter["dueDate"] = {
                "eq": _linear_due_date(query.due_at)
            }
        data = await self._execute(
            SEARCH_ISSUES,
            {
                "limit": min(query.limit, self._max_search_results),
                "filter": issue_filter,
            },
        )
        return TaskSearchResult(tasks=self._issues_from_connection(data))

    async def get(self, task_ref: str) -> ExternalTask:
        data = await self._execute(GET_ISSUE, {"issueId": task_ref})
        raw_issue = data.get("issue")
        if raw_issue is None:
            raise LookupError("Linear Issue was not found")
        return self._normalize_issue(_mapping(raw_issue, "Linear Issue"))

    async def create(
        self,
        draft: TaskDraft,
        *,
        action_key: str,
    ) -> TaskCreateResult:
        issue_input: dict[str, object] = {
            "teamId": self.connection.team_id,
            "title": draft.title,
            "description": _render_description(draft, action_key),
        }
        if self.connection.default_project_id is not None:
            issue_input["projectId"] = self.connection.default_project_id
        if draft.assignee_ref is not None:
            issue_input["assigneeId"] = draft.assignee_ref
        if draft.due_at is not None:
            issue_input["dueDate"] = _linear_due_date(draft.due_at)
        priority = linear_priority(draft.priority)
        if priority is not None:
            issue_input["priority"] = priority

        data = await self._execute(CREATE_ISSUE, {"input": issue_input})
        payload = _mapping(data.get("issueCreate"), "Linear issueCreate")
        if payload.get("success") is not True:
            raise LinearTaskSystemError("Linear did not confirm Issue creation")
        task = self._normalize_issue(
            _mapping(payload.get("issue"), "created Linear Issue")
        )
        if task.action_key != action_key:
            raise LinearTaskSystemError(
                "Created Linear Issue omitted its action-key marker"
            )
        return TaskCreateResult(task=task, created=True)

    async def reconcile_create(
        self,
        *,
        action_key: str,
    ) -> TaskReconciliationResult:
        result = await self._search_by_action_key(
            action_key,
            limit=self._max_search_results,
            require_evidence_marker=True,
        )
        matches = result.tasks
        status = "none" if not matches else (
            "single" if len(matches) == 1 else "multiple"
        )
        return TaskReconciliationResult(status=status, matches=matches)

    async def search_members(self, query: str) -> tuple[ExternalMember, ...]:
        normalized = " ".join(query.split())
        if not normalized:
            return ()
        data = await self._execute(
            SEARCH_MEMBERS,
            {
                "limit": self._max_search_results,
                "filter": {
                    "or": [
                        {"name": {"containsIgnoreCase": normalized}},
                        {"email": {"containsIgnoreCase": normalized}},
                    ]
                },
            },
        )
        users = _connection_nodes(data.get("users"), "Linear users")
        return tuple(self._normalize_member(user) for user in users)

    async def get_member(self, external_user_id: str) -> ExternalMember:
        data = await self._execute(GET_MEMBER, {"userId": external_user_id})
        raw_user = data.get("user")
        if raw_user is None:
            raise LookupError("Linear member was not found")
        return self._normalize_member(_mapping(raw_user, "Linear member"))

    async def _search_by_action_key(
        self,
        action_key: str,
        *,
        limit: int,
        require_evidence_marker: bool = False,
    ) -> TaskSearchResult:
        marker = f"matinier-action-key: {action_key}"
        data = await self._execute(
            FIND_ISSUE_BY_ACTION_KEY,
            {
                "limit": min(limit, self._max_search_results),
                "filter": {
                    "team": {"id": {"eq": self.connection.team_id}},
                    "description": {"contains": marker},
                },
            },
        )
        tasks = tuple(
            task
            for task in (
                self._normalize_issue(issue)
                for issue in _connection_nodes(
                    data.get("issues"),
                    "Linear issues",
                )
            )
            if task.action_key == action_key
            and (
                not require_evidence_marker
                or _EVIDENCE_MARKER in (task.description or "")
            )
        )
        return TaskSearchResult(tasks=tasks)

    def _issues_from_connection(
        self,
        data: Mapping[str, Any],
    ) -> tuple[ExternalTask, ...]:
        return tuple(
            self._normalize_issue(issue)
            for issue in _connection_nodes(data.get("issues"), "Linear issues")
        )

    def _normalize_issue(self, value: Mapping[str, Any]) -> ExternalTask:
        team = _mapping(value.get("team"), "Linear Issue Team")
        team_id = _required_text(team.get("id"), "Linear Issue Team ID")
        if team_id != self.connection.team_id:
            raise LinearTaskSystemError(
                "Linear Issue belongs to a different configured Team"
            )
        description = _optional_text(value.get("description"))
        project = value.get("project")
        assignee = value.get("assignee")
        state = value.get("state")
        return ExternalTask(
            external_id=_required_text(value.get("id"), "Linear Issue ID"),
            identifier=_required_text(
                value.get("identifier"),
                "Linear Issue identifier",
            ),
            url=_required_text(value.get("url"), "Linear Issue URL"),
            title=_required_text(value.get("title"), "Linear Issue title"),
            description=description,
            assignee_ref=(
                _required_text(assignee.get("id"), "Linear assignee ID")
                if isinstance(assignee, Mapping)
                else None
            ),
            due_at=_parse_due_date(value.get("dueDate")),
            priority=_TASK_PRIORITY.get(value.get("priority")),
            status=_normalize_status(value, state),
            team_id=team_id,
            project_id=(
                _required_text(project.get("id"), "Linear project ID")
                if isinstance(project, Mapping)
                else None
            ),
            action_key=_extract_action_key(description),
            created_at=_parse_timestamp(value.get("createdAt"), "createdAt"),
            updated_at=_parse_timestamp(value.get("updatedAt"), "updatedAt"),
        )

    @staticmethod
    def _normalize_member(value: Mapping[str, Any]) -> ExternalMember:
        teams = _connection_nodes(value.get("teams"), "Linear member Teams")
        return ExternalMember(
            external_user_id=_required_text(value.get("id"), "Linear user ID"),
            display_name=_required_text(value.get("name"), "Linear user name"),
            email=_optional_text(value.get("email")),
            team_ids=tuple(
                _required_text(team.get("id"), "Linear Team ID")
                for team in teams
            ),
            active=value.get("active") is True,
        )

    async def _execute(
        self,
        document: str,
        variables: Mapping[str, object],
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._api_url,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": self._api_key,
                    },
                    json={"query": document, "variables": dict(variables)},
                )
        except httpx.HTTPError as error:
            raise LinearTaskSystemError(
                f"Linear transport failed: {type(error).__name__}"
            ) from None
        if not 200 <= response.status_code < 300:
            raise LinearTaskSystemError(
                f"Linear returned HTTP {response.status_code}"
            )
        try:
            envelope = _GraphQLEnvelope.model_validate(response.json())
        except (ValueError, ValidationError):
            raise LinearTaskSystemError(
                "Linear returned an invalid GraphQL envelope"
            ) from None
        if envelope.errors:
            messages = "; ".join(
                _sanitize_external_message(error.message, secret=self._api_key)
                for error in envelope.errors[:3]
            )
            raise LinearTaskSystemError(
                f"Linear GraphQL request failed: {messages}"
            )
        if envelope.data is None:
            raise LinearTaskSystemError("Linear GraphQL response contained no data")
        return envelope.data


def _render_description(draft: TaskDraft, action_key: str) -> str:
    body_lines = []
    for line in (draft.description or "").splitlines():
        normalized = line.strip().casefold()
        if normalized.startswith("meeting evidence:"):
            continue
        if normalized.startswith("matinier meeting evidence:"):
            continue
        if normalized.startswith("matinier-action-key:"):
            continue
        body_lines.append(line.rstrip())
    body = "\n".join(body_lines).strip()
    footer = (
        "---\n"
        f"{_EVIDENCE_MARKER} {', '.join(draft.source_evidence_ids)}\n"
        f"matinier-action-key: {action_key}"
    )
    available = _MAX_DESCRIPTION_CHARS - len(footer) - 2
    if available < 0:
        raise ValueError("Linear evidence footer exceeds description limit")
    bounded_body = body[:available].rstrip()
    return f"{bounded_body}\n\n{footer}" if bounded_body else footer


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LinearTaskSystemError(f"{label} was missing from the response")
    return value


def _connection_nodes(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    connection = _mapping(value, label)
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise LinearTaskSystemError(f"{label} nodes were missing from the response")
    return tuple(_mapping(node, f"{label} node") for node in nodes)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LinearTaskSystemError(f"{label} was missing from the response")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _linear_due_date(value: dt.datetime) -> str:
    return value.date().isoformat()


def _parse_due_date(value: object) -> dt.datetime | None:
    if value is None:
        return None
    raw = _required_text(value, "Linear due date")
    try:
        parsed = dt.date.fromisoformat(raw)
    except ValueError:
        raise LinearTaskSystemError("Linear due date was invalid") from None
    return dt.datetime.combine(parsed, dt.time.min, tzinfo=dt.UTC)


def _parse_timestamp(value: object, label: str) -> dt.datetime:
    raw = _required_text(value, f"Linear {label}")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise LinearTaskSystemError(f"Linear {label} was invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _extract_action_key(description: str | None) -> str | None:
    if description is None:
        return None
    match = _ACTION_KEY_PATTERN.search(description)
    return match.group(1) if match is not None else None


def _normalize_status(
    issue: Mapping[str, Any],
    state: object,
) -> TaskStatus:
    state_type = (
        str(state.get("type", "")).casefold()
        if isinstance(state, Mapping)
        else ""
    )
    if issue.get("canceledAt") is not None or state_type in {
        "canceled",
        "cancelled",
    }:
        return "cancelled"
    if issue.get("completedAt") is not None or state_type == "completed":
        return "completed"
    return "active"


def _sanitize_external_message(value: str, *, secret: str) -> str:
    sanitized = value.replace(secret, "[redacted]") if secret else value
    sanitized = " ".join(
        "".join(character for character in sanitized if character.isprintable()).split()
    )
    return (sanitized or "Linear GraphQL request failed")[:300]


__all__ = [
    "CREATE_ISSUE",
    "FIND_ISSUE_BY_ACTION_KEY",
    "GET_ISSUE",
    "GET_MEMBER",
    "LinearTaskSystemAdapter",
    "LinearTaskSystemError",
    "SEARCH_ISSUES",
    "SEARCH_MEMBERS",
    "VALIDATE_TEAM",
    "linear_priority",
]
