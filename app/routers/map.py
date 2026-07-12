from __future__ import annotations

import structlog
from fastapi import APIRouter

from app.models.requests import MapRequest
from app.models.responses import MapResponse
from app.services.site_map import map_site

logger = structlog.get_logger()

router = APIRouter(tags=["map"])


@router.post("/map")
async def map_endpoint(req: MapRequest) -> MapResponse:
    links = await map_site(req.url, limit=req.limit, search=req.search)
    return MapResponse(status="ok", url=req.url, links=links, count=len(links))
