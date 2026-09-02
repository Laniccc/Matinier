"""Deterministic identifiers and fault outcomes for scripted local evals."""
from __future__ import annotations

import hashlib
import uuid


def deterministic_id(seed: int, *parts: object) -> str:
    value = ":".join((str(seed), *(str(part) for part in parts)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"matinier-eval:{value}"))


def digest(*parts: object) -> str:
    return hashlib.sha256(":".join(str(part) for part in parts).encode("utf-8")).hexdigest()


__all__ = ["deterministic_id", "digest"]
