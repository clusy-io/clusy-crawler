from __future__ import annotations

import pytest

from app.config import settings
from app.services import renderer as renderer_module
from app.services.document_policy import (
    DocumentPolicyBlockReason,
    DocumentPolicyDecision,
    DocumentPolicyDeniedError,
)
from app.services.renderer import OptimizedRenderer, RenderResult, needs_js_rendering


def test_ssr_framework_marker_with_content_does_not_force_render():
    html = (
        '<html><body><div id="__next"><main><p>'
        + ("This is substantive server rendered article content. " * 20)
        + "</p></main></div></body></html>"
    )
    assert needs_js_rendering(html, "https://example.com/article") is False


def test_sparse_framework_shell_requires_render():
    html = (
        '<html><body><div id="root">Loading…</div>'
        '<script src="/react-app.js"></script></body></html>'
    )
    assert needs_js_rendering(html, "https://example.com/app") is True


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/psf/requests",
        "https://github.com/psf/requests/tree/main/src",
        "https://github.com/psf/requests/blob/main/README.md",
        "https://github.com/psf/requests/commit/6e83187",
        "https://github.com/psf/requests/releases/tag/v2.34.2",
    ],
)
def test_server_rendered_github_content_skips_browser(url):
    sparse_hydration_shell = (
        '<html><body><div id="__next">Loading…</div>'
        '<script src="/react-app.js"></script></body></html>'
    )

    assert needs_js_rendering(sparse_hydration_shell, url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/psf/requests/issues/7284",
        "https://github.com/psf/requests/pull/7441",
        "https://github.com/vercel/next.js/discussions/35773",
    ],
)
def test_github_threads_remain_browser_eligible(url):
    sparse_hydration_shell = (
        '<html><body><div id="__next">Loading…</div>'
        '<script src="/react-app.js"></script></body></html>'
    )

    assert needs_js_rendering(sparse_hydration_shell, url) is True


@pytest.mark.anyio
async def test_renderer_blocks_private_hostname_subrequest(monkeypatch):
    renderer = OptimizedRenderer()
    validations: list[str] = []

    async def fake_validate(url):
        validations.append(url)
        if "internal.example" in url:
            return "internal.example resolves to non-public address 127.0.0.1"
        return None

    class FakeRequest:
        url = "http://internal.example/admin"
        resource_type = "script"

    class FakeRoute:
        request = FakeRequest()
        aborted = False
        continued = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    route = FakeRoute()

    class FakePage:
        handler = None

        def set_default_timeout(self, timeout):
            pass

        async def route(self, pattern, handler):
            self.handler = handler

        async def close(self):
            pass

    page = FakePage()

    class FakeContext:
        handler = None

        async def route(self, pattern, handler):
            self.handler = handler
            page.handler = handler

        async def route_web_socket(self, pattern, handler):
            pass

        async def new_page(self):
            return page

    async def fake_acquire():
        return FakeContext()

    async def fake_release(ctx, healthy):
        assert healthy is True

    async def fake_do_render(page_arg, url, wait_for_selector, timeout):
        assert page_arg.handler is not None
        await page_arg.handler(route)
        return RenderResult(
            html="<html><body>safe main page</body></html>",
            rendered=True,
        )

    monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
    monkeypatch.setattr(renderer, "_acquire_context", fake_acquire)
    monkeypatch.setattr(renderer, "_release_context", fake_release)
    monkeypatch.setattr(renderer, "_do_render", fake_do_render)

    result = await renderer.render("https://public.example/")

    assert result.rendered is True
    assert route.aborted is True
    assert route.continued is False
    assert validations == [
        "https://public.example/",
        "http://internal.example/admin",
    ]


