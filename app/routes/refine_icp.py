import traceback
import logging
import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.models.schemas.refined_icp import (
    RefinedICPInput,
    RefinedICPOutput,
)
from app.utils.refine_icp.refined_icp_generation import call_llm_for_icp_refinement

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/refine_icp", response_model=RefinedICPOutput)
async def refine_icp(request: RefinedICPInput):
    try:
        response_text = await call_llm_for_icp_refinement(request)

        if not response_text:
            return JSONResponse(
                content={"error": "Failed to generate ICP from LLM"},
                status_code=500,
            )

        title_match = re.search(r"<title>(.*?)</title>", response_text, re.DOTALL)
        description_match = re.search(
            r"<description>(.*?)</description>", response_text, re.DOTALL
        )

        title = title_match.group(1).strip() if title_match else ""
        description = description_match.group(1).strip() if description_match else ""

        if not title or not description:
            logger.error(f"LLM response missing fields: {response_text}")
            return JSONResponse(
                content={
                    "error": "LLM response did not contain title or description",
                    "llm_output": response_text,
                },
                status_code=500,
            )

        # Return final response in your original JSONResponse style
        return JSONResponse(content={"title": title, "description": description})

    except Exception as e:
        logger.error(f"Error in refine_icp endpoint: {e}")
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)
