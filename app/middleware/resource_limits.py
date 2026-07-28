from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_UNMETERED_PATHS = frozenset({"/health", "/health/ready", "/health/version"})


class ResourceLimitMiddleware:
    """Bound request memory, active work, and total wall-clock duration.

    FastAPI normally parses the complete JSON body before request models run.
    Pre-reading it here with a hard cap prevents chunked requests from bypassing
    a Content-Length-only check. At most ``max_body_bytes`` is retained and then
    replayed once to the downstream ASGI app.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        max_active_requests: int,
        request_timeout_s: float,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_active_requests = max_active_requests
        self.request_timeout_s = request_timeout_s
        self._active = 0
        self._active_lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _UNMETERED_PATHS:
            await self.app(scope, receive, send)
            return

        response_started = False
        admitted = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            try:
                async with asyncio.timeout(self.request_timeout_s):
                    content_length = _content_length(scope)
                    if content_length is not None and content_length > self.max_body_bytes:
                        await _send_error(
                            tracking_send,
                            413,
                            "Request body too large",
                        )
                        return

                    at_capacity = False
                    async with self._active_lock:
                        if self._active >= self.max_active_requests:
                            at_capacity = True
                        else:
                            self._active += 1
                            admitted = True
                    if at_capacity:
                        await _send_error(
                            tracking_send,
                            429,
                            "Crawler is at capacity",
                        )
                        return

                    body = await _read_bounded_body(
                        receive,
                        max_body_bytes=self.max_body_bytes,
                    )
                    if body is None:
                        await _send_error(
                            tracking_send,
                            413,
                            "Request body too large",
                        )
                        return

                    replayed = False

                    async def replay_receive() -> Message:
                        nonlocal replayed
                        if not replayed:
                            replayed = True
                            return {
                                "type": "http.request",
                                "body": body,
                                "more_body": False,
                            }
                        return {"type": "http.disconnect"}

                    await self.app(scope, replay_receive, tracking_send)
            except TimeoutError:
                if not response_started:
                    await _send_error(
                        tracking_send,
                        504,
                        "Crawler request deadline exceeded",
                    )
        finally:
            if admitted:
                async with self._active_lock:
                    self._active -= 1


def _content_length(scope: Scope) -> int | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() != b"content-length":
            continue
        try:
            value = int(raw_value)
        except ValueError:
            return None
        return max(value, 0)
    return None


async def _read_bounded_body(
    receive: Receive,
    *,
    max_body_bytes: int,
) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b""
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > max_body_bytes:
            return None
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _send_error(send: Send, status_code: int, detail: str) -> None:
    body = json.dumps(
        {"detail": detail, "status": "error"},
        separators=(",", ":"),
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (
                    (b"retry-after", b"1")
                    if status_code == 429
                    else (b"x-content-type-options", b"nosniff")
                ),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
