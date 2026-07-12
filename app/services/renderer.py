from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import time as time_module
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright

from app.config import settings

if TYPE_CHECKING:
    from typing import Any

logger = structlog.get_logger()

# Resource types to block for faster rendering (30-50% speedup)
BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet", "texttrack", "imageset"}

# Stealth user agents — rotate to avoid fingerprinting
STEALTH_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

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
        # Reusable BrowserContext pool. Creating a context (incl. evaluating the
        # stealth init script) costs 100-300ms; the old code paid that on EVERY
        # render and threw the context away. The page semaphore caps concurrent
        # renders at max_concurrent_pages, so that many contexts suffice.
        self._ctx_pool: asyncio.Queue[BrowserContext] = asyncio.Queue()
        self._ctx_created = 0
        self._ctx_lock = asyncio.Lock()

    async def _acquire_context(self) -> BrowserContext:
        try:
            return self._ctx_pool.get_nowait()
        except asyncio.QueueEmpty:
            pass
        async with self._ctx_lock:
            if self._ctx_created < settings.max_concurrent_pages:
                ctx = await self._create_context()
                self._ctx_created += 1
                return ctx
        return await self._ctx_pool.get()

    async def _release_context(self, ctx: BrowserContext, healthy: bool) -> None:
        if healthy:
            try:
                await ctx.clear_cookies()  # avoid cross-site cookie leakage
            except Exception:
                healthy = False
        if healthy:
            await self._ctx_pool.put(ctx)
            return
        with contextlib.suppress(Exception):
            await ctx.close()
        async with self._ctx_lock:
            self._ctx_created -= 1

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser

            logger.info("launching_playwright_browser")
            self._playwright = await async_playwright().start()
            launch_kwargs: dict[str, Any] = {}
            if settings.playwright_proxy:
                launch_kwargs["proxy"] = {"server": settings.playwright_proxy}
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                **launch_kwargs,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    # NOTE: --disable-web-security and the IsolateOrigins/
                    # site-per-process opt-outs were removed. They disabled the
                    # same-origin policy, letting a fetched page read
                    # cross-origin (incl. internal) responses via fetch() — an
                    # SSRF amplifier. Site isolation stays ON.
                    "--disable-features=VizDisplayCompositor",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-ipc-flooding-protection",
                    "--disable-hang-monitor",
                    "--disable-prompt-on-repost",
                    "--disable-sync",
                    "--disable-translate",
                    "--disable-default-apps",
                    "--disable-component-extensions-with-background-pages",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--no-first-run",
                    "--safebrowsing-disable-auto-update",
                    "--use-gl=angle",
                    "--enable-features=NetworkService,NetworkServiceInProcess",
                ],
            )
            logger.info("playwright_browser_ready")
            return self._browser

    async def _create_context(self) -> BrowserContext:
        browser = await self._ensure_browser()
        ua = STEALTH_AGENTS[self._agent_idx % len(STEALTH_AGENTS)]
        self._agent_idx += 1

        context = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            locale="en-US",
            timezone_id="America/New_York",
            permissions=["geolocation"],
            geolocation={"latitude": 40.7128, "longitude": -74.0060},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-CH-UA-Platform": '"macOS"',
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
    ) -> RenderResult:
        """Render a URL with full JS execution. Returns HTML + metadata.

        Uses resource blocking, stealth evasion, and fast load strategies.
        """
        timeout = timeout_ms or int(settings.playwright_timeout_s * 1000)

        # SSRF: validate before spending a browser page on it, and re-validate
        # here so the renderer is safe regardless of which caller invoked it.
        from app.services.fetcher import _ip_is_blocked, validate_public_url

        ssrf_err = await validate_public_url(url)
        if ssrf_err:
            logger.warning("render_ssrf_blocked", url=url, reason=ssrf_err)
            return RenderResult(html="", title="", rendered=False)

        # Overall wall-clock ceiling. Without it a hung page.content()/title()
        # holds a page-semaphore permit forever and wedges the renderer (only
        # MAX_CONCURRENT_PAGES permits exist). Generous vs the per-nav timeout.
        overall_timeout_s = (timeout * 3 + 15000) / 1000.0

        async with self._page_sem:
            context = await self._acquire_context()
            page: Page | None = None
            healthy = True
            try:
                page = await context.new_page()
                page.set_default_timeout(timeout)

                # Block unnecessary resources for speed, and abort any request
                # whose host is a non-public IP literal or a non-http(s) scheme
                # (SSRF via a sub-resource / redirect the top-level check missed).
                async def _block_unnecessary(route: Route) -> None:
                    req = route.request
                    parsed = urlparse(req.url)
                    if parsed.scheme not in ("http", "https"):
                        await route.abort()
                        return
                    host = parsed.hostname or ""
                    try:
                        ipaddress.ip_address(host)
                        is_ip_literal = True
                    except ValueError:
                        is_ip_literal = False
                    if is_ip_literal and _ip_is_blocked(host):
                        await route.abort()
                        return
                    if req.resource_type in BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", _block_unnecessary)

                return await asyncio.wait_for(
                    self._do_render(page, url, wait_for_selector, timeout),
                    timeout=overall_timeout_s,
                )
            except (TimeoutError, Exception):
                healthy = False
                raise
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        healthy = False
                await self._release_context(context, healthy)

    async def _do_render(
        self,
        page: Page,
        url: str,
        wait_for_selector: str | None,
        timeout: int,
    ) -> RenderResult:
        t0 = time_module.monotonic()

        # Fast load: domcontentloaded first, then wait for selector if needed
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            # Retry with longer timeout and networkidle for slow pages.
            # Best-effort — get whatever content is there.
            with contextlib.suppress(Exception):
                await page.goto(url, wait_until="load", timeout=timeout * 2)

        # If a specific selector is requested, wait for it
        if wait_for_selector:
            with contextlib.suppress(Exception):
                await page.wait_for_selector(wait_for_selector, timeout=min(timeout, 10000))

        # Extra brief settle time for dynamic content (only if content looks sparse)
        html = await page.content()
        if len(html) < 5000:
            try:
                await page.wait_for_timeout(2000)  # 2s max settle
                html = await page.content()
            except Exception:
                pass

        title = await page.title()
        elapsed = (time_module.monotonic() - t0) * 1000

        return RenderResult(
            html=html,
            title=title,
            rendered=True,
            latency_ms=round(elapsed, 1),
        )

    async def stop(self) -> None:
        while not self._ctx_pool.empty():
            try:
                ctx = self._ctx_pool.get_nowait()
                await ctx.close()
            except Exception:
                pass
        self._ctx_created = 0
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("playwright_stopped")


