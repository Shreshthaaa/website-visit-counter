from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ....schemas.counter import VisitCount
from ....services.visit_counter import VisitCounterService

logger = logging.getLogger(__name__)

router = APIRouter()


def get_visit_counter_service(request: Request) -> VisitCounterService:
    return request.app.state.service


@router.post("/visit/{page_id}")
async def record_visit(
    page_id: str,
    counter_service: VisitCounterService = Depends(get_visit_counter_service),
):
    """Record a visit for a page."""
    try:
        await counter_service.increment_visit(page_id)
    except Exception:
        logger.exception("failed to record visit", extra={"extra_fields": {"page_id": page_id}})
        raise HTTPException(status_code=500, detail="failed to record visit") from None

    return {"status": "success", "message": f"Visit recorded for page {page_id}"}


@router.get("/visits/{page_id}", response_model=VisitCount)
async def get_visits(
    page_id: str,
    counter_service: VisitCounterService = Depends(get_visit_counter_service),
):
    """Return the visit count for a page, and where the value came from."""
    try:
        count, served_via = await counter_service.get_visit_count(page_id)
    except Exception:
        logger.exception("failed to read visits", extra={"extra_fields": {"page_id": page_id}})
        raise HTTPException(status_code=500, detail="failed to read visit count") from None

    return VisitCount(count=count, served_via=served_via)