@pytest.mark.anyio
async def test_renderer_blocks_redirected_document_before_navigation_bytes(monkeypatch):
    renderer = OptimizedRenderer()
    source = "https://example.com/start"
    destination = "https://example.com/private"
    policy_calls: list[str] = []
    continued_documents: list[str] = []

    async def fake_validate(_url):
        return None

    async def document_policy(url):
        policy_calls.append(url)
        if url == destination:
            return DocumentPolicyDecision(
                allowed=False,
                reason=DocumentPolicyBlockReason.ROBOTS_DISALLOWED,
                error="rendered navigation denied by robots.txt",
            )
        return DocumentPolicyDecision(allowed=True)

    class FakeRequest:
        def __init__(self, url, frame):
            self.url = url
            self.resource_type = "document"
            self.frame = frame

    class FakeRoute:
        def __init__(self, url, frame):
            self.request = FakeRequest(url, frame)
            self.aborted = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            continued_documents.append(self.request.url)

    class FakePage:
        handler = None
        goto_calls = 0
        main_frame = object()

        def set_default_timeout(self, _timeout):
            pass

        async def goto(self, url, **_kwargs):
            self.goto_calls += 1
            initial = FakeRoute(url, self.main_frame)
            await self.handler(initial)
            assert not initial.aborted
            redirected = FakeRoute(destination, self.main_frame)
            await self.handler(redirected)
            assert redirected.aborted
            raise RuntimeError("navigation aborted")

        async def close(self):
            pass

        async def content(self):
            raise AssertionError("denied destination must not produce DOM bytes")

    page = FakePage()

    class FakeContext:
        async def route(self, _pattern, handler):
            page.handler = handler

        async def route_web_socket(self, _pattern, _handler):
            pass

        async def new_page(self):
            return page

    async def fake_acquire():
        return FakeContext()

    async def fake_release(_context, healthy):
        assert healthy is False

    monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
    monkeypatch.setattr(renderer, "_acquire_context", fake_acquire)
    monkeypatch.setattr(renderer, "_release_context", fake_release)

    with pytest.raises(DocumentPolicyDeniedError, match="denied by robots"):
        await renderer.render(source, document_policy=document_policy)

    assert page.goto_calls == 1
    assert continued_documents == [source]
    assert policy_calls == [source, destination]


@pytest.mark.anyio
async def test_renderer_aborts_denied_subframe_without_failing_allowed_main_page(
    monkeypatch,
):
    renderer = OptimizedRenderer()
    source = "https://example.com/article"
    embed = "https://video.example/embed"
    policy_calls: list[str] = []
    continued_documents: list[str] = []

    async def fake_validate(_url):
        return None

    async def document_policy(url):
        policy_calls.append(url)
        if url == embed:
            return DocumentPolicyDecision(
                allowed=False,
                reason=DocumentPolicyBlockReason.OFF_SITE,
                error="embed is outside recursive scope",
            )
        return DocumentPolicyDecision(allowed=True)

    class FakeRequest:
        resource_type = "document"

        def __init__(self, url, frame):
            self.url = url
            self.frame = frame

    class FakeRoute:
        def __init__(self, url, frame):
            self.request = FakeRequest(url, frame)
            self.aborted = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            continued_documents.append(self.request.url)

    class FakeResponse:
        status = 200

        async def all_headers(self):
            return {"content-type": "text/html"}

    class FakePage:
        handler = None
        main_frame = object()
        child_frame = object()
        url = source
        html = "<html><body>" + ("allowed main content " * 400) + "</body></html>"

        def set_default_timeout(self, _timeout):
            pass

        async def goto(self, url, **_kwargs):
            main = FakeRoute(url, self.main_frame)
            await self.handler(main)
            assert not main.aborted
            child = FakeRoute(embed, self.child_frame)
            await self.handler(child)
            assert child.aborted
            return FakeResponse()

        async def evaluate(self, _script):
            return len(self.html)

        async def content(self):
            return self.html

        async def title(self):
            return "Allowed article"

        async def close(self):
            pass

    page = FakePage()

    class FakeContext:
        async def route(self, _pattern, handler):
            page.handler = handler

        async def route_web_socket(self, _pattern, _handler):
            pass

        async def new_page(self):
            return page

    async def fake_acquire():
        return FakeContext()

    async def fake_release(_context, healthy):
        assert healthy is True

    monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
    monkeypatch.setattr(renderer, "_acquire_context", fake_acquire)
    monkeypatch.setattr(renderer, "_release_context", fake_release)

    result = await renderer.render(source, document_policy=document_policy)

    assert result.rendered is True
    assert result.title == "Allowed article"
    assert continued_documents == [source]
    assert policy_calls == [source, embed]


