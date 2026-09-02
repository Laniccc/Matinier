"""Server-owned intent records; these are not user-grant API request models."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator


Hash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
HostAction = Literal[
    "meeting.analysis.activate", "meeting.analysis.deactivate", "meeting.ask",
    "meeting.mark.create", "meeting.mark.accept", "meeting.mark.dismiss",
    "meeting.execute", "meeting.input", "meeting.cancel",
]


class HostActionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    plugin_id: str = Field(min_length=5, max_length=128)
    plugin_version: str = Field(min_length=1, max_length=64)
    media_session_id: str = Field(min_length=1, max_length=36)
    legacy_session_id: str = Field(min_length=1, max_length=36)
    actor_id: Id
    authority_epoch: int = Field(ge=0, strict=True)
    analysis_epoch: int | None = Field(default=None, ge=0, strict=True)
    action: HostAction
    payload_hash: Hash
    candidate_revisions: dict[Id, Annotated[int, Field(ge=1, strict=True)]] = Field(default_factory=dict, max_length=20)
    snapshot_id: Id | None = None
    team_id: Id | None = None
    max_side_effects: int = Field(default=0, ge=0, le=20, strict=True)
    source: Literal["plugin", "host_history"] = "plugin"
    action_grant_id: Id | None = None
    capability_grant_id: Id | None = None

    @model_validator(mode="after")
    def bounded_authority(self):
        if self.source == "host_history" and self.action != "meeting.cancel":
            raise ValueError("Host history only permits cancellation")
        if self.action.startswith("meeting.analysis.") and self.analysis_epoch is None:
            raise ValueError("analysis controls require analysis epoch")
        if self.action == "meeting.execute":
            if not self.team_id or not self.candidate_revisions or self.max_side_effects < 1:
                raise ValueError("execution requires candidates, fixed team and positive budget")
            if self.max_side_effects > len(self.candidate_revisions):
                raise ValueError("execution budget exceeds selected candidates")
        elif self.max_side_effects or self.team_id or self.candidate_revisions:
            raise ValueError("only execution intents carry external-write authority")
        return self


class HostActionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    token_hash: Hash = Field(repr=False)
    scope: HostActionScope
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def short_lived(self):
        lifetime = self.expires_at - self.created_at
        if not dt.timedelta(0) < lifetime <= dt.timedelta(minutes=15):
            raise ValueError("intent lifetime must be positive and at most 15 minutes")
        return self


class HostActionPrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    action: HostAction
    request_id: Id
    plugin_version: str | None = Field(default=None, min_length=1, max_length=64)
    view_version: int | None = Field(default=None, ge=1, strict=True)
    arguments: dict[str, JsonValue] = Field(default_factory=dict, max_length=12)

    @model_validator(mode="after")
    def bounded_arguments(self):
        from app.media.contracts import validate_bounded_json
        validate_bounded_json(self.arguments, max_bytes=32_768, max_depth=8)
        return self


class HostActionConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    preview_id: str = Field(min_length=32, max_length=128)
    preview_hash: Hash
    confirmed: bool = Field(strict=True)
