import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)


async def fetch_all_news_ids_for_reference(
    *,
    reference_public_identifier: str,
) -> set:
    """
    Fetch all news_ids that exist in the database for a reference profile.
    Returns a set of news_id strings (for duplicate checking).
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT DISTINCT news_id
                    FROM ranked_news_bd
                    WHERE reference_public_identifier = :reference_public_identifier
                      AND news_id IS NOT NULL
                    """),
                {"reference_public_identifier": reference_public_identifier},
            )
            rows = res.fetchall()
            # Convert to set of strings for consistent matching
            news_ids = {str(row[0]) for row in rows if row[0] is not None}
            return news_ids
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error fetching all news_ids for reference_public_identifier=%s: %s",
                reference_public_identifier,
                str(e),
            )
            # Return empty set on error to allow insertion (fail-safe)
            return set()


async def fetch_latest_ranked_news_for_reference(
    *,
    reference_public_identifier: str,
) -> List[Dict[str, Any]]:
    """
    Fetch the most recently saved ranked news feed for a reference profile.

    Since `ranked_news_bd` doesn't have a run_id, we treat "latest feed" as
    rows with created_at == max(created_at) for that reference (stable per INSERT statement).
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT *
                    FROM ranked_news_bd
                    WHERE reference_public_identifier = :reference_public_identifier
                      AND created_at = (
                        SELECT MAX(created_at)
                        FROM ranked_news_bd
                        WHERE reference_public_identifier = :reference_public_identifier
                      )
                    ORDER BY rank ASC NULLS LAST, id ASC
                    """),
                {"reference_public_identifier": reference_public_identifier},
            )
            rows = res.mappings().all()
            return [dict(r) for r in rows]
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error fetching latest ranked news for reference_public_identifier=%s: %s",
                reference_public_identifier,
                str(e),
            )
            raise


