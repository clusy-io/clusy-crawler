from __future__ import annotations

import asyncio
import contextlib
import re
import time as time_module
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Response, Route, async_playwright

from app.config import settings
from app.services.document_policy import (
    DocumentPolicyDeniedError,
    enforce_document_policy,
)
from app.services.github import GitHubPageKind, classify_github_url

if TYPE_CHECKING:
    from typing import Any

    from app.services.document_policy import DocumentPolicyCallback

logger = structlog.get_logger()

# Resource types to block for faster rendering (30-50% speedup)
BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet", "texttrack", "imageset"}

# Pages known to require JS rendering — skip the conditional check entirely
JS_REQUIRED_DOMAINS = re.compile(
    r"acm\.org|springer\.com|ieee\.org|sciencedirect\.com|"
    r"nature\.com/articles|cell\.com|nejm\.org|thelancet\.com|"
    r"pubmed\.ncbi\.nlm\.nih\.gov|sci-hub\.|"
    r"medium\.com|substack\.com|dev\.to|hashnode\.dev",
    re.IGNORECASE,
)

# Pages that are SPA frameworks — needs JS to render any content
SPA_SIGNATURES = re.compile(
    r"<div\s+id=\"(?:root|app|__next|__nuxt)\"|"
    r"react-root|vue-app|angular-app|"
    r"<script\s+src=\"[^\"]*(?:react|vue|angular|svelte)[^\"]*\.js",
    re.IGNORECASE,
)

# A framework marker alone is not evidence that rendering is required: Next,
# Nuxt, React, and Vue commonly server-render complete HTML while retaining the
# same root marker for hydration. Only treat a marker as an SPA shell when the
# server response also has very little visible text.
SPA_SHELL_VISIBLE_TEXT_MAX = 200


def _raise_navigation_denial(
    navigation_denials: list[DocumentPolicyDeniedError] | None,
) -> None:
    if navigation_denials:
        raise navigation_denials[0]


