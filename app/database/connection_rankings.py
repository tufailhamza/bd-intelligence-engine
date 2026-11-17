import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)

# When a connection is ranked or re-ranked, reset priority to this baseline (not NULL).
DEFAULT_RANKING_PRIORITY = "Low"


async def insert_connection_ranking(
    *,
    reference_public_identifier: str,
    reference_es_id: Optional[str],
    connection_public_identifier: str,
    connection_es_id: Optional[str],
    total_score: float,
    metric_scores: Dict[str, float],
    reasoning: Any,  # Can be str or Dict[str, str]
    nullify: Optional[bool] = None,
    impact_ranking: Optional[str] = None,
) -> None:
    """
    Insert or update a connection ranking.
    Uses ON CONFLICT to handle re-scoring the same connection.

    Args:
        reasoning: Can be a string or dict. Will be stored as JSONB.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(
                text("""
                    INSERT INTO connection_rankings_bd (
                        reference_public_identifier,
                        reference_es_id,
                        connection_public_identifier,
                        connection_es_id,
                        total_score,
                        metric_scores,
                        reasoning,
                        nullify,
                        priority,
                        impact_ranking
                    )
                    VALUES (
                        :reference_public_identifier,
                        :reference_es_id,
                        :connection_public_identifier,
                        :connection_es_id,
                        :total_score,
                        CAST(:metric_scores AS jsonb),
                        CAST(:reasoning AS jsonb),
                        :nullify,
                        :priority_default,
                        :impact_ranking
                    )
                    ON CONFLICT (reference_public_identifier, connection_public_identifier)
                    DO UPDATE SET
                        reference_es_id = EXCLUDED.reference_es_id,
                        connection_es_id = EXCLUDED.connection_es_id,
                        total_score = EXCLUDED.total_score,
                        metric_scores = EXCLUDED.metric_scores,
                        reasoning = EXCLUDED.reasoning,
                        nullify = EXCLUDED.nullify,
                        priority = EXCLUDED.priority,
                        impact_ranking = EXCLUDED.impact_ranking,
                        scored_at = NOW()
                    """),
                {
                    "reference_public_identifier": reference_public_identifier,
                    "reference_es_id": reference_es_id,
                    "connection_public_identifier": connection_public_identifier,
                    "connection_es_id": connection_es_id,
                    "total_score": total_score,
                    "metric_scores": json.dumps(metric_scores, ensure_ascii=False),
                    "reasoning": (
                        json.dumps(reasoning, ensure_ascii=False)
                        if isinstance(reasoning, (dict, list))
                        else json.dumps({"text": str(reasoning)}, ensure_ascii=False)
                    ),
                    "nullify": nullify,
                    "priority_default": DEFAULT_RANKING_PRIORITY,
                    "impact_ranking": impact_ranking,
                },
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error inserting connection ranking for reference=%s, connection=%s: %s",
                reference_public_identifier,
                connection_public_identifier,
                str(e),
            )
            raise


async def insert_batch_rankings(
    *,
    reference_public_identifier: str,
    reference_es_id: Optional[str],
    ranked_connections: List[Dict[str, Any]],
) -> int:
    """
    Insert a batch of connection rankings.
    ranked_connections should match your API response format.
    Returns the number of successfully inserted connections.
    """
    inserted_count = 0
    failed_count = 0
    for conn in ranked_connections:
        try:
            await insert_connection_ranking(
                reference_public_identifier=reference_public_identifier,
                reference_es_id=reference_es_id,
                connection_public_identifier=conn["public_identifier"],
                connection_es_id=conn.get("es_id"),
                total_score=conn["score"],
                metric_scores=conn["metric_scores"],
                reasoning=conn.get("reasoning", {}),
                nullify=conn.get("nullify"),  # Can be None, True, or False
                impact_ranking=conn.get("impact_ranking"),
            )
            inserted_count += 1
        except Exception as e:
            failed_count += 1
            logger.error(
                "Failed to insert ranking for reference=%s, connection=%s: %s",
                reference_public_identifier,
                conn.get("public_identifier", "unknown"),
                str(e),
            )

    logger.info(
        "insert_batch_rankings: reference_public_identifier=%s, inserted=%d, failed=%d, total=%d",
        reference_public_identifier,
        inserted_count,
        failed_count,
        len(ranked_connections),
    )
    return inserted_count


async def fetch_ranked_connections(
    *,
    reference_public_identifier: str,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch ranked connections for a reference profile.
    Returns list sorted by total_score DESC.
    Rank position can be computed client-side using enumerate() if needed.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            query = """
                SELECT 
                    connection_public_identifier,
                    connection_es_id,
                    total_score,
                    metric_scores,
                    reasoning,
                    nullify,
                    priority,
                    impact_ranking,
                    scored_at
                FROM connection_rankings_bd
                WHERE reference_public_identifier = :reference_public_identifier
                ORDER BY total_score DESC
            """
            params = {"reference_public_identifier": reference_public_identifier}

            if limit:
                query += " LIMIT :limit"
                params["limit"] = limit
            if offset:
                query += " OFFSET :offset"
                params["offset"] = offset

            res = await session.execute(text(query), params)
            rows = res.mappings().all()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(
                "Error fetching ranked connections for reference=%s: %s",
                reference_public_identifier,
                str(e),
            )
            raise


async def fetch_ranked_connections_with_meeting_points(
    *,
    reference_public_identifier: str,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch ranked connections for a reference profile where meeting_points > 0.
    Returns list sorted by total_score DESC.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            query = """
                SELECT 
                    connection_public_identifier,
                    connection_es_id,
                    total_score,
                    metric_scores,
                    reasoning,
                    nullify,
                    priority,
                    impact_ranking,
                    scored_at
                FROM connection_rankings_bd
                WHERE reference_public_identifier = :reference_public_identifier
                  AND COALESCE((metric_scores->>'meeting_points')::numeric, 0) > 0
                ORDER BY total_score DESC
            """
            params = {"reference_public_identifier": reference_public_identifier}

            if limit:
                query += " LIMIT :limit"
                params["limit"] = limit
            if offset:
                query += " OFFSET :offset"
                params["offset"] = offset

            res = await session.execute(text(query), params)
            rows = res.mappings().all()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(
                "Error fetching ranked connections with meeting_points for reference=%s: %s",
                reference_public_identifier,
                str(e),
            )
            raise


async def fetch_specific_ranked_connections(
    *,
    reference_public_identifier: str,
    connection_public_identifiers: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch rankings for specific connection public identifiers.
    Returns a dict mapping connection_public_identifier -> ranking data.
    Only returns connections that exist in the database.
    """
    if not connection_public_identifiers:
        return {}

    async with get_sqlalchemy_session_ai() as session:
        try:
            # Build IN clause with placeholders
            placeholders = ",".join(
                [f":conn_id_{i}" for i in range(len(connection_public_identifiers))]
            )
            query = f"""
                SELECT 
                    reference_es_id,
                    connection_public_identifier,
                    connection_es_id,
                    total_score,
                    metric_scores,
                    reasoning,
                    nullify,
                    priority,
                    scored_at
                FROM connection_rankings_bd
                WHERE reference_public_identifier = :reference_public_identifier
                  AND connection_public_identifier IN ({placeholders})
            """

            params = {"reference_public_identifier": reference_public_identifier}
            for i, conn_id in enumerate(connection_public_identifiers):
                params[f"conn_id_{i}"] = conn_id

            res = await session.execute(text(query), params)
            rows = res.mappings().all()

            # Convert to dict keyed by connection_public_identifier
            result = {}
            for row in rows:
                conn_id = row["connection_public_identifier"]
                result[conn_id] = dict(row)

            return result
        except Exception as e:
            logger.error(
                "Error fetching specific ranked connections for reference=%s: %s",
                reference_public_identifier,
                str(e),
            )
            raise


