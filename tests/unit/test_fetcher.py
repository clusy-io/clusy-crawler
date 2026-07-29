from __future__ import annotations

import asyncio
import codecs

import httpx
import pytest

from app.config import settings
from app.services import fetcher as fetcher_mod
from app.services.document_policy import (
    DocumentPolicyBlockReason,
    DocumentPolicyDecision,
    DocumentPolicyDeniedError,
)
from app.services.fetcher import (
    _ip_is_blocked,
    _is_private_ip,
    _response_peer_error,
    _safe_log_url,
    _stream_one,
    fetch_url,
    validate_public_url,
)


def test_safe_log_url_omits_credentials_path_query_and_fragment() -> None:
    value = _safe_log_url(
        "https://user:secret@example.com/private/bearer-token?signature=secret#fragment"
    )

    assert value == "https://example.com"
    assert "secret" not in value
    assert "bearer-token" not in value


class TestSSRFGuard:
    def test_localhost_rejected(self):
        assert _is_private_ip("127.0.0.1") is True

    def test_private_10_rejected(self):
        assert _is_private_ip("10.0.0.1") is True

    def test_private_192_rejected(self):
        assert _is_private_ip("192.168.1.1") is True

    def test_private_172_rejected(self):
        assert _is_private_ip("172.16.0.1") is True

    def test_link_local_rejected(self):
        assert _is_private_ip("169.254.1.1") is True

    def test_cloud_metadata_rejected(self):
        # AWS/GCP/Azure metadata endpoint is link-local.
        assert _is_private_ip("169.254.169.254") is True

    def test_public_ip_allowed(self):
        assert _is_private_ip("93.184.216.34") is False

    def test_ipv6_localhost_rejected(self):
        assert _is_private_ip("::1") is True

    def test_non_ip_string_not_rejected(self):
        # Hostnames are not IP literals; they must be resolved first.
        assert _is_private_ip("example.com") is False


class TestIpIsBlocked:
    def test_unspecified_blocked(self):
        assert _ip_is_blocked("0.0.0.0") is True

    def test_ipv4_mapped_metadata_blocked(self):
        # ::ffff:169.254.169.254 must be unwrapped and blocked.
        assert _ip_is_blocked("::ffff:169.254.169.254") is True

    def test_ipv6_unique_local_blocked(self):
        assert _ip_is_blocked("fc00::1") is True

    def test_garbage_blocked(self):
        assert _ip_is_blocked("not-an-ip") is True

    def test_public_allowed(self):
        assert _ip_is_blocked("93.184.216.34") is False

    def test_shared_address_space_blocked(self):
        assert _ip_is_blocked("100.64.0.1") is True


class TestValidatePublicUrl:
    @pytest.mark.anyio
    async def test_non_http_scheme_blocked(self):
        assert await validate_public_url("file:///etc/passwd") is not None
        assert await validate_public_url("gopher://x/") is not None

    @pytest.mark.anyio
    async def test_ip_literal_metadata_blocked(self):
        err = await validate_public_url("http://169.254.169.254/latest/meta-data/")
        assert err is not None

    @pytest.mark.anyio
    async def test_localhost_literal_blocked(self):
        assert await validate_public_url("http://127.0.0.1:8000/") is not None

    @pytest.mark.anyio
    async def test_all_resolved_addresses_checked(self, monkeypatch):
        # If a host resolves to a public AND a private address, block it
        # (multi-record / DNS-rebinding style bypass).
        async def fake_resolve(host):
            return ["93.184.216.34", "127.0.0.1"]

        monkeypatch.setattr("app.services.fetcher._resolve_all", fake_resolve)
        err = await validate_public_url("http://mixed.example/")
        assert err is not None
        assert "127.0.0.1" in err


