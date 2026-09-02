from __future__ import annotations

from app.meeting_state.repository import MeetingStateRepository
from app.persistence.database import Database
from app.task_system.contracts import TaskSystemAdapter
from app.task_system.models import (
    ExternalMember,
    IdentityBinding,
    PersonMention,
    ResolvedIdentity,
)


def normalize_person_text(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    if not normalized:
        raise ValueError("person text must not be blank")
    return normalized


class IdentityResolver:
    """Exact-first identity resolution that never guesses a nearby person."""

    def __init__(
        self,
        database: Database,
        adapter: TaskSystemAdapter,
    ) -> None:
        self._database = database
        self._adapter = adapter

    async def resolve(self, mention: PersonMention) -> ResolvedIdentity:
        normalized = normalize_person_text(mention.spoken_text)
        if normalized in {"我", "i", "me"} and (
            mention.authenticated_speaker_actor_id is None
            or mention.authenticated_speaker_actor_id != mention.actor_id
        ):
            return self._placeholder(mention.spoken_text, ambiguous=True)

        if mention.explicit_external_user_id is not None:
            try:
                member = await self._adapter.get_member(
                    mention.explicit_external_user_id
                )
            except LookupError:
                member = None
            if member is not None and self._valid_member(member):
                return self._known(
                    mention.spoken_text,
                    member.external_user_id,
                    source="explicit_id",
                )

        if mention.email is not None:
            members = await self._adapter.search_members(mention.email)
            exact = tuple(
                member
                for member in members
                if member.email is not None
                and normalize_person_text(member.email)
                == normalize_person_text(mention.email)
                and self._valid_member(member)
            )
            if len(exact) == 1:
                return self._known(
                    mention.spoken_text,
                    exact[0].external_user_id,
                    source="email",
                )
            if len(exact) > 1:
                return self._placeholder(mention.spoken_text, ambiguous=True)

        binding = self._binding_for(mention)
        if binding is not None:
            try:
                member = await self._adapter.get_member(binding.external_user_id)
            except LookupError:
                member = None
            if member is not None and self._valid_member(member):
                return self._known(
                    mention.spoken_text,
                    member.external_user_id,
                    source="confirmed_binding",
                )

        members = await self._adapter.search_members(mention.spoken_text)
        exact_names = tuple(
            member
            for member in members
            if normalize_person_text(member.display_name) == normalized
            and self._valid_member(member)
        )
        if len(exact_names) == 1:
            return self._known(
                mention.spoken_text,
                exact_names[0].external_user_id,
                source="exact_display_name",
            )
        return self._placeholder(
            mention.spoken_text,
            ambiguous=len(exact_names) > 1 or bool(members),
        )

    def _binding_for(self, mention: PersonMention) -> IdentityBinding | None:
        with self._database.session() as db_session:
            record = MeetingStateRepository(db_session).find_identity_binding(
                actor_id=mention.actor_id,
                linear_team_id=self._adapter.connection.team_id,
                mention=mention.spoken_text,
            )
            if record is None:
                return None
            return IdentityBinding(
                binding_id=record.id,
                actor_id=record.actor_id,
                team_id=record.linear_team_id,
                normalized_mention=record.normalized_mention,
                external_user_id=record.linear_user_id,
                status=record.status,
                confirmed_by_actor_id=record.confirmed_by_actor_id,
                last_verified_at=record.last_verified_at,
            )

    def _valid_member(self, member: ExternalMember) -> bool:
        return (
            member.active
            and self._adapter.connection.team_id in member.team_ids
        )

    @staticmethod
    def _known(
        spoken_text: str,
        external_user_id: str,
        *,
        source: str,
    ) -> ResolvedIdentity:
        return ResolvedIdentity(
            spoken_text=spoken_text,
            external_user_id=external_user_id,
            resolution="known",
            source=source,
            is_placeholder=False,
        )

    @staticmethod
    def _placeholder(
        spoken_text: str,
        *,
        ambiguous: bool,
    ) -> ResolvedIdentity:
        return ResolvedIdentity(
            spoken_text=spoken_text,
            external_user_id=None,
            resolution="ambiguous" if ambiguous else "missing",
            source="placeholder",
            is_placeholder=True,
        )


__all__ = ["IdentityResolver", "normalize_person_text"]
