import logging
import traceback

from typing import Optional
from app.models.schemas.refined_icp import RefinedICPInput

from qutils.llm.utilities import asynchronous_llm
from app.utils.refine_icp.prompts import REFINED_ICP_PROMPT

logger = logging.getLogger(__name__)


async def call_llm_for_icp_refinement(request: RefinedICPInput) -> Optional[str]:
    """
    Builds the prompt from RefinedICPInput, calls the LLM, and returns the raw response text.
    """
    try:
        prompt = REFINED_ICP_PROMPT.format(
            roles=request.roles,
            industries=request.industries,
            locations=request.locations,
            minRevenue=request.minRevenue if request.minRevenue is not None else "null",
            maxRevenue=request.maxRevenue if request.maxRevenue is not None else "null",
            minEmployees=(
                request.minEmployees if request.minEmployees is not None else "null"
            ),
            maxEmployees=(
                request.maxEmployees if request.maxEmployees is not None else "null"
            ),
        )

        response = await asynchronous_llm(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0.1,
            max_tokens=4000,
            fallback_openai_model="gpt-4o",
            fallback_anthropic_model="claude-3-5-sonnet-latest",
            verbose=False,
        )

        if not response:
            logger.error("LLM returned empty response")
            return None

        return response

    except Exception as e:
        logger.error(f"Error calling asynchronous_llm: {e}")
        traceback.print_exc()
        return None
