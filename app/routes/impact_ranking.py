"""
PATCH /update_impact_ranking

Manually override the impact_ranking for one or more connections.
Auto-computation happens automatically inside /rank_connections.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database.connection_rankings import update_batch_impact_rankings
from app.models.schemas.impact_ranking import (
    UpdateImpactRankingInput,
    UpdateImpactRankingOutput,
    UpdateImpactRankingResult,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.patch("/update_impact_ranking", response_model=UpdateImpactRankingOutput)
async def update_impact_ranking_endpoint(request: UpdateImpactRankingInput):
    """
    Manually set impact_ranking for one or more connections.

    Pass a dict of ``{connection_public_identifier: "Strong" | "Moderate" | "Weak" | null}``.
    Passing null clears the field back to NULL in the DB.

    Impact rankings are auto-computed when /rank_connections runs;
    use this endpoint only to override individual values after the fact.
    """
    try:
        db_results = await update_batch_impact_rankings(
            reference_public_identifier=request.reference_public_identifier,
            impact_rankings=request.connection_public_identifiers,
        )

        not_found_in_db = [pid for pid, rows in db_results.items() if rows == 0]

        results = [
            UpdateImpactRankingResult(
                connection_public_identifier=pid,
                impact_ranking=value,
                updated_in_db=db_results.get(pid, 0) > 0,
            )
            for pid, value in request.connection_public_identifiers.items()
        ]

        logger.info(
            "update_impact_ranking: reference=%s updated=%d not_found=%d",
            request.reference_public_identifier,
            sum(1 for r in results if r.updated_in_db),
            len(not_found_in_db),
        )

        return UpdateImpactRankingOutput(
            reference_public_identifier=request.reference_public_identifier,
            results=results,
            not_found_in_db=not_found_in_db,
        )

    except Exception as e:
        logger.exception("Unhandled exception in /update_impact_ranking: %s", str(e))
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)
