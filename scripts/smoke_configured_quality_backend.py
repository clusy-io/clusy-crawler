"""Release smoke for the configured quality adapter and authenticated output."""

from __future__ import annotations

import asyncio
import json

from app.services.quality_extractor import (
    close_quality_extractor,
    extract_quality_content,
)
from app.services.source_serialization_receipt_v1 import (
    SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA,
    QualitySourceSerializationReceiptV1,
    verify_quality_source_serialization_receipt_v1,
)


async def _run() -> dict[str, object]:
    body = " ".join(
        [
            "Release verification article body contains factual bounded text "
            "for the configured quality adapter."
        ]
        * 12
    )
    raw_html = (
        "<html><head><title>Release verification</title></head><body>"
        "<article><h1>Release verification</h1>"
        f"<p>{body}</p></article></body></html>"
    )
    source_url = "https://release-smoke.invalid/article"
    try:
        result = await extract_quality_content(raw_html, source_url)
        if result is None:
            raise RuntimeError("configured quality adapter returned fallback")
        receipt = result.selection_receipt
        if type(receipt) is not QualitySourceSerializationReceiptV1:
            raise RuntimeError("configured quality adapter returned an unauthenticated receipt")
        if receipt.schema_version != SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA:
            raise RuntimeError("configured quality adapter returned the wrong receipt schema")
        if not verify_quality_source_serialization_receipt_v1(
            receipt,
            raw_html=raw_html,
            source_url=source_url,
            output_text=result.text,
        ):
            raise RuntimeError("configured quality adapter receipt did not verify")
        if not all(value in result.text for value in ("Release verification", "factual bounded")):
            raise RuntimeError("configured quality adapter lost source-backed smoke content")
        return {
            "item_count": receipt.item_count,
            "output_chars": len(result.text),
            "receipt_sha256": receipt.receipt_sha256,
            "schema": receipt.schema_version,
            "selected_count": receipt.selected_count,
        }
    finally:
        await close_quality_extractor()


def main() -> int:
    print(json.dumps(asyncio.run(_run()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