async def update_connection_priority(
    *,
    reference_public_identifier: str,
    connection_public_identifier: str,
    priority: Optional[str],
) -> int:
    """
    Set priority for one ranked connection (High / Medium / Low, or NULL to clear).

    Returns:
        Number of rows updated (0 if no matching row).
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(
                text("""
                    UPDATE connection_rankings_bd
                    SET priority = :priority
                    WHERE reference_public_identifier = :reference_public_identifier
                      AND connection_public_identifier = :connection_public_identifier
                    """),
                {
                    "reference_public_identifier": reference_public_identifier,
                    "connection_public_identifier": connection_public_identifier,
                    "priority": priority,
                },
            )
            await session.commit()
            return int(result.rowcount or 0)
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error updating priority for reference=%s connection=%s: %s",
                reference_public_identifier,
                connection_public_identifier,
                str(e),
            )
            raise


async def delete_connection_rankings(
    *,
    reference_public_identifier: str,
    connection_public_identifier: Optional[str] = None,
) -> int:
    """
    Delete connection rankings from connection_rankings_bd table.

    Args:
        reference_public_identifier: Required. The reference profile identifier.
        connection_public_identifier: Optional. If provided, deletes only that specific connection ranking.
                                     If None, deletes all rankings for the reference profile.

    Returns:
        Number of rows deleted.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            if connection_public_identifier:
                # Delete specific connection ranking
                query = """
                    DELETE FROM connection_rankings_bd
                    WHERE reference_public_identifier = :reference_public_identifier
                      AND connection_public_identifier = :connection_public_identifier
                """
                params = {
                    "reference_public_identifier": reference_public_identifier,
                    "connection_public_identifier": connection_public_identifier,
                }
                logger.info(
                    "Deleting connection ranking: reference=%s, connection=%s",
                    reference_public_identifier,
                    connection_public_identifier,
                )
            else:
                # Delete all rankings for the reference profile
                query = """
                    DELETE FROM connection_rankings_bd
                    WHERE reference_public_identifier = :reference_public_identifier
                """
                params = {"reference_public_identifier": reference_public_identifier}
                logger.info(
                    "Deleting all connection rankings for reference=%s",
                    reference_public_identifier,
                )

            result = await session.execute(text(query), params)
            await session.commit()
            deleted_count = result.rowcount

            logger.info(
                "Successfully deleted %d row(s) from connection_rankings_bd: reference=%s%s",
                deleted_count,
                reference_public_identifier,
                (
                    f", connection={connection_public_identifier}"
                    if connection_public_identifier
                    else ""
                ),
            )

            return deleted_count
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error deleting connection rankings for reference=%s%s: %s",
                reference_public_identifier,
                (
                    f", connection={connection_public_identifier}"
                    if connection_public_identifier
                    else ""
                ),
                str(e),
            )
            raise


