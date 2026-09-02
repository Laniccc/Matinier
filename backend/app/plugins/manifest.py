from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.contract_versions import HOST_API_VERSION
from app.plugins.contracts import PluginManifest


_RANGE_TOKEN = re.compile(
    r"^(?P<operator>>=|<=|>|<|=|\^|~)?(?P<version>\d+(?:\.\d+){1,2})$"
)


def load_manifest(raw: bytes) -> tuple[PluginManifest, dict[str, object]]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("plugin.json must contain UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("plugin.json root must be an object")
    try:
        manifest = PluginManifest.model_validate(value)
    except ValidationError as error:
        raise ValueError("plugin.json does not match manifest schema") from error
    return manifest, value


def host_api_compatible(
    requirement: str,
    host_version: str = HOST_API_VERSION,
) -> bool:
    host = _version_tuple(host_version)
    for token in requirement.split():
        match = _RANGE_TOKEN.fullmatch(token)
        if match is None:
            return False
        target = _version_tuple(match.group("version"))
        operator = match.group("operator") or "="
        if operator == ">=" and not host >= target:
            return False
        if operator == "<=" and not host <= target:
            return False
        if operator == ">" and not host > target:
            return False
        if operator == "<" and not host < target:
            return False
        if operator == "=" and not host == target:
            return False
        if operator == "^" and not (host >= target and host < _caret_upper(target)):
            return False
        if operator == "~" and not (host >= target and host < _tilde_upper(target)):
            return False
    return True


def _version_tuple(value: str) -> tuple[int, int, int]:
    core = value.split("-", 1)[0].split("+", 1)[0]
    parts = [int(part) for part in core.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def _caret_upper(value: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, patch = value
    if major:
        return major + 1, 0, 0
    if minor:
        return 0, minor + 1, 0
    return 0, 0, patch + 1


def _tilde_upper(value: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, _patch = value
    return major, minor + 1, 0

