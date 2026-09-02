"""Protocol-neutral tool discovery boundary owned by the Host process."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.assistant.tools.contracts import ToolAdapter, ToolSpec


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    spec: ToolSpec
    adapter: ToolAdapter


@runtime_checkable
class ToolProvider(Protocol):
    """Return a bounded, already constructed set of tool registrations."""

    @property
    def provider_name(self) -> str: ...

    def registrations(self) -> tuple[ToolRegistration, ...]: ...


@dataclass(frozen=True, slots=True)
class StaticToolProvider:
    provider_name: str
    tools: tuple[ToolRegistration, ...]

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return self.tools


__all__ = ["StaticToolProvider", "ToolProvider", "ToolRegistration"]
