#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib

from matinier_plugin import PluginError, PluginRuntime

from course_organizer.session import CourseSession
from course_organizer.view import COURSE_COMMANDS
from course_organizer.version import PLUGIN_VERSION


PLUGIN_ID = "com.matinier.course-organizer"
PROTOCOL_VERSION = "1.0"


class CourseOrganizerPlugin:
    def __init__(self, runtime: PluginRuntime) -> None:
        self.runtime = runtime
        self.sessions: dict[str, CourseSession] = {}

    async def initialize(self, params: dict[str, object]) -> dict[str, object]:
        if (
            params.get("plugin_id") != PLUGIN_ID
            or params.get("version") != PLUGIN_VERSION
            or params.get("protocol_version") != PROTOCOL_VERSION
        ):
            raise PluginError("plugin.protocol.invalid", "Plugin identity mismatch")
        return {
            "plugin_id": PLUGIN_ID,
            "version": PLUGIN_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "host_api": params.get("host_api"),
        }

    async def open_session(self, params: dict[str, object]) -> dict[str, object]:
        scope = params.get("session_scope")
        if not isinstance(scope, str):
            raise PluginError("plugin.scope.invalid", "Session scope is invalid")
        session = CourseSession(
            self.runtime,
            create_task=self.runtime.create_task,
        )
        result = await session.open(params)
        self.sessions[scope] = session
        return result

    async def event_batch(self, params: dict[str, object]) -> dict[str, object]:
        return await self._session(params).event_batch(params)

    async def heartbeat(self, _params: dict[str, object]) -> dict[str, object]:
        status = "ready"
        if any(item.state.status == "degraded" for item in self.sessions.values()):
            status = "degraded"
        return {"ok": True, "status": status}

    async def migrate_state(self, params: dict[str, object]) -> dict[str, object]:
        if (
            params.get("from_schema_version") != 1
            or params.get("to_schema_version") != 1
        ):
            raise PluginError(
                "plugin.protocol.invalid",
                "Unsupported course organizer state migration",
            )
        items = params.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise PluginError(
                "plugin.protocol.invalid",
                "Invalid course organizer state snapshot",
            )
        migrated = []
        for item in items:
            value = item.get("value")
            if item.get("key") == "course-session" and isinstance(value, dict):
                notes = value.get("notes", [])
                needs_replay = any(
                    isinstance(source_id, str) and source_id.startswith(("transcript:", "translation:"))
                    for note in notes if isinstance(note, dict)
                    for source_id in note.get("source_segment_ids", [])
                )
                if needs_replay:
                    # Old-version state remains intact in the Host snapshot.
                    # Rebuild from durable events, never guess translation IDs.
                    value = {**value, "last_sequence": 0, "pending": [], "notes": [],
                             "terminal_job_id": None, "terminal_trigger": None,
                             "terminal_final_sequence": 0, "final_status": "idle",
                             "final_error": None, "status": "ready"}
                    item = {**item, "value": value}
            if (item.get("key") == "course-session" and isinstance(value, dict)
                    and isinstance(value.get("terminal_job_id"), str)
                    and value["terminal_job_id"]):
                job_id = "migrated-" + hashlib.sha256(
                    f"{PLUGIN_VERSION}:{value['terminal_job_id']}".encode("utf-8")
                ).hexdigest()[:32]
                item = {**item, "value": {**value, "terminal_job_id": job_id}}
            migrated.append(item)
        return {"items": migrated}

    async def command(self, params: dict[str, object]) -> dict[str, object]:
        command = params.get("command")
        if command not in COURSE_COMMANDS:
            raise PluginError(
                "plugin.protocol.method_not_found",
                "Unknown course organizer command",
            )
        try:
            return await self._session(params).command(params)
        except ValueError as error:
            raise PluginError(
                "plugin.protocol.invalid",
                "Invalid course organizer command",
            ) from error

    async def close_session(self, params: dict[str, object]) -> dict[str, object]:
        scope = params.get("session_scope")
        session = self.sessions.pop(str(scope), None)
        if session is not None:
            await session.close()
        return {"closed": True}

    async def shutdown(self, _params: dict[str, object]) -> dict[str, object]:
        await asyncio.gather(
            *(item.close() for item in self.sessions.values()),
            return_exceptions=True,
        )
        self.sessions.clear()
        return {"stopped": True}

    def _session(self, params: dict[str, object]) -> CourseSession:
        scope = params.get("session_scope")
        if not isinstance(scope, str) or scope not in self.sessions:
            raise PluginError("plugin.scope.invalid", "Session scope is invalid")
        return self.sessions[scope]


async def main() -> None:
    runtime = PluginRuntime()
    plugin = CourseOrganizerPlugin(runtime)
    runtime.register("plugin.initialize", plugin.initialize)
    runtime.register("plugin.heartbeat", plugin.heartbeat)
    runtime.register("plugin.migrate_state", plugin.migrate_state)
    runtime.register("session.open", plugin.open_session)
    runtime.register("session.close", plugin.close_session)
    runtime.register("event.batch", plugin.event_batch)
    runtime.register("command.execute", plugin.command)
    runtime.register("plugin.shutdown", plugin.shutdown)
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
