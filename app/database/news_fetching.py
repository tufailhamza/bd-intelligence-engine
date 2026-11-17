import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)


def _reference_identifiers(
    public_identifier: str,
    entity_urn: Optional[str] = None,
) -> List[str]:
    identifiers = [
        str(value).strip()
        for value in (public_identifier, entity_urn)
        if value and str(value).strip()
    ]
    return list(dict.fromkeys(identifiers))


async def fetch_seen_news_ids(
    *,
    reference_public_identifier: str,
    news_ids: List[str],
    entity_urn: Optional[str] = None,
) -> set:
    if not news_ids:
        return set()

    reference_identifiers = _reference_identifiers(
        reference_public_identifier,
        entity_urn,
    )
    if not reference_identifiers:
        return set()

    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(
                text("""
                    SELECT news_id
                    FROM profile_news_seen_bd
                    WHERE reference_public_identifier = ANY(:reference_identifiers)
                      AND news_id = ANY(:news_ids)
                      AND seen = TRUE
                    """),
                {
                    "reference_identifiers": reference_identifiers,
                    "news_ids": [str(x) for x in news_ids if x],
                },
            )
            return {str(row[0]) for row in result.fetchall() if row[0] is not None}
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error fetching seen news ids for reference_public_identifier=%s: %s",
                reference_public_identifier,
                str(e),
            )
            return set()


async def fetch_all_seen_news_ids(
    *,
    reference_public_identifier: str,
    entity_urn: Optional[str] = None,
) -> set:
    reference_identifiers = _reference_identifiers(
        reference_public_identifier,
        entity_urn,
    )
    if not reference_identifiers:
        return set()

    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(
                text("""
                    SELECT news_id
                    FROM profile_news_seen_bd
                    WHERE reference_public_identifier = ANY(:reference_identifiers)
                      AND seen = TRUE
                    """),
                {"reference_identifiers": reference_identifiers},
            )
            return {str(row[0]) for row in result.fetchall() if row[0] is not None}
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error fetching all seen news ids for reference_public_identifier=%s: %s",
                reference_public_identifier,
                str(e),
            )
            return set()


