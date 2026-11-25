"""Outbound URL validation (SSRF defence) for upstream endpoints and discovery fetches.

Checks: scheme allowlist, no embedded credentials, hostname present, every resolved address public
(unless private networks are explicitly allowed for local-model deployments). Resolution happens at
call time, immediately before each request, which narrows the DNS-rebinding window; redirects are never
followed automatically, so each hop is re-validated by the caller.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]

_BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "instance-data", "localhost.localdomain"}


class UnsafeUrlError(ValueError):
    pass


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


def check_url_shape(url: str, *, allow_http: bool = False) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise UnsafeUrlError(f"scheme '{parts.scheme}' not allowed")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in URL are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise UnsafeUrlError("host is blocked")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host, port


async def assert_safe_url(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Resolver | None = None,
) -> None:
    host, port = check_url_shape(url, allow_http=allow_private)
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = await (resolver or system_resolver)(host, port)
        except OSError as exc:
            raise UnsafeUrlError(f"cannot resolve host '{host}'") from exc
    if not addresses:
        raise UnsafeUrlError(f"host '{host}' resolved to no addresses")
    if allow_private:
        return
    for addr in addresses:
        if not _is_public(ipaddress.ip_address(addr)):
            raise UnsafeUrlError(f"host '{host}' resolves to a non-public address")


async def resolves_private_only(url: str, *, resolver: Resolver | None = None) -> bool:
    """True when every address the URL's host resolves to is private or loopback (a local server)."""
    try:
        host, port = check_url_shape(url, allow_http=True)
    except UnsafeUrlError:
        return False
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await (resolver or system_resolver)(host, port)
        except OSError:
            return False
    return bool(addresses) and not any(_is_public(ipaddress.ip_address(a)) for a in addresses)
