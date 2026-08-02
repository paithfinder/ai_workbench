from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterable

import httpx
import pytest

from knowledge_workbench.core.errors import AppError
from knowledge_workbench.infrastructure.web.safe_http import SafeHttpWebFetcher


class Resolver:
    def __init__(self, answers: dict[str, list[tuple[str, ...]]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> Iterable[str]:
        self.calls.append((host, port))
        values = self.answers[host]
        return values.pop(0) if len(values) > 1 else values[0]


class TrackingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.was_read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.was_read = True
        yield self.content


class TrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.open_count = 0
        self.close_count = 0
        self.requests: list[httpx.Request] = []

    async def __aenter__(self) -> TrackingTransport:
        self.open_count += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        self.close_count += 1

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 1:
            return httpx.Response(
                302,
                headers={"Location": "https://second.example/final"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=b"safe",
            request=request,
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "https://user@example.com/",
        "https://user:password@example.com/",
        "https://example.com\\@127.0.0.1/",
        " https://example.com/",
        "https://example.com/\nnext",
        "https:///missing-host",
        "http://[::1]/",
        "http://[fe80::1%25eth0]/",
        "http://127.0.0.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data",
        "http://224.0.0.1/",
        "http://192.0.2.1/",
        "http://metadata.google.internal/",
        "http://localhost/",
    ],
)
async def test_rejects_ambiguous_or_forbidden_urls_without_network(url: str) -> None:
    async def fail_transport(_: httpx.Request) -> httpx.Response:
        raise AssertionError("transport should not be reached")

    fetcher = SafeHttpWebFetcher(
        resolver=Resolver({"example.com": [("93.184.216.34",)]}),
        transport=httpx.MockTransport(fail_transport),
    )

    with pytest.raises(AppError) as caught:
        await fetcher.fetch(url)

    assert caught.value.code in {"invalid_web_url", "web_address_not_allowed"}


async def test_fetches_pinned_address_and_preserves_safe_metadata() -> None:
    resolver = Resolver({"example.com": [("93.184.216.34",)]})

    async def transport(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://93.184.216.34/article")
        assert request.headers["host"] == "example.com"
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "ETag": '"abc"',
                "Set-Cookie": "secret=1",
                "Server": "private-stack",
            },
            content=b"<html><body>safe</body></html>",
        )

    result = await SafeHttpWebFetcher(
        resolver=resolver, transport=httpx.MockTransport(transport)
    ).fetch("https://example.com/article#fragment")

    assert result.requested_url == "https://example.com/article"
    assert result.final_url == "https://example.com/article"
    assert result.media_type == "text/html"
    assert result.headers == {"content-type": "text/html; charset=utf-8", "etag": '"abc"'}
    assert result.content_sha256 == hashlib.sha256(result.body).hexdigest()
    assert resolver.calls == [("example.com", 443)]


async def test_rejects_if_any_dns_answer_is_not_public() -> None:
    resolver = Resolver({"example.com": [("93.184.216.34", "127.0.0.1")]})
    reached = False

    async def transport(_: httpx.Request) -> httpx.Response:
        nonlocal reached
        reached = True
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"no")

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            resolver=resolver, transport=httpx.MockTransport(transport)
        ).fetch("https://example.com")

    assert caught.value.code == "web_address_not_allowed"
    assert not reached


async def test_redirect_is_resolved_and_private_target_is_rejected() -> None:
    resolver = Resolver(
        {
            "public.example": [("93.184.216.34",)],
            "private.example": [("10.0.0.8",)],
        }
    )
    requests: list[httpx.Request] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://private.example/admin"})

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            resolver=resolver, transport=httpx.MockTransport(transport)
        ).fetch("https://public.example/start")

    assert caught.value.code == "web_address_not_allowed"
    assert len(requests) == 1
    assert resolver.calls == [("public.example", 443), ("private.example", 80)]


async def test_dns_rebinding_cannot_change_pinned_connection_target() -> None:
    resolver = Resolver(
        {"example.com": [("93.184.216.34",), ("127.0.0.1",)]}
    )

    async def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"safe")

    result = await SafeHttpWebFetcher(
        resolver=resolver, transport=httpx.MockTransport(transport)
    ).fetch("https://example.com")

    assert result.body == b"safe"
    assert resolver.calls == [("example.com", 443)]