class TestFetchUrl:
    @pytest.mark.anyio
    async def test_private_ip_rejected(self):
        result = await fetch_url("http://127.0.0.1/test")
        assert result.error is not None
        assert "ssrf" in result.error.lower()

    @pytest.mark.anyio
    async def test_redirect_to_private_ip_blocked(self, monkeypatch):
        # A public URL that 302-redirects to the metadata endpoint must be
        # blocked on the SECOND hop, not silently followed.
        async def fake_validate(url):
            # Real guard would resolve DNS; here we assert the loop re-checks
            # the redirect target. Public host allowed, metadata IP blocked.
            if "169.254.169.254" in url:
                return "169.254.169.254 resolves to non-public address 169.254.169.254"
            return None

        async def fake_stream(url, client):
            if "evil.example" in url:
                return 302, {"location": "http://169.254.169.254/"}, b""
            return 200, {"content-type": "text/html"}, b"<html>ok</html>"

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("http://evil.example/redirect")
        assert result.error is not None
        assert "ssrf" in result.error.lower()

    @pytest.mark.anyio
    async def test_non_html_content_type(self, monkeypatch):
        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            return 200, {"content-type": "image/png"}, b"not html"

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/doc.pdf")
        assert result.error is not None
        assert "Not a supported HTML, text, or PDF" in result.error

    @pytest.mark.anyio
    async def test_plain_text_source_is_supported(self, monkeypatch):
        async def fake_validate(url):
            return None

        body = b"# Project\\n\\nplain source content\\n"

        async def fake_stream(url, client):
            return 200, {"content-type": "text/markdown; charset=utf-8"}, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://raw.githubusercontent.com/o/r/main/README.md")

        assert result.error is None
        assert result.content_type.startswith("text/markdown")
        assert result.html == body.decode()

    @pytest.mark.anyio
    async def test_binary_body_labeled_text_is_rejected(self, monkeypatch):
        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            return 200, {"content-type": "text/plain"}, b"\x00\x01binary"

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/binary")

        assert result.error is not None
        assert "Not a supported" in result.error

    @pytest.mark.anyio
    @pytest.mark.parametrize("status_code", [401, 403, 404])
    async def test_http_error_page_is_never_returned_as_content(
        self,
        monkeypatch,
        status_code,
    ):
        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            return (
                status_code,
                {"content-type": "text/html"},
                b"<html><article>sensitive error text</article></html>",
            )

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/error")

        assert result.error == f"HTTP {status_code}"
        assert result.status_code == status_code
        assert result.html == ""

    @pytest.mark.anyio
    async def test_retry_after_then_success(self, monkeypatch):
        calls = 0

        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 429, {"retry-after": "0"}, b""
            return 200, {"content-type": "text/html"}, b"<html>recovered</html>"

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/rate-limited")

        assert result.error is None
        assert "recovered" in result.html
        assert calls == 2

    @pytest.mark.anyio
    async def test_retryable_status_has_bounded_attempts(self, monkeypatch):
        calls = 0

        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            nonlocal calls
            calls += 1
            return 503, {"retry-after": "0"}, b""

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        monkeypatch.setattr(settings, "http_max_attempts", 3)
        result = await fetch_url("https://example.com/unavailable")

        assert result.error == "HTTP 503"
        assert calls == 3

    @pytest.mark.anyio
    async def test_oversized_body_is_explicit_failure(self, monkeypatch):
        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            raise fetcher_mod._ResponseTooLargeError(
                200,
                bytes_read=fetcher_mod._MAX_RESPONSE_BYTES + 1,
            )

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/huge")
        assert result.error is not None
        assert "Response too large" in result.error
        assert result.status_code == 200
        assert result.bytes_downloaded == fetcher_mod._MAX_RESPONSE_BYTES + 1
        assert result.final_url == "https://example.com/huge"

    @pytest.mark.anyio
    async def test_declared_charset_is_used(self, monkeypatch):
        async def fake_validate(url):
            return None

        body = "<!doctype html><html><body>café — résumé</body></html>".encode("windows-1252")

        async def fake_stream(url, client):
            return 200, {"Content-Type": "text/html; charset=windows-1252"}, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/legacy")
        assert result.error is None
        assert "café — résumé" in result.html

    @pytest.mark.anyio
    async def test_meta_charset_is_used(self, monkeypatch):
        async def fake_validate(url):
            return None

        body = b'<!doctype html><meta charset="windows-1252"><body>\x93quoted\x94 \x80 price</body>'

        async def fake_stream(url, client):
            return 200, {"content-type": "text/html"}, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/meta")
        assert result.error is None
        assert "“quoted” € price" in result.html

    @pytest.mark.anyio
    async def test_bom_overrides_incorrect_declared_charset(self, monkeypatch):
        async def fake_validate(url):
            return None

        text = "<html><body>UTF-16 snowman ☃</body></html>"
        body = codecs.BOM_UTF16_LE + text.encode("utf-16-le")

        async def fake_stream(url, client):
            return 200, {"content-type": "text/html; charset=windows-1252"}, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/bom")
        assert result.error is None
        assert result.html == text

    @pytest.mark.anyio
    @pytest.mark.parametrize("charset", ["not-a-codec", "zlib_codec"])
    async def test_invalid_declared_charset_has_robust_fallback(
        self,
        monkeypatch,
        charset,
    ):
        async def fake_validate(url):
            return None

        body = b"<!doctype html><html><body>caf\xe9</body></html>"

        async def fake_stream(url, client):
            return 200, {"content-type": f"text/html; charset={charset}"}, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/fallback")
        assert result.error is None
        assert "café" in result.html

    @pytest.mark.anyio
    async def test_bytes_downloaded_counts_bytes_not_characters(self, monkeypatch):
        async def fake_validate(url):
            return None

        body = "<html><body>雪だるま ☃</body></html>".encode()

        async def fake_stream(url, client):
            return 200, {"content-type": "text/html; charset=utf-8"}, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/unicode")
        assert result.error is None
        assert result.bytes_downloaded == len(body)
        assert result.bytes_downloaded > len(result.html)

    @pytest.mark.anyio
    @pytest.mark.parametrize("headers", [{}, {"content-type": "image/png"}])
    async def test_html_body_is_sniffed_when_type_missing_or_wrong(
        self,
        monkeypatch,
        headers,
    ):
        async def fake_validate(url):
            return None

        body = b" \n<!-- lead --><html><body>real html</body></html>"

        async def fake_stream(url, client):
            return 200, headers, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/sniff")
        assert result.error is None
        assert result.content_type == "text/html"
        assert "real html" in result.html

    @pytest.mark.anyio
    @pytest.mark.parametrize("headers", [{}, {"content-type": "application/octet-stream"}])
    async def test_pdf_body_is_sniffed_when_type_missing_or_wrong(
        self,
        monkeypatch,
        headers,
    ):
        async def fake_validate(url):
            return None

        body = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\nmock"

        async def fake_stream(url, client):
            return 200, headers, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/paper")
        assert result.error is None
        assert result.content_type == "application/pdf"
        assert result.raw_bytes == body
        assert result.bytes_downloaded == len(body)

    @pytest.mark.anyio
    async def test_html_signature_corrects_wrong_pdf_header(self, monkeypatch):
        async def fake_validate(url):
            return None

        body = b"<!doctype html><html><body>not a PDF</body></html>"

        async def fake_stream(url, client):
            return 200, {"content-type": "application/pdf"}, body

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/wrong-pdf")
        assert result.error is None
        assert result.content_type == "text/html"
        assert result.raw_bytes is None
        assert "not a PDF" in result.html

    @pytest.mark.anyio
    async def test_final_url_follows_relative_redirects(self, monkeypatch):
        seen: list[str] = []

        async def fake_validate(url):
            seen.append(url)
            return None

        async def fake_stream(url, client):
            if url.endswith("/start"):
                return 302, {"Location": "../final"}, b""
            return 200, {"Content-Type": "text/html"}, b"<html>done</html>"

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/path/start")
        assert result.error is None
        assert result.final_url == "https://example.com/final"
        assert seen == [
            "https://example.com/path/start",
            "https://example.com/final",
        ]

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "destination",
        [
            "https://example.com/private",
            "https://outside.example/private",
        ],
    )
    async def test_redirect_document_policy_denies_before_destination_request(
        self,
        monkeypatch,
        destination,
    ):
        source = "https://example.com/start"
        events: list[tuple[str, str]] = []
        page_requests: list[str] = []

        async def fake_validate(url):
            events.append(("ssrf", url))
            return None

        async def document_policy(url):
            events.append(("policy", url))
            if url == destination:
                return DocumentPolicyDecision(
                    allowed=False,
                    reason=(
                        DocumentPolicyBlockReason.OFF_SITE
                        if "outside.example" in url
                        else DocumentPolicyBlockReason.ROBOTS_DISALLOWED
                    ),
                    error="redirect denied by test policy",
                )
            return DocumentPolicyDecision(allowed=True)

        async def fake_stream(url, client):
            page_requests.append(url)
            if url == source:
                return 302, {"location": destination}, b""
            raise AssertionError("denied redirect target must not receive a page request")

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)

        with pytest.raises(DocumentPolicyDeniedError, match="redirect denied"):
            await fetch_url(source, document_policy=document_policy)

        assert page_requests == [source]
        assert events == [
            ("ssrf", source),
            ("policy", source),
            ("ssrf", destination),
            ("policy", destination),
        ]

    @pytest.mark.anyio
    async def test_allowed_redirect_rechecks_same_origin_path_policy(self, monkeypatch):
        source = "https://example.com/start"
        destination = "https://example.com/public"
        policy_calls: list[str] = []
        page_requests: list[str] = []

        async def fake_validate(_url):
            return None

        async def document_policy(url):
            policy_calls.append(url)
            return DocumentPolicyDecision(allowed=True)

        async def fake_stream(url, client):
            page_requests.append(url)
            if url == source:
                return 302, {"location": "/public"}, b""
            return 200, {"content-type": "text/html"}, b"<html>allowed</html>"

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)

        result = await fetch_url(source, document_policy=document_policy)

        assert result.error is None
        assert result.final_url == destination
        assert page_requests == [source, destination]
        assert policy_calls == [source, destination]

    @pytest.mark.anyio
    async def test_connected_private_peer_is_reported_as_ssrf(self, monkeypatch):
        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            raise fetcher_mod._UnsafePeerAddressError(
                "connected to non-public peer address 127.0.0.1"
            )

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://public.example/")
        assert result.error is not None
        assert "SSRF blocked" in result.error
        assert result.final_url == "https://public.example/"

    @pytest.mark.anyio
    async def test_js_render_goes_directly_to_browser(self, monkeypatch):
        from app.services.renderer import RenderResult

        calls = {"render": 0, "static": 0}

        class FakeRenderer:
            async def render(self, url, wait_for_selector=None):
                calls["render"] += 1
                assert wait_for_selector == "#ready"
                await asyncio.sleep(0.002)
                return RenderResult(
                    html="<html><body>" + ("rendered content " * 20) + "</body></html>",
                    title="Rendered",
                    rendered=True,
                    latency_ms=12.5,
                    status_code=200,
                    content_type="text/html; charset=utf-8",
                    final_url="https://example.com/final/",
                )

        async def fail_static(url, client):
            calls["static"] += 1
            raise AssertionError("forced JS path must not pre-fetch with httpx")

        monkeypatch.setattr(settings, "playwright_enabled", True)
        monkeypatch.setattr(settings, "playwright_java_script_enabled", True)
        monkeypatch.setattr("app.services.renderer.get_renderer", FakeRenderer)
        monkeypatch.setattr("app.services.fetcher._stream_one", fail_static)

        result = await fetch_url(
            "https://example.com/start",
            js_render=True,
            wait_for_selector="#ready",
        )

        assert result.error is None
        assert result.rendered is True
        assert result.final_url == "https://example.com/final/"
        assert result.status_code == 200
        assert result.title == "Rendered"
        assert result.fetch_latency_ms == 0
        assert result.render_latency_ms > 0
        assert result.latency_ms == pytest.approx(
            result.render_latency_ms,
            abs=0.11,
        )
        assert calls == {"render": 1, "static": 0}

    @pytest.mark.anyio
    async def test_failed_render_falls_back_once_to_static(self, monkeypatch):
        from app.services.renderer import RenderResult

        calls = {"render": 0, "static": 0}

        class FakeRenderer:
            async def render(self, url, wait_for_selector=None):
                calls["render"] += 1
                await asyncio.sleep(0.002)
                return RenderResult(rendered=False)

        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            calls["static"] += 1
            await asyncio.sleep(0.002)
            return (
                200,
                {"content-type": "text/html"},
                b"<html><body>static fallback</body></html>",
            )

        monkeypatch.setattr(settings, "playwright_enabled", True)
        monkeypatch.setattr(settings, "playwright_java_script_enabled", True)
        monkeypatch.setattr("app.services.renderer.get_renderer", FakeRenderer)
        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)

        result = await fetch_url("https://example.com/", js_render=True)

        assert result.error is None
        assert result.rendered is False
        assert "static fallback" in result.html
        assert result.fetch_latency_ms > 0
        assert result.render_latency_ms > 0
        assert result.latency_ms == pytest.approx(
            result.fetch_latency_ms + result.render_latency_ms,
            abs=0.11,
        )
        assert calls == {"render": 1, "static": 1}

    @pytest.mark.anyio
    async def test_forced_js_with_browser_disabled_is_static_fetch_time(
        self,
        monkeypatch,
    ):
        async def fake_validate(url):
            return None

        async def fake_stream(url, client):
            await asyncio.sleep(0.002)
            return (
                200,
                {"content-type": "text/html"},
                b"<html><body>static because browser is disabled</body></html>",
            )

        monkeypatch.setattr(settings, "playwright_enabled", False)
        monkeypatch.setattr(
            "app.services.fetcher.validate_public_url",
            fake_validate,
        )
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)

        result = await fetch_url("https://example.com/", js_render=True)

        assert result.error is None
        assert result.rendered is False
        assert result.fetch_latency_ms > 0
        assert result.render_latency_ms == 0