class RenderResult:
    __slots__ = ("html", "title", "rendered", "latency_ms")

    def __init__(
        self,
        html: str = "",
        title: str = "",
        rendered: bool = False,
        latency_ms: float = 0.0,
    ) -> None:
        self.html = html
        self.title = title
        self.rendered = rendered
        self.latency_ms = latency_ms


_renderer: OptimizedRenderer | None = None


def get_renderer() -> OptimizedRenderer:
    global _renderer
    if _renderer is None:
        _renderer = OptimizedRenderer()
    return _renderer


# Known static sites that DON'T need JS — skip escalation
STATIC_SITES = re.compile(
    r"docs\.python\.org|readthedocs\.io|doc\.rust-lang\.org|"
    r"wikipedia\.org|github\.com/[^/]+/[^/]+$|"
    r"stackoverflow\.com|stackexchange\.com|"
    r"docs\.docker\.com|kubernetes\.io/docs|"
    r"manpages\.|\.mdn\.|"
    r"pubmed\.ncbi\.nlm\.nih\.gov|"
    r"arxiv\.org/abs/|arxiv\.org/pdf/",
    re.IGNORECASE,
)


def needs_js_rendering(html: str, url: str) -> bool:
    """Fast heuristic: does this page need JS to render meaningful content?"""
    # Known static sites — don't waste time on JS rendering
    if STATIC_SITES.search(url):
        return False

    # Known JS-required domains
    if JS_REQUIRED_DOMAINS.search(url):
        return True

    # SPA shell detection
    stripped = html[:5000].strip()
    if SPA_SIGNATURES.search(stripped):
        return True

    # Too little content (< 200 chars of visible text after stripping tags)
    visible = re.sub(r"<[^>]+>", " ", stripped)
    visible = re.sub(r"\s+", " ", visible).strip()
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
