import logging
import json
import math
import os
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from elasticsearch import NotFoundError
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.database.profile_news_semantics import (
    fetch_profile_news_semantics,
    upsert_profile_news_semantics,
)
from app.models.schemas.news_fetching import (
    FetchNewsInput,
    FetchNewsOutput,
    FetchNewsV2Connection,
    FetchNewsV2Input,
    NewsItem,
    NEWS_CATEGORY_MAPPING,
    UpdateNewsSeenInput,
    UpdateNewsSeenOutput,
)
from app.database.news_fetching import (
    fetch_all_seen_news_ids,
    fetch_news_by_public_identifier,
    update_ranked_news_seen,
)
from app.routes.dependancies import get_es_client_bd, get_es_client_with_compression
from app.utils.news_semantics import (
    EVENT_PRIORITY_BY_LABEL,
    NEWS_SEMANTIC_DIMENSIONS,
    NEWS_SEMANTIC_MODEL,
    PROFILE_RUBRIC_VERSION,
    build_profile_semantics,
    profile_fingerprint,
)
from app.utils.profile_fetching.public_identifier_fetch import (
    fetch_profile_by_public_identifier,
)
from app.routes.news_scoring import _format_relative_time, _normalize_epoch_seconds

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_CONSECUTIVE_AUTHOR = 2
DIVERSITY_OVERFETCH_FACTOR = 2
CURSOR_BUFFER_MAX = 100
RECENCY_DECAY_DAYS = 21
PIT_KEEP_ALIVE = "10m"
CURSOR_VERSION = 2

PRIORITY_LABEL_SCORES = {
    "HIGH": 1.0,
    "MEDIUM": 0.65,
    "LOW": 0.35,
}
RELATIONSHIP_LABEL_SCORES = {
    "STRONG": 1.0,
    "MODERATE": 0.65,
    "WEAK": 0.35,
}