class TestStreamingLimits:
    @pytest.mark.anyio
    async def test_declared_oversize_fails_before_body_read(self, monkeypatch):
        monkeypatch.setattr(fetcher_mod, "_MAX_RESPONSE_BYTES", 5)

        async def handler(request):
            return httpx.Response(
                200,
                headers={"content-length": "6"},
                content=b"",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(fetcher_mod._ResponseTooLargeError) as exc:
                await _stream_one("https://example.com/", client)
        assert exc.value.declared_bytes == 6
        assert exc.value.bytes_read == 0

    @pytest.mark.anyio
    async def test_chunked_oversize_fails_instead_of_truncating(self, monkeypatch):
        monkeypatch.setattr(fetcher_mod, "_MAX_RESPONSE_BYTES", 5)

        class OversizeStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"1234"
                yield b"56"

        async def handler(request):
            return httpx.Response(200, stream=OversizeStream())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(fetcher_mod._ResponseTooLargeError) as exc:
                await _stream_one("https://example.com/", client)
        assert exc.value.bytes_read == 6

    @pytest.mark.anyio
    async def test_http_error_body_is_not_materialized(self):
        class ExplodingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError("error response body must not be read")
                yield b""  # pragma: no cover

        async def handler(request):
            return httpx.Response(
                503,
                headers={"retry-after": "1"},
                stream=ExplodingStream(),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            status, headers, body = await _stream_one("https://example.com/", client)

        assert status == 503
        assert headers["retry-after"] == "1"
        assert body == b""


class TestPeerValidation:
    class NetworkStream:
        def __init__(self, peer):
            self.peer = peer

        def get_extra_info(self, name):
            assert name == "server_addr"
            return self.peer

    @pytest.fixture(autouse=True)
    def no_proxy(self, monkeypatch):
        monkeypatch.setattr(settings, "http_proxy", "")
        for name in fetcher_mod._PROXY_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_private_direct_peer_is_blocked(self):
        response = httpx.Response(
            200,
            extensions={"network_stream": self.NetworkStream(("127.0.0.1", 443))},
        )
        assert "127.0.0.1" in (_response_peer_error(response) or "")

    def test_public_direct_peer_is_allowed(self):
        response = httpx.Response(
            200,
            extensions={"network_stream": self.NetworkStream(("93.184.216.34", 443))},
        )
        assert _response_peer_error(response) is None

    def test_proxy_peer_is_not_mistaken_for_origin(self, monkeypatch):
        monkeypatch.setattr(settings, "http_proxy", "http://127.0.0.1:8080")
        response = httpx.Response(
            200,
            extensions={"network_stream": self.NetworkStream(("127.0.0.1", 8080))},
        )
        assert _response_peer_error(response) is None
