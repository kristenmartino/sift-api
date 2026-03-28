from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.models import CompareRequest, CompareResponse

router = APIRouter(prefix="/analyze", tags=["compare"])


@router.post("/compare", response_model=CompareResponse)
async def compare_sources(request: CompareRequest):
    raise HTTPException(status_code=501, detail="Comparison workflow not yet implemented")
