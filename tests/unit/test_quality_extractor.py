from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import extractor as extractor_module
from app.services import quality_extractor as quality_module
from app.services.extractor import ExtractionResult, extract_content, extract_content_async
from app.services.quality_extractor import (
    QUALITY_STRATEGY,
    QualityExtraction,
    QualityExtractionConfig,
    QualityExtractor,
    _OfficialBindings,
)


class _FakeConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeInput:
    def __init__(self, *, raw_html: str, url: str | None) -> None:
        self.raw_html = raw_html
        self.url = url


class _FakeLLM:
    def __init__(self) -> None:
        self.async_sdk = _FakeSDK()

    async def call_api_async(self, prompt: str) -> object:
        return SimpleNamespace(generated_text=prompt)

    async def process_async(self, prompt_list: list[str]) -> list[object]:
        return [await self.call_api_async(prompt) for prompt in prompt_list]


class _FakeSDK:
    async def close(self) -> None:
        return None


def _config(**overrides: Any) -> QualityExtractionConfig:
    values: dict[str, Any] = {
        "base_url": "https://quality.invalid/v1",
        "api_key": "secret",
        "model": "mineru",
        "timeout_s": 0.3,
        "max_concurrency": 2,
        "failure_threshold": 3,
        "cooldown_s": 10.0,
        "max_input_chars": 100_000,
        "max_output_chars": 1000,
    }
    values.update(overrides)
    return QualityExtractionConfig(**values)


def _bindings(
    process: Any,
    *,
    init_capture: list[dict[str, Any]] | None = None,
) -> _OfficialBindings:
    class FakeExtractor:
        def __init__(self, **kwargs: Any) -> None:
            if init_capture is not None:
                init_capture.append(kwargs)
            self.llm = _FakeLLM()

        def process(self, input_data: _FakeInput) -> list[object]:
            return process(input_data)

    return _OfficialBindings(
        extractor_type=FakeExtractor,
        config_type=_FakeConfig,
        input_type=_FakeInput,
    )


def _success(text: str = "# Main\n\nBody") -> list[object]:
    return [
        SimpleNamespace(
            error=None,
            output_data=SimpleNamespace(main_content=text),
        )
    ]


def test_quality_runtime_configuration_rejects_unknown_prompt_contract() -> None:
    with pytest.raises(ValueError, match="prompt profile"):
        _config(prompt_profile="unknown")


@pytest.mark.asyncio
async def test_official_pipeline_success_uses_empty_fallback_and_clear_strategy() -> None:
    captured_inputs: list[_FakeInput] = []
    captured_init: list[dict[str, Any]] = []

    def process(input_data: _FakeInput) -> list[object]:
        captured_inputs.append(input_data)
        return _success("# Paper\n\nUseful content")

    extractor = QualityExtractor(
        _config(),
        bindings_loader=lambda: _bindings(process, init_capture=captured_init),
    )

    result = await extractor.extract("<article>paper</article>", "https://example.test/p")

    assert result == QualityExtraction(
        text="# Paper\n\nUseful content",
        strategy=QUALITY_STRATEGY,
    )
    assert captured_inputs[0].url == "https://example.test/p"
    mineru_config = captured_init[0]["config"]
    assert mineru_config.kwargs == {
        "use_fall_back": "empty",
        "early_load": False,
        "prompt_version": "v2",
        "response_format": "json",
        "output_format": "mm_md",
    }
    assert captured_init[0]["retry_times"] == 1
    await extractor.aclose()


@pytest.mark.asyncio
async def test_compact_profile_uses_official_0_5b_prompt_contract() -> None:
    captured_init: list[dict[str, Any]] = []
    extractor = QualityExtractor(
        _config(prompt_profile="mineru_compact"),
        bindings_loader=lambda: _bindings(
            lambda _: _success("# Compact\n\nUseful compact model content"),
            init_capture=captured_init,
        ),
    )

    result = await extractor.extract("<article>compact model content</article>")

    assert result is not None
    mineru_config = captured_init[0]["config"]
    assert mineru_config.kwargs["prompt_version"] == "short_compact"
    assert mineru_config.kwargs["response_format"] == "compact"
    await extractor.aclose()


