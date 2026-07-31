"""Shared contract for recursive document-navigation policy checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class DocumentPolicyBlockReason(StrEnum):
    """Why a recursive document navigation was denied."""

    ROBOTS_DISALLOWED = "robots_disallowed"
    OFF_SITE = "off_site"


@dataclass(frozen=True, slots=True)
class DocumentPolicyDecision:
    """Bounded decision returned before a document request is issued."""

    allowed: bool
    reason: DocumentPolicyBlockReason | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if self.allowed and self.reason is not None:
            raise ValueError("an allowed decision cannot have a block reason")
        if not self.allowed and self.reason is None:
            raise ValueError("a denied decision requires a block reason")
        if len(self.error) > 500:
            raise ValueError("document policy error exceeds 500 characters")


type DocumentPolicyCallback = Callable[
    [str],
    Awaitable[DocumentPolicyDecision],
]


class DocumentPolicyDeniedError(RuntimeError):
    """A document target was rejected before its request was issued."""

    def __init__(self, decision: DocumentPolicyDecision) -> None:
        if decision.allowed or decision.reason is None:
            raise ValueError("DocumentPolicyDeniedError requires a denied decision")
        self.decision = decision
        super().__init__(decision.error or "recursive document navigation denied by policy")


async def enforce_document_policy(
    callback: DocumentPolicyCallback | None,
    url: str,
) -> None:
    """Raise a bounded denial when an optional recursive policy rejects URL."""

    if callback is None:
        return
    try:
        decision = await callback(url)
    except BaseException as exc:
        # Cancellation and process-control exceptions must retain their normal
        # semantics; ordinary callback failures fail closed.
        if not isinstance(exc, Exception):
            raise
        decision = DocumentPolicyDecision(
            allowed=False,
            reason=DocumentPolicyBlockReason.ROBOTS_DISALLOWED,
            error="document policy check failed; recursive crawling is denied by policy",
        )
    if not decision.allowed:
        raise DocumentPolicyDeniedError(decision)
