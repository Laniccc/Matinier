#!/usr/bin/env python3
"""Matinier diagnostic plugin using only the Python standard library."""

from __future__ import annotations

import itertools
import json
import sys
from typing import Any


PLUGIN_ID = "com.matinier.diagnostic"
PLUGIN_VERSION = "1.0.0"
PROTOCOL_VERSION = "1.0"


def build_view(session_scope: str, view_version: int, final_count: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "surface": "panel",
        "view_id": "diagnostics",
        "view_version": view_version,
        "root": {
            "id": "diagnostic-section",
            "type": "section",
            "title": "Diagnostic plugin",
            "children": [
                {
                    "id": "runtime-badge",
                    "type": "badge",
                    "text": "Sandbox connected",
                    "tone": "success",
                },
                {
                    "id": "final-count",
                    "type": "metric",
                    "label": "Final transcript events",
                    "value": str(final_count),
                    "detail": f"Session scope {session_scope[:8]}…",
                },
                {
                    "id": "refresh-button",
                    "type": "button",
                    "label": "Refresh diagnostics",
                    "action_id": "refresh",
                    "tone": "primary",
                },
            ],
        },
        "actions": [
            {"id": "refresh", "kind": "command", "command": "refresh"}
        ],
    }


class DiagnosticPlugin:
    def __init__(self) -> None:
        self._request_ids = itertools.count(1_000)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.running = True

    def handle(self, message: object) -> list[dict[str, Any]]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return [self._error(None, "plugin.protocol.invalid", "Invalid JSON-RPC envelope")]
        if "method" not in message:
            return []
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            return [self._error(request_id, "plugin.protocol.invalid", "Invalid request")]
        try:
            if method == "plugin.initialize":
                return [self._initialize(request_id, params)]
            if method == "plugin.heartbeat":
                return [self._result(request_id, {"ok": True})]
            if method == "session.open":
                return self._open_session(request_id, params)
            if method == "session.close":
                self.sessions.pop(str(params.get("session_scope", "")), None)
                return [] if request_id is None else [self._result(request_id, {"closed": True})]
            if method == "event.batch":
                return self._event_batch(request_id, params)
            if method == "command.execute":
                return self._command(request_id, params)
            if method == "plugin.shutdown":
                self.running = False
                return [] if request_id is None else [self._result(request_id, {"stopped": True})]
        except (KeyError, TypeError, ValueError):
            return [self._error(request_id, "plugin.protocol.invalid", "Invalid method parameters")]
        return [
            self._error(
                request_id,
                "plugin.protocol.method_not_found",
                "Method is not supported",
            )
        ]

    def _initialize(self, request_id: object, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("plugin_id") != PLUGIN_ID or params.get("version") != PLUGIN_VERSION:
            return self._error(
                request_id,
                "plugin.protocol.invalid",
                "Plugin identity mismatch",
            )
        if params.get("protocol_version") != PROTOCOL_VERSION:
            return self._error(
                request_id,
                "plugin.protocol.invalid",
                "Protocol version mismatch",
            )
        return self._result(
            request_id,
            {
                "plugin_id": PLUGIN_ID,
                "version": PLUGIN_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "host_api": params.get("host_api"),
            },
        )

    def _open_session(
        self,
        request_id: object,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        scope = self._scope(params)
        self.sessions[scope] = {
            "media_session_id": str(params["media_session_id"]),
            "view_version": 1,
            "final_count": 0,
            "last_sequence": int(params.get("after_sequence", 0)),
            "state_version": 0,
        }
        return [
            self._result(request_id, {"opened": True}),
            self._capability(scope, "state.get", {"key": "diagnostics"}),
            self._publish_view(scope),
        ]

    def _event_batch(
        self,
        request_id: object,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        scope = self._scope(params)
        state = self.sessions[scope]
        events = params.get("events")
        if not isinstance(events, list):
            raise ValueError("events")
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("event")
            sequence = int(event["sequence"])
            state["last_sequence"] = max(int(state["last_sequence"]), sequence)
            if event.get("event_type") == "transcript.final":
                state["final_count"] = int(state["final_count"]) + 1
        state["view_version"] = int(state["view_version"]) + 1
        return [
            self._result(
                request_id,
                {"acknowledged_sequence": state["last_sequence"]},
            ),
            self._capability(
                scope,
                "state.put",
                {
                    "key": "diagnostics",
                    "value": {
                        "final_count": state["final_count"],
                        "last_sequence": state["last_sequence"],
                    },
                    "expected_version": state["state_version"],
                },
            ),
            self._publish_view(scope),
        ]

    def _command(
        self,
        request_id: object,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        scope = self._scope(params)
        if params.get("command") != "refresh":
            return [
                self._error(
                    request_id,
                    "plugin.protocol.method_not_found",
                    "Unknown plugin command",
                )
            ]
        state = self.sessions[scope]
        state["view_version"] = int(state["view_version"]) + 1
        return [
            self._result(request_id, {"accepted": True}),
            self._publish_view(scope),
        ]

    def _publish_view(self, scope: str) -> dict[str, Any]:
        state = self.sessions[scope]
        view = build_view(
            scope,
            int(state["view_version"]),
            int(state["final_count"]),
        )
        return self._capability(
            scope,
            "ui.publish",
            {
                "surface": view["surface"],
                "view_id": view["view_id"],
                "view_version": view["view_version"],
                "view": view["root"],
                "actions": view["actions"],
            },
        )

    def _capability(
        self,
        scope: str,
        capability: str,
        input_value: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": next(self._request_ids),
            "method": "capability.invoke",
            "params": {
                "capability": capability,
                "session_scope": scope,
                "input": input_value,
            },
        }

    def _scope(self, params: dict[str, Any]) -> str:
        scope = params.get("session_scope")
        if not isinstance(scope, str) or scope not in self.sessions and "media_session_id" not in params:
            raise ValueError("scope")
        return scope

    @staticmethod
    def _result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: object,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message, "data": None},
        }


def main() -> int:
    plugin = DiagnosticPlugin()
    for raw_line in sys.stdin.buffer:
        try:
            message = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            messages = [plugin._error(None, "plugin.protocol.invalid", "Invalid JSON")]
        else:
            messages = plugin.handle(message)
        for output in messages:
            encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.write(encoded + "\n")
        sys.stdout.flush()
        if not plugin.running:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