@pytest.mark.asyncio
async def test_clients_are_bounded_reused_and_closed_on_their_io_loop() -> None:
    init_count = 0
    active = 0
    maximum_active = 0
    inference_loops: set[int] = set()
    close_loops: set[int] = set()
    clients: list[object] = []

    class ReusableSDK:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True
            close_loops.add(id(asyncio.get_running_loop()))

    class ReusableLLM:
        def __init__(self) -> None:
            self.async_sdk = ReusableSDK()
            clients.append(self.async_sdk)

        async def call_api_async(self, prompt: str) -> object:
            return SimpleNamespace(generated_text=prompt)

        async def process_async(self, prompt_list: list[str]) -> list[object]:
            nonlocal active, maximum_active
            inference_loops.add(id(asyncio.get_running_loop()))
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return [SimpleNamespace(generated_text=item) for item in prompt_list]

    class ReusableExtractor:
        def __init__(self, **_: Any) -> None:
            nonlocal init_count
            init_count += 1
            self.llm = ReusableLLM()

        def process(self, _: _FakeInput) -> list[object]:
            self.llm.generate(["prompt"])
            return _success("# Reused\n\nStable reusable client content")

    bindings = _OfficialBindings(
        extractor_type=ReusableExtractor,
        config_type=_FakeConfig,
        input_type=_FakeInput,
    )
    extractor = QualityExtractor(
        _config(max_concurrency=2, timeout_s=1),
        bindings_loader=lambda: bindings,
    )

    results = await asyncio.gather(
        *(extractor.extract(f"<p>request {index}</p>") for index in range(6))
    )

    assert all(result is not None for result in results)
    assert init_count == 2
    assert len(clients) == 2
    assert maximum_active == 2
    assert len(inference_loops) == 1
    assert all(not client.closed for client in clients)

    await extractor.aclose()

    assert all(client.closed for client in clients)
    assert close_loops == inference_loops


def test_reused_client_never_crosses_sequential_request_event_loops() -> None:
    request_loops: set[asyncio.AbstractEventLoop] = set()
    inference_loops: set[asyncio.AbstractEventLoop] = set()
    init_count = 0

    class LoopSDK:
        async def close(self) -> None:
            inference_loops.add(asyncio.get_running_loop())

    class LoopLLM:
        def __init__(self) -> None:
            self.async_sdk = LoopSDK()

        async def call_api_async(self, prompt: str) -> object:
            return SimpleNamespace(generated_text=prompt)

        async def process_async(self, prompt_list: list[str]) -> list[object]:
            inference_loops.add(asyncio.get_running_loop())
            return [SimpleNamespace(generated_text=item) for item in prompt_list]

    class LoopExtractor:
        def __init__(self, **_: Any) -> None:
            nonlocal init_count
            init_count += 1
            self.llm = LoopLLM()

        def process(self, _: _FakeInput) -> list[object]:
            self.llm.generate(["prompt"])
            return _success("# Loop safe\n\nReusable client content stays grounded")

    extractor = QualityExtractor(
        _config(max_concurrency=1),
        bindings_loader=lambda: _OfficialBindings(
            extractor_type=LoopExtractor,
            config_type=_FakeConfig,
            input_type=_FakeInput,
        ),
    )

    async def request() -> QualityExtraction | None:
        request_loops.add(asyncio.get_running_loop())
        return await extractor.extract("<p>grounded request content</p>")

    assert asyncio.run(request()) is not None
    assert asyncio.run(request()) is not None
    asyncio.run(extractor.aclose())

    assert init_count == 1
    assert len(request_loops) == 2
    assert len(inference_loops) == 1
    assert inference_loops.isdisjoint(request_loops)


