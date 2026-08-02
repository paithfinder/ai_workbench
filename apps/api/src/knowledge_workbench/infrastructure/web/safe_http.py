from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime

import httpx

from knowledge_workbench.application.ports.web_fetcher import WebFetcher, WebFetchResult
from knowledge_workbench.core.errors import AppError

DnsResolver = Callable[[str, int], Awaitable[Iterable[str]]]

_ALLOWED_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
_MAX_URL_CHARACTERS = 2000
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-language", "content-type", "etag", "last-modified"}
)
_METADATA_HOSTNAMES = frozenset(
    {
        "instance-data.ec2.internal",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
)
# Metadata services outside link-local ranges that must be denied explicitly.
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("168.63.129.16"),
    }
)


class SafeHttpWebFetcher(WebFetcher):
    """Fetch a static textual resource without allowing the HTTP client to resolve DNS.

    Each hop is resolved and validated here. The outbound request is then rewritten to a
    validated address while retaining the original Host header and TLS SNI hostname. This
    closes the validation-to-connect DNS rebinding gap instead of relying on a second lookup
    inside the default transport.
    """

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 5.0,
        total_timeout_seconds: float = 15.0,
        max_body_bytes: int = 5 * 1024 * 1024,
        max_redirects: int = 5,
        resolver: DnsResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if connect_timeout_seconds <= 0 or total_timeout_seconds <= 0:
            raise ValueError("web fetch timeouts must be positive")
        if max_body_bytes <= 0 or max_redirects < 0:
            raise ValueError("web fetch limits are invalid")
        self._connect_timeout = connect_timeout_seconds
        self._total_timeout = total_timeout_seconds
        self._max_body_bytes = max_body_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver or _resolve_addresses
        self._transport = transport

    async def fetch(self, url: str) -> WebFetchResult:
        requested_url = httpx.URL(normalize_web_url(url))
        current_url = requested_url
        try:
            async with asyncio.timeout(self._total_timeout):
                for redirect_count in range(self._max_redirects + 1):
                    # A redirect may change the HTTPS hostname while retaining the same pinned
                    # address. Use a fresh pool for every hop so a TLS connection authenticated
                    # for the previous hostname can never be reused for the next hostname.
                    async with self._client() as client:
                        response = await self._request_hop(client, current_url)
                        try:
                            if response.status_code in _REDIRECT_STATUSES:
                                if redirect_count >= self._max_redirects:
                                    raise AppError(
                                        "web_redirect_limit_exceeded",
                                        "The web resource exceeded the redirect limit.",
                                        status_code=422,
                                    )
                                current_url = _redirect_url(current_url, response)
                                continue
                            if not 200 <= response.status_code < 300:
                                raise AppError(
                                    "web_fetch_failed",
                                    "The web server returned an unsuccessful response.",
                                    status_code=422,
                                    details=[{"status_code": response.status_code}],
                                )
                            media_type = _validate_content_type(response)
                            body = await self._read_limited(response)
                            return WebFetchResult(
                                requested_url=str(requested_url),
                                final_url=str(current_url),
                                fetched_at=datetime.now(UTC),
                                body=body,
                                content_sha256=hashlib.sha256(body).hexdigest(),
                                media_type=media_type,
                                headers=_safe_headers(response.headers),
                                status_code=response.status_code,
                            )
                        finally:
                            await response.aclose()
        except TimeoutError as exc:
            raise _timeout_error() from exc
        except httpx.TimeoutException as exc:
            raise _timeout_error() from exc
        except httpx.HTTPError as exc:
            raise AppError(
                "web_fetch_unavailable",
                "The web resource could not be fetched. Please retry.",
                status_code=503,
            ) from exc
        raise AssertionError("redirect loop terminated unexpectedly")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=self._total_timeout,
                write=self._connect_timeout,
                pool=self._connect_timeout,
            ),
            transport=self._transport,
            trust_env=False,
            headers={
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                "Accept-Encoding": "identity",
                "User-Agent": "KnowledgeWorkbenchStaticFetcher/1.0",
            },
        )

    async def _request_hop(
        self, client: httpx.AsyncClient, url: httpx.URL
    ) -> httpx.Response:
        hostname = url.host
        if hostname is None:
            raise _invalid_url("The web URL must include a host.")
        _validate_hostname(hostname)
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            resolved = tuple(await self._resolver(hostname, port))
        except (OSError, UnicodeError) as exc:
            raise AppError(
                "web_host_unresolvable",
                "The web URL host could not be resolved.",
                status_code=422,
            ) from exc
        addresses = _validate_addresses(resolved)
        pinned_url = url.copy_with(host=str(addresses[0]))
        request = client.build_request(
            "GET",
            pinned_url,
            headers={"Host": _host_header(url)},
        )
        # httpcore uses this extension for TLS SNI and certificate hostname validation while
        # connecting to the pinned literal address above.
        request.extensions["sni_hostname"] = hostname
        return await client.send(request, stream=True, follow_redirects=False)

    async def _read_limited(self, response: httpx.Response) -> bytes:
        content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise AppError(
                "web_unsupported_content_encoding",
                "The web resource must not use compressed transfer encoding.",
                status_code=422,
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise AppError(
                    "web_invalid_response",
                    "The web server returned an invalid Content-Length header.",
                    status_code=422,
                ) from exc
            if declared_length < 0:
                raise AppError(
                    "web_invalid_response",
                    "The web server returned an invalid Content-Length header.",
                    status_code=422,
                )
            if declared_length > self._max_body_bytes:
                raise _body_too_large()

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self._max_body_bytes:
                raise _body_too_large()
            body.extend(chunk)
        return bytes(body)


async def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(record[4][0] for record in records))