async def fetch_ranked_news_for_reference_and_connections(
    *,
    reference_public_identifier: str,
    connection_public_identifiers: List[str],
) -> List[Dict[str, Any]]:
    """
    For each connection_identifier, fetch ranked news from ranked_news_bd (latest batch).
    Returns rows with news_id, connection_public_identifier, news_title, content, etc.
    Used to then fetch full news from bd_news ES by news_id.
    """
    if not connection_public_identifiers:
        return []
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT *
                    FROM ranked_news_bd
                    WHERE reference_public_identifier = :reference_public_identifier
                      AND connection_public_identifier = ANY(:connection_public_identifiers)
                      AND created_at = (
                        SELECT MAX(created_at)
                        FROM ranked_news_bd
                        WHERE reference_public_identifier = :reference_public_identifier
                      )
                    ORDER BY rank ASC NULLS LAST, id ASC
                    """),
                {
                    "reference_public_identifier": reference_public_identifier,
                    "connection_public_identifiers": connection_public_identifiers,
                },
            )
            rows = res.mappings().all()
            return [dict(r) for r in rows]
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error fetching ranked news for reference=%s, connections=%s: %s",
                reference_public_identifier,
                connection_public_identifiers[:5],
                str(e),
            )
            raise


async def insert_ranked_news_for_reference(
    *,
    reference_public_identifier: str,
    reference_es_id: Optional[str],
    ranked_news: List[Dict[str, Any]],
) -> None:
    """
    Insert ranked news rows for a reference profile.
    Skips news_ids that already exist in the database for this reference (prevents duplicates).

    Expects the table:
      ranked_news_bd(
        id BIGSERIAL PRIMARY KEY,
        reference_public_identifier TEXT NOT NULL,
        reference_es_id TEXT NULL,
        connection_public_identifier TEXT NOT NULL,
        news_id TEXT NOT NULL,
        link TEXT NULL,
        news_title TEXT NULL,
        content TEXT NULL,
        news_type TEXT NULL,
        news_sub_type TEXT NULL,
        published_date BIGINT NULL,
        source TEXT NULL,
        author TEXT NULL,
        author_description TEXT NULL,
        rank INT NULL,
        final_score DOUBLE PRECISION NULL,
        reasoning TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      )
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            if not ranked_news:
                return

            # Fetch all existing news_ids for this reference to prevent duplicates
            existing_news_ids = await fetch_all_news_ids_for_reference(
                reference_public_identifier=reference_public_identifier
            )

            # Filter out items that already exist in the database
            new_items = []
            skipped_duplicates = 0
            for item in ranked_news:
                if not item.get("connection_public_identifier") or not item.get(
                    "news_id"
                ):
                    continue

                news_id_str = str(item.get("news_id"))
                if news_id_str in existing_news_ids:
                    skipped_duplicates += 1
                    continue

                new_items.append(
                    {
                        "reference_public_identifier": reference_public_identifier,
                        "reference_es_id": reference_es_id,
                        "connection_public_identifier": str(
                            item.get("connection_public_identifier") or ""
                        ),
                        "news_id": news_id_str,
                        "link": item.get("link"),
                        "news_title": item.get("news_title"),
                        "content": item.get("content"),
                        "news_type": item.get("news_type"),
                        "news_sub_type": item.get("news_sub_type"),
                        "published_date": item.get("published_date"),
                        "source": item.get("source"),
                        "author": item.get("author"),
                        "author_description": item.get("author_description"),
                        "rank": item.get("rank"),
                        "final_score": item.get("final_score"),
                        "reasoning": item.get("reasoning"),
                    }
                )

            if skipped_duplicates > 0:
                logger.info(
                    "Skipped %d duplicate news items (already exist in DB) for reference=%s",
                    skipped_duplicates,
                    reference_public_identifier,
                )

            if new_items:
                await session.execute(
                    text("""
                        INSERT INTO ranked_news_bd (
                            reference_public_identifier,
                            reference_es_id,
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
                            reasoning
                        )
                        VALUES (
                            :reference_public_identifier,
                            :reference_es_id,
                            :connection_public_identifier,
                            :news_id,
                            :link,
                            :news_title,
                            :content,
                            :news_type,
                            :news_sub_type,
                            :published_date,
                            :source,
                            :author,
                            :author_description,
                            :rank,
                            :final_score,
                            :reasoning
                        )
                        """),
                    new_items,
                )
                logger.info(
                    "Inserted %d new news items (skipped %d duplicates) for reference=%s",
                    len(new_items),
                    skipped_duplicates,
                    reference_public_identifier,
                )
            else:
                logger.info(
                    "No new items to insert (all %d items were duplicates) for reference=%s",
                    len(ranked_news),
                    reference_public_identifier,
                )

            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error inserting ranked news for reference_public_identifier=%s: %s",
                reference_public_identifier,
                str(e),
            )
            raise


async def delete_predictive_ranked_news(
    *,
    news_type: str,
) -> Dict[str, int]:
    async with get_sqlalchemy_session_ai() as session:
        try:
            params = {"news_type": news_type}
            seen_result = await session.execute(
                text("""
                    WITH predictive_news AS (
                        SELECT DISTINCT news_id
                        FROM ranked_news_bd
                        WHERE news_type = :news_type
                    )
                    DELETE FROM profile_news_seen_bd
                    WHERE news_id IN (
                        SELECT news_id
                        FROM predictive_news
                        WHERE news_id IS NOT NULL
                    )
                """),
                params,
            )
            ranked_result = await session.execute(
                text("""
                    DELETE FROM ranked_news_bd
                    WHERE news_type = :news_type
                """),
                params,
            )
            await session.commit()
            return {
                "ranked_news_deleted": max(int(ranked_result.rowcount or 0), 0),
                "seen_rows_deleted": max(int(seen_result.rowcount or 0), 0),
            }
        except Exception as e:
            await session.rollback()
            logger.error("Error deleting predictive ranked news: %s", str(e))
            raise