@pytest.mark.asyncio
async def test_missing_configuration_falls_back_without_loading_dependency() -> None:
    loader_called = False

    def loader() -> _OfficialBindings:
        nonlocal loader_called
        loader_called = True
        raise AssertionError("loader must not run")

    extractor = QualityExtractor(
        _config(api_key=""),
        bindings_loader=loader,
    )

    assert await extractor.extract("<p>body</p>") is None
    assert loader_called is False


@pytest.mark.asyncio
async def test_oversized_input_falls_back_without_loading_dependency() -> None:
    loader_called = False

    def loader() -> _OfficialBindings:
        nonlocal loader_called
        loader_called = True
        raise AssertionError("loader must not run")

    extractor = QualityExtractor(
        _config(max_input_chars=16),
        bindings_loader=loader,
    )

    assert await extractor.extract("<article>oversized input</article>") is None
    assert loader_called is False


@pytest.mark.asyncio
async def test_missing_dependency_is_cached_and_falls_back_immediately() -> None:
    load_count = 0

    def loader() -> _OfficialBindings:
        nonlocal load_count
        load_count += 1
        raise ModuleNotFoundError("mineru_html")

    extractor = QualityExtractor(_config(), bindings_loader=loader)

    assert await extractor.extract("<p>body</p>") is None
    assert await extractor.extract("<p>body</p>") is None
    assert load_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["", "x" * 1001])
async def test_empty_or_oversized_output_is_rejected(output: str) -> None:
    extractor = QualityExtractor(
        _config(max_output_chars=1000),
        bindings_loader=lambda: _bindings(lambda _: _success(output)),
    )

    assert await extractor.extract("<p>body</p>") is None
    await extractor.aclose()


@pytest.mark.asyncio
async def test_timeout_falls_back_and_opens_circuit_at_threshold() -> None:
    calls = 0

    def process(_: _FakeInput) -> list[object]:
        nonlocal calls
        calls += 1
        time.sleep(0.08)
        return _success()

    extractor = QualityExtractor(
        _config(timeout_s=0.01, max_concurrency=2, failure_threshold=2),
        bindings_loader=lambda: _bindings(process),
    )

    assert await extractor.extract("<p>one</p>") is None
    assert await extractor.extract("<p>two</p>") is None
    # Both timed-out workers remain live, but the open circuit makes the third
    # request fall back without queueing or starting another worker.
    assert await extractor.extract("<p>three</p>") is None
    assert calls == 2
    await asyncio.sleep(0.1)
    await extractor.aclose()


@pytest.mark.asyncio
async def test_half_open_probe_recovers_after_cooldown() -> None:
    now = 10.0
    fail = True
    calls = 0

    def clock() -> float:
        return now

    def process(_: _FakeInput) -> list[object]:
        nonlocal calls
        calls += 1
        if fail:
            raise RuntimeError("remote down")
        return _success("# Recovered")

    extractor = QualityExtractor(
        _config(failure_threshold=1, cooldown_s=5),
        bindings_loader=lambda: _bindings(process),
        clock=clock,
    )

    assert await extractor.extract("<p>first</p>") is None
    assert await extractor.extract("<p>blocked</p>") is None
    assert calls == 1

    now = 16.0
    fail = False
    recovered = await extractor.extract("<p>probe</p>")
    assert recovered is not None
    assert recovered.text == "# Recovered"
    assert calls == 2
    await extractor.aclose()


@pytest.mark.asyncio
async def test_worker_concurrency_is_bounded() -> None:
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    def process(_: _FakeInput) -> list[object]:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return _success()

    extractor = QualityExtractor(
        _config(max_concurrency=2, timeout_s=1),
        bindings_loader=lambda: _bindings(process),
    )

    results = await asyncio.gather(
        *(extractor.extract(f"<p>{index}</p>") for index in range(6))
    )

    assert all(result is not None for result in results)
    assert maximum_active == 2
    await extractor.aclose()