def normalize_web_url(value: str) -> str:
    return str(_parse_url(value))


def _parse_url(value: str) -> httpx.URL:
    if not isinstance(value, str):
        raise _invalid_url("The web URL must be a string.")
    if not value or value != value.strip() or "\\" in value:
        raise _invalid_url("The web URL is malformed.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid_url("The web URL contains control characters.")
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise _invalid_url("The web URL is malformed.") from exc
    if url.scheme not in {"http", "https"}:
        raise _invalid_url("Only HTTP and HTTPS web URLs are supported.")
    if url.userinfo:
        raise _invalid_url("Web URLs containing credentials are not allowed.")
    if url.host is None or not url.host:
        raise _invalid_url("The web URL must include a host.")
    if url.fragment:
        url = url.copy_with(fragment=None)
    if len(str(url)) > _MAX_URL_CHARACTERS:
        raise _invalid_url("The web URL exceeds the supported length.")
    return url


def _validate_hostname(hostname: str) -> None:
    normalized = hostname.rstrip(".").lower()
    if not normalized or normalized in _METADATA_HOSTNAMES:
        raise _blocked_host()
    if "%" in normalized:
        raise _blocked_host()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if normalized == "localhost" or normalized.endswith(".localhost"):
            raise _blocked_host() from None
        # Reject non-canonical numeric host spellings (for example 2130706433 or
        # dotted hexadecimal) rather than allowing the platform resolver to reinterpret them.
        labels = normalized.split(".")
        if all(label and (label.isdigit() or label.lower().startswith("0x")) for label in labels):
            raise _blocked_host() from None
        return
    _validate_address(address)


def _validate_addresses(
    values: Iterable[str],
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise _blocked_host() from exc
        _validate_address(address)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise AppError(
            "web_host_unresolvable",
            "The web URL host did not resolve to an address.",
            status_code=422,
        )
    return tuple(addresses)


def _validate_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address in _METADATA_ADDRESSES
    ):
        raise _blocked_host()


def _host_header(url: httpx.URL) -> str:
    hostname = url.host
    if hostname is None:
        raise _invalid_url("The web URL must include a host.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        authority_host = hostname.encode("idna").decode("ascii")
    else:
        authority_host = f"[{address}]" if address.version == 6 else str(address)
    default_port = 443 if url.scheme == "https" else 80
    return authority_host if url.port in {None, default_port} else f"{authority_host}:{url.port}"


def _redirect_url(current_url: httpx.URL, response: httpx.Response) -> httpx.URL:
    locations = response.headers.get_list("location")
    if len(locations) != 1 or not locations[0].strip():
        raise AppError(
            "web_invalid_redirect",
            "The web server returned an invalid redirect.",
            status_code=422,
        )
    try:
        return _parse_url(str(current_url.join(locations[0])))
    except httpx.InvalidURL as exc:
        raise AppError(
            "web_invalid_redirect",
            "The web server returned an invalid redirect URL.",
            status_code=422,
        ) from exc


def _validate_content_type(response: httpx.Response) -> str:
    raw_value = response.headers.get("content-type", "")
    media_type: str = raw_value.split(";", 1)[0].strip().lower()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise AppError(
            "web_unsupported_content_type",
            "The web resource must be HTML, XHTML, or plain text.",
            status_code=422,
        )
    return media_type


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: headers[name] for name in sorted(_SAFE_RESPONSE_HEADERS) if name in headers}


def _invalid_url(message: str) -> AppError:
    return AppError("invalid_web_url", message, status_code=422)


def _blocked_host() -> AppError:
    return AppError(
        "web_address_not_allowed",
        "The web URL resolves to an address that is not allowed.",
        status_code=422,
    )


def _body_too_large() -> AppError:
    return AppError(
        "web_response_too_large",
        "The web resource exceeds the configured size limit.",
        status_code=413,
    )


def _timeout_error() -> AppError:
    return AppError(
        "web_fetch_timeout",
        "The web resource did not respond within the configured timeout.",
        status_code=503,
    )