class OptimizedRenderer:
    """High-performance JS renderer with stealth and resource blocking."""

    _playwright = None
    _browser: Browser | None = None
    _page_sem: asyncio.Semaphore
    _browser_lock: asyncio.Lock
    _agent_idx: int = 0

    def __init__(self) -> None:
        self._page_sem = asyncio.Semaphore(settings.max_concurrent_pages)
        self._browser_lock = asyncio.Lock()

    async def _acquire_context(self) -> BrowserContext:
        # A context is a security boundary. Reusing one after merely clearing
        # cookies leaves localStorage, IndexedDB, caches, service workers, and
        # other origin state visible to the next crawl.
        return await self._create_context()

    async def _release_context(self, ctx: BrowserContext, healthy: bool) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ctx.close(), timeout=5)

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser

            logger.info("launching_playwright_browser")
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None
            self._playwright = await async_playwright().start()
            launch_kwargs: dict[str, Any] = {}
            if settings.playwright_proxy:
                launch_kwargs["proxy"] = {"server": settings.playwright_proxy}
            # Playwright defaults chromium_sandbox to False even when no
            # --no-sandbox argument is supplied, so enable it explicitly for
            # untrusted crawl targets.
            launch_kwargs["chromium_sandbox"] = not settings.playwright_disable_sandbox
            launch_args = [
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--mute-audio",
                "--no-first-run",
            ]
            if settings.playwright_disable_sandbox:
                launch_args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                **launch_kwargs,
                args=launch_args,
            )
            logger.info("playwright_browser_ready")
            return self._browser

    async def _create_context(self) -> BrowserContext:
        browser = await self._ensure_browser()
        chromium_version = browser.version
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36"
        )
        self._agent_idx += 1

        context = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            locale="en-US",
            timezone_id="UTC",
            service_workers="block",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        # Comprehensive stealth: override all known bot detection vectors
        await context.add_init_script("""
            // Core anti-detection
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

            // Chrome runtime detection
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {},
            };

            // Permissions API
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
            );

            // WebGL vendor spoofing
            const getParameterProto = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameterProto.call(this, parameter);
            };

            // Canvas fingerprint randomization
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const ctx = this.getContext('2d');
                if (ctx) {
                    const imageData = ctx.getImageData(0, 0, this.width, this.height);
                    for (let i = 0; i < imageData.data.length; i += 4) {
                        imageData.data[i] ^= 1;
                    }
                    ctx.putImageData(imageData, 0, 0);
                }
                return origToDataURL.apply(this, arguments);
            };

            // Battery API
            if (navigator.getBattery) {
                const origGetBattery = navigator.getBattery;
                navigator.getBattery = () => Promise.resolve({
                    charging: true, chargingTime: 0, dischargingTime: Infinity,
                    level: 1, addEventListener: () => {},
                });
            }

            // Connection info
            Object.defineProperty(navigator, 'connection', {
                get: () => ({ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false }),
            });
        """)

        return context

    async def render(
        self,
        url: str,
        wait_for_selector: str | None = None,
        timeout_ms: int | None = None,
        *,
        document_policy: DocumentPolicyCallback | None = None,
    ) -> RenderResult:
        """Render a URL with full JS execution. Returns HTML + metadata.

        Uses resource blocking, stealth evasion, and fast load strategies.
        """
        timeout = timeout_ms or int(settings.playwright_timeout_s * 1000)

        # SSRF: validate before spending a browser page on it, and re-validate
        # here so the renderer is safe regardless of which caller invoked it.
        from app.services.fetcher import _safe_log_url, validate_public_url

        ssrf_err = await validate_public_url(url)
        if ssrf_err:
            logger.warning(
                "render_ssrf_blocked",
                url=_safe_log_url(url),
                reason=ssrf_err,
            )
            return RenderResult(html="", title="", rendered=False)

        # The deadline covers queueing, context creation, navigation, DOM
        # serialization, and cleanup—not merely page.goto().
        overall_timeout_s = (timeout * 3 + 15000) / 1000.0

        try:
            async with asyncio.timeout(overall_timeout_s):
                async with self._page_sem:
                    context = await self._acquire_context()
                    page: Page | None = None
                    healthy = True
                    try:
                        # Context routing covers popups as well as the main page.
                        origin_validation: dict[str, asyncio.Task[str | None]] = {}
                        navigation_denials: list[DocumentPolicyDeniedError] = []

                        async def _block_unnecessary(route: Route) -> None:
                            req = route.request
                            if req.resource_type in BLOCKED_RESOURCES:
                                await route.abort()
                                return

                            parsed = urlparse(req.url)
                            if parsed.scheme not in ("http", "https"):
                                await route.abort()
                                return

                            origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
                            if req.resource_type == "document":
                                unsafe_reason = await validate_public_url(req.url)
                                if unsafe_reason is None:
                                    if document_policy is not None:
                                        try:
                                            await enforce_document_policy(
                                                document_policy,
                                                req.url,
                                            )
                                        except DocumentPolicyDeniedError as exc:
                                            # Every document (including
                                            # iframes) is checked and denied
                                            # before bytes, but a blocked embed
                                            # must not fail an otherwise
                                            # allowed main page.
                                            if (
                                                page is not None
                                                and req.frame == page.main_frame
                                            ):
                                                navigation_denials.append(exc)
                                            await route.abort()
                                            return
                                    from app.services.rate_limiter import get_rate_limiter

                                    await get_rate_limiter().acquire(req.url)
                            else:
                                validation = origin_validation.get(origin)
                                if validation is None:
                                    validation = asyncio.create_task(
                                        validate_public_url(req.url)
                                    )
                                    origin_validation[origin] = validation
                                unsafe_reason = await validation
                            if unsafe_reason:
                                logger.warning(
                                    "render_subrequest_ssrf_blocked",
                                    url=_safe_log_url(req.url),
                                    reason=unsafe_reason,
                                    resource_type=req.resource_type,
                                )
                                await route.abort()
                                return
                            await route.continue_()

                        async def _block_websocket(web_socket: Any) -> None:
                            await web_socket.close(code=1008, reason="WebSocket disabled")

                        await context.route("**/*", _block_unnecessary)
                        await context.route_web_socket("**/*", _block_websocket)
                        page = await context.new_page()
                        page.set_default_timeout(timeout)
                        if document_policy is None:
                            return await self._do_render(
                                page,
                                url,
                                wait_for_selector,
                                timeout,
                            )
                        try:
                            return await self._do_render(
                                page,
                                url,
                                wait_for_selector,
                                timeout,
                                navigation_denials=navigation_denials,
                            )
                        except Exception:
                            # A document route can be denied concurrently with
                            # DOM work. Prefer the typed policy denial over a
                            # secondary Playwright "navigation interrupted"
                            # exception so the frontier records the right
                            # terminal reason.
                            _raise_navigation_denial(navigation_denials)
                            raise
                    except BaseException:
                        healthy = False
                        raise
                    finally:
                        if page:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(page.close(), timeout=3)
                        for validation in origin_validation.values():
                            if not validation.done():
                                validation.cancel()
                        if origin_validation:
                            await asyncio.gather(
                                *origin_validation.values(),
                                return_exceptions=True,
                            )
                        await self._release_context(context, healthy)
        except TimeoutError as exc:
            raise RuntimeError("browser render deadline exceeded") from exc

    async def _do_render(
        self,
        page: Page,
        url: str,
        wait_for_selector: str | None,
        timeout: int,
        *,
        navigation_denials: list[DocumentPolicyDeniedError] | None = None,
    ) -> RenderResult:
        t0 = time_module.monotonic()

        # Fast load: domcontentloaded first, then wait for selector if needed.
        response: Response | None = None
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout,
            )
        except Exception:
            _raise_navigation_denial(navigation_denials)
            # Retry with a longer timeout and the full load event for slow pages.
            # Best-effort — get whatever content is there.
            with contextlib.suppress(Exception):
                response = await page.goto(
                    url,
                    wait_until="load",
                    timeout=timeout * 2,
                )
        _raise_navigation_denial(navigation_denials)

        # If a specific selector is requested, wait for it
        if wait_for_selector:
            with contextlib.suppress(Exception):
                await page.wait_for_selector(wait_for_selector, timeout=min(timeout, 10000))
        _raise_navigation_denial(navigation_denials)

        if response is not None and not 200 <= response.status < 300:
            return RenderResult(
                rendered=False,
                status_code=response.status,
                final_url=page.url,
            )

        headers = await response.all_headers() if response is not None else {}
        declared_length = headers.get("content-length", "")
        if declared_length.isdigit() and int(declared_length) > settings.playwright_max_html_bytes:
            raise RuntimeError("rendered response exceeds configured byte limit")

        # Check DOM character count before page.content() creates a second,
        # potentially huge Python string.
        dom_chars = await page.evaluate(
            "() => document.documentElement ? document.documentElement.outerHTML.length : 0"
        )
        if isinstance(dom_chars, int) and dom_chars > settings.playwright_max_html_bytes:
            raise RuntimeError("rendered DOM exceeds configured byte limit")

        # Extra brief settle time for dynamic content (only if content looks sparse)
        html = await page.content()
        _raise_navigation_denial(navigation_denials)
        if len(html.encode("utf-8")) > settings.playwright_max_html_bytes:
            raise RuntimeError("rendered HTML exceeds configured byte limit")
        if len(html) < 5000:
            with contextlib.suppress(Exception):
                await page.wait_for_timeout(2000)  # 2s max settle
            _raise_navigation_denial(navigation_denials)
            dom_chars = await page.evaluate(
                "() => document.documentElement ? document.documentElement.outerHTML.length : 0"
            )
            if (
                isinstance(dom_chars, int)
                and dom_chars > settings.playwright_max_html_bytes
            ):
                raise RuntimeError("rendered DOM exceeds configured byte limit")
            html = await page.content()
            _raise_navigation_denial(navigation_denials)
            if len(html.encode("utf-8")) > settings.playwright_max_html_bytes:
                raise RuntimeError("rendered HTML exceeds configured byte limit")

        title = await page.title()
        _raise_navigation_denial(navigation_denials)
        elapsed = (time_module.monotonic() - t0) * 1000

        return RenderResult(
            html=html,
            title=title,
            # No main-document response means navigation was aborted/timed out.
            # Treat any partial browser error page as a failed render so callers
            # can safely fall back to the static fetch path.
            rendered=response is not None,
            latency_ms=round(elapsed, 1),
            status_code=response.status if response is not None else 0,
            content_type=headers.get("content-type", ""),
            final_url=page.url if response is not None else "",
        )
    async def stop(self) -> None:
        # Clear references first so readiness fails immediately and a partial
        # close cannot leave a stale, disconnected browser in the singleton.
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if browser:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except Exception as exc:
                # A terminal SIGINT can reach the Playwright driver before the
                # parent's lifespan cleanup. Driver.stop still needs to run to
                # retrieve its connection future and avoid an asyncio leak.
                logger.warning(
                    "playwright_browser_close_failed",
                    error_type=type(exc).__name__,
                )
        if playwright:
            try:
                await asyncio.wait_for(playwright.stop(), timeout=5)
            except Exception as exc:
                logger.warning(
                    "playwright_driver_stop_failed",
                    error_type=type(exc).__name__,
                )
        logger.info("playwright_stopped")


