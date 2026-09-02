"""Provider-neutral task domain and adapters for the meeting Agent."""

from app.task_system.bootstrap import (
    DisabledTaskSystemAdapter,
    build_task_system_adapter,
    validate_task_system_adapter,
)
from app.task_system.contracts import TaskSystemAdapter
from app.task_system.fake import FakeTaskSystemAdapter
from app.task_system.identity import IdentityResolver
from app.task_system.linear import (
    LinearTaskSystemAdapter,
    LinearTaskSystemError,
)
from app.task_system.models import (
    ExternalMember,
    ExternalTask,
    IdentityBinding,
    PersonMention,
    ResolvedIdentity,
    TaskCreateResult,
    TaskDecisionContext,
    TaskDraft,
    TaskReconciliationResult,
    TaskSearchQuery,
    TaskSearchResult,
    TaskServiceOutcome,
    TaskSystemConnection,
)
from app.task_system.service import (
    TaskNeedsInput,
    TaskSystemService,
    derive_task_create_key,
)
from app.task_system.tools import (
    TaskSystemInvocationPolicy,
    TaskSystemToolProvider,
    register_task_tools,
)

__all__ = [
    "ExternalMember",
    "ExternalTask",
    "DisabledTaskSystemAdapter",
    "FakeTaskSystemAdapter",
    "IdentityBinding",
    "IdentityResolver",
    "LinearTaskSystemAdapter",
    "LinearTaskSystemError",
    "PersonMention",
    "ResolvedIdentity",
    "TaskCreateResult",
    "TaskDecisionContext",
    "TaskDraft",
    "TaskNeedsInput",
    "TaskReconciliationResult",
    "TaskSearchQuery",
    "TaskSearchResult",
    "TaskServiceOutcome",
    "TaskSystemAdapter",
    "TaskSystemConnection",
    "TaskSystemInvocationPolicy",
    "TaskSystemToolProvider",
    "TaskSystemService",
    "build_task_system_adapter",
    "derive_task_create_key",
    "register_task_tools",
    "validate_task_system_adapter",
]
