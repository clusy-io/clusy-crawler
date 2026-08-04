"""CI-only bounded OpenAI-compatible stub for the quality adapter smoke."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, cast

_MAX_REQUEST_BYTES: Final = 256 * 1024
_EXPECTED_AUTHORIZATION: Final = "Bearer ci-quality-key"
_EXPECTED_MODEL: Final = "ci-quality-model"
_JSON_RESPONSE_CONTENT: Final = '{"1":"main","2":"main"}'
_COMPACT_RESPONSE_CONTENT: Final = "1main2main"
_SERVICE_JSON_RESPONSE_CONTENT: Final = '{"1":"main","2":"main","3":"main"}'
_SERVICE_COMPACT_RESPONSE_CONTENT: Final = "1main2main3main"
_EXPECTED_REQUEST_KEYS = frozenset({"messages", "model", "max_tokens", "temperature"})
_EXPECTED_MESSAGE_KEYS = frozenset({"content", "role"})
_DIRECT_SMOKE_CONTENT_MARKERS = (
    "Input HTML:",
    "Release verification",
    "factual bounded text for the configured quality adapter",
)
_SERVICE_SMOKE_CONTENT_MARKERS = (
    "Input HTML:",
    "Example Domain",
    "This domain is for use in documentation examples without needing permission.",
)


def _request_smoke_case(value: object) -> tuple[str, str] | None:
    """Return the reviewed prompt profile and fixture, or reject the request."""

    if type(value) is not dict:
        return None
    request = cast("dict[object, object]", value)
    if frozenset(request) != _EXPECTED_REQUEST_KEYS:
        return None
    if request.get("model") != _EXPECTED_MODEL:
        return None
    if type(request.get("max_tokens")) is not int or request["max_tokens"] != 8192:
        return None
    if type(request.get("temperature")) is not int or request["temperature"] != 0:
        return None
    messages = request.get("messages")
    if type(messages) is not list or len(messages) != 1:
        return None
    message = messages[0]
    if type(message) is not dict:
        return None
    message = cast("dict[object, object]", message)
    if frozenset(message) != _EXPECTED_MESSAGE_KEYS or message.get("role") != "user":
        return None
    content = message.get("content")
    if type(content) is not str or not 1 <= len(content) <= _MAX_REQUEST_BYTES:
        return None
    direct_fixture = all(marker in content for marker in _DIRECT_SMOKE_CONTENT_MARKERS)
    service_fixture = all(marker in content for marker in _SERVICE_SMOKE_CONTENT_MARKERS)
    if direct_fixture == service_fixture:
        return None
    expected_item_ids = ("1", "2") if direct_fixture else ("1", "2", "3")
    if tuple(re.findall(r'_item_id="([^"]+)"', content)) != expected_item_ids:
        return None
    json_contract = "只返回JSON格式数据" in content and '{"1": "main"' in content
    compact_contract = "Output Format:" in content and "1main2other3other4main" in content
    if json_contract == compact_contract:
        return None
    profile = "openai_json" if json_contract else "mineru_compact"
    fixture = "direct" if direct_fixture else "service"
    return profile, fixture


def _request_smoke_profile(value: object) -> str | None:
    smoke_case = _request_smoke_case(value)
    return smoke_case[0] if smoke_case is not None else None


def _request_is_exact_smoke_contract(value: object) -> bool:
    return _request_smoke_profile(value) is not None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/v1/chat/completions":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if self.headers.get("authorization") != _EXPECTED_AUTHORIZATION:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            content_length = int(self.headers.get("content-length", ""))
        except ValueError:
            content_length = -1
        if not 1 <= content_length <= _MAX_REQUEST_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "size"})
            return
        try:
            request = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "json"})
            return
        smoke_case = _request_smoke_case(request)
        if smoke_case is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "contract"})
            return
        profile, fixture = smoke_case
        if fixture == "direct":
            response_content = (
                _JSON_RESPONSE_CONTENT if profile == "openai_json" else _COMPACT_RESPONSE_CONTENT
            )
        else:
            response_content = (
                _SERVICE_JSON_RESPONSE_CONTENT
                if profile == "openai_json"
                else _SERVICE_COMPACT_RESPONSE_CONTENT
            )
        self._send_json(
            HTTPStatus.OK,
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "message": {"content": response_content, "role": "assistant"},
                    }
                ],
                "created": 0,
                "id": "clusy-quality-ci-smoke",
                "model": _EXPECTED_MODEL,
                "object": "chat.completion",
                "usage": {"completion_tokens": 8, "prompt_tokens": 64, "total_tokens": 72},
            },
        )

    def _send_json(self, status: HTTPStatus, value: dict[str, object]) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(encoded)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", 8000), _Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