class RenderResult:
    __slots__ = (
        "html",
        "title",
        "rendered",
        "latency_ms",
        "status_code",
        "content_type",
        "final_url",
    )

    def __init__(
        self,
        html: str = "",
        title: str = "",
        rendered: bool = False,
        latency_ms: float = 0.0,
        status_code: int = 0,
        content_type: str = "",
        final_url: str = "",
    ) -> None:
        self.html = html
        self.title = title
        self.rendered = rendered
        self.latency_ms = latency_ms
        self.status_code = status_code
        self.content_type = content_type
        self.final_url = final_url


_renderer: OptimizedRenderer | None = None


def get_renderer() -> OptimizedRenderer:
    global _renderer
    if _renderer is None:
        _renderer = OptimizedRenderer()
    return _renderer


async def start_renderer() -> None:
    """Warm the process-global browser once during application startup."""
    renderer = get_renderer()
    await asyncio.wait_for(
        renderer._ensure_browser(),
        timeout=settings.playwright_timeout_s,
    )


def renderer_is_ready() -> bool:
    renderer = _renderer
    return bool(
        renderer is not None
        and renderer._browser is not None
        and renderer._browser.is_connected()
    )


async def stop_renderer() -> None:
    """Stop the process-global browser, if it was started."""
    global _renderer
    renderer = _renderer
    _renderer = None
    if renderer is not None:
        await renderer.stop()


