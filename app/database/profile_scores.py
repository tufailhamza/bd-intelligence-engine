import json
import logging
import traceback
from typing import Any, Dict, Optional

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)


async def insert_profile_score(
    *,
    public_identifier: str,
    es_id: Optional[str],
    rubric_json: Dict[str, Any],
) -> None:
    """
    Insert a new rubric row (history-preserving).

    Expects a Postgres table:
      profile_scores_bd(public_identifier TEXT, es_id TEXT, rubric JSONB, created_at TIMESTAMP/TIMESTAMPTZ DEFAULT now())

    Note: es_id can be None if the Elasticsearch document ID is not available.
    """
    logger.info(
        "insert_profile_score called: public_identifier=%s, es_id=%s, rubric_keys=%s",
        public_identifier,
        es_id,
        list(rubric_json.keys()) if isinstance(rubric_json, dict) else "not_a_dict",
    )

    async with get_sqlalchemy_session_ai() as session:
        try:
            rubric_json_str = json.dumps(rubric_json, ensure_ascii=False)
            logger.debug(
                "Preparing DB insert: public_identifier=%s, es_id=%s, rubric_length=%d",
                public_identifier,
                es_id,
                len(rubric_json_str),
            )

            result = await session.execute(
                text("""
                    INSERT INTO profile_scores_bd (public_identifier, es_id, rubric)
                    VALUES (:public_identifier, :es_id, CAST(:rubric AS jsonb))
                    """),
                {
                    "public_identifier": public_identifier,
                    "es_id": (
                        es_id if es_id else None
                    ),  # Explicitly set to None if empty
                    "rubric": rubric_json_str,
                },
            )
            logger.info(
                "DB INSERT executed successfully for public_identifier=%s, rowcount=%s",
                public_identifier,
                result.rowcount if hasattr(result, "rowcount") else "unknown",
            )

            await session.commit()
            logger.info(
                "DB transaction committed successfully for public_identifier=%s",
                public_identifier,
            )
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error inserting profile score for public_identifier=%s: %s",
                public_identifier,
                str(e),
            )
            logger.error("DB insertion error traceback:\n%s", traceback.format_exc())
            raise


async def delete_profile_scores(*, public_identifier: str) -> int:
    """
    Delete all profile scores (rubrics) for a given public_identifier from profile_scores_bd table.

    Args:
        public_identifier: The profile public identifier.

    Returns:
        Number of rows deleted.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            query = """
                DELETE FROM profile_scores_bd
                WHERE public_identifier = :public_identifier
            """
            params = {"public_identifier": public_identifier}

            logger.info(
                "Deleting profile scores for public_identifier=%s",
                public_identifier,
            )

            result = await session.execute(text(query), params)
            await session.commit()
            deleted_count = result.rowcount

            logger.info(
                "Successfully deleted %d profile score row(s) for public_identifier=%s",
                deleted_count,
                public_identifier,
            )

            return deleted_count
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error deleting profile scores for public_identifier=%s: %s",
                public_identifier,
                str(e),
            )
            raise


async def fetch_latest_profile_score(
    *, public_identifier: str
) -> Optional[Dict[str, Any]]:
    """
    Fetch most recent rubric row for a given public_identifier.
    Returns a dict with keys: id, public_identifier, es_id, rubric, created_at
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT id, public_identifier, es_id, rubric, created_at
                    FROM profile_scores_bd
                    WHERE public_identifier = :public_identifier
                    ORDER BY created_at DESC
                    LIMIT 1
                    """),
                {"public_identifier": public_identifier},
            )
            row = res.mappings().first()
            return dict(row) if row else None
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error fetching latest profile score for public_identifier=%s: %s",
                public_identifier,
                str(e),
            )
            raise