@pytest.mark.asyncio
async def test_cancelling_waiter_does_not_release_live_worker_permit() -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def process(_: _FakeInput) -> list[object]:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=1)
        return _success()

    extractor = QualityExtractor(
        _config(max_concurrency=1, timeout_s=0.03, cooldown_s=0.01),
        bindings_loader=lambda: _bindings(process),
    )

    first = asyncio.create_task(extractor.extract("<p>first</p>"))
    assert await asyncio.to_thread(started.wait, 0.5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    # The worker outlives its cancelled waiter, so capacity remains occupied.
    assert await extractor.extract("<p>second</p>") is None
    assert calls == 1

    release.set()
    await asyncio.sleep(0.03)
    assert await extractor.extract("<p>third</p>") is not None
    assert calls == 2
    await extractor.aclose()


@pytest.mark.asyncio
async def test_cancelled_waiter_and_blocked_worker_open_fast_capacity_circuit() -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def process(_: _FakeInput) -> list[object]:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=1)
        return _success()

    extractor = QualityExtractor(
        _config(
            max_concurrency=1,
            timeout_s=0.2,
            capacity_timeout_s=0.01,
            cooldown_s=1,
        ),
        bindings_loader=lambda: _bindings(process),
    )

    abandoned = asyncio.create_task(extractor.extract("<p>blocked</p>"))
    assert await asyncio.to_thread(started.wait, 0.5)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned

    assert await extractor.extract("<p>capacity probe</p>") is None
    started_at = time.monotonic()
    assert await extractor.extract("<p>circuit fallback</p>") is None
    assert time.monotonic() - started_at < 0.05
    assert calls == 1

    release.set()
    await asyncio.sleep(0.03)
    await extractor.aclose()


