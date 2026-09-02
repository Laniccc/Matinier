from __future__ import annotations

from dataclasses import dataclass, replace

from app.assistant.tools.contracts import ToolAdapter, ToolSpec
from app.assistant.tools.provider import ToolProvider


class ToolRegistrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    adapter: ToolAdapter


class ToolRegistry:
    """Startup-owned registry with one active version for each tool name."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, adapter: ToolAdapter) -> RegisteredTool:
        if spec.name in self._tools:
            current = self._tools[spec.name].spec
            raise ToolRegistrationError(
                f"tool {spec.name} is already registered at version {current.version}"
            )
        provider_name = adapter.provider_name.strip()
        if not provider_name or len(provider_name) > 64:
            raise ToolRegistrationError(
                "tool adapter provider_name must contain at most 64 characters"
            )
        if not isinstance(adapter, ToolAdapter):
            raise ToolRegistrationError(
                "tool adapter must implement execute and reconcile"
            )
        registered = RegisteredTool(spec=spec, adapter=adapter)
        self._tools[spec.name] = registered
        return registered

    def register_provider(self, provider: ToolProvider) -> tuple[RegisteredTool, ...]:
        if not isinstance(provider, ToolProvider):
            raise ToolRegistrationError("tool provider must implement registrations")
        provider_name = provider.provider_name.strip()
        if not provider_name or len(provider_name) > 64:
            raise ToolRegistrationError("tool provider_name must contain at most 64 characters")
        registrations = tuple(provider.registrations())
        if len(registrations) > 32:
            raise ToolRegistrationError("tool provider returned more than 32 registrations")
        names = [registration.spec.name for registration in registrations]
        if len(names) != len(set(names)):
            raise ToolRegistrationError("tool provider returned duplicate tool names")
        accepted = []
        for registration in registrations:
            spec = registration.spec
            if spec.effect == "external_write":
                spec = replace(spec, allowed_profiles=frozenset({"action_run"}))
            accepted.append(self.register(spec, registration.adapter))
        return tuple(accepted)

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise LookupError(f"Tool is not registered: {name}") from error

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name].spec for name in sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools


__all__ = ["RegisteredTool", "ToolRegistrationError", "ToolRegistry"]
