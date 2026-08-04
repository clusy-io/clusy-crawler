from __future__ import annotations

import asyncio
import importlib
import inspect
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import structlog

from app.config import settings
from app.services.source_serialization_receipt_v1 import (
    MINERU_HTML_REVISION,
    QualitySourceInputIneligibleError,
    QualitySourceSerializationReceiptV1,
    mint_quality_source_serialization_v1,
    preflight_quality_source_input_v1,
    probe_pinned_quality_serialization_runtime_v1,
)

__all__ = ["MINERU_HTML_REVISION"]

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future

    from app.services.source_selection_receipt_v0 import (
        QualitySourceSelectionReceiptV0,
    )

logger = structlog.get_logger()
T = TypeVar("T")

# Keep the strategy name stable so benchmark reports and production telemetry
# can distinguish model-assisted output from the deterministic fallback.  The
# exact upstream revision lives with the receipt verifier that enforces it.
QUALITY_STRATEGY = "mineru-html-v1.1-openai"
QualityPromptProfile = Literal["openai_json", "mineru_compact"]


@dataclass(frozen=True, slots=True)
class QualityExtractionConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_s: float
    max_concurrency: int
    failure_threshold: int
    cooldown_s: float
    max_input_chars: int
    max_output_chars: int
    prompt_profile: QualityPromptProfile = "openai_json"
    capacity_timeout_s: float = 1.0
    shutdown_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if self.prompt_profile not in {"openai_json", "mineru_compact"}:
            raise ValueError("unsupported quality prompt profile")
        if self.capacity_timeout_s <= 0:
            raise ValueError("quality capacity timeout must be positive")
        if self.shutdown_timeout_s <= 0:
            raise ValueError("quality shutdown timeout must be positive")

    @property
    def enabled(self) -> bool:
        # A blank key intentionally disables the feature. This agrees with the
        # service configuration contract and avoids accidentally sending a
        # request to an endpoint that the operator only partially configured.
        return bool(self.base_url.strip() and self.api_key and self.model.strip())

    @property
    def prompt_version(self) -> str:
        if self.prompt_profile == "mineru_compact":
            return "short_compact"
        return "v2"

    @property
    def response_format(self) -> str:
        if self.prompt_profile == "mineru_compact":
            return "compact"
        return "json"


@dataclass(frozen=True, slots=True)
class QualityExtraction:
    text: str
    strategy: str = QUALITY_STRATEGY
    selection_receipt: (
        QualitySourceSelectionReceiptV0 | QualitySourceSerializationReceiptV1 | None
    ) = None


@dataclass(frozen=True, slots=True)
class _OfficialBindings:
    extractor_type: Any
    config_type: Any
    input_type: Any


@dataclass(slots=True)
class _OfficialRuntime:
    """One exclusive upstream extractor/client slot.

    MinerU's synchronous ``process`` API has small mutable backend state. A
    runtime is therefore leased to only one worker at a time rather than
    sharing an extractor across concurrent preprocessing calls.
    """

    extractor: Any | None = None
    async_sdk: Any | None = None
    active_call: Future[Any] | None = None
    cancel_requested: bool = False
    call_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
    )