@pytest.mark.asyncio
async def test_shutdown_abandons_wedged_sync_worker_without_forging_capacity() -> None:
    started = threading.Event()
    release = threading.Event()

    def process(_: _FakeInput) -> list[object]:
        started.set()
        release.wait(timeout=1)
        return _success()

    extractor = QualityExtractor(
        _config(
            max_concurrency=1,
            timeout_s=0.2,
            shutdown_timeout_s=0.02,
        ),
        bindings_loader=lambda: _bindings(process),
    )
    request = asyncio.create_task(extractor.extract("<p>wedged</p>"))
    assert await asyncio.to_thread(started.wait, 0.5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    started_at = time.monotonic()
    await extractor.aclose()
    assert time.monotonic() - started_at < 0.2
    assert not release.is_set()
    assert await extractor.extract("<p>must stay closed</p>") is None

    # Let the abandoned test worker unwind; its eventual callback may return
    # the original permit, but the permanently closed extractor cannot admit it.
    release.set()
    await asyncio.sleep(0.03)


@pytest.mark.asyncio
async def test_cancelled_request_stops_remote_model_work_before_releasing_slot() -> None:
    inference_started = threading.Event()
    inference_cancelled = threading.Event()

    class CancellableSDK:
        async def close(self) -> None:
            return None

    class CancellableLLM:
        def __init__(self) -> None:
            self.async_sdk = CancellableSDK()

        async def call_api_async(self, prompt: str) -> object:
            return SimpleNamespace(generated_text=prompt)

        async def process_async(self, _: list[str]) -> list[object]:
            inference_started.set()
            try:
                await asyncio.Future()
            finally:
                inference_cancelled.set()

    class CancellableExtractor:
        def __init__(self, **_: Any) -> None:
            self.llm = CancellableLLM()

        def process(self, _: _FakeInput) -> list[object]:
            self.llm.generate(["prompt"])
            return _success()

    extractor = QualityExtractor(
        _config(max_concurrency=1, timeout_s=1),
        bindings_loader=lambda: _OfficialBindings(
            extractor_type=CancellableExtractor,
            config_type=_FakeConfig,
            input_type=_FakeInput,
        ),
    )
    request = asyncio.create_task(extractor.extract("<p>cancel model</p>"))
    assert await asyncio.to_thread(inference_started.wait, 0.5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert await asyncio.to_thread(inference_cancelled.wait, 0.5)
    await extractor.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancels_wedged_model_task_on_owning_io_loop() -> None:
    inference_started = threading.Event()
    inference_cancelled = threading.Event()
    client_closed = threading.Event()

    class WedgedSDK:
        async def close(self) -> None:
            client_closed.set()

    class WedgedLLM:
        def __init__(self) -> None:
            self.async_sdk = WedgedSDK()

        async def call_api_async(self, prompt: str) -> object:
            return SimpleNamespace(generated_text=prompt)

        async def process_async(self, _: list[str]) -> list[object]:
            inference_started.set()
            try:
                await asyncio.Future()
            finally:
                inference_cancelled.set()

    class WedgedExtractor:
        def __init__(self, **_: Any) -> None:
            self.llm = WedgedLLM()

        def process(self, _: _FakeInput) -> list[object]:
            self.llm.generate(["prompt"])
            return _success()

    extractor = QualityExtractor(
        _config(
            max_concurrency=1,
            timeout_s=1,
            shutdown_timeout_s=0.03,
        ),
        bindings_loader=lambda: _OfficialBindings(
            extractor_type=WedgedExtractor,
            config_type=_FakeConfig,
            input_type=_FakeInput,
        ),
    )
    request = asyncio.create_task(extractor.extract("<p>wedged model</p>"))
    assert await asyncio.to_thread(inference_started.wait, 0.5)

    await extractor.aclose()

    with pytest.raises(asyncio.CancelledError):
        await request
    assert inference_cancelled.is_set()
    assert client_closed.is_set()
    assert await extractor.extract("<p>must stay closed</p>") is None


@pytest.mark.asyncio
async def test_async_integration_returns_quality_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def quality_result(_: str, __: str) -> QualityExtraction:
        return QualityExtraction(
            text=(
                "# Neural\n\nSelected body with grounded words for the useful "
                "page content."
            )
        )

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    result = await extract_content_async(
        (
            "<html lang='en'><head><title>Neural</title>"
            "<meta name='description' content='Useful description'></head>"
            "<body><nav>noise</nav><main>Selected body with grounded words for "
            "the useful page content.</main></body></html>"
        ),
        "https://example.test",
        extraction_profile="quality",
    )

    assert result.text.startswith("# Neural")
    assert result.strategy == QUALITY_STRATEGY
    assert result.title == "Neural"
    assert result.description == "Useful description"
    assert result.language == "en"


@pytest.mark.asyncio
async def test_quality_verifier_accepts_grounded_cjk_without_space_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "# 架构\n\n复用连接能够降低重复抓取请求的延迟并保持正文结构完整。"

    async def quality_result(_: str, __: str) -> QualityExtraction:
        return QualityExtraction(text=text)

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    result = await extract_content_async(
        (
            "<html lang='zh'><head><title>架构</title></head><body><main>"
            "复用连接能够降低重复抓取请求的延迟并保持正文结构完整。"
            "</main></body></html>"
        ),
        "https://example.test/zh",
        extraction_profile="quality",
    )

    assert result.text == text
    assert result.strategy == QUALITY_STRATEGY
    assert result.language == "zh"


@pytest.mark.asyncio
async def test_async_quality_failure_and_sync_quality_use_balanced_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_: str, __: str) -> None:
        return None

    monkeypatch.setattr(quality_module, "extract_quality_content", unavailable)
    html = (
        "<html><head><title>Fallback</title></head><body><main><p>"
        "This deterministic paragraph contains enough words for safe fallback output."
        "</p></main></body></html>"
    )

    async_result = await extract_content_async(
        html,
        "https://example.test",
        extraction_profile="quality",
    )
    sync_result = extract_content(
        html,
        "https://example.test",
        extraction_profile="quality",
    )

    assert async_result.text
    assert sync_result.text
    assert async_result.strategy != QUALITY_STRATEGY
    assert sync_result.strategy != QUALITY_STRATEGY


def _adaptive_candidate(
    *,
    page_type: str = "article",
    confidence: float = 0.95,
) -> ExtractionResult:
    text = (
        "Deterministic content remains the stable fallback for every adaptive "
        "extraction request."
    )
    return ExtractionResult(
        text=text,
        word_count=len(text.split()),
        strategy="rs-trafilatura",
        confidence=confidence,
        page_type=page_type,
    )


@pytest.mark.asyncio
async def test_adaptive_high_confidence_page_stays_on_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: _adaptive_candidate(),
    )

    async def unexpected_quality_call(*_args: object) -> None:
        raise AssertionError("high-confidence simple page must not use model assistance")

    monkeypatch.setattr(quality_module, "extract_quality_content", unexpected_quality_call)

    result = await extract_content_async(
        "<html><body><article><p>Simple prose content.</p></article></body></html>",
        "https://example.test/simple",
        extraction_profile="adaptive",
    )

    assert result.strategy == "rs-trafilatura"
    assert result.text.startswith("Deterministic content")


