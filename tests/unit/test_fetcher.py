from __future__ import annotations

import pytest

from app.services.fetcher import (
    _ip_is_blocked,
    _is_private_ip,
    fetch_url,
    validate_public_url,
)


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
        assert "Not an HTML or PDF" in result.error

    @pytest.mark.anyio
    async def test_oversized_body_is_capped(self, monkeypatch):
        async def fake_validate(url):
            return None

        # 20 MB of HTML — must be truncated to the 10 MB ceiling.
        big = b"<html>" + b"a" * (20 * 1024 * 1024) + b"</html>"

        async def fake_stream(url, client):
            from app.services.fetcher import _MAX_RESPONSE_BYTES

            return 200, {"content-type": "text/html"}, big[:_MAX_RESPONSE_BYTES]

        monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
        monkeypatch.setattr("app.services.fetcher._stream_one", fake_stream)
        result = await fetch_url("https://example.com/huge")
        assert result.error is None
        assert result.bytes_downloaded <= 10 * 1024 * 1024
