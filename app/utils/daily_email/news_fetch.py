"""
Fetch news documents from bd_news Elasticsearch index by news_id or recent by linkedin_identifier.
Used by daily email and related flows.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# bd_news: match filter values against this subfield (keyword) for exact terms queries.
BD_NEWS_LINKEDIN_IDENTIFIER_MATCH_FIELD = "linkedin_identifier.keyword"

# _source fields to request from bd_news (matches news_scoring usage)
BD_NEWS_SOURCE_FIELDS = [
    "news_id",
    "news_title",
    "content",
    "link",
    "published_date",
    "news_type",
    "news_sub_type",
    "author",
    "author_description",
    "source",
    "linkedin_identifier",
]


async def fetch_bd_news_by_news_ids(
    *,
    es_client_bd: Any,
    news_ids: List[str],
    index_name: str = "bd_news",
) -> List[Dict[str, Any]]:
    """
    Fetch news documents from bd_news ES index by a list of news_id values.
    Returns a list of dicts with _source fields (news_id, news_title, content, etc.).
    """
    if not es_client_bd or not news_ids:
        return []

    # Deduplicate and ensure strings (bd_news docs are indexed with _id = news_id)
    ids = list(
        {str(nid).strip() for nid in news_ids if nid is not None and str(nid).strip()}
    )
    if not ids:
        return []

    # Prefer query by document _id (indexing uses _id = news_id)
    body = {
        "query": {"ids": {"values": ids}},
        "size": len(ids),
        "_source": BD_NEWS_SOURCE_FIELDS,
    }
    try:
        res = await es_client_bd.search(index=index_name, body=body)
    except Exception as e:
        logger.warning("Failed to fetch bd_news by ids query: %s", e)
        return []

    hits = res.get("hits", {}).get("hits", []) or []
    out = []
    for h in hits:
        src = h.get("_source")
        if isinstance(src, dict):
            out.append(dict(src))
    return out


# Fields for daily-email company / participant news from bd_news (linkedin_identifier = person or company universal)
BD_NEWS_RECENT_SOURCE_FIELDS = [
    "news_id",
    "news_title",
    "content",
    "link",
    "imagecount",
    "videocount",
    "published_date",
    "published_date_relative",
    "news_type",
    "linkedin_identifier",
    "company_name",
    "entity_type",
]


async def fetch_bd_news_recent_for_linkedin_identifiers(
    *,
    es_client_bd: Any,
    linkedin_identifiers: List[str],
    index_name: str = "bd_news",
    max_hits: int = 80,
    max_age_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch recent bd_news docs for a set of linkedin_identifier values (people public_identifiers
    or company universal names). Matching uses ``linkedin_identifier.keyword`` for exact terms
    (see BD_NEWS_LINKEDIN_IDENTIFIER_MATCH_FIELD).
    """
    if not es_client_bd or not linkedin_identifiers:
        return []

    ids = sorted({str(x).strip() for x in linkedin_identifiers if x and str(x).strip()})
    if not ids:
        return []

    size = min(max_hits, 10000)
    must_filters: List[Dict[str, Any]] = [
        {"terms": {BD_NEWS_LINKEDIN_IDENTIFIER_MATCH_FIELD: ids}},
    ]
    if isinstance(max_age_days, int) and max_age_days > 0:
        gte_ts = int(
            (datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp()
        )
        must_filters.append({"range": {"published_date": {"gte": gte_ts}}})

    body = {
        "query": {"bool": {"must": must_filters}},
        "sort": [{"published_date": {"order": "desc"}}],
        "size": size,
        "_source": BD_NEWS_RECENT_SOURCE_FIELDS,
    }
    try:
        res = await es_client_bd.search(index=index_name, body=body)
    except Exception as e:
        logger.warning("fetch_bd_news_recent_for_linkedin_identifiers failed: %s", e)
        return []

    hits = res.get("hits", {}).get("hits", []) or []
    out: List[Dict[str, Any]] = []
    for h in hits:
        src = h.get("_source")
        if isinstance(src, dict):
            out.append(dict(src))
    return out


async def fetch_latest_bd_news_per_linkedin_identifier(
    *,
    es_client_bd: Any,
    linkedin_identifiers: List[str],
    index_name: str = "bd_news",
    max_hits: int = 500,
    max_age_days: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    For each linkedin_identifier (e.g. company universal name), return the most recent bd_news
    document by published_date. First hit per identifier after global sort wins.
    """
    docs = await fetch_bd_news_recent_for_linkedin_identifiers(
        es_client_bd=es_client_bd,
        linkedin_identifiers=linkedin_identifiers,
        index_name=index_name,
        max_hits=max_hits,
        max_age_days=max_age_days,
    )
    out: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        lid = str(d.get("linkedin_identifier") or "").strip()
        if lid and lid not in out:
            out[lid] = d
    return out
