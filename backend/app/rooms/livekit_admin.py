from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from livekit import api

from app.settings import Settings


class RoomAdmin(Protocol):
    async def ensure_agent_dispatch(
        self,
        room_name: str,
        agent_name: str,
    ) -> None: ...

    async def delete_room(self, room_name: str) -> None: ...

    async def remove_participant(
        self,
        room_name: str,
        participant_identity: str,
    ) -> None: ...

    async def aclose(self) -> None: ...


RoomAdminFactory = Callable[[Settings], RoomAdmin]


class LiveKitRoomAdmin:
    def __init__(self, settings: Settings) -> None:
        self._client = api.LiveKitAPI(
            settings.livekit_url,
            settings.livekit_api_key,
            settings.livekit_api_secret,
        )

    async def ensure_agent_dispatch(
        self,
        room_name: str,
        agent_name: str,
    ) -> None:
        dispatches = await self._client.agent_dispatch.list_dispatch(
            room_name
        )
        active_statuses = {
            api.JobStatus.JS_PENDING,
            api.JobStatus.JS_RUNNING,
        }
        for dispatch in dispatches:
            if dispatch.agent_name != agent_name or dispatch.state.deleted_at:
                continue
            jobs = tuple(dispatch.state.jobs)
            if not jobs or any(
                job.state.status in active_statuses for job in jobs
            ):
                return
            await self._client.agent_dispatch.delete_dispatch(
                dispatch.id,
                room_name,
            )

        await self._client.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room_name,
                restart_policy=api.JobRestartPolicy.JRP_ON_FAILURE,
            )
        )

    async def delete_room(self, room_name: str) -> None:
        rooms = await self._client.room.list_rooms(
            api.ListRoomsRequest(names=[room_name])
        )
        if any(room.name == room_name for room in rooms.rooms):
            await self._client.room.delete_room(
                api.DeleteRoomRequest(room=room_name)
            )

    async def remove_participant(
        self,
        room_name: str,
        participant_identity: str,
    ) -> None:
        await self._client.room.remove_participant(
            api.RoomParticipantIdentity(
                room=room_name,
                identity=participant_identity,
            )
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def build_room_admin(settings: Settings) -> RoomAdmin:
    return LiveKitRoomAdmin(settings)
