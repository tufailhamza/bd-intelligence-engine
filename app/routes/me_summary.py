import logging
import traceback

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.me_summary import MeSummaryInput, MeSummaryOutput
from app.routes.dependancies import get_es_client_with_compression
from app.utils.me_summary.summary_service import (
    fetch_profile_by_public_identifier,
    generate_ai_summary_and_specializations,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/me_summary", response_model=MeSummaryOutput)
async def me_summary(
    request: MeSummaryInput,
    es_client_compressed=Depends(get_es_client_with_compression),
):
    """
    Generate AI summary and specialization areas for one profile.
    Receives public_identifier, fetches profile from ES, and generates response via qutils LLM.
    """
    try:
        public_identifier = (request.public_identifier or "").strip()
        if not public_identifier:
            return JSONResponse(
                content={"error": "public_identifier is required"},
                status_code=400,
            )

        es_id, profile = await fetch_profile_by_public_identifier(
            public_identifier=public_identifier,
            es_client=es_client_compressed,
        )
        if profile is None:
            return JSONResponse(
                content={
                    "error": f"Profile not found for public_identifier: {public_identifier}",
                },
                status_code=404,
            )

        generated = await generate_ai_summary_and_specializations(profile=profile)
        return JSONResponse(
            content={
                "public_identifier": public_identifier,
                "es_id": es_id,
                "ai_summary": generated.get("ai_summary", ""),
                "specialization_areas": generated.get("specialization_areas", []),
            }
        )
    except Exception as e:
        logger.error("Unhandled exception in /me_summary: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)
