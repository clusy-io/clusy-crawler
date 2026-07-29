from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
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


def _configure_quality_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the integration lane without exposing or contacting a real backend."""
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_base_url",
        "https://quality.example.test/v1",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_api_key",
        "configured-test-key",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_model",
        "test-model",
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
    _configure_quality_backend(monkeypatch)

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
async def test_quality_output_is_capped_before_completeness_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_quality_backend(monkeypatch)
    monkeypatch.setattr(
        extractor_module.settings,
        "extract_max_text_length",
        160,
    )
    body = " ".join(f"groundedword{index}" for index in range(80))

    async def quality_result(_: str, __: str) -> QualityExtraction:
        return QualityExtraction(text=body)

    monkeypatch.setattr(quality_module, "extract_quality_content", quality_result)
    result = await extract_content_async(
        f"<html><body><main><p>{body}</p></main></body></html>",
        "https://example.test/quality-truncation",
        extraction_profile="quality",
    )

    assert result.strategy == QUALITY_STRATEGY
    assert result.truncated is True
    assert result.truncation_reason == "configured text limit"
    assert len(result.text) <= 160
    assert "content truncated at configured limit" in result.text
    assert "output_truncated" in result.completeness_reasons
    assert result.completeness_score <= 0.65
    assert result.word_count == extractor_module._count_words(result.text)


@pytest.mark.asyncio
async def test_quality_verifier_accepts_grounded_cjk_without_space_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_quality_backend(monkeypatch)

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
async def test_disabled_quality_profile_is_balanced_semantics_without_backend_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_backend_call(_: str, __: str) -> None:
        raise AssertionError("an unconfigured quality backend must not be called")

    monkeypatch.setattr(
        quality_module,
        "extract_quality_content",
        unexpected_backend_call,
    )
    monkeypatch.setattr(extractor_module.settings, "quality_extraction_base_url", "")
    monkeypatch.setattr(extractor_module.settings, "quality_extraction_api_key", "")
    monkeypatch.setattr(extractor_module.settings, "quality_extraction_model", "")
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
    balanced_result = await extract_content_async(
        html,
        "https://example.test",
        extraction_profile="balanced",
    )

    semantic_fields = (
        "text",
        "title",
        "description",
        "language",
        "word_count",
        "strategy",
        "confidence",
        "page_type",
        "truncated",
        "truncation_reason",
        "route",
        "candidate_count",
        "candidate_disagreement",
        "completeness_score",
        "completeness_coverage",
        "completeness_reasons",
    )
    assert async_result.text
    assert async_result.strategy != QUALITY_STRATEGY
    assert tuple(getattr(async_result, field) for field in semantic_fields) == tuple(
        getattr(balanced_result, field) for field in semantic_fields
    )
    assert sync_result.text == balanced_result.text
    assert sync_result.strategy == balanced_result.strategy
    assert async_result.quality_attempted is False
    assert async_result.route_reasons == ("quality_backend_disabled",)


@pytest.mark.asyncio
async def test_failed_quality_profile_preserves_balanced_extraction_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_base_url",
        "https://quality.example.test/v1",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_api_key",
        "configured-test-key",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_model",
        "test-model",
    )

    async def failed_quality_call(*_args: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(
        quality_module,
        "extract_quality_content",
        failed_quality_call,
    )
    html = (
        "<html><head><title>Fallback</title></head><body><main><p>"
        "This deterministic paragraph contains enough words for safe fallback "
        "output and a stable comparison against the balanced runtime profile."
        "</p></main></body></html>"
    )

    quality_result = await extract_content_async(
        html,
        "https://example.test/failure",
        extraction_profile="quality",
    )
    balanced_result = await extract_content_async(
        html,
        "https://example.test/failure",
        extraction_profile="balanced",
    )

    assert quality_result.text == balanced_result.text
    assert quality_result.strategy == balanced_result.strategy
    assert quality_result.word_count == balanced_result.word_count
    assert quality_result.route == balanced_result.route
    assert quality_result.completeness_score == balanced_result.completeness_score
    assert (
        quality_result.completeness_coverage
        == balanced_result.completeness_coverage
    )
    assert quality_result.quality_attempted is True
    assert quality_result.quality_succeeded is False
    assert quality_result.route_reasons == ("quality_backend_fallback",)


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
    _configure_quality_backend(monkeypatch)
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
    assert result.route_reasons == ("adaptive_fast_path",)
    assert result.quality_attempted is False
    assert result.model_assisted is False
    assert result.candidate_count == 2
    assert result.candidate_disagreement == 0.0
    assert result.completeness_coverage == "source_full"


@pytest.mark.asyncio
async def test_unconfigured_adaptive_native_hit_skips_upgrade_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "quality_extraction_base_url", "")
    monkeypatch.setattr(settings, "quality_extraction_api_key", "")
    monkeypatch.setattr(settings, "quality_extraction_model", "")
    profiles: list[str] = []

    def candidate() -> ExtractionResult:
        text = " ".join(f"nativeword{index:04d}" for index in range(400))
        return ExtractionResult(
            text=text,
            title="Stable native title",
            description="Stable native description",
            language="en",
            word_count=400,
            strategy="rs-trafilatura",
            confidence=0.95,
            page_type="product",
        )

    def native(_: str, __: str, profile: str) -> ExtractionResult:
        profiles.append(profile)
        return candidate()

    def forbidden_upgrade_work(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled adaptive path must skip upgrade-only work")

    async def forbidden_quality(*_args: object) -> None:
        raise AssertionError("disabled adaptive path must not call quality")

    monkeypatch.setattr(extractor_module, "_extract_with_native", native)
    monkeypatch.setattr(
        extractor_module,
        "_adaptive_risk_decision",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_candidate_disagreement",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_structural_loss_score",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_bounded_grounding_coverage",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_python_cascade",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_parallel_extract",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        quality_module,
        "extract_quality_content",
        forbidden_quality,
    )
    html = (
        "<html><body><main><p>Product information remains source-backed.</p></main></body></html>"
    )

    result = await extract_content_async(
        html,
        "https://example.test/product",
        extraction_profile="adaptive",
    )

    assert profiles == ["balanced"]
    assert result.text == candidate().text
    assert result.strategy == "rs-trafilatura"
    assert result.route_reasons == ("adaptive_quality_backend_disabled_fast_path",)
    assert result.quality_attempted is False
    assert result.quality_succeeded is False
    assert result.model_assisted is False
    assert result.completeness_score == 0.0
    assert result.completeness_coverage == "output_only"


@pytest.mark.asyncio
async def test_unconfigured_adaptive_low_confidence_article_preserves_async_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "quality_extraction_base_url", "")
    monkeypatch.setattr(settings, "quality_extraction_api_key", "")
    monkeypatch.setattr(settings, "quality_extraction_model", "")
    monkeypatch.setattr(
        settings,
        "parallel_extraction_enabled",
        False,
    )
    profiles: list[str] = []
    fallback_calls = 0
    native_text = " ".join(f"nativearticle{index:04d}" for index in range(400))
    fallback_text = " ".join(f"fallbackbody{index:04d}" for index in range(450))

    def native(_: str, __: str, profile: str) -> ExtractionResult:
        profiles.append(profile)
        return ExtractionResult(
            text=native_text,
            word_count=400,
            strategy="rs-trafilatura",
            confidence=0.2,
            page_type="article",
        )

    def fallback(_: str, __: str, page_type: str) -> ExtractionResult:
        nonlocal fallback_calls
        fallback_calls += 1
        return ExtractionResult(
            text=fallback_text,
            word_count=450,
            strategy="trafilatura",
            confidence=0.0,
            page_type=page_type,
        )

    def forbidden_upgrade_work(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled adaptive path must skip upgrade-only work")

    async def forbidden_quality(*_args: object) -> None:
        raise AssertionError("disabled adaptive path must not call quality")

    monkeypatch.setattr(extractor_module, "_extract_with_native", native)
    monkeypatch.setattr(extractor_module, "_python_cascade", fallback)
    monkeypatch.setattr(
        extractor_module,
        "_adaptive_risk_decision",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_candidate_disagreement",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_structural_loss_score",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_bounded_grounding_coverage",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_parallel_extract",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        quality_module,
        "extract_quality_content",
        forbidden_quality,
    )

    result = await extract_content_async(
        "<html><body><article>Low-confidence source.</article></body></html>",
        "https://example.test/low-confidence-article",
        extraction_profile="adaptive",
    )

    assert profiles == ["balanced"]
    assert fallback_calls == 1
    assert result.text == fallback_text
    assert result.strategy == "trafilatura"
    assert result.route == "deterministic_fallback"
    assert result.route_reasons == ("adaptive_quality_backend_disabled_fast_path",)
    assert result.candidate_count == 1
    assert result.candidate_disagreement == 0.0
    assert result.completeness_score == 0.0
    assert result.completeness_coverage == "output_only"
    assert result.quality_attempted is False
    assert result.model_assisted is False


@pytest.mark.asyncio
async def test_adaptive_structural_risk_escalates_after_deterministic_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_quality_backend(monkeypatch)

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
        assert events == ["deterministic", "deterministic"]
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
    assert events == ["deterministic", "deterministic", "quality"]
    assert result.strategy == QUALITY_STRATEGY
    assert result.text.startswith("# Architecture")
    assert result.model_assisted is True
    assert result.quality_attempted is True
    assert result.quality_succeeded is True
    assert "structure_loss" in result.route_reasons


@pytest.mark.asyncio
async def test_adaptive_quality_failure_preserves_exact_deterministic_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _adaptive_candidate(page_type="product")
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_base_url",
        "https://quality.example.test/v1",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_api_key",
        "configured-test-key",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_model",
        "test-model",
    )
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
    assert result.quality_attempted is True
    assert result.quality_succeeded is False
    assert result.model_assisted is False
    assert result.route_reasons[-1] == "quality_backend_fallback"


@pytest.mark.asyncio
async def test_unconfigured_adaptive_fallback_is_stable_and_not_marked_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _adaptive_candidate(page_type="product")
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: candidate,
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_base_url",
        "",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_api_key",
        "",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "quality_extraction_model",
        "",
    )

    async def disabled_quality_call(*_args: object) -> None:
        return None

    monkeypatch.setattr(
        quality_module,
        "extract_quality_content",
        disabled_quality_call,
    )

    result = await extract_content_async(
        "<html><body><main><p>Product information.</p></main></body></html>",
        "https://example.test/product",
        extraction_profile="adaptive",
    )

    assert result.text == candidate.text
    assert result.quality_attempted is False
    assert result.quality_succeeded is False
    assert result.route_reasons[-1] == "adaptive_quality_backend_disabled_fast_path"


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
    _configure_quality_backend(monkeypatch)

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
    _configure_quality_backend(monkeypatch)

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
    _configure_quality_backend(monkeypatch)

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
    _configure_quality_backend(monkeypatch)

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


def test_quality_verifier_rejects_grounded_paragraph_reordering() -> None:
    first = " ".join(f"firstword{index}" for index in range(24))
    second = " ".join(f"secondword{index}" for index in range(24))
    html = f"<html><body><main><p>{first}</p><p>{second}</p></main></body></html>"

    rejection = extractor_module._quality_rejection_reason(
        f"{second}\n\n{first}",
        html,
        None,
    )

    assert rejection == "source_order_violation"


def test_adaptive_router_escalates_a_single_lost_table_category() -> None:
    candidate = _adaptive_candidate(page_type="webpage", confidence=0.99)
    html = """
    <html><body><main>
      <h1>Product details</h1>
      <table><tr><th>Name</th><th>Value</th></tr>
      <tr><td>Latency</td><td>Low</td></tr></table>
      <p>Deterministic content remains the stable fallback for every adaptive
      extraction request.</p>
    </main></body></html>
    """

    decision = extractor_module._adaptive_risk_decision(candidate, html)

    assert decision.risky is True
    assert decision.structural_loss_score >= 1
    assert "structure_loss" in decision.reasons
    assert "tables_missing" in decision.reasons
