import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import ulid

logger = logging.getLogger(__name__)


def _to_epoch_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None
    return None


def build_bd_news_docs_from_company_items(
    *,
    company_universal_name: str,
    entity_urn: Optional[str],
    entity_id: Optional[str],
    company_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Convert `company_news()` db_items into ES documents for the `bd_news` index.
    Uses `news_id` (uuid) as stable identifier.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    docs: List[Dict[str, Any]] = []

    for item in company_items:
        if not isinstance(item, dict):
            continue
        news_id = item.get("news_id")
        if not news_id:
            continue

        # Match existing ES company doc shape:
        # - `id` is a ULID-like identifier (and is used as ES _id)
        # - `news_id` remains the UUID for this news item
        doc_id = ulid.new().str
        published_date = _to_epoch_seconds(item.get("published_date")) or now_ts

        doc = {
            "id": doc_id,
            "news_id": str(news_id),
            # For company news, we store the company universal name in linkedin_identifier
            "linkedin_identifier": company_universal_name,
            "entity_id": entity_id,
            "entity_type": "company_search",
            "company_name": item.get("company_name") or "",
            "news_title": item.get("news_title") or "",
            "content": item.get("content") or "",
            "link": item.get("link") or "",
            "source": item.get("source") or "",
            "published_date": published_date,
            "news_type": item.get("news_type") or "",
            "news_sub_type": item.get("news_sub_type") or "",
            "cache_date": int(item.get("cache_date") or now_ts),
            "expiration_days": int(item.get("expiration_days") or 1),
        }

        docs.append(doc)

    return docs