async def update_impact_ranking(
    *,
    reference_public_identifier: str,
    connection_public_identifier: str,
    impact_ranking: Optional[str],
) -> int:
    """
    Set impact_ranking for one ranked connection (Strong / Moderate / Weak, or NULL to clear).

    Returns:
        Number of rows updated (0 if no matching row).
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(
                text("""
                    UPDATE connection_rankings_bd
                    SET impact_ranking = :impact_ranking
                    WHERE reference_public_identifier = :reference_public_identifier
                      AND connection_public_identifier = :connection_public_identifier
                """),
                {
                    "reference_public_identifier": reference_public_identifier,
                    "connection_public_identifier": connection_public_identifier,
                    "impact_ranking": impact_ranking,
                },
            )
            await session.commit()
            return int(result.rowcount or 0)
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error updating impact_ranking for reference=%s connection=%s: %s",
                reference_public_identifier,
                connection_public_identifier,
                str(e),
            )
            raise


async def update_batch_impact_rankings(
    *,
    reference_public_identifier: str,
    impact_rankings: Dict[str, Optional[str]],
) -> Dict[str, int]:
    """
    Update impact_ranking for multiple connections in one transaction.

    Args:
        impact_rankings: Dict[connection_public_identifier -> impact_ranking value]

    Returns:
        Dict[connection_public_identifier -> rows_updated (0 or 1)]
    """
    if not impact_rankings:
        return {}

    results: Dict[str, int] = {}
    async with get_sqlalchemy_session_ai() as session:
        try:
            for conn_id, ranking in impact_rankings.items():
                res = await session.execute(
                    text("""
                        UPDATE connection_rankings_bd
                        SET impact_ranking = :impact_ranking
                        WHERE reference_public_identifier = :reference_public_identifier
                          AND connection_public_identifier = :connection_public_identifier
                    """),
                    {
                        "reference_public_identifier": reference_public_identifier,
                        "connection_public_identifier": conn_id,
                        "impact_ranking": ranking,
                    },
                )
                results[conn_id] = int(res.rowcount or 0)
            await session.commit()
            logger.info(
                "update_batch_impact_rankings: reference=%s updated=%d connections",
                reference_public_identifier,
                sum(1 for v in results.values() if v > 0),
            )
            return results
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error in update_batch_impact_rankings for reference=%s: %s",
                reference_public_identifier,
                str(e),
            )
            raise


async def delete_all_reference_data_in_transaction(
    *,
    reference_public_identifier: str,
    connection_public_identifier: Optional[str] = None,
) -> Tuple[int, int, int]:
    """
    Delete connection rankings, profile scores, and ranked news atomically in a single transaction.

    This function ensures that all three delete operations either succeed together or fail together,
    preventing partial state in the database.

    Args:
        reference_public_identifier: Required. The reference profile identifier.
        connection_public_identifier: Optional. If provided, deletes only that specific connection ranking.
                                     If None, deletes all rankings for the reference profile.
                                     Note: Profile scores and ranked news are always deleted for the entire reference profile.

    Returns:
        Tuple of (connection_rankings_deleted, profile_scores_deleted, ranked_news_deleted)

    Raises:
        Exception: If any delete operation fails, the entire transaction is rolled back.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            # 1. Delete connection rankings
            if connection_public_identifier:
                connection_rankings_query = """
                    DELETE FROM connection_rankings_bd
                    WHERE reference_public_identifier = :reference_public_identifier
                      AND connection_public_identifier = :connection_public_identifier
                """
                connection_rankings_params = {
                    "reference_public_identifier": reference_public_identifier,
                    "connection_public_identifier": connection_public_identifier,
                }
                logger.info(
                    "Deleting connection ranking in transaction: reference=%s, connection=%s",
                    reference_public_identifier,
                    connection_public_identifier,
                )
            else:
                connection_rankings_query = """
                    DELETE FROM connection_rankings_bd
                    WHERE reference_public_identifier = :reference_public_identifier
                """
                connection_rankings_params = {
                    "reference_public_identifier": reference_public_identifier
                }
                logger.info(
                    "Deleting all connection rankings in transaction for reference=%s",
                    reference_public_identifier,
                )

            connection_rankings_result = await session.execute(
                text(connection_rankings_query), connection_rankings_params
            )
            connection_rankings_deleted = connection_rankings_result.rowcount

            # 2. Delete profile scores (always for entire reference profile)
            profile_scores_query = """
                DELETE FROM profile_scores_bd
                WHERE public_identifier = :public_identifier
            """
            profile_scores_params = {"public_identifier": reference_public_identifier}
            logger.info(
                "Deleting profile scores in transaction for public_identifier=%s",
                reference_public_identifier,
            )

            profile_scores_result = await session.execute(
                text(profile_scores_query), profile_scores_params
            )
            profile_scores_deleted = profile_scores_result.rowcount

            # 3. Delete ranked news (always for entire reference profile)
            ranked_news_query = """
                DELETE FROM ranked_news_bd
                WHERE reference_public_identifier = :reference_public_identifier
            """
            ranked_news_params = {
                "reference_public_identifier": reference_public_identifier
            }
            logger.info(
                "Deleting ranked news in transaction for reference_public_identifier=%s",
                reference_public_identifier,
            )

            ranked_news_result = await session.execute(
                text(ranked_news_query), ranked_news_params
            )
            ranked_news_deleted = ranked_news_result.rowcount

            # Commit all three deletes atomically
            await session.commit()

            logger.info(
                "Successfully deleted in transaction for reference=%s: "
                "connection_rankings=%d, profile_scores=%d, ranked_news=%d",
                reference_public_identifier,
                connection_rankings_deleted,
                profile_scores_deleted,
                ranked_news_deleted,
            )

            return (
                connection_rankings_deleted,
                profile_scores_deleted,
                ranked_news_deleted,
            )
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error in transactional delete for reference=%s%s: %s. All changes rolled back.",
                reference_public_identifier,
                (
                    f", connection={connection_public_identifier}"
                    if connection_public_identifier
                    else ""
                ),
                str(e),
            )
            raise