class _QualityEventLoop:
    """A process-local event loop dedicated to reusable quality HTTP clients."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="clusy-quality-io",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("quality I/O loop startup timed out")
        if self._startup_error is not None:
            raise RuntimeError("quality I/O loop startup failed") from self._startup_error

    def _run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()
        except BaseException as error:
            self._startup_error = error
            self._ready.set()

    def run(
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        on_submit: Callable[[Future[T]], None] | None = None,
    ) -> T:
        loop = self._loop
        if loop is None or not loop.is_running():
            coroutine.close()
            raise RuntimeError("quality I/O loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        if on_submit is not None:
            on_submit(future)
        return future.result()

    async def close(
        self,
        clients: list[Any],
        *,
        cancel_pending: bool,
        timeout_s: float,
    ) -> None:
        loop = self._loop
        if loop is None:
            return

        async def close_clients() -> None:
            if cancel_pending:
                current = asyncio.current_task()
                pending = [
                    task
                    for task in asyncio.all_tasks()
                    if task is not current and not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            close_calls = []
            for client in clients:
                close = getattr(client, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        close_calls.append(result)
            if close_calls:
                results = await asyncio.gather(*close_calls, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise RuntimeError("quality client cleanup failed") from result

        cleanup_error: BaseException | None = None
        if loop.is_running():
            cleanup = asyncio.run_coroutine_threadsafe(close_clients(), loop)
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(cleanup),
                    timeout=timeout_s,
                )
            except BaseException as error:
                cleanup_error = error
                cleanup.cancel()
            finally:
                loop.call_soon_threadsafe(loop.stop)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._thread.join, timeout_s),
                timeout=timeout_s + 0.1,
            )
        finally:
            self._loop = None
        if self._thread.is_alive() and cleanup_error is None:
            cleanup_error = RuntimeError("quality I/O loop did not stop")
        if cleanup_error is not None:
            raise cleanup_error


def _load_official_bindings() -> _OfficialBindings:
    """Import the pinned optional MinerU package only on the first quality call."""
    mineru_html = importlib.import_module("mineru_html")
    mineru_base = importlib.import_module("mineru_html.base")
    return _OfficialBindings(
        extractor_type=mineru_html.MinerUHTML_OpenAI,
        config_type=mineru_html.MinerUHTMLConfig,
        input_type=mineru_base.MinerUHTMLInput,
    )


@lru_cache(maxsize=1)
def quality_dependency_available() -> bool:
    """Return whether the complete pinned v1 quality capability can execute.

    The lightweight runtime image intentionally omits MinerU-HTML. A configured
    remote endpoint is therefore not sufficient to call the assisted lane: the
    local pinned adapter performs preprocessing and response validation, and
    MinerU Webkit owns the accepted serialization. Cache the functional probe
    because readiness endpoints may be polled often.
    """
    try:
        _load_official_bindings()
        probe_pinned_quality_serialization_runtime_v1()
    except Exception:
        return False
    return True


class QualityExtractor:
    """Production guardrails around MinerU-HTML's OpenAI-compatible pipeline.

    MinerU exposes a synchronous ``process`` method even for its remote backend.
    Calls therefore run in worker threads. A permit is released by a completion
    callback on the *actual worker*, not by the awaiting request task: timing
    out or cancelling a request cannot start more workers while the old one is
    still running.
    """

    def __init__(
        self,
        config: QualityExtractionConfig,
        *,
        bindings_loader: Callable[[], _OfficialBindings] = _load_official_bindings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._bindings_loader = bindings_loader
        self._clock = clock
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

        self._bindings: _OfficialBindings | None = None
        self._dependency_unavailable = False
        self._bindings_lock = threading.Lock()

        self._state_lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._half_open_probe = False
        self._closed = False

        # Each runtime owns one upstream extractor and AsyncOpenAI client.
        # Clients all perform I/O on one persistent loop, so HTTP keep-alive is
        # reused without ever moving an async connection pool between loops.
        self._runtime_lock = threading.Lock()
        self._available_runtimes = deque(
            _OfficialRuntime() for _ in range(config.max_concurrency)
        )
        self._all_runtimes = tuple(self._available_runtimes)
        self._io_loop: _QualityEventLoop | None = None
        self._io_loop_lock = threading.Lock()

    def _load_bindings_once(self) -> _OfficialBindings:
        if self._bindings is not None:
            return self._bindings
        if self._dependency_unavailable:
            raise RuntimeError("quality dependency unavailable")

        with self._bindings_lock:
            if self._bindings is not None:
                return self._bindings
            if self._dependency_unavailable:
                raise RuntimeError("quality dependency unavailable")
            try:
                bindings = self._bindings_loader()
            except (ImportError, ModuleNotFoundError, AttributeError):
                self._dependency_unavailable = True
                raise
            self._bindings = bindings
            return bindings

    def _admit(self) -> tuple[bool, bool]:
        """Return ``(admitted, is_half_open_probe)`` for the circuit breaker."""
        now = self._clock()
        with self._state_lock:
            if self._open_until <= 0:
                return True, False
            if now < self._open_until:
                return False, False
            if self._half_open_probe:
                return False, False
            self._half_open_probe = True
            return True, True

    def _record_success(self) -> None:
        with self._state_lock:
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._half_open_probe = False

    def _record_failure(self) -> None:
        with self._state_lock:
            self._half_open_probe = False
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.failure_threshold:
                self._open_until = self._clock() + self._config.cooldown_s

    def _record_capacity_timeout(self) -> None:
        """Open immediately when every bounded worker missed the queue SLO."""
        with self._state_lock:
            self._half_open_probe = False
            self._consecutive_failures = max(
                self._consecutive_failures + 1,
                self._config.failure_threshold,
            )
            self._open_until = self._clock() + self._config.cooldown_s

    def _release_probe(self, was_probe: bool) -> None:
        if not was_probe:
            return
        with self._state_lock:
            self._half_open_probe = False

    @staticmethod
    def _cancel_runtime_call(runtime: _OfficialRuntime) -> None:
        """Cancel remote I/O without making the worker slot look available."""
        with runtime.call_lock:
            runtime.cancel_requested = True
            active_call = runtime.active_call
        if active_call is not None:
            active_call.cancel()

    def _get_io_loop(self) -> _QualityEventLoop:
        if self._closed:
            raise RuntimeError("quality extractor is closed")
        if self._io_loop is not None:
            return self._io_loop
        with self._io_loop_lock:
            if self._closed:
                raise RuntimeError("quality extractor is closed")
            if self._io_loop is None:
                self._io_loop = _QualityEventLoop()
            return self._io_loop

    def _initialize_runtime(self, runtime: _OfficialRuntime) -> Any:
        bindings = self._load_bindings_once()
        mineru_config = bindings.config_type(
            use_fall_back="empty",
            early_load=False,
            prompt_version=self._config.prompt_version,
            response_format=self._config.response_format,
            # The trusted Clusy mint performs the one authoritative local
            # serialization after source derivation replay.  Asking upstream
            # to serialize first would duplicate CPU work and widen the race
            # surface of MinerU Webkit's converter.
            output_format="none",
        )
        io_loop = self._get_io_loop()

        async def build_extractor() -> Any:
            return bindings.extractor_type(
                base_url=self._config.base_url,
                sk=self._config.api_key,
                model=self._config.model,
                config=mineru_config,
                retry_times=1,
            )

        # AsyncOpenAI is constructed, used, and eventually closed on this same
        # persistent loop. Constructing one client per bounded runtime avoids
        # undocumented concurrent mutation of MinerU's synchronous extractor
        # while retaining steady-state HTTP connection pooling.
        extractor = io_loop.run(build_extractor())
        runtime.extractor = extractor

        # Upstream's one-attempt retry helper prints the full exception. Replace
        # that helper with an otherwise-equivalent silent call so configured
        # endpoint details (and any vendor diagnostic containing credentials)
        # cannot leak into stdout/stderr.
        llm = extractor.llm

        async def call_once(prompt: str) -> Any:
            return await llm.call_api_async(prompt)

        llm.call_api_async_with_retry = call_once
        process_async = getattr(llm, "process_async", None)
        async_sdk = getattr(llm, "async_sdk", None)
        runtime.async_sdk = async_sdk
        if not callable(process_async) or async_sdk is None:
            raise AttributeError("pinned MinerU OpenAI runtime contract changed")

        def generate_on_quality_loop(
            prompt_list: list[str],
            **_: Any,
        ) -> list[Any]:
            def remember_call(call: Future[list[Any]]) -> None:
                with runtime.call_lock:
                    runtime.active_call = call
                    cancel_requested = runtime.cancel_requested
                if cancel_requested:
                    call.cancel()

            try:
                result: list[Any] = io_loop.run(
                    process_async(prompt_list),
                    on_submit=remember_call,
                )
                return result
            finally:
                with runtime.call_lock:
                    runtime.active_call = None

        llm.generate = generate_on_quality_loop
        return extractor

    def _run_official_pipeline(
        self,
        runtime: _OfficialRuntime,
        html_content: str,
        url: str,
    ) -> QualityExtraction:
        extractor = runtime.extractor
        if extractor is None:
            extractor = self._initialize_runtime(runtime)

        bindings = self._load_bindings_once()
        input_data = bindings.input_type(raw_html=html_content, url=url or None)
        cases = extractor.process(input_data)
        if not isinstance(cases, list) or len(cases) != 1:
            raise ValueError("unexpected MinerU result cardinality")
        case = cases[0]
        if getattr(case, "error", None) is not None:
            raise ValueError("MinerU extraction failed")
        output_data = getattr(case, "output_data", None)
        process_data = getattr(case, "process_data", None)
        simplified_html = getattr(process_data, "simpled_html", None)
        mapped_html = getattr(process_data, "map_html", None)
        generate_output = getattr(case, "generate_output", None)
        raw_model_response = getattr(generate_output, "response", None)
        parse_result = getattr(case, "parse_result", None)
        item_labels = getattr(parse_result, "item_label", None)
        selected_html = getattr(output_data, "main_html", None)
        if (
            type(simplified_html) is not str
            or type(mapped_html) is not str
            or type(raw_model_response) is not str
            or type(selected_html) is not str
        ):
            raise ValueError("MinerU source-selection artifacts are not text")
        assert isinstance(simplified_html, str)
        assert isinstance(mapped_html, str)
        assert isinstance(raw_model_response, str)
        assert isinstance(selected_html, str)
        minted = mint_quality_source_serialization_v1(
            raw_html=html_content,
            source_url=url,
            raw_model_response=raw_model_response,
            response_format=self._config.response_format,
            simplified_html=simplified_html,
            mapped_html=mapped_html,
            item_labels=item_labels,
            selected_html=selected_html,
            upstream_revision=MINERU_HTML_REVISION,
            prompt_profile=self._config.prompt_profile,
            max_output_chars=self._config.max_output_chars,
        )
        if len(minted.text) > self._config.max_output_chars:
            raise ValueError("MinerU output exceeds the configured limit")
        return QualityExtraction(
            text=minted.text,
            selection_receipt=minted.receipt,
        )

    async def extract(self, html_content: str, url: str = "") -> QualityExtraction | None:
        if not self._config.enabled or self._dependency_unavailable or self._closed:
            return None
        if len(html_content) > self._config.max_input_chars:
            logger.info("quality_extraction_fallback", reason="input_too_large")
            return None
        # This fixed local admission is intentionally before breaker and
        # semaphore state. An ineligible page is not a backend attempt and must
        # neither queue behind inference nor turn saturation into an outage.
        try:
            preflight_quality_source_input_v1(html_content)
        except QualitySourceInputIneligibleError:
            logger.info("quality_extraction_fallback", reason="input_ineligible")
            return None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.timeout_s
        admitted, was_probe = self._admit()
        if not admitted:
            logger.info("quality_extraction_fallback", reason="circuit_open")
            return None

        # Bound queueing as well as inference. A wedged worker keeps its permit,
        # while new requests fail fast to the deterministic path.
        try:
            capacity_timeout = min(
                max(0.0, deadline - loop.time()),
                self._config.capacity_timeout_s,
            )
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=capacity_timeout,
            )
        except TimeoutError:
            self._release_probe(was_probe)
            self._record_capacity_timeout()
            logger.warning("quality_extraction_fallback", reason="capacity_timeout")
            return None
        except asyncio.CancelledError:
            self._release_probe(was_probe)
            raise

        with self._runtime_lock:
            if self._closed:
                self._semaphore.release()
                self._release_probe(was_probe)
                return None
            runtime = self._available_runtimes.popleft()
        with runtime.call_lock:
            runtime.active_call = None
            runtime.cancel_requested = False

        try:
            worker = loop.run_in_executor(
                None,
                self._run_official_pipeline,
                runtime,
                html_content,
                url,
            )
        except Exception:
            with self._runtime_lock:
                self._available_runtimes.append(runtime)
            self._semaphore.release()
            self._record_failure()
            logger.warning("quality_extraction_fallback", reason="worker_start_failed")
            return None

        def release_when_worker_really_finishes(
            done: asyncio.Future[QualityExtraction],
        ) -> None:
            # Consume a late exception after a timeout/cancellation to avoid an
            # unhandled-future warning. Never include its message in telemetry.
            if not done.cancelled():
                done.exception()
            with self._runtime_lock:
                self._available_runtimes.append(runtime)
            self._semaphore.release()

        worker.add_done_callback(release_when_worker_really_finishes)

        try:
            result = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=max(0.0, deadline - loop.time()),
            )
        except TimeoutError:
            self._cancel_runtime_call(runtime)
            self._record_failure()
            logger.warning("quality_extraction_fallback", reason="inference_timeout")
            return None
        except asyncio.CancelledError:
            self._cancel_runtime_call(runtime)
            self._release_probe(was_probe)
            raise
        except Exception as error:
            if isinstance(error, QualitySourceInputIneligibleError):
                self._release_probe(was_probe)
                logger.info(
                    "quality_extraction_fallback",
                    reason="input_ineligible",
                )
                return None
            self._record_failure()
            reason = (
                "dependency_unavailable"
                if isinstance(error, (ImportError, ModuleNotFoundError, AttributeError))
                else "inference_failed"
            )
            logger.warning(
                "quality_extraction_fallback",
                reason=reason,
                failure_type=type(error).__name__,
            )
            return None

        self._record_success()
        return result

    async def aclose(self) -> None:
        """Drain workers and close every reusable client on its owning loop."""
        with self._runtime_lock:
            if self._closed:
                return
            self._closed = True

        # New admissions observe ``_closed``. Taking every permit waits for
        # timed-out/cancelled requests whose worker is still genuinely alive.
        # If a worker is wedged, the object remains permanently closed and no
        # permit is forged; the dedicated I/O loop is aborted below.
        drained = False
        try:
            async with asyncio.timeout(self._config.shutdown_timeout_s):
                for _ in range(self._config.max_concurrency):
                    await self._semaphore.acquire()
            drained = True
        except TimeoutError:
            logger.warning(
                "quality_extraction_shutdown",
                reason="worker_drain_timeout",
            )

        io_loop = self._io_loop
        try:
            if io_loop is not None:
                clients = [
                    runtime.async_sdk
                    for runtime in self._all_runtimes
                    if runtime.async_sdk is not None
                ]
                await io_loop.close(
                    clients,
                    cancel_pending=not drained,
                    timeout_s=self._config.shutdown_timeout_s,
                )
        finally:
            self._io_loop = None
            for runtime in self._all_runtimes:
                runtime.extractor = None
                runtime.async_sdk = None
                with runtime.call_lock:
                    runtime.active_call = None
                    runtime.cancel_requested = True


def _config_from_settings() -> QualityExtractionConfig:
    return QualityExtractionConfig(
        base_url=settings.quality_extraction_base_url,
        api_key=settings.quality_extraction_api_key,
        model=settings.quality_extraction_model,
        timeout_s=settings.quality_extraction_timeout_s,
        max_concurrency=settings.quality_extraction_max_concurrency,
        failure_threshold=settings.quality_extraction_failure_threshold,
        cooldown_s=settings.quality_extraction_cooldown_s,
        max_input_chars=settings.quality_extraction_max_input_chars,
        max_output_chars=settings.extract_max_text_length,
        prompt_profile=settings.quality_extraction_prompt_profile,
        capacity_timeout_s=settings.quality_extraction_capacity_timeout_s,
        shutdown_timeout_s=settings.quality_extraction_shutdown_timeout_s,
    )


_global_lock = threading.Lock()
_global_config: QualityExtractionConfig | None = None
_global_extractor: QualityExtractor | None = None


def _get_global_extractor(
    config: QualityExtractionConfig,
) -> tuple[QualityExtractor, QualityExtractor | None]:
    global _global_config, _global_extractor
    with _global_lock:
        if _global_extractor is not None and _global_config == config:
            return _global_extractor, None
        retired = None
        if _global_extractor is None or _global_config != config:
            retired = _global_extractor
            _global_extractor = QualityExtractor(config)
            _global_config = config
        return _global_extractor, retired


async def extract_quality_content(
    html_content: str,
    url: str = "",
) -> QualityExtraction | None:
    """Try the optional quality path, returning ``None`` for safe fallback."""
    config = _config_from_settings()
    if not config.enabled:
        return None
    extractor, retired = _get_global_extractor(config)
    if retired is not None:
        await retired.aclose()
    return await extractor.extract(html_content, url)


async def close_quality_extractor() -> None:
    """Close the process-global MinerU clients during graceful shutdown."""
    global _global_config, _global_extractor
    with _global_lock:
        extractor = _global_extractor
        _global_extractor = None
        _global_config = None
    if extractor is not None:
        await extractor.aclose()
