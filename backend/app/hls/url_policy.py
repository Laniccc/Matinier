from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


AddressResolver = Callable[[str, int], Awaitable[Iterable[str]]]
RedirectProbe = Callable[[str, float, float], Awaitable[str | None]]


class HLSURLValidationError(ValueError):
    """Raised when a remote media URL is not safe for server-side fetching."""


@dataclass(frozen=True)
class ValidatedHLSURL:
    fetch_url: str
    display_url: str
    resolved_host: str
    resolved_ips: tuple[str, ...]
    redirect_count: int


@dataclass(frozen=True)
class RemoteMediaUrlDecision:
    """Auditable result of validating a remote media URL and its redirects."""

    normalized_url: str | None
    display_url: str | None
    resolved_host: str | None
    resolved_ips: tuple[str, ...]
    allowed: bool
    rejection_reason: str | None
    redirect_count: int

    def require_allowed(self) -> ValidatedHLSURL:
        if not self.allowed:
            reason = self.rejection_reason or "invalid_url"
            raise HLSURLValidationError(_REJECTION_MESSAGES[reason])
        assert self.normalized_url is not None
        assert self.display_url is not None
        assert self.resolved_host is not None
        return ValidatedHLSURL(
            fetch_url=self.normalized_url,
            display_url=self.display_url,
            resolved_host=self.resolved_host,
            resolved_ips=self.resolved_ips,
            redirect_count=self.redirect_count,
        )


@dataclass(frozen=True)
class _NormalizedUrl:
    fetch_url: str
    display_url: str
    host: str
    port: int


_REJECTION_MESSAGES = {
    "invalid_url": "Remote media URL is invalid",
    "unsupported_scheme": "Remote media URL must use HTTP or HTTPS",
    "missing_host": "Remote media URL must include a host",
    "credentials_not_allowed": "Remote media URL must not include credentials",
    "invalid_port": "Remote media URL port is invalid",
    "localhost_not_allowed": "Remote media URL must not use localhost",
    "host_resolution_failed": "Remote media URL host could not be resolved",
    "non_public_address": "Remote media URL must resolve only to public addresses",
    "redirect_probe_failed": "Remote media URL could not be checked safely",
    "redirect_limit_exceeded": "Remote media URL exceeded the redirect limit",
}
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


async def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({record[4][0] for record in records}))


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def _display_netloc(host: str, port: int | None) -> str:
    display_host = f"[{host}]" if ":" in host else host
    if port is None:
        return display_host
    return f"{display_host}:{port}"


def _normalize_url(value: str) -> tuple[_NormalizedUrl | None, str | None]:
    candidate = value.strip()
    if not candidate or any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        return None, "invalid_url"
    if "\\" in candidate:
        return None, "invalid_url"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None, "invalid_url"
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None, "unsupported_scheme"
    try:
        raw_host = parsed.hostname
    except ValueError:
        return None, "invalid_url"
    if raw_host is None:
        return None, "missing_host"
    if parsed.username is not None or parsed.password is not None:
        return None, "credentials_not_allowed"
    try:
        explicit_port = parsed.port
    except ValueError:
        return None, "invalid_port"

    try:
        literal_address = ipaddress.ip_address(raw_host)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        host = literal_address.compressed
    else:
        try:
            host = raw_host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None, "invalid_url"
        if not host:
            return None, "missing_host"
        if host == "localhost" or host.endswith(".localhost"):
            return None, "localhost_not_allowed"

    port = explicit_port or (443 if scheme == "https" else 80)
    netloc = _display_netloc(host, explicit_port)
    path = parsed.path or "/"
    fetch_url = urlunsplit((scheme, netloc, path, parsed.query, ""))
    display_url = urlunsplit((scheme, netloc, path, "", ""))[:255]
    return (
        _NormalizedUrl(
            fetch_url=fetch_url,
            display_url=display_url,
            host=host,
            port=port,
        ),
        None,
    )


async def _probe_redirect(
    url: str,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
) -> str | None:
    timeout = httpx.Timeout(
        connect=connect_timeout_seconds,
        read=read_timeout_seconds,
        write=read_timeout_seconds,
        pool=connect_timeout_seconds,
    )
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    ) as client:
        async with client.stream(
            "GET",
            url,
            headers={"Range": "bytes=0-0"},
        ) as response:
            if response.status_code not in _REDIRECT_STATUS_CODES:
                return None
            location = response.headers.get("location")
            if not location:
                raise HLSURLValidationError("Redirect response omitted Location")
            return location