@pytest.mark.anyio
async def test_context_is_destroyed_after_every_render():
    renderer = OptimizedRenderer()

    class FakeContext:
        closed = False

        async def close(self):
            self.closed = True

    context = FakeContext()
    await renderer._release_context(context, healthy=True)
    assert context.closed is True


@pytest.mark.anyio
async def test_new_context_blocks_service_workers_and_grants_no_permissions(monkeypatch):
    renderer = OptimizedRenderer()
    captured = {}

    class FakeContext:
        async def add_init_script(self, script):
            pass

    class FakeBrowser:
        version = "140.0.0.0"

        async def new_context(self, **kwargs):
            captured.update(kwargs)
            return FakeContext()

    async def fake_browser():
        return FakeBrowser()

    monkeypatch.setattr(renderer, "_ensure_browser", fake_browser)
    await renderer._create_context()

    assert captured["service_workers"] == "block"
    assert "permissions" not in captured
    assert "geolocation" not in captured
    assert "Chrome/140.0.0.0" in captured["user_agent"]


@pytest.mark.anyio
async def test_declared_oversized_render_never_serializes_dom(monkeypatch):
    renderer = OptimizedRenderer()
    monkeypatch.setattr(settings, "playwright_max_html_bytes", 10)

    class FakeResponse:
        status = 200

        async def all_headers(self):
            return {"content-length": "11", "content-type": "text/html"}

    class FakePage:
        url = "https://example.com/"

        async def goto(self, *args, **kwargs):
            return FakeResponse()

        async def evaluate(self, script):
            raise AssertionError("declared oversize must fail before DOM inspection")

        async def content(self):
            raise AssertionError("declared oversize must fail before page.content")

    with pytest.raises(RuntimeError, match="byte limit"):
        await renderer._do_render(
            FakePage(),
            "https://example.com/",
            None,
            100,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("disable_sandbox", [False, True])
async def test_browser_launch_explicitly_controls_chromium_sandbox(
    monkeypatch,
    disable_sandbox,
):
    renderer = OptimizedRenderer()
    captured = {}

    class FakeBrowser:
        version = "140.0.0.0"

        def is_connected(self):
            return True

        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kwargs):
            captured.update(kwargs)
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            pass

    class FakePlaywrightStarter:
        async def start(self):
            return FakePlaywright()

    def fake_async_playwright():
        return FakePlaywrightStarter()

    monkeypatch.setattr(settings, "playwright_disable_sandbox", disable_sandbox)
    monkeypatch.setattr(renderer_module, "async_playwright", fake_async_playwright)

    await renderer._ensure_browser()

    assert captured["chromium_sandbox"] is not disable_sandbox
    assert ("--no-sandbox" in captured["args"]) is disable_sandbox
    assert ("--disable-setuid-sandbox" in captured["args"]) is disable_sandbox
    await renderer.stop()


@pytest.mark.anyio
async def test_stop_driver_even_when_browser_is_already_disconnected():
    renderer = OptimizedRenderer()
    driver_stopped = False

    class FailedBrowser:
        async def close(self):
            raise RuntimeError("driver transport already closed")

    class FakePlaywright:
        async def stop(self):
            nonlocal driver_stopped
            driver_stopped = True

    renderer._browser = FailedBrowser()
    renderer._playwright = FakePlaywright()

    await renderer.stop()

    assert driver_stopped is True
    assert renderer._browser is None
    assert renderer._playwright is None


@pytest.mark.anyio
async def test_ssrf_log_removes_credentials_query_and_fragment(monkeypatch):
    renderer = OptimizedRenderer()
    events = []

    async def fake_validate(url):
        return "blocked"

    class FakeLogger:
        def warning(self, event, **kwargs):
            events.append((event, kwargs))

    monkeypatch.setattr("app.services.fetcher.validate_public_url", fake_validate)
    monkeypatch.setattr(renderer_module, "logger", FakeLogger())

    result = await renderer.render(
        "https://alice:secret@example.com/private?token=top-secret#fragment"
    )

    assert result.rendered is False
    assert events == [
        (
            "render_ssrf_blocked",
            {"url": "https://example.com", "reason": "blocked"},
        )
    ]
