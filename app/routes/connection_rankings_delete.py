import logging
from fastapi import APIRouter, HTTPException

from app.models.schemas.connection_rankings_delete import (
    DeleteConnectionRankingsBdOnlyInput,
    DeleteConnectionRankingsBdOnlyOutput,
    DeleteConnectionRankingsInput,
    DeleteConnectionRankingsOutput,
)
from app.database.connection_rankings import (
    delete_all_reference_data_in_transaction,
    delete_connection_rankings,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.delete("/connection_rankings", response_model=DeleteConnectionRankingsOutput)
async def delete_connection_rankings_endpoint(request: DeleteConnectionRankingsInput):
    """
    Delete connection rankings, profile scores, and ranked news from PostgreSQL.

    - If only reference_public_identifier is provided:
      - Deletes all connection rankings for that reference profile
      - Deletes all profile scores for that reference profile
      - Deletes all ranked news for that reference profile

    - If both reference_public_identifier and connection_public_identifier are provided:
      - Deletes only that specific connection ranking
      - Profile scores and ranked news are still deleted for the entire reference profile

    This endpoint affects the following tables:
    - connection_rankings_bd
    - profile_scores_bd
    - ranked_news_bd
    """
    try:
        # Execute all three deletes atomically in a single transaction
        (
            connection_rankings_deleted,
            profile_scores_deleted,
            ranked_news_deleted,
        ) = await delete_all_reference_data_in_transaction(
            reference_public_identifier=request.reference_public_identifier,
            connection_public_identifier=request.connection_public_identifier,
        )

        return DeleteConnectionRankingsOutput(
            reference_public_identifier=request.reference_public_identifier,
            connection_public_identifier=request.connection_public_identifier,
            connection_rankings_deleted=connection_rankings_deleted,
            profile_scores_deleted=profile_scores_deleted,
            ranked_news_deleted=ranked_news_deleted,
        )
    except Exception as e:
        logger.error(
            "Error in delete_connection_rankings endpoint: %s", e, exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete(
    "/connection_rankings_bd",
    response_model=DeleteConnectionRankingsBdOnlyOutput,
)
async def delete_connection_rankings_bd_only(
    request: DeleteConnectionRankingsBdOnlyInput,
):
    """
    Delete rows from **connection_rankings_bd** only (by reference profile).

    Does not modify `profile_scores_bd` or `ranked_news_bd`.
    """
    try:
        n = await delete_connection_rankings(
            reference_public_identifier=request.reference_public_identifier.strip(),
            connection_public_identifier=(
                request.connection_public_identifier.strip()
                if request.connection_public_identifier
                else None
            ),
        )
        return DeleteConnectionRankingsBdOnlyOutput(
            reference_public_identifier=request.reference_public_identifier.strip(),
            connection_public_identifier=(
                request.connection_public_identifier.strip()
                if request.connection_public_identifier
                else None
            ),
            connection_rankings_deleted=n,
        )
    except Exception as e:
        logger.error(
            "Error in delete_connection_rankings_bd_only: %s", e, exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")