@pytest.mark.asyncio
async def test_adaptive_structural_risk_escalates_after_deterministic_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def deterministic_candidate(*_args: object) -> ExtractionResult:
        events.append("deterministic")
        return _adaptive_candidate(page_type="webpage")

    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        deterministic_candidate,
    )
    calls = 0

    async def quality_result(_: str, __: str) -> QualityExtraction:
        nonlocal calls
        assert events == ["deterministic"]
        events.append("quality")
        calls += 1
        return QualityExtraction(
            text=(
                "# Architecture\n\nReusable connections reduce latency for "
                "repeated extraction requests while preserving ordered "
                "structural content."
            )
        )

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    html = """
    <html><body><main>
      <table><tr><th colspan="2">Architecture</th></tr>
      <tr><td>Reusable connections reduce latency for repeated extraction
      requests while preserving ordered structural content.</td></tr></table>
      <table><tr><th>Lifecycle</th></tr>
      <tr><td>Clients close during graceful service shutdown.</td></tr></table>
    </main></body></html>
    """

    result = await extract_content_async(
        html,
        "https://example.test/structured",
        extraction_profile="adaptive",
    )

    assert calls == 1
    assert events == ["deterministic", "quality"]
    assert result.strategy == QUALITY_STRATEGY
    assert result.text.startswith("# Architecture")


@pytest.mark.asyncio
async def test_adaptive_quality_failure_preserves_exact_deterministic_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _adaptive_candidate(page_type="product")
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: candidate,
    )

    async def failed_quality_call(*_args: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(quality_module, "extract_quality_content", failed_quality_call)

    result = await extract_content_async(
        "<html><body><main><p>Product information.</p></main></body></html>",
        "https://example.test/product",
        extraction_profile="adaptive",
    )
    sync_adaptive = extract_content(
        "<html><body><main><p>Product information.</p></main></body></html>",
        "https://example.test/product",
        extraction_profile="adaptive",
    )

    assert result.text == candidate.text
    assert result.strategy == candidate.strategy
    assert sync_adaptive.text == candidate.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assisted_text",
    [
        "Too short",
        (
            "Completely invented output mentions galaxies orchestras volcanoes "
            "satellites kingdoms and unrelated hallucinations."
        ),
        (
            "Grounded reusable connection content remains stable.\n\n"
            "Grounded reusable connection content remains stable.\n\n"
            "Grounded reusable connection content remains stable."
        ),
        (
            "```python\nGrounded reusable connection content remains stable "
            "for every extraction request."
        ),
    ],
)
async def test_adaptive_verifier_rejects_unsafe_assisted_output_without_metadata_loss(
    monkeypatch: pytest.MonkeyPatch,
    assisted_text: str,
) -> None:
    candidate = _adaptive_candidate(page_type="product", confidence=0.95)
    candidate.title = "Stable title"
    candidate.description = "Stable description"
    candidate.language = "en"
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: candidate,
    )

    async def quality_result(*_args: object) -> QualityExtraction:
        return QualityExtraction(text=assisted_text)

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    html = """
    <html><body><main>
      <h1>Stable title</h1>
      <p>Grounded reusable connection content remains stable for every
      extraction request and preserves useful product details.</p>
      <table><tr><th colspan="2">Product details</th></tr>
      <tr><td>Fast</td><td>Safe</td></tr></table>
      <table><tr><th>Lifecycle</th></tr><tr><td>Bounded</td></tr></table>
    </main></body></html>
    """

    result = await extract_content_async(
        html,
        "https://example.test/product",
        extraction_profile="adaptive",
    )

    assert result.text == candidate.text
    assert result.title == "Stable title"
    assert result.description == "Stable description"
    assert result.language == "en"
    assert result.strategy == candidate.strategy


