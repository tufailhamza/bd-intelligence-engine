import json
import logging
from typing import Any, Dict, Optional

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)


async def fetch_profile_news_semantics(
    *,
    public_identifier: str,
    profile_fingerprint: str,
    rubric_version: str,
    semantic_model: str,
) -> Optional[Dict[str, Any]]:
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT
                        public_identifier,
                        profile_es_id,
                        profile_fingerprint,
                        rubric_version,
                        semantic_query_text,
                        semantic_keywords,
                        event_type_weights,
                        semantic_embedding,
                        semantic_model
                    FROM profile_news_semantics_bd
                    WHERE public_identifier = :public_identifier
                      AND profile_fingerprint = :profile_fingerprint
                      AND rubric_version = :rubric_version
                      AND semantic_model = :semantic_model
                    LIMIT 1
                    """),
                {
                    "public_identifier": public_identifier,
                    "profile_fingerprint": profile_fingerprint,
                    "rubric_version": rubric_version,
                    "semantic_model": semantic_model,
                },
            )
            row = res.mappings().first()
            return dict(row) if row else None
        except Exception as exc:
            await session.rollback()
            logger.error(
                "Error fetching profile news semantics for %s: %s",
                public_identifier,
                exc,
            )
            raise


async def upsert_profile_news_semantics(
    *,
    public_identifier: str,
    profile_es_id: Optional[str],
    profile_fingerprint: str,
    rubric_version: str,
    semantic_model: str,
    semantics: Dict[str, Any],
) -> Dict[str, Any]:
    semantic_keywords = semantics.get("semantic_keywords") or []
    event_type_weights = semantics.get("event_type_weights") or {}
    semantic_embedding = semantics.get("semantic_embedding") or []
    semantic_query_text = str(semantics.get("semantic_query_text") or "")

    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    INSERT INTO profile_news_semantics_bd (
                        public_identifier,
                        profile_es_id,
                        profile_fingerprint,
                        rubric_version,
                        semantic_query_text,
                        semantic_keywords,
                        event_type_weights,
                        semantic_embedding,
                        semantic_model,
                        rubric
                    )
                    VALUES (
                        :public_identifier,
                        :profile_es_id,
                        :profile_fingerprint,
                        :rubric_version,
                        :semantic_query_text,
                        CAST(:semantic_keywords AS jsonb),
                        CAST(:event_type_weights AS jsonb),
                        CAST(:semantic_embedding AS jsonb),
                        :semantic_model,
                        CAST(:rubric AS jsonb)
                    )
                    ON CONFLICT (
                        public_identifier,
                        profile_fingerprint,
                        rubric_version,
                        semantic_model
                    )
                    DO UPDATE SET
                        profile_es_id = EXCLUDED.profile_es_id,
                        semantic_query_text = EXCLUDED.semantic_query_text,
                        semantic_keywords = EXCLUDED.semantic_keywords,
                        event_type_weights = EXCLUDED.event_type_weights,
                        semantic_embedding = EXCLUDED.semantic_embedding,
                        rubric = EXCLUDED.rubric,
                        updated_at = now()
                    RETURNING id
                    """),
                {
                    "public_identifier": public_identifier,
                    "profile_es_id": profile_es_id,
                    "profile_fingerprint": profile_fingerprint,
                    "rubric_version": rubric_version,
                    "semantic_query_text": semantic_query_text,
                    "semantic_keywords": json.dumps(semantic_keywords),
                    "event_type_weights": json.dumps(event_type_weights),
                    "semantic_embedding": json.dumps(semantic_embedding),
                    "semantic_model": semantic_model,
                    "rubric": json.dumps(semantics),
                },
            )
            row = res.mappings().first()
            await session.commit()
            return dict(row) if row else {}
        except Exception as exc:
            await session.rollback()
            logger.error(
                "Error upserting profile news semantics for %s: %s",
                public_identifier,
                exc,
            )
            raise
