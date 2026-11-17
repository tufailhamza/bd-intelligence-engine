"""
Endpoints for connection_rankings_bd.priority (High / Medium / Low).
"""

import logging
from typing import List

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database.connection_rankings import (
    fetch_ranked_connections,
    update_connection_priority,
)
from app.models.schemas.connection_priority import (
    ConnectionPriority,
    SetPrioritiesLlmInput,
    SetPrioritiesLlmOutput,
    PriorityAssignment,
    UpdateConnectionPriorityInput,
    UpdateConnectionPriorityOutput,
)
from app.utils.connection_priority.zscore_priority import assign_priorities_zscore

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/set_connection_priorities_llm", response_model=SetPrioritiesLlmOutput)
async def set_connection_priorities_llm(request: SetPrioritiesLlmInput):
    """
    Fetch top 20 ranked connections for the reference from connection_rankings_bd,
    assign High/Medium/Low per connection using z-scores of ``total_score`` within the cohort
    (see ``app.utils.connection_priority.zscore_priority``), and persist to the priority column.
    """
    try:
        rows = await fetch_ranked_connections(
            reference_public_identifier=request.reference_public_identifier,
            limit=20,
            offset=0,
        )
        if not rows:
            return SetPrioritiesLlmOutput(
                reference_public_identifier=request.reference_public_identifier,
                updated=[],
                message="No ranked connections found for this reference.",
            )

        pairs = assign_priorities_zscore(rows)
        updated: List[PriorityAssignment] = []
        for cid, pr in pairs:
            n = await update_connection_priority(
                reference_public_identifier=request.reference_public_identifier,
                connection_public_identifier=cid,
                priority=pr,
            )
            if n > 0:
                updated.append(
                    PriorityAssignment(
                        connection_public_identifier=cid,
                        priority=ConnectionPriority(pr),
                    )
                )
            else:
                logger.warning(
                    "Priority result not persisted (row missing): ref=%s conn=%s",
                    request.reference_public_identifier,
                    cid,
                )

        return SetPrioritiesLlmOutput(
            reference_public_identifier=request.reference_public_identifier,
            updated=updated,
            message=None,
        )
    except Exception as e:
        logger.exception("set_connection_priorities_llm failed: %s", e)
        return JSONResponse(
            content={"error": "Internal Server Error"},
            status_code=500,
        )


@router.patch("/connection_priority", response_model=UpdateConnectionPriorityOutput)
async def patch_connection_priority(request: UpdateConnectionPriorityInput):
    """
    Manually set priority for one connection (no LLM).
    """
    try:
        n = await update_connection_priority(
            reference_public_identifier=request.reference_public_identifier,
            connection_public_identifier=request.connection_public_identifier,
            priority=request.priority.value,
        )
        if n == 0:
            return UpdateConnectionPriorityOutput(
                success=False,
                message="No matching row in connection_rankings_bd for this reference and connection.",
            )
        return UpdateConnectionPriorityOutput(
            success=True,
            message="Priority updated.",
        )
    except Exception as e:
        logger.exception("patch_connection_priority failed: %s", e)
        return JSONResponse(
            content={"error": "Internal Server Error"},
            status_code=500,
        )