@pytest.mark.asyncio
async def test_adaptive_verifier_rejects_grounded_fragment_of_trusted_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    article_words = [f"articleword{index}" for index in range(1000)]
    candidate = ExtractionResult(
        text=" ".join(article_words),
        title="Complete article",
        description="Complete body",
        language="en",
        word_count=1000,
        strategy="rs-trafilatura",
        confidence=0.95,
        page_type="article",
    )
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: candidate,
    )

    fragment = " ".join(article_words[:24])

    async def quality_result(*_args: object) -> QualityExtraction:
        return QualityExtraction(text=fragment)

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    html = (
        "<html><body><article>"
        + " ".join(article_words)
        + "</article><table><tr><th colspan='2'>Structure</th></tr></table>"
        + "<table><tr><th>More structure</th></tr></table></body></html>"
    )

    result = await extract_content_async(
        html,
        "https://example.test/article",
        extraction_profile="adaptive",
    )

    assert result.text == candidate.text
    assert result.title == "Complete article"
    assert result.strategy == candidate.strategy


@pytest.mark.asyncio
async def test_quality_profile_requires_complete_deterministic_comparator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    article_words = [f"qualityword{index}" for index in range(600)]
    candidate = ExtractionResult(
        text=" ".join(article_words),
        title="Complete quality article",
        word_count=600,
        strategy="rs-trafilatura",
        confidence=0.95,
        page_type="article",
    )
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: candidate,
    )

    async def quality_result(*_args: object) -> QualityExtraction:
        return QualityExtraction(text=" ".join(article_words[:24]))

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    html = "<html><body><article>" + " ".join(article_words) + "</article></body></html>"

    result = await extract_content_async(
        html,
        "https://example.test/quality-article",
        extraction_profile="quality",
    )

    assert result.text == candidate.text
    assert result.strategy == candidate.strategy


@pytest.mark.asyncio
async def test_adaptive_verifier_allows_short_grounded_cleanup_for_noisy_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = ExtractionResult(
        text=" ".join(["catalog"] * 500),
        title="Product",
        word_count=500,
        strategy="rs-trafilatura",
        confidence=0.5,
        page_type="product",
    )
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: candidate,
    )
    useful_words = [f"detail{index}" for index in range(24)]
    assisted = " ".join(useful_words)

    async def quality_result(*_args: object) -> QualityExtraction:
        return QualityExtraction(text=assisted)

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    html = (
        "<html><body><main><h1>Product</h1><p>"
        + assisted
        + "</p><table><tr><th colspan='2'>Details</th></tr></table>"
        + "<table><tr><th>More</th></tr></table></main></body></html>"
    )

    result = await extract_content_async(
        html,
        "https://example.test/product",
        extraction_profile="adaptive",
    )

    assert result.text == assisted
    assert result.strategy == QUALITY_STRATEGY
    assert result.title == "Product"
