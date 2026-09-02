from __future__ import annotations

import asyncio

from matinier_plugin import PluginError, PluginRuntime
from meeting_assistant.contracts import PLUGIN_ID, PLUGIN_VERSION
from meeting_assistant.session import MeetingSession


class MeetingAssistantPlugin:
    def __init__(self, runtime):
        self.runtime = runtime
        self.sessions = {}
        self._scope_locks = {}
        self._stopping = False

    async def initialize(self, params):
        if (params.get("plugin_id"), params.get("version"), params.get("protocol_version")) != (PLUGIN_ID, PLUGIN_VERSION, "1.0"):
            raise PluginError("plugin.protocol.invalid", "Plugin identity mismatch")
        return {"plugin_id": PLUGIN_ID, "version": PLUGIN_VERSION, "protocol_version": "1.0", "host_api": params.get("host_api")}

    async def open_session(self, params):
        scope = params.get("session_scope")
        if not isinstance(scope, str) or not scope:
            raise PluginError("plugin.scope.invalid", "Session scope is invalid")
        lock = self._scope_locks.setdefault(scope, asyncio.Lock())
        async with lock:
            if self._stopping:
                raise PluginError("plugin.scope.invalid", "Plugin is stopping")
            previous = self.sessions.pop(scope, None)
            if previous:
                await previous.close()
            session = MeetingSession(self.runtime, create_task=self.runtime.create_task)
            try:
                result = await session.open(params)
            except BaseException:
                await session.close()
                raise
            self.sessions[scope] = session
            return result

    def _session(self, params):
        session = self.sessions.get(params.get("session_scope"))
        if session is None:
            raise PluginError("plugin.scope.invalid", "Session scope is invalid")
        return session

    async def event_batch(self, params):
        return await self._session(params).event_batch(params)

    async def command(self, params):
        try:
            return await self._session(params).command(params)
        except ValueError as error:
            raise PluginError("plugin.protocol.invalid", "Invalid meeting command") from error

    async def heartbeat(self, _params):
        return {"ok": True, "status": "degraded" if any(s.error_code for s in self.sessions.values()) else "ready"}

    async def close_session(self, params):
        scope = params.get("session_scope")
        if not isinstance(scope, str):
            raise PluginError("plugin.scope.invalid", "Session scope is invalid")
        async with self._scope_locks.setdefault(scope, asyncio.Lock()):
            session = self.sessions.pop(scope, None)
            if session:
                await session.close()
        return {"closed": True}

    async def shutdown(self, _params):
        self._stopping = True
        await asyncio.gather(*(self.close_session({"session_scope": scope}) for scope in tuple(self._scope_locks)))
        return {"stopped": True}


async def main():
    runtime = PluginRuntime()
    plugin = MeetingAssistantPlugin(runtime)
    for method, handler in {"plugin.initialize": plugin.initialize, "plugin.heartbeat": plugin.heartbeat,
        "session.open": plugin.open_session, "session.close": plugin.close_session,
        "event.batch": plugin.event_batch, "command.execute": plugin.command, "plugin.shutdown": plugin.shutdown}.items():
        runtime.register(method, handler)
    try:
        await runtime.run()
    finally:
        await plugin.shutdown({})


if __name__ == "__main__":
    asyncio.run(main())