def _scoring_weights() -> Dict[str, float]:
    recency = 0.50
    personalized_event = 0.17
    semantic = 0.08
    connection = 0.25
    total = recency + semantic + personalized_event + connection

    return {
        "recency": recency / total,
        "semantic": semantic / total,
        "personalized_event": personalized_event / total,
        "connection": connection / total,
    }


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _normalize_label(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().upper()


def _connection_rank_components(
    connection: FetchNewsV2Connection,
    *,
    max_raw_score: float,
) -> Dict[str, float]:
    raw_score = connection.score if connection.score is not None else 0.0
    score_denominator = max(100.0, max_raw_score)
    score_signal = _clamp01(
        raw_score / score_denominator,
        default=0.5,
    )
    priority_signal = PRIORITY_LABEL_SCORES.get(
        _normalize_label(connection.priority_label),
        0.5,
    )
    relationship_signal = RELATIONSHIP_LABEL_SCORES.get(
        _normalize_label(connection.relationship_label),
        0.5,
    )
    connection_score = _clamp01(
        (0.30 * score_signal) + (0.50 * priority_signal) + (0.20 * relationship_signal),
        default=0.5,
    )
    return {
        "raw_score": float(raw_score),
        "profile_score": score_signal,
        "priority_score": priority_signal,
        "relationship_score": relationship_signal,
        "connection_score": connection_score,
    }


def _connection_identity_values(connection: FetchNewsV2Connection) -> List[str]:
    values = [
        connection.public_identifier,
        connection.entity_urn,
        connection.profile_id,
    ]
    return list(
        dict.fromkeys(
            str(value).strip() for value in values if value and str(value).strip()
        )
    )


def _public_identifiers_from_v2_connections(
    connections: List[FetchNewsV2Connection],
) -> List[str]:
    return list(
        dict.fromkeys(
            str(connection.public_identifier).strip()
            for connection in connections
            if connection.public_identifier
            and str(connection.public_identifier).strip()
        )
    )


def _build_v2_connection_context(
    connections: List[FetchNewsV2Connection],
    company_universal_names_map: Dict[str, str],
) -> Tuple[List[str], Dict[str, float], Dict[str, Dict[str, Any]]]:
    identifiers: List[str] = []
    connection_scores: Dict[str, float] = {}
    connection_metadata: Dict[str, Dict[str, Any]] = {}
    raw_scores = [
        float(connection.score)
        for connection in connections
        if connection.score is not None
    ]
    max_raw_score = max(raw_scores) if raw_scores else 100.0

    for connection in connections:
        components = _connection_rank_components(
            connection,
            max_raw_score=max_raw_score,
        )
        metadata = {
            "public_identifier": connection.public_identifier,
            "entity_urn": connection.entity_urn,
            "profile_id": connection.profile_id,
            "score": connection.score,
            "priority_label": _normalize_label(connection.priority_label) or None,
            "relationship_label": _normalize_label(connection.relationship_label)
            or None,
            "connection_raw_score": components["raw_score"],
            "connection_profile_score": components["profile_score"],
            "connection_priority_score": components["priority_score"],
            "connection_relationship_score": components["relationship_score"],
            "connection_score": components["connection_score"],
        }

        candidate_identifiers = _connection_identity_values(connection)
        if connection.public_identifier:
            company_name = company_universal_names_map.get(connection.public_identifier)
            if company_name:
                candidate_identifiers.append(company_name)

        for identifier in dict.fromkeys(candidate_identifiers):
            identifiers.append(identifier)
            existing_score = connection_scores.get(identifier)
            if (
                existing_score is None
                or components["connection_score"] > existing_score
            ):
                connection_scores[identifier] = components["connection_score"]
                connection_metadata[identifier] = metadata

    return list(dict.fromkeys(identifiers)), connection_scores, connection_metadata


def _get_author_identity(item: dict) -> str:
    """
    Extract author identity from a news item dict.
    Uses author field first, falls back to connection_public_identifier if author is missing/empty.

    Args:
        item: News item dict

    Returns:
        Author identity string (author field or connection_public_identifier)
    """
    author = item.get("author") or ""
    if author and isinstance(author, str) and author.strip():
        return author.strip()
    # Fallback to connection_public_identifier
    return item.get("connection_public_identifier", "") or ""


def _hit_news_id(hit: Dict[str, Any]) -> str:
    src = hit.get("_source") or {}
    return str(src.get("news_id") or hit.get("_id") or "")


def _hit_author_identity(hit: Dict[str, Any]) -> str:
    src = hit.get("_source") or {}
    author = src.get("author") or ""
    if isinstance(author, str) and author.strip():
        return author.strip()
    identifier = src.get("linkedin_identifier") or ""
    return str(identifier).strip()


def _compact_hit_for_cursor(hit: Dict[str, Any]) -> Dict[str, Any]:
    src = hit.get("_source") or {}
    return {
        "_score": hit.get("_score"),
        "_source": {
            "news_id": src.get("news_id"),
            "linkedin_identifier": src.get("linkedin_identifier"),
            "published_date": src.get("published_date"),
            "event_priority_score": src.get("event_priority_score"),
            "event_priority_label": src.get("event_priority_label"),
            "news_type": src.get("news_type"),
            "news_sub_type": src.get("news_sub_type"),
            "author": src.get("author"),
        },
        "fields": hit.get("fields") or {},
        "sort": hit.get("sort"),
    }


def _dedupe_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for hit in hits:
        news_id = _hit_news_id(hit)
        if not news_id or news_id in seen:
            continue
        seen.add(news_id)
        out.append(hit)
    return out


def _would_exceed_author_streak(
    recent_authors: List[str],
    author: str,
    max_consecutive: int,
) -> bool:
    if not author or max_consecutive < 1:
        return False
    if len(recent_authors) < max_consecutive:
        return False
    return all(prev == author for prev in recent_authors[-max_consecutive:])


def _select_diverse_hits(
    candidates: List[Dict[str, Any]],
    *,
    limit: int,
    recent_authors: Optional[List[str]],
    max_consecutive: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """
    Pick a page from already-ranked candidates while avoiding author floods.

    The input order remains the primary rank signal. We only skip ahead within the
    overfetched window when doing so breaks a same-author streak. If there is no
    alternative author in the window, we allow the repeat so low-variety feeds
    still fill.
    """
    selected: List[Dict[str, Any]] = []
    remaining = list(candidates)
    author_tail = [
        str(author).strip() for author in (recent_authors or []) if str(author).strip()
    ][-max_consecutive:]

    while remaining and len(selected) < limit:
        pick_index = None
        for idx, hit in enumerate(remaining):
            author = _hit_author_identity(hit)
            if not _would_exceed_author_streak(
                author_tail,
                author,
                max_consecutive,
            ):
                pick_index = idx
                break

        if pick_index is None:
            pick_index = 0

        hit = remaining.pop(pick_index)
        selected.append(hit)
        author = _hit_author_identity(hit)
        if author:
            author_tail.append(author)
            author_tail = author_tail[-max_consecutive:]

    return selected, remaining, author_tail


def _prevent_consecutive_authors(
    news_items: List[NewsItem], max_consecutive: int = 3
) -> List[NewsItem]:
    """
    Shuffle news items to prevent more than max_consecutive consecutive items from the same author.
    Uses the author field first, falling back to connection_public_identifier when author is missing/empty.
    Only affects the order of items in the response, not the database.

    Args:
        news_items: List of news items to reorder
        max_consecutive: Maximum allowed consecutive items from same author (default: 3, must be >= 1)

    Returns:
        Reordered list with no more than max_consecutive consecutive items from same author

    Raises:
        ValueError: If max_consecutive is less than 1
    """
    # Input validation: max_consecutive must be >= 1 (guard before any slicing or indexing)
    if max_consecutive < 1:
        raise ValueError("max_consecutive must be >= 1")

    if len(news_items) <= max_consecutive:
        return news_items

    # Convert to list of dicts for easier manipulation
    items = [item.dict() if isinstance(item, NewsItem) else item for item in news_items]

    # Check if we have consecutive sequences
    has_consecutive = False
    for i in range(len(items) - max_consecutive + 1):
        authors = [
            _get_author_identity(items[j]) for j in range(i, i + max_consecutive)
        ]
        if len(set(authors)) == 1 and authors[0]:  # All same author
            has_consecutive = True
            break

    if not has_consecutive:
        return news_items

    # Reorder to break consecutive sequences
    reordered: List[dict] = []
    remaining = items.copy()

    while remaining:
        if not reordered:
            # Add first item
            reordered.append(remaining.pop(0))
            continue

        # Check if last max_consecutive items are all from the same author
        if len(reordered) >= max_consecutive:
            last_authors = [
                _get_author_identity(reordered[j])
                for j in range(len(reordered) - max_consecutive, len(reordered))
            ]
            # If all last max_consecutive items have the same author, we need to break it
            if len(set(last_authors)) == 1 and last_authors[0]:
                # Find an item with a different author
                found_different = False
                for i, item in enumerate(remaining):
                    item_author = _get_author_identity(item)
                    if item_author != last_authors[0] and item_author:
                        reordered.append(remaining.pop(i))
                        found_different = True
                        break

                # If all remaining items have the same author, just add them
                if not found_different:
                    reordered.append(remaining.pop(0))
            else:
                # No consecutive sequence, add next item
                reordered.append(remaining.pop(0))
        else:
            # Not enough items yet, add next item
            reordered.append(remaining.pop(0))

    # Convert back to NewsItem objects
    return [NewsItem(**item) for item in reordered]


async def _fetch_company_universal_names_for_connections(
    connection_identifiers: List[str],
    es_client_compressed,
) -> Dict[str, str]:
    """
    Fetch company_universal_names for given connection_identifiers from Elasticsearch.
    Returns a dict mapping connection_public_identifier -> company_universal_name.

    Args:
        connection_identifiers: List of connection public_identifiers
        es_client_compressed: Elasticsearch client

    Returns:
        Dict mapping connection_public_identifier to company_universal_name
    """
    if not connection_identifiers:
        return {}

    company_universal_names: Dict[str, str] = {}

    try:
        people_index = (os.getenv("ES_PROFILES_INDEX") or "people").strip()

        # Query Elasticsearch for profiles
        body = {
            "query": {
                "bool": {
                    "should": [
                        {"terms": {"public_identifier": connection_identifiers}},
                        {
                            "terms": {
                                "public_identifier.keyword": connection_identifiers
                            }
                        },
                        {"terms": {"publicIdentifier": connection_identifiers}},
                        {"terms": {"publicIdentifier.keyword": connection_identifiers}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "_source": ["public_identifier", "publicIdentifier", "experience"],
            "size": len(connection_identifiers),
        }

        res = await es_client_compressed.search(index=people_index, body=body)
        hits = res.get("hits", {}).get("hits", []) or []

        # Initialize with empty for all connections
        for pid in connection_identifiers:
            company_universal_names[pid] = ""

        for h in hits:
            src = h.get("_source") or {}
            if not isinstance(src, dict):
                continue
            pid = src.get("public_identifier") or src.get("publicIdentifier")
            if not isinstance(pid, str) or not pid:
                continue

            company_universal = ""
            exp = src.get("experience")
            if isinstance(exp, list) and exp and isinstance(exp[0], dict):
                e0 = exp[0]
                company_universal = (
                    e0.get("company_universal_name")
                    or e0.get("company_universalname")
                    or e0.get("companyUniversalName")
                    or e0.get("companyUniversalname")
                    or ""
                )

            if isinstance(company_universal, str) and company_universal:
                company_universal_names[pid] = company_universal
    except Exception as e:
        # Log error but don't fail - company news is optional
        logger.warning(
            "Failed to fetch company_universal_names for connections: %s",
            str(e),
        )
        company_universal_names = {}

    return company_universal_names


def _get_news_filters_from_categories(
    categories: List[str],
) -> Tuple[List[str], List[str]]:
    """
    Convert UI category filters to news_type and news_sub_type values.
    All values from categories are searched in BOTH news_type AND news_sub_type fields.

    Args:
        categories: List of category names (e.g., ["company_posts", "personal_posts"])

    Returns:
        Tuple of (news_types, news_sub_types) lists to filter by.
        Both lists contain the same values, which will be searched in both fields.
    """
    all_values = []

    for category in categories:
        if category in NEWS_CATEGORY_MAPPING:
            category_values = NEWS_CATEGORY_MAPPING[category]
            if isinstance(category_values, list):
                all_values.extend(category_values)
            elif isinstance(category_values, dict):
                # Backward compatibility: handle old dict format
                if category_values.get("news_types"):
                    all_values.extend(category_values["news_types"])
                if category_values.get("news_sub_types"):
                    all_values.extend(category_values["news_sub_types"])

    # Remove duplicates while preserving order
    unique_values = list(dict.fromkeys(all_values))

    # Return the same list for both news_types and news_sub_types
    # The database query will search both fields with OR logic
    return (unique_values, unique_values)


def _coerce_json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return _coerce_json_value(json.loads(value), default)
        except Exception:
            return default
    return value


def _valid_semantic_embedding(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != NEWS_SEMANTIC_DIMENSIONS:
        return False
    try:
        for item in value:
            float(item)
    except (TypeError, ValueError):
        return False
    return True


async def _get_or_create_profile_semantics(
    *,
    public_identifier: str,
    profile_es_id: Optional[str],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    fingerprint = profile_fingerprint(profile)
    cached = await fetch_profile_news_semantics(
        public_identifier=public_identifier,
        profile_fingerprint=fingerprint,
        rubric_version=PROFILE_RUBRIC_VERSION,
        semantic_model=NEWS_SEMANTIC_MODEL,
    )
    if cached:
        embedding = _coerce_json_value(cached.get("semantic_embedding"), [])
        if _valid_semantic_embedding(embedding):
            return {
                "semantic_query_text": cached.get("semantic_query_text") or "",
                "semantic_keywords": _coerce_json_value(
                    cached.get("semantic_keywords"), []
                ),
                "event_type_weights": _coerce_json_value(
                    cached.get("event_type_weights"), {}
                ),
                "semantic_embedding": embedding,
                "semantic_model": cached.get("semantic_model") or NEWS_SEMANTIC_MODEL,
                "rubric_version": cached.get("rubric_version")
                or PROFILE_RUBRIC_VERSION,
                "profile_fingerprint": fingerprint,
                "_from_cache": True,
            }

    semantics = await build_profile_semantics(profile)
    if not _valid_semantic_embedding(semantics.get("semantic_embedding")):
        raise ValueError(
            f"Generated profile semantic embedding has invalid dimensions; expected {NEWS_SEMANTIC_DIMENSIONS}"
        )
    await upsert_profile_news_semantics(
        public_identifier=public_identifier,
        profile_es_id=profile_es_id,
        profile_fingerprint=fingerprint,
        rubric_version=PROFILE_RUBRIC_VERSION,
        semantic_model=NEWS_SEMANTIC_MODEL,
        semantics=semantics,
    )
    semantics["profile_fingerprint"] = fingerprint
    semantics["_from_cache"] = False
    return semantics


def _event_score_functions(
    profile_semantics: Dict[str, Any],
) -> List[Dict[str, Any]]:
    weights = _scoring_weights()
    user_event_weights = profile_semantics.get("event_type_weights") or {}
    functions: List[Dict[str, Any]] = []

    for label, base_score in EVENT_PRIORITY_BY_LABEL.items():
        try:
            event_weight = float(user_event_weights.get(label, 1.0))
        except (TypeError, ValueError):
            event_weight = 1.0
        event_score = max(0.0, min(1.0, float(base_score) * event_weight))
        functions.append(
            {
                "filter": {
                    "bool": {
                        "should": [
                            {"term": {"event_priority_label.keyword": label}},
                            {"term": {"news_sub_type.keyword": label}},
                            {"term": {"news_type.keyword": label}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                "weight": weights["personalized_event"] * event_score,
            }
        )

    return functions


def _connection_score_functions(
    connection_scores: Optional[Dict[str, float]],
) -> List[Dict[str, Any]]:
    weights = _scoring_weights()
    functions: List[Dict[str, Any]] = []
    for identifier, score in (connection_scores or {}).items():
        identifier = str(identifier).strip()
        if not identifier:
            continue
        functions.append(
            {
                "filter": {"term": {"linkedin_identifier.keyword": identifier}},
                "weight": weights["connection"] * _clamp01(score, default=0.5),
            }
        )
    return functions


def _recency_score_functions(now_epoch: int) -> List[Dict[str, Any]]:
    weights = _scoring_weights()
    return [
        {
            "exp": {
                "published_date": {
                    "origin": int(now_epoch),
                    "scale": f"{RECENCY_DECAY_DAYS * 86400}ms",
                    "offset": f"{RECENCY_DECAY_DAYS * 86400}ms",
                    "decay": 0.367879,
                }
            },
            "weight": weights["recency"],
        },
    ]


def _build_news_query(
    *,
    identifiers: List[str],
    news_types: Optional[List[str]],
    news_sub_types: Optional[List[str]],
    search_query: Optional[str],
    profile_semantics: Dict[str, Any],
    connection_scores: Optional[Dict[str, float]],
    seen_news_ids: Optional[Dict[str, bool]],
    now_epoch: int,
    limit: int,
    pit_id: str,
    search_after: Optional[List[Any]],
    use_semantic: bool,
) -> Dict[str, Any]:
    filters: List[Dict[str, Any]] = [
        {"terms": {"linkedin_identifier.keyword": identifiers}}
    ]
    must: List[Dict[str, Any]] = []
    if use_semantic:
        filters.append({"exists": {"field": "semantic_embedding"}})

    filter_values = list(dict.fromkeys((news_types or []) + (news_sub_types or [])))
    if filter_values:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"terms": {"news_type.keyword": filter_values}},
                        {"terms": {"news_sub_type.keyword": filter_values}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    if search_query:
        must.append(
            {
                "multi_match": {
                    "query": search_query,
                    "fields": ["news_title^3", "content", "company_name^2", "author"],
                    "type": "best_fields",
                }
            }
        )

    weights = _scoring_weights()
    bool_query: Dict[str, Any] = {"filter": filters}
    if must:
        bool_query["must"] = must
    if use_semantic:
        bool_query.setdefault("should", []).append(
            {
                "knn": {
                    "field": "semantic_embedding",
                    "query_vector": profile_semantics.get("semantic_embedding") or [],
                    "num_candidates": min(
                        10000,
                        max(1000, int(limit) * 200, len(identifiers) * 100),
                    ),
                    "filter": filters,
                    "boost": weights["semantic"],
                }
            }
        )

    scoring_functions: List[Dict[str, Any]] = [
        *_recency_score_functions(now_epoch),
        *_event_score_functions(profile_semantics),
        *_connection_score_functions(connection_scores),
    ]

    query: Dict[str, Any] = {
        "function_score": {
            "query": {"bool": bool_query},
            "functions": scoring_functions,
            "score_mode": "sum",
            "boost_mode": "sum",
        }
    }
    if seen_news_ids:
        query = {
            "function_score": {
                "query": query,
                "functions": [
                    {
                        "filter": {
                            "terms": {
                                "news_id.keyword": (
                                    list(seen_news_ids.keys())
                                    if isinstance(seen_news_ids, dict)
                                    else list(seen_news_ids)
                                )
                            }
                        },
                        "weight": 0.01,
                    }
                ],
                "score_mode": "multiply",
                "boost_mode": "multiply",
            }
        }

    body: Dict[str, Any] = {
        "pit": {"id": pit_id, "keep_alive": PIT_KEEP_ALIVE},
        "query": query,
        "size": int(limit),
        "track_total_hits": 1000,
        "_source": [
            "news_id",
            "linkedin_identifier",
            "author",
            "published_date",
            "event_priority_score",
            "event_priority_label",
            "news_type",
            "news_sub_type",
        ],
        "sort": [
            {"_score": {"order": "desc"}},
            {"published_date": {"order": "desc", "unmapped_type": "date"}},
            {
                "news_id.keyword": {
                    "order": "asc",
                    "unmapped_type": "keyword",
                    "missing": "_last",
                }
            },
        ],
    }
    if search_after:
        body["search_after"] = search_after
    return body


def _es_total_count(res: Dict[str, Any]) -> int:
    total = (res.get("hits") or {}).get("total")
    if isinstance(total, dict):
        return int(total.get("value") or 0)
    try:
        return int(total or 0)
    except Exception:
        return 0


def _is_missing_search_context_error(exc: Exception) -> bool:
    if not isinstance(exc, NotFoundError):
        return False
    text = str(exc)
    return (
        "No search context found" in text or "search_context_missing_exception" in text
    )


def _valid_search_after(value: Any) -> Optional[List[Any]]:
    if not isinstance(value, list) or len(value) != 3:
        return None
    if any(item is None for item in value):
        return None
    return value


@router.post("/fetch_news", response_model=FetchNewsOutput)
async def fetch_news(
    request: FetchNewsInput,
    es_client_compressed=Depends(get_es_client_with_compression),
):
    """
    Fetch paginated news items for a public_identifier with optional category filtering, connection filtering, and text search.

    Categories:
    - company_posts: Company posts from LinkedIn and search
    - company_financial_releases: Financial news and reports
    - company_news_releases: Official company announcements
    - general_company_news: General company news
    - personal_posts: Personal posts and updates

    Filters:
    - connection_identifiers: Required list of connection public_identifiers to filter news for specific connections. If not provided or empty, returns empty results. Can provide single or multiple connections.
    - search_query: Optional text to search in news titles and content (case-insensitive)
    """
    try:
        # Convert categories to news_type and news_sub_type filters if provided
        news_types = None
        news_sub_types = None
        if request.categories:
            # Validate categories first
            valid_categories = set(NEWS_CATEGORY_MAPPING.keys())
            invalid_categories = [
                cat for cat in request.categories if cat not in valid_categories
            ]

            if invalid_categories:
                return JSONResponse(
                    content={
                        "error": f"Invalid categories provided: {invalid_categories}",
                        "valid_categories": list(valid_categories),
                        "received_categories": request.categories,
                    },
                    status_code=400,
                )

            # Get filter values - same list is returned for both news_types and news_sub_types
            # This ensures each value is searched against BOTH news_type AND news_sub_type columns
            news_types, news_sub_types = _get_news_filters_from_categories(
                request.categories
            )
            if not news_types and not news_sub_types:
                return JSONResponse(
                    content={
                        "error": "No valid filters extracted from categories",
                        "valid_categories": list(valid_categories),
                    },
                    status_code=400,
                )

        # Require connection_identifiers - if not provided or empty, return empty results
        if (
            not request.connection_identifiers
            or len(request.connection_identifiers) == 0
        ):
            logger.info(
                "Fetch News: public_identifier=%s, connection_identifiers not provided or empty, returning empty results",
                request.public_identifier,
            )
            return JSONResponse(
                content={
                    "public_identifier": request.public_identifier,
                    "total_count": 0,
                    "page": request.page,
                    "limit": request.limit,
                    "total_pages": 0,
                    "news_items": [],
                    "categories_applied": request.categories,
                }
            )

        # Fetch company_universal_names for the connections to include company news
        company_universal_names_map = (
            await _fetch_company_universal_names_for_connections(
                connection_identifiers=request.connection_identifiers,
                es_client_compressed=es_client_compressed,
            )
        )

        # Collect unique company_universal_names (filter out empty strings)
        company_universal_names = [
            name for name in company_universal_names_map.values() if name
        ]

        # Combine connection_identifiers (people) with company_universal_names (companies)
        # This ensures we fetch both person news and company news
        all_identifiers = list(request.connection_identifiers) + company_universal_names

        logger.info(
            "Fetch News: public_identifier=%s, page=%d, limit=%d, categories=%s, "
            "connection_identifiers=%s (%d people, %d companies), search_query=%s",
            request.public_identifier,
            request.page,
            request.limit,
            request.categories,
            request.connection_identifiers,
            len(request.connection_identifiers),
            len(company_universal_names),
            request.search_query[:50] if request.search_query else None,
        )

        # Fetch news from database
        # The query will match records where ANY value appears in EITHER news_type OR news_sub_type column
        # If search_query is provided, it will also search in news_title and content fields
        # Includes news for both person connection_identifiers AND their companies' company_universal_names
        result = await fetch_news_by_public_identifier(
            public_identifier=request.public_identifier,
            entity_urn=request.entity_urn,
            connection_identifiers=all_identifiers,
            news_types=news_types,
            news_sub_types=news_sub_types,
            search_query=request.search_query,
            page=request.page,
            limit=request.limit,
        )

        total_count = result["total_count"]
        items = result["items"]

        logger.info(
            "Fetch News: public_identifier=%s, fetched %d items (total_count=%d, page=%d, limit=%d)",
            request.public_identifier,
            len(items),
            total_count,
            request.page,
            request.limit,
        )

        # Calculate total pages
        total_pages = math.ceil(total_count / request.limit) if total_count > 0 else 0

        # Format news items
        news_items: List[NewsItem] = []
        now_epoch = int(time.time())
        for item in items:
            # Format published_date_relative
            published_date_relative = None
            published_date = item.get("published_date")
            if published_date:
                # Normalize the timestamp first
                normalized_date = _normalize_epoch_seconds(published_date)
                if normalized_date:
                    published_date_relative = _format_relative_time(
                        now_epoch=now_epoch,
                        published_epoch=normalized_date,
                    )

            news_items.append(
                NewsItem(
                    connection_public_identifier=item.get(
                        "connection_public_identifier", ""
                    ),
                    news_id=item.get("news_id"),
                    news_title=item.get("news_title"),
                    content=item.get("content"),
                    link=item.get("link"),
                    published_date=item.get("published_date"),
                    published_date_relative=published_date_relative,
                    news_type=item.get("news_type"),
                    news_sub_type=item.get("news_sub_type"),
                    source=item.get("source"),
                    author=item.get("author"),
                    author_description=item.get("author_description"),
                    score=(
                        float(item.get("final_score", 0.0))
                        if item.get("final_score") is not None
                        else None
                    ),
                    llm_score=(
                        float(item.get("final_score", 0.0) * 100.0)
                        if item.get("final_score")
                        else None
                    ),
                    llm_reasoning=item.get("reasoning"),
                )
            )

        # Prevent 3 consecutive items from the same author (only for paginated response)
        news_items = _prevent_consecutive_authors(news_items, max_consecutive=3)

        return JSONResponse(
            content={
                "public_identifier": request.public_identifier,
                "total_count": total_count,
                "page": request.page,
                "limit": request.limit,
                "total_pages": total_pages,
                "news_items": [item.dict() for item in news_items],
                "categories_applied": request.categories,
            }
        )

    except Exception as e:
        logger.error("Unhandled exception in /fetch_news: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(
            content={"error": "Internal Server Error"},
            status_code=500,
        )


@router.post("/v2/fetch_news", response_model=FetchNewsOutput)
async def fetch_news_v2(
    request: FetchNewsV2Input,
    es_client_compressed=Depends(get_es_client_with_compression),
    es_client_bd=Depends(get_es_client_bd),
):
    """
    Fetch runtime-ranked news IDs directly from BD Elasticsearch.

    Ranking is recency-first and personalized. Connection score, priority,
    and relationship labels provide a small per-connection boost.
    """
    try:
        reference_public_identifier = (request.public_identifier or "").strip()
        reference_entity_urn = (request.entity_urn or "").strip() or None
        reference_profile_id = (request.profile_id or "").strip() or None
        reference_identity = (
            reference_profile_id or reference_public_identifier or reference_entity_urn
        )
        if not reference_identity:
            return JSONResponse(
                content={
                    "error": "One of profile_id, public_identifier, or entity_urn is required"
                },
                status_code=400,
            )

        if es_client_bd is None:
            return JSONResponse(
                content={
                    "error": "BD Elasticsearch client not initialized (missing ES_URL_BD / ES_API_KEY_BD)"
                },
                status_code=500,
            )

        news_types = None
        news_sub_types = None
        if request.categories:
            valid_categories = set(NEWS_CATEGORY_MAPPING.keys())
            invalid_categories = [
                cat for cat in request.categories if cat not in valid_categories
            ]

            if invalid_categories:
                return JSONResponse(
                    content={
                        "error": f"Invalid categories provided: {invalid_categories}",
                        "valid_categories": list(valid_categories),
                        "received_categories": request.categories,
                    },
                    status_code=400,
                )

            news_types, news_sub_types = _get_news_filters_from_categories(
                request.categories
            )
            if not news_types and not news_sub_types:
                return JSONResponse(
                    content={
                        "error": "No valid filters extracted from categories",
                        "valid_categories": list(valid_categories),
                    },
                    status_code=400,
                )

        if (
            not request.connection_identifiers
            or len(request.connection_identifiers) == 0
        ):
            logger.info(
                "Fetch News: public_identifier=%s, connection_identifiers not provided or empty, returning empty results",
                request.public_identifier,
            )
            return JSONResponse(
                content={
                    "public_identifier": reference_public_identifier,
                    "total_count": 0,
                    "page": request.page,
                    "limit": request.limit,
                    "total_pages": 0,
                    "news_items": [],
                    "categories_applied": request.categories,
                }
            )

        request_started = time.perf_counter()
        timings_ms: Dict[str, int] = {}
        connection_public_identifiers = _public_identifiers_from_v2_connections(
            request.connection_identifiers
        )
        stage_started = time.perf_counter()
        company_universal_names_map = (
            await _fetch_company_universal_names_for_connections(
                connection_identifiers=connection_public_identifiers,
                es_client_compressed=es_client_compressed,
            )
        )
        timings_ms["company_lookup"] = int((time.perf_counter() - stage_started) * 1000)

        all_identifiers, connection_scores, connection_metadata = (
            _build_v2_connection_context(
                request.connection_identifiers,
                company_universal_names_map,
            )
        )
        company_universal_names = list(
            dict.fromkeys(name for name in company_universal_names_map.values() if name)
        )
        if not all_identifiers:
            return JSONResponse(
                content={
                    "public_identifier": reference_public_identifier,
                    "total_count": 0,
                    "page": request.page,
                    "limit": request.limit,
                    "total_pages": 0,
                    "news_items": [],
                    "categories_applied": request.categories,
                }
            )

        stage_started = time.perf_counter()

        reference_es_id, reference_profile = await fetch_profile_by_public_identifier(
            reference_public_identifier,
            es_client=es_client_compressed,
            source_fields=None,
            only_top_experience=True,
            entity_urn=reference_entity_urn,
        )

        timings_ms["profile_lookup"] = int((time.perf_counter() - stage_started) * 1000)
        if reference_profile is None:
            return JSONResponse(
                content={"error": f"Reference profile not found: {reference_identity}"},
                status_code=404,
            )
        if not reference_public_identifier and isinstance(reference_profile, dict):
            reference_public_identifier = str(
                reference_profile.get("public_identifier")
                or reference_profile.get("publicIdentifier")
                or ""
            ).strip()
        semantics_key = (
            reference_profile_id or reference_public_identifier or reference_identity
        )

        stage_started = time.perf_counter()
        profile_semantics = await _get_or_create_profile_semantics(
            public_identifier=semantics_key,
            profile_es_id=reference_es_id,
            profile=reference_profile,
        )
        timings_ms["profile_semantics"] = int(
            (time.perf_counter() - stage_started) * 1000
        )

        logger.info(
            "Fetch News ES: public_identifier=%s, page=%d, limit=%d, categories=%s, "
            "connection_identifiers=%s (%d people, %d companies), search_query=%s, rubric_from_cache=%s",
            reference_identity,
            request.page,
            request.limit,
            request.categories,
            all_identifiers,
            len(connection_public_identifiers),
            len(company_universal_names),
            request.search_query[:50] if request.search_query else None,
            profile_semantics.get("_from_cache"),
        )

        bd_news_index = (os.getenv("ES_BD_NEWS_INDEX") or "bd_news").strip()
        now_epoch = int(time.time())
        cursor = request.cursor or {}
        pit_id = cursor.get("pit_id") if isinstance(cursor, dict) else None
        search_after = None
        cursor_buffer: List[Dict[str, Any]] = []
        cursor_recent_authors: List[str] = []
        if isinstance(cursor, dict):
            cursor_version = int(cursor.get("version") or 1)
            search_after = _valid_search_after(
                cursor.get("es_sort") or cursor.get("sort")
            )
            if cursor_version == CURSOR_VERSION and search_after:
                raw_buffer = cursor.get("buffer")
                if isinstance(raw_buffer, list):
                    cursor_buffer = [hit for hit in raw_buffer if isinstance(hit, dict)]
                raw_recent_authors = cursor.get("recent_authors")
                if isinstance(raw_recent_authors, list):
                    cursor_recent_authors = [
                        str(author).strip()
                        for author in raw_recent_authors
                        if str(author).strip()
                    ]
            else:
                pit_id = None
                search_after = None
        if not pit_id:
            pit = await es_client_bd.open_point_in_time(
                index=bd_news_index,
                keep_alive=PIT_KEEP_ALIVE,
            )
            pit_id = pit.get("id")

        max_consecutive_authors = MAX_CONSECUTIVE_AUTHOR
        overfetch_factor = DIVERSITY_OVERFETCH_FACTOR
        cursor_buffer_max = CURSOR_BUFFER_MAX
        query_limit = min(500, max(request.limit, request.limit * overfetch_factor))

        stage_started = time.perf_counter()
        all_seen_ids = await fetch_all_seen_news_ids(
            reference_public_identifier=reference_public_identifier,
            entity_urn=reference_entity_urn,
        )
        seen_id_map = {news_id: True for news_id in all_seen_ids}
        timings_ms["seen_lookup"] = int((time.perf_counter() - stage_started) * 1000)

        async def run_search(use_semantic: bool):
            body = _build_news_query(
                identifiers=all_identifiers,
                news_types=news_types,
                news_sub_types=news_sub_types,
                search_query=request.search_query,
                profile_semantics=profile_semantics,
                connection_scores=connection_scores,
                seen_news_ids=seen_id_map,
                now_epoch=now_epoch,
                limit=query_limit,
                pit_id=pit_id,
                search_after=search_after if isinstance(search_after, list) else None,
                use_semantic=use_semantic,
            )
            return await es_client_bd.search(body=body)

        async def reopen_pit_and_search(use_semantic: bool):
            nonlocal pit_id
            pit = await es_client_bd.open_point_in_time(
                index=bd_news_index,
                keep_alive=PIT_KEEP_ALIVE,
            )
            pit_id = pit.get("id")
            return await run_search(use_semantic=use_semantic)

        use_semantic = _valid_semantic_embedding(
            profile_semantics.get("semantic_embedding")
        )
        try:
            stage_started = time.perf_counter()
            res = await run_search(use_semantic=use_semantic)
            timings_ms["es_search"] = int((time.perf_counter() - stage_started) * 1000)
        except Exception as exc:
            if _is_missing_search_context_error(exc):
                logger.info(
                    "PIT search context expired for public_identifier=%s; reopening PIT and retrying once.",
                    reference_public_identifier,
                )
                stage_started = time.perf_counter()
                res = await reopen_pit_and_search(use_semantic=use_semantic)
                timings_ms["es_search"] = int(
                    (time.perf_counter() - stage_started) * 1000
                )
            else:
                raise

        es_hits = (res.get("hits") or {}).get("hits", []) or []
        total_count = _es_total_count(res)
        candidate_hits = _dedupe_hits(cursor_buffer + es_hits)
        hits, remaining_hits, recent_authors = _select_diverse_hits(
            candidate_hits,
            limit=request.limit,
            recent_authors=cursor_recent_authors,
            max_consecutive=max_consecutive_authors,
        )
        news_ids = [
            str((hit.get("_source") or {}).get("news_id"))
            for hit in hits
            if (hit.get("_source") or {}).get("news_id") is not None
        ]
        seen_ids = {news_id for news_id in news_ids if news_id in all_seen_ids}

        news_items: List[Dict[str, Any]] = []
        for hit in hits:
            src = hit.get("_source") or {}
            news_id = str(src.get("news_id") or "")
            es_score = float(hit.get("_score") or 0.0)
            connection_identifier = str(src.get("linkedin_identifier") or "")
            connection_meta = connection_metadata.get(connection_identifier) or {}
            news_items.append(
                {
                    "connection_public_identifier": connection_identifier,
                    "author": src.get("author"),
                    "news_id": news_id,
                    "published_date": src.get("published_date"),
                    "news_type": src.get("news_type"),
                    "news_sub_type": src.get("news_sub_type"),
                    "score": es_score,
                    "connection_priority_label": connection_meta.get("priority_label"),
                    "connection_relationship_label": connection_meta.get(
                        "relationship_label"
                    ),
                    "seen": news_id in seen_ids,
                }
            )

        next_cursor = None
        es_sort = es_hits[-1].get("sort") if es_hits else search_after
        buffered_for_next = [
            _compact_hit_for_cursor(hit) for hit in remaining_hits[:cursor_buffer_max]
        ]
        has_more_es_hits = len(es_hits) == query_limit
        has_buffered_hits = bool(buffered_for_next)
        has_more_by_total = total_count > len(hits)
        if (has_buffered_hits or has_more_es_hits or has_more_by_total) and es_sort:
            next_cursor = {
                "version": CURSOR_VERSION,
                "pit_id": res.get("pit_id") or pit_id,
                "sort": es_sort,
                "es_sort": es_sort,
                "buffer": buffered_for_next,
                "recent_authors": recent_authors,
            }
        elif pit_id:
            try:
                await es_client_bd.close_point_in_time(id=pit_id)
            except Exception:
                logger.debug("Failed to close PIT id=%s", pit_id, exc_info=True)

        total_pages = math.ceil(total_count / request.limit) if request.limit else 0
        timings_ms["total"] = int((time.perf_counter() - request_started) * 1000)
        logger.info(
            "Fetch News timings: public_identifier=%s timings_ms=%s es_hits=%d returned=%d buffered=%d",
            reference_identity,
            timings_ms,
            len(es_hits),
            len(news_items),
            len(buffered_for_next),
        )

        return JSONResponse(
            content={
                "public_identifier": reference_public_identifier,
                "reference_es_id": reference_es_id,
                "total_count": total_count,
                "page": request.page,
                "limit": request.limit,
                "total_pages": total_pages,
                "news_items": news_items,
                "categories_applied": request.categories,
                "next_cursor": next_cursor,
            }
        )

    except Exception as e:
        logger.error("Unhandled exception in /fetch_news: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(
            content={"error": "Internal Server Error"},
            status_code=500,
        )


@router.patch("/news_seen", response_model=UpdateNewsSeenOutput)
async def update_news_seen(request: UpdateNewsSeenInput):
    """
    Update `seen` flag for a ranked news item scoped by:
    - news_id
    - reference_public_identifier
    """
    try:
        update_result = await update_ranked_news_seen(
            news_id=request.news_id,
            reference_public_identifier=request.reference_public_identifier,
            entity_urn=request.entity_urn,
            seen=request.seen,
        )
        if update_result["matched"] == 0:
            return UpdateNewsSeenOutput(
                success=False,
                message="No matching news row found for given identifiers.",
            )
        if update_result["updated"] == 0:
            return UpdateNewsSeenOutput(
                success=True,
                message=f"News seen status already {request.seen}. No DB change needed.",
            )
        return UpdateNewsSeenOutput(
            success=True,
            message="News seen status updated.",
        )
    except Exception as e:
        logger.error(
            "Error in /news_seen for ref=%s news_id=%s: %s",
            request.reference_public_identifier,
            request.news_id,
            str(e),
        )
        return JSONResponse(
            content={"success": False, "message": "Internal Server Error"},
            status_code=500,
        )