# Known static sites that DON'T need JS — skip escalation
STATIC_SITES = re.compile(
    r"docs\.python\.org|readthedocs\.io|doc\.rust-lang\.org|"
    r"wikipedia\.org|"
    r"stackoverflow\.com|stackexchange\.com|"
    r"docs\.docker\.com|kubernetes\.io/docs|"
    r"manpages\.|\.mdn\.|"
    r"pubmed\.ncbi\.nlm\.nih\.gov|"
    r"arxiv\.org/abs/|arxiv\.org/pdf/",
    re.IGNORECASE,
)


def _github_has_static_specialized_content(url: str) -> bool:
    parsed = classify_github_url(url)
    if parsed is None:
        return False
    if parsed.kind in {
        GitHubPageKind.REPOSITORY,
        GitHubPageKind.TREE,
        GitHubPageKind.BLOB,
        GitHubPageKind.COMMIT,
    }:
        return True
    if parsed.kind != GitHubPageKind.RELEASES:
        return False
    try:
        path = urlparse(url).path.casefold()
    except ValueError:
        return False
    return "/releases/tag/" in path or path.endswith("/releases/latest")


def needs_js_rendering(html: str, url: str) -> bool:
    """Fast heuristic: does this page need JS to render meaningful content?"""
    # GitHub server-renders repositories, directory listings, source controls,
    # release bodies, and commit diffs. Their route-specific extractor is both
    # cleaner and faster than hydrating the surrounding application chrome.
    # Threads remain render-eligible because comments/replies can be lazy-loaded.
    if _github_has_static_specialized_content(url):
        return False

    # Known static sites — don't waste time on JS rendering
    if STATIC_SITES.search(url):
        return False

    # Known JS-required domains
    if JS_REQUIRED_DOMAINS.search(url):
        return True

    # Strip script/style payloads before measuring visible text. Framework
    # bundles and serialized hydration state are not content a reader can see.
    stripped = html[:50000].strip()
    visible_source = re.sub(
        r"<(?:script|style|template|noscript|svg)\b[^>]*>.*?</(?:script|style|template|noscript|svg)>",
        " ",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible = re.sub(r"<[^>]+>", " ", visible_source)
    visible = re.sub(r"\s+", " ", visible).strip()

    # SPA shell detection. A framework root plus substantial server-rendered
    # text is a hydrated SSR page, not an empty client-only shell.
    if SPA_SIGNATURES.search(stripped) and len(visible) < SPA_SHELL_VISIBLE_TEXT_MAX:
        return True

    # Too little content (< 200 chars of visible text after stripping tags)
    if len(visible) < 200:
        return True

    # Cloudflare / bot challenge detection
    if any(
        k in stripped.lower()
        for k in (
            "just a moment",
            "checking your browser",
            "cf-browser-verification",
            "enable javascript",
            "please enable cookies",
            "attention required!",
            "captcha",
            "ddos-guard",
            "_cf_chl_opt",
        )
    ):
        return True

    # No substantial text content found
    return (
        "<p>" not in stripped
        and "<article" not in stripped
        and "<main" not in stripped
        and len(visible) < 500
    )
