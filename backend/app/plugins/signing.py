from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field


class SignatureEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    algorithm: str = Field(pattern=r"^Ed25519$")
    publisher: str = Field(min_length=1, max_length=128)
    public_key: str = Field(min_length=40, max_length=128)
    assets_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signature: str = Field(min_length=80, max_length=128)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def assets_digest(entries: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(entries):
        encoded_name = name.encode("utf-8")
        content_hash = hashlib.sha256(entries[name]).digest()
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(content_hash)
    return "sha256:" + digest.hexdigest()


def signed_material(
    manifest: object,
    image_digest: str,
    package_assets_digest: str,
) -> bytes:
    return (
        canonical_json_bytes(manifest)
        + b"\n"
        + image_digest.encode("ascii")
        + b"\n"
        + package_assets_digest.encode("ascii")
    )


def decode_public_key(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ValueError("publisher public key is not valid base64") from error
    if len(raw) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    return raw


def public_key_fingerprint(public_key_text: str) -> str:
    return "sha256:" + hashlib.sha256(decode_public_key(public_key_text)).hexdigest()


def verify_signature(
    envelope: SignatureEnvelope,
    *,
    manifest: object,
    image_digest: str,
    package_assets_digest: str,
) -> None:
    public_key = Ed25519PublicKey.from_public_bytes(decode_public_key(envelope.public_key))
    try:
        signature = base64.b64decode(envelope.signature, validate=True)
    except ValueError as error:
        raise ValueError("signature is not valid base64") from error
    try:
        public_key.verify(
            signature,
            signed_material(manifest, image_digest, package_assets_digest),
        )
    except InvalidSignature as error:
        raise ValueError("plugin package signature is invalid") from error