async def fetch_news_by_public_identifier(
    *,
    public_identifier: str,
    entity_urn: Optional[str] = None,
    connection_identifiers: Optional[List[str]] = None,
    news_types: Optional[List[str]] = None,
    news_sub_types: Optional[List[str]] = None,
    search_query: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    Fetch paginated news items for a public_identifier, optionally filtered by connection_identifiers,
    news_type OR news_sub_type, and optionally searched by text query in news titles and content.

    Args:
        public_identifier: The public identifier to fetch news for
        connection_identifiers: Optional list of connection public_identifiers to filter news for specific connections
        news_types: Optional list of news_type values to filter by
        news_sub_types: Optional list of news_sub_type values to filter by
        search_query: Optional text search query to search in news_title and content fields
        page: Page number (1-indexed)
        limit: Number of items per page

    Returns:
        Dictionary with:
        - total_count: Total number of matching items
        - items: List of news items for the current page

    Note:
        Filtering uses OR condition: matches if news_type is in news_types OR news_sub_type is in news_sub_types
        Text search uses ILIKE pattern matching on news_title and content fields (case-insensitive)
        If connection_identifiers is provided, only news for those specific connections will be returned
    """
    reference_identifiers = _reference_identifiers(public_identifier, entity_urn)
    if not reference_identifiers:
        return {"total_count": 0, "items": []}

    async with get_sqlalchemy_session_ai() as session:
        try:
            offset = (page - 1) * limit

            # Build WHERE clause
            where_conditions = [
                "reference_public_identifier = ANY(:reference_identifiers)"
            ]
            params = {"reference_identifiers": reference_identifiers}

            # Add connection_identifiers filter if provided
            if connection_identifiers:
                where_conditions.append(
                    "connection_public_identifier = ANY(:connection_identifiers)"
                )
                params["connection_identifiers"] = connection_identifiers

            # Build OR condition to search values in BOTH news_type AND news_sub_type columns
            # Each value is searched against both columns: (news_type IN (...) OR news_sub_type IN (...))
            filter_conditions = []
            if news_types:
                filter_conditions.append("news_type = ANY(:news_types)")
                params["news_types"] = news_types

            if news_sub_types:
                filter_conditions.append("news_sub_type = ANY(:news_sub_types)")
                params["news_sub_types"] = news_sub_types

            # Add OR condition if we have any filters
            # This ensures values match EITHER news_type OR news_sub_type column
            if filter_conditions:
                if len(filter_conditions) == 1:
                    where_conditions.append(filter_conditions[0])
                else:
                    # Multiple conditions: (news_type IN (...) OR news_sub_type IN (...))
                    # Each value in the list is checked against BOTH columns
                    where_conditions.append(f"({' OR '.join(filter_conditions)})")

            # Add text search condition if search_query is provided
            # Search in both news_title and content fields (case-insensitive)
            if search_query:
                search_pattern = f"%{search_query}%"
                where_conditions.append(
                    "(news_title ILIKE :search_query OR content ILIKE :search_query)"
                )
                params["search_query"] = search_pattern

            where_clause = " AND ".join(where_conditions)

            # Count total matching items
            count_query = f"""
                SELECT COUNT(*) as total
                FROM ranked_news_bd
                WHERE {where_clause}
            """

            count_result = await session.execute(
                text(count_query),
                params,
            )
            total_count = count_result.scalar() or 0

            # Fetch paginated items
            fetch_query = f"""
                SELECT 
                    connection_public_identifier,
                    news_id,
                    link,
                    news_title,
                    content,
                    news_type,
                    news_sub_type,
                    published_date,
                    source,
                    author,
                    author_description,
                    rank,
                    final_score,
                    reasoning,
                    created_at
                FROM ranked_news_bd
                WHERE {where_clause}
                ORDER BY COALESCE(seen, FALSE) ASC, rank ASC NULLS LAST, published_date DESC
                LIMIT :limit OFFSET :offset
            """

            params["limit"] = limit
            params["offset"] = offset

            result = await session.execute(
                text(fetch_query),
                params,
            )
            rows = result.mappings().all()

            items = [dict(row) for row in rows]

            return {
                "total_count": total_count,
                "items": items,
            }

        except Exception as e:
            await session.rollback()
            logger.error(
                "Error fetching news for public_identifier=%s: %s",
                public_identifier,
                str(e),
            )
            raise


async def update_ranked_news_seen(
    *,
    news_id: str,
    reference_public_identifier: str,
    entity_urn: Optional[str] = None,
    seen: bool,
) -> Dict[str, int]:
    """
    Update `seen` boolean for a ranked news row.

    Returns dict with:
    - matched: number of rows matching identifiers
    - updated: number of rows whose seen value was changed
    """
    reference_identifiers = _reference_identifiers(
        reference_public_identifier,
        entity_urn,
    )
    if not reference_identifiers:
        return {"matched": 0, "updated": 0}

    async with get_sqlalchemy_session_ai() as session:
        try:
            result = await session.execute(
                text("""
                    WITH matched AS (
                        SELECT 1
                        FROM ranked_news_bd
                        WHERE news_id = :news_id
                          AND reference_public_identifier = ANY(:reference_identifiers)
                    ),
                    updated AS (
                        UPDATE ranked_news_bd
                        SET seen = :seen
                        WHERE news_id = :news_id
                          AND reference_public_identifier = ANY(:reference_identifiers)
                          AND COALESCE(seen, FALSE) IS DISTINCT FROM :seen
                        RETURNING 1
                    )
                    SELECT
                        (SELECT COUNT(*) FROM matched) AS matched_count,
                        (SELECT COUNT(*) FROM updated) AS updated_count
                    """),
                {
                    "news_id": news_id,
                    "reference_identifiers": reference_identifiers,
                    "seen": seen,
                },
            )
            row = result.mappings().first() or {}
            seen_rows = [
                {
                    "reference_public_identifier": identifier,
                    "news_id": news_id,
                    "seen": seen,
                }
                for identifier in reference_identifiers
            ]
            await session.execute(
                text("""
                    INSERT INTO profile_news_seen_bd (
                        reference_public_identifier,
                        news_id,
                        seen,
                        updated_at
                    )
                    VALUES (
                        :reference_public_identifier,
                        :news_id,
                        :seen,
                        now()
                    )
                    ON CONFLICT (reference_public_identifier, news_id)
                    DO UPDATE SET
                        seen = EXCLUDED.seen,
                        updated_at = now()
                    """),
                seen_rows,
            )
            seen_upserted = len(seen_rows)
            await session.commit()
            return {
                "matched": max(int(row.get("matched_count") or 0), seen_upserted),
                "updated": max(int(row.get("updated_count") or 0), seen_upserted),
            }
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error updating ranked_news seen (ref=%s news_id=%s): %s",
                reference_public_identifier,
                news_id,
                str(e),
            )
            raise
