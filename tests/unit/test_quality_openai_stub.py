from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.quality_openai_stub import (
    _request_is_exact_smoke_contract,
    _request_smoke_profile,
)


def _request() -> dict[str, object]:
    return {
        "messages": [
            {
                "content": (
                    '只返回JSON格式数据 {"1": "main"} Input HTML: '
                    '<h1 _item_id="1">Release verification</h1>'
                    '<p _item_id="2">factual bounded text for the configured '
                    "quality adapter</p>"
                ),
                "role": "user",
            }
        ],
        "max_tokens": 8192,
        "model": "ci-quality-model",
        "temperature": 0,
    }


def _service_request() -> dict[str, object]:
    request = _request()
    messages = request["messages"]
    assert type(messages) is list
    message = messages[0]
    assert type(message) is dict
    message["content"] = (
        '只返回JSON格式数据 {"1": "main"} Input HTML: '
        '<h1 _item_id="1">Example Domain</h1>'
        '<p _item_id="2">This domain is for use in documentation examples '
        "without needing permission. Avoid use in operations.</p>"
        '<p _item_id="3"><a>Learn more</a></p>'
    )
    return request


def test_quality_stub_accepts_only_complete_expected_prompt() -> None:
    assert _request_is_exact_smoke_contract(_request()) is True
    assert _request_smoke_profile(_request()) == "openai_json"


def test_quality_stub_accepts_pinned_compact_prompt_contract() -> None:
    request = _request()
    messages = request["messages"]
    assert type(messages) is list
    message = messages[0]
    assert type(message) is dict
    message["content"] = (
        "Output Format: 1main2other3other4main Input HTML: "
        '<h1 _item_id="1">Release verification</h1>'
        '<p _item_id="2">factual bounded text for the configured quality adapter</p>'
    )

    assert _request_smoke_profile(request) == "mineru_compact"


def test_quality_stub_accepts_exact_running_service_fixture() -> None:
    assert _request_is_exact_smoke_contract(_service_request()) is True
    assert _request_smoke_profile(_service_request()) == "openai_json"


def test_quality_stub_rejects_service_fixture_with_unexpected_item_id() -> None:
    request = _service_request()
    messages = request["messages"]
    assert type(messages) is list
    message = messages[0]
    assert type(message) is dict
    content = message["content"]
    assert type(content) is str
    message["content"] = content + '<p _item_id="4">unreviewed</p>'

    assert _request_is_exact_smoke_contract(request) is False


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("model",), "other-model"),
        (("max_tokens",), 4096),
        (("temperature",), 1),
        (("messages",), []),
        (("messages", 0, "role"), "system"),
        (("messages", 0, "content"), "Input HTML: no source identifiers"),
    ],
)
def test_quality_stub_rejects_protocol_or_prompt_drift(
    path: tuple[object, ...],
    replacement: object,
) -> None:
    request = deepcopy(_request())
    target: object = request
    for component in path[:-1]:
        if type(target) is dict or (type(target) is list and type(component) is int):
            target = target[component]
        else:
            raise AssertionError("invalid test mutation path")
    final = path[-1]
    if type(target) is dict or (type(target) is list and type(final) is int):
        target[final] = replacement
    else:
        raise AssertionError("invalid test mutation target")

    assert _request_is_exact_smoke_contract(request) is False


def test_quality_stub_rejects_unreviewed_request_fields() -> None:
    request = _request()
    request["stream"] = False

    assert _request_is_exact_smoke_contract(request) is False
