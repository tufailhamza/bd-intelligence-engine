import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)


async def get_or_create_single_session(
    *,
    entity_urn: str,
    feature_key: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    async with get_sqlalchemy_session_ai() as session:
        try:
            existing = await session.execute(
                text("""
                    SELECT id, entity_urn, feature_key, title, metadata, created_at, updated_at
                    FROM me_chat_sessions_bd
                    WHERE entity_urn = :entity_urn
                      AND feature_key = :feature_key
                    LIMIT 1
                """),
                {
                    "entity_urn": entity_urn,
                    "feature_key": feature_key,
                },
            )
            row = existing.mappings().first()
            if row:
                return dict(row)

            new_id = str(uuid.uuid4())
            await session.execute(
                text("""
                    INSERT INTO me_chat_sessions_bd (
                        id, entity_urn, feature_key, title, metadata
                    ) VALUES (
                        :id, :entity_urn, :feature_key, :title, CAST(:metadata AS jsonb)
                    )
                """),
                {
                    "id": new_id,
                    "entity_urn": entity_urn,
                    "feature_key": feature_key,
                    "title": title,
                    "metadata": json.dumps({}, ensure_ascii=False),
                },
            )
            await session.commit()

            return {
                "id": new_id,
                "entity_urn": entity_urn,
                "feature_key": feature_key,
                "title": title,
                "metadata": {},
            }
        except Exception:
            await session.rollback()
            raise


async def update_chat_session_metadata(
    *,
    session_id: str,
    metadata: Dict[str, Any],
) -> None:
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(
                text("""
                    UPDATE me_chat_sessions_bd
                    SET metadata = CAST(:metadata AS jsonb),
                        updated_at = NOW()
                    WHERE id = :session_id
                """),
                {
                    "session_id": session_id,
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                },
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def insert_chat_message(
    *,
    session_id: str,
    entity_urn: str,
    feature_key: str,
    role: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    async with get_sqlalchemy_session_ai() as session:
        try:
            await session.execute(
                text("""
                    INSERT INTO me_chat_messages_bd (
                        id, session_id, entity_urn, feature_key, role, content, metadata
                    ) VALUES (
                        :id, :session_id, :entity_urn, :feature_key, :role, :content, CAST(:metadata AS jsonb)
                    )
                """),
                {
                    "id": str(uuid.uuid4()),
                    "session_id": session_id,
                    "entity_urn": entity_urn,
                    "feature_key": feature_key,
                    "role": role,
                    "content": content,
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                },
            )
            await session.execute(
                text("""
                    UPDATE me_chat_sessions_bd
                    SET updated_at = NOW()
                    WHERE id = :session_id
                """),
                {"session_id": session_id},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def fetch_recent_chat_messages(
    *,
    session_id: str,
    limit: int = 20,
    assignment_feature_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    async with get_sqlalchemy_session_ai() as session:
        try:
            params = {"session_id": session_id, "limit": int(limit)}
            assignment_filter = ""
            if assignment_feature_key:
                assignment_filter = """
                    AND metadata->>'assignment_feature_key' = :assignment_feature_key
                """
                params["assignment_feature_key"] = assignment_feature_key

            res = await session.execute(
                text(f"""
                    SELECT role, content, created_at
                    FROM me_chat_messages_bd
                    WHERE session_id = :session_id
                    {assignment_filter}
                    ORDER BY created_at DESC
                    LIMIT :limit
                """),
                params,
            )
            rows = res.mappings().all()
            # Return chronological order for prompt building.
            return list(reversed([dict(r) for r in rows]))
        except Exception:
            logger.exception(
                "Failed to fetch recent messages for session_id=%s",
                session_id,
            )
            return []


async def fetch_assignment_contexts(
    *,
    entity_urn: str,
    feature_key_prefix: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT id, entity_urn, feature_key, assignment_draft, completion_score, status, last_user_message, created_at, updated_at
                    FROM me_assignment_context_bd
                    WHERE entity_urn = :entity_urn
                      AND (
                        feature_key = :feature_key_prefix
                        OR feature_key LIKE :feature_key_like
                      )
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {
                    "entity_urn": entity_urn,
                    "feature_key_prefix": feature_key_prefix,
                    "feature_key_like": f"{feature_key_prefix}:%",
                    "limit": int(limit),
                },
            )
            return [dict(row) for row in res.mappings().all()]
        except Exception:
            logger.exception(
                "Failed to fetch assignment contexts for entity_urn=%s feature_key_prefix=%s",
                entity_urn,
                feature_key_prefix,
            )
            return []


async def fetch_assignment_context(
    *,
    entity_urn: str,
    feature_key: str,
) -> Optional[Dict[str, Any]]:
    """
    Legacy helper: fetch a single context by (entity_urn, feature_key).
    Kept for backwards compatibility with older flows that keyed contexts
    by feature_key patterns (e.g. "assignment_creation:...").
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT id, entity_urn, feature_key, assignment_draft, completion_score, status, last_user_message, created_at, updated_at
                    FROM me_assignment_context_bd
                    WHERE entity_urn = :entity_urn
                      AND feature_key = :feature_key
                    LIMIT 1
                """),
                {
                    "entity_urn": entity_urn,
                    "feature_key": feature_key,
                },
            )
            row = res.mappings().first()
            return dict(row) if row else None
        except Exception:
            logger.exception(
                "Failed to fetch assignment context for entity_urn=%s feature_key=%s",
                entity_urn,
                feature_key,
            )
            return None


async def fetch_assignment_context_by_id(
    *,
    entity_urn: str,
    context_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch a single context row by external context id (stored in id column).
    This is the preferred path for new assignment/content chat flows that
    receive an entityId from the frontend.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT id, entity_urn, feature_key, assignment_draft, completion_score, status, last_user_message, created_at, updated_at
                    FROM me_assignment_context_bd
                    WHERE entity_urn = :entity_urn
                      AND id = :id
                    LIMIT 1
                """),
                {
                    "entity_urn": entity_urn,
                    "id": context_id,
                },
            )
            row = res.mappings().first()
            return dict(row) if row else None
        except Exception:
            logger.exception(
                "Failed to fetch assignment context for entity_urn=%s id=%s",
                entity_urn,
                context_id,
            )
            return None


async def fetch_assignment_context_by_assignment_id(
    assignment_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch a context row by external assignment id (me_assignment_context_bd.id).
    Id is globally unique (upsert ON CONFLICT (id)).
    """
    assignment_id = (assignment_id or "").strip()
    if not assignment_id:
        return None

    async with get_sqlalchemy_session_ai() as session:
        try:
            res = await session.execute(
                text("""
                    SELECT id, entity_urn, feature_key, assignment_draft, completion_score, status, last_user_message, created_at, updated_at
                    FROM me_assignment_context_bd
                    WHERE id = :id
                    LIMIT 1
                """),
                {"id": assignment_id},
            )
            row = res.mappings().first()
            return dict(row) if row else None
        except Exception:
            logger.exception(
                "Failed to fetch assignment context for assignment_id=%s",
                assignment_id,
            )
            return None


async def upsert_assignment_context(
    *,
    entity_urn: str,
    feature_key: str,
    assignment_draft: Dict[str, Any],
    completion_score: float,
    last_user_message: Optional[str],
    context_id: str,
) -> Dict[str, Any]:
    """
    Upsert a context row.

    - id: external context identifier (frontend entity_id).
    - feature_key: high-level feature label, such as "assignment_creation" or "content_chat".
    - ON CONFLICT is keyed on id so multiple contexts can share the same feature_key.
    """
    async with get_sqlalchemy_session_ai() as session:
        try:
            ctx_id = str(context_id or "").strip()
            if not ctx_id:
                raise ValueError(
                    "context_id is required for upsert_assignment_context; backend ID generation is disabled."
                )
            await session.execute(
                text("""
                    INSERT INTO me_assignment_context_bd (
                        id, entity_urn, feature_key, assignment_draft, completion_score, status, last_user_message
                    ) VALUES (
                        :id, :entity_urn, :feature_key, CAST(:assignment_draft AS jsonb), :completion_score, :status, :last_user_message
                    )
                    ON CONFLICT (id)
                    DO UPDATE SET
                        assignment_draft = EXCLUDED.assignment_draft,
                        completion_score = EXCLUDED.completion_score,
                        status = EXCLUDED.status,
                        last_user_message = EXCLUDED.last_user_message,
                        updated_at = NOW()
                """),
                {
                    "id": ctx_id,
                    "entity_urn": entity_urn,
                    "feature_key": feature_key,
                    "assignment_draft": json.dumps(
                        assignment_draft or {}, ensure_ascii=False
                    ),
                    "completion_score": float(completion_score),
                    "status": (
                        "ready_to_save"
                        if float(completion_score) >= 1.0
                        else "in_progress"
                    ),
                    "last_user_message": last_user_message,
                },
            )
            await session.commit()
            return {
                "entity_urn": entity_urn,
                "feature_key": feature_key,
                "assignment_draft": assignment_draft,
                "completion_score": float(completion_score),
                "status": (
                    "ready_to_save" if float(completion_score) >= 1.0 else "in_progress"
                ),
                "last_user_message": last_user_message,
            }
        except Exception:
            await session.rollback()
            raise