async def test_same_host_redirect_is_resolved_again_and_rebinding_is_rejected() -> None:
    resolver = Resolver(
        {"example.com": [("93.184.216.34",), ("127.0.0.1",)]}
    )

    async def transport(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/next"})

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            resolver=resolver, transport=httpx.MockTransport(transport)
        ).fetch("https://example.com/start")

    assert caught.value.code == "web_address_not_allowed"
    assert resolver.calls == [("example.com", 443), ("example.com", 443)]


async def test_redirect_between_hostnames_on_same_ip_uses_fresh_client_pool() -> None:
    transport = TrackingTransport()
    resolver = Resolver(
        {
            "first.example": [("93.184.216.34",)],
            "second.example": [("93.184.216.34",)],
        }
    )

    result = await SafeHttpWebFetcher(
        resolver=resolver,
        transport=transport,
    ).fetch("https://first.example/start")

    assert result.final_url == "https://second.example/final"
    assert result.body == b"safe"
    assert transport.open_count == 2
    assert transport.close_count == 2
    assert [request.headers["host"] for request in transport.requests] == [
        "first.example",
        "second.example",
    ]
    assert [request.extensions["sni_hostname"] for request in transport.requests] == [
        "first.example",
        "second.example",
    ]
    assert all(request.url.host == "93.184.216.34" for request in transport.requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_enforces_redirect_limit(status: int) -> None:
    resolver = Resolver({"example.com": [("93.184.216.34",)]})

    async def transport(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Location": "/again"})

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            max_redirects=1,
            resolver=resolver,
            transport=httpx.MockTransport(transport),
        ).fetch("https://example.com")

    assert caught.value.code == "web_redirect_limit_exceeded"


@pytest.mark.parametrize("content_type", ["application/json", "image/svg+xml", ""])
async def test_rejects_non_allowlisted_content_type(content_type: str) -> None:
    async def transport(_: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": content_type} if content_type else {}
        return httpx.Response(200, headers=headers, content=b"payload")

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            resolver=Resolver({"example.com": [("93.184.216.34",)]}),
            transport=httpx.MockTransport(transport),
        ).fetch("https://example.com")

    assert caught.value.code == "web_unsupported_content_type"


async def test_rejects_declared_and_streaming_body_overflow() -> None:
    responses = [
        httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Content-Length": "9"},
            content=b"ignored",
        ),
        httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"123456"),
    ]

    async def transport(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    fetcher = SafeHttpWebFetcher(
        max_body_bytes=5,
        resolver=Resolver({"example.com": [("93.184.216.34",)]}),
        transport=httpx.MockTransport(transport),
    )
    for _ in range(2):
        with pytest.raises(AppError) as caught:
            await fetcher.fetch("https://example.com")
        assert caught.value.code == "web_response_too_large"
        assert caught.value.status_code == 413


async def test_rejects_compressed_response_before_decoding_body() -> None:
    stream = TrackingAsyncByteStream(b"not-read")

    async def transport(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain",
                "Content-Encoding": "gzip",
            },
            stream=stream,
        )

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            resolver=Resolver({"example.com": [("93.184.216.34",)]}),
            transport=httpx.MockTransport(transport),
        ).fetch("https://example.com")

    assert caught.value.code == "web_unsupported_content_encoding"
    assert not stream.was_read


async def test_rejects_overlong_redirect_before_next_request() -> None:
    requests: list[httpx.Request] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/" + "a" * 2100})

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            resolver=Resolver({"example.com": [("93.184.216.34",)]}),
            transport=httpx.MockTransport(transport),
        ).fetch("https://example.com")

    assert caught.value.code == "invalid_web_url"
    assert len(requests) == 1


async def test_maps_transport_timeout_without_real_network() -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AppError) as caught:
        await SafeHttpWebFetcher(
            resolver=Resolver({"example.com": [("93.184.216.34",)]}),
            transport=httpx.MockTransport(transport),
        ).fetch("https://example.com")

    assert caught.value.code == "web_fetch_timeout"
    assert caught.value.status_code == 503
