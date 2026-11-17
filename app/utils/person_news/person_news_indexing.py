import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk

from app.utils.news_semantics import enrich_news_docs_with_semantics

logger = logging.getLogger(__name__)


def _to_epoch_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        # Accept ISO strings like "2025-07-07T11:00:00Z" or "2025-07-07 11:00:00"
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None
    return None


def build_bd_news_docs_from_processed_posts(
    *,
    user_id: str,
    entity_urn: str,
    entity_id: str,
    processed_posts: List[Dict[str, Any]],
    last_author_name: str = "",
    last_author_description: str = "",
    expiration_days: int = 60,
) -> List[Dict[str, Any]]:
    """
    Convert `process_linkedin_posts()` output into ES documents for the `bd_news` index.
    Uses `activityUrn` as stable identifiers.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    docs: List[Dict[str, Any]] = []

    for post in processed_posts:
        news_id = post.get("activityUrn")
        if not news_id:
            continue

        published_date = _to_epoch_seconds(post.get("postDateISO")) or now_ts

        doc = {
            # Use same id as news_id for stability (idempotent indexing)
            "id": str(news_id),
            "news_id": str(news_id),
            "linkedin_identifier": user_id,
            "entity_urn": entity_urn,
            "entity_id": entity_id,
            "entity_type": "person_linkedin",
            "author": post.get("author", ""),
            "author_description": post.get("author_description", ""),
            "last_author_name": last_author_name or "",
            "last_author_description": last_author_description or "",
            "news_title": post.get("newsTitle", ""),
            "content": post.get("postText", ""),
            "link": post.get("postURL", ""),
            "source": "LinkedIn",
            "published_date": published_date,
            "published_date_relative": post.get("postDateRelative", ""),
            "post_type": (post.get("postType") or "ORIGINAL").upper(),
            "news_type": post.get("newsType", ""),
            "news_sub_type": (
                "company_linkedin_repost"
                if post.get("newsType") == "company_news"
                else ""
            ),
            "cache_date": now_ts,
            "expiration_days": expiration_days,
            "original_author_linkedin_identifier": post.get(
                "originalAuthorLinkedinIdentifier", ""
            ),
            "original_author_entity_id": post.get("originalAuthorEntityId", ""),
            "original_author_entity_urn": post.get("originalAuthorEntityUrn", ""),
            "repost_original_entity_type": post.get("repostOriginalEntityType", ""),
            "imagecount": post.get("imageCount", 0),
            "videocount": post.get("videoCount", 0),
        }

        docs.append(doc)

    return docs


async def bulk_index_bd_news(
    *,
    es_client_bd: AsyncElasticsearch,
    docs: List[Dict[str, Any]],
    index_name: str = "bd_news",
) -> Dict[str, Any]:
    """
    Bulk index docs into BD Elasticsearch.
    Uses doc["id"] as _id for idempotency.
    """
    if not docs:
        return {"indexed": 0}

    # docs = await enrich_news_docs_with_semantics(docs)

    actions = [
        {
            "_op_type": "index",
            "_index": index_name,
            "_id": doc.get("id"),
            "_source": doc,
        }
        for doc in docs
    ]

    success, errors = await async_bulk(es_client_bd, actions, raise_on_error=False)
    if errors:
        logger.error("Bulk indexing had errors (showing first 3): %s", errors[:3])

    return {"indexed": int(success), "errors": errors}