class RemoteMediaUrlValidator:
    def __init__(
        self,
        *,
        resolver: AddressResolver = _resolve_addresses,
        redirect_probe: RedirectProbe = _probe_redirect,
        max_redirects: int = 3,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 20.0,
    ) -> None:
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        if min(connect_timeout_seconds, read_timeout_seconds) <= 0:
            raise ValueError("remote media timeouts must be positive")
        self._resolver = resolver
        self._redirect_probe = redirect_probe
        self.max_redirects = max_redirects
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds

    async def validate(self, value: str) -> RemoteMediaUrlDecision:
        current_url = value
        redirect_count = 0
        resolved_addresses: tuple[str, ...] = ()
        final_url: _NormalizedUrl | None = None

        while True:
            normalized, rejection_reason = _normalize_url(current_url)
            if normalized is None:
                return self._deny(
                    rejection_reason or "invalid_url",
                    final_url=final_url,
                    addresses=resolved_addresses,
                    redirect_count=redirect_count,
                )
            final_url = normalized

            try:
                literal_address = ipaddress.ip_address(normalized.host)
            except ValueError:
                literal_address = None
            if literal_address is not None:
                addresses = (literal_address.compressed,)
            else:
                try:
                    addresses = tuple(
                        sorted(
                            set(
                                await asyncio.wait_for(
                                    self._resolver(
                                        normalized.host,
                                        normalized.port,
                                    ),
                                    timeout=self.connect_timeout_seconds,
                                )
                            )
                        )
                    )
                except (OSError, TimeoutError):
                    return self._deny(
                        "host_resolution_failed",
                        final_url=final_url,
                        addresses=resolved_addresses,
                        redirect_count=redirect_count,
                    )
            if not addresses or any(
                not _is_public_address(address) for address in addresses
            ):
                return self._deny(
                    "non_public_address",
                    final_url=final_url,
                    addresses=addresses,
                    redirect_count=redirect_count,
                )
            resolved_addresses = addresses

            try:
                location = await self._redirect_probe(
                    normalized.fetch_url,
                    self.connect_timeout_seconds,
                    self.read_timeout_seconds,
                )
            except (HLSURLValidationError, httpx.HTTPError, OSError, TimeoutError):
                return self._deny(
                    "redirect_probe_failed",
                    final_url=final_url,
                    addresses=resolved_addresses,
                    redirect_count=redirect_count,
                )
            if location is None:
                return RemoteMediaUrlDecision(
                    normalized_url=normalized.fetch_url,
                    display_url=normalized.display_url,
                    resolved_host=normalized.host,
                    resolved_ips=resolved_addresses,
                    allowed=True,
                    rejection_reason=None,
                    redirect_count=redirect_count,
                )
            if redirect_count >= self.max_redirects:
                return self._deny(
                    "redirect_limit_exceeded",
                    final_url=final_url,
                    addresses=resolved_addresses,
                    redirect_count=redirect_count,
                )
            current_url = urljoin(normalized.fetch_url, location)
            redirect_count += 1

    @staticmethod
    def _deny(
        reason: str,
        *,
        final_url: _NormalizedUrl | None,
        addresses: Iterable[str],
        redirect_count: int,
    ) -> RemoteMediaUrlDecision:
        return RemoteMediaUrlDecision(
            normalized_url=(final_url.fetch_url if final_url else None),
            display_url=(final_url.display_url if final_url else None),
            resolved_host=(final_url.host if final_url else None),
            resolved_ips=tuple(sorted(set(addresses))),
            allowed=False,
            rejection_reason=reason,
            redirect_count=redirect_count,
        )


async def validate_hls_url(
    value: str,
    *,
    resolver: AddressResolver = _resolve_addresses,
    redirect_probe: RedirectProbe = _probe_redirect,
    max_redirects: int = 3,
    connect_timeout_seconds: float = 10.0,
    read_timeout_seconds: float = 20.0,
) -> ValidatedHLSURL:
    decision = await RemoteMediaUrlValidator(
        resolver=resolver,
        redirect_probe=redirect_probe,
        max_redirects=max_redirects,
        connect_timeout_seconds=connect_timeout_seconds,
        read_timeout_seconds=read_timeout_seconds,
    ).validate(value)
    return decision.require_allowed()
