import asyncio
import json
import logging
import os
import re
import traceback
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from elasticsearch import ApiError
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from qutils.llm.asynchronous import invoke

from app.database.connection_rankings import fetch_ranked_connections
from app.database.ranked_news import (
    fetch_latest_ranked_news_for_reference,
    fetch_all_news_ids_for_reference,
    insert_ranked_news_for_reference,
)
from app.models.schemas.news_scoring import NewsScoringInput, NewsScoringOutput
from app.routes.dependancies import get_es_client_bd, get_es_client_with_compression
from app.utils.profile_fetching.public_identifier_fetch import (
    fetch_profile_by_public_identifier,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _safe_json_loads_from_text(text: str) -> Any:
    """
    Try to parse JSON (array/object) from an LLM response which might contain extra text.
    Handles cases where JSON is followed by extra text or multiple JSON objects.
    """
    if not text:
        raise ValueError("Empty response")

    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", t).replace("```", "").strip()

    # Try to find a complete JSON array by matching balanced brackets
    # This handles cases where there's extra text after the JSON
    bracket_count = 0
    start_idx = -1

    for i, char in enumerate(t):
        if char == "[":
            if bracket_count == 0:
                start_idx = i
            bracket_count += 1
        elif char == "]":
            bracket_count -= 1
            if bracket_count == 0 and start_idx != -1:
                # Found complete array, try to parse it
                try:
                    json_str = t[start_idx : i + 1]
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    # If parsing fails, continue searching
                    start_idx = -1
                    continue

    # Fallback: try regex-based extraction (prefer array over object)
    arr = re.search(r"\[[\s\S]*?\]", t, re.DOTALL)
    if arr:
        try:
            return json.loads(arr.group(0))
        except json.JSONDecodeError:
            pass

    # Try to find complete JSON object
    brace_count = 0
    start_idx = -1

    for i, char in enumerate(t):
        if char == "{":
            if brace_count == 0:
                start_idx = i
            brace_count += 1
        elif char == "}":
            brace_count -= 1
            if brace_count == 0 and start_idx != -1:
                # Found complete object, try to parse it
                try:
                    json_str = t[start_idx : i + 1]
                    parsed = json.loads(json_str)
                    # If it's a single object but we expect an array, wrap it
                    if isinstance(parsed, dict):
                        return [parsed]
                    return parsed
                except json.JSONDecodeError:
                    start_idx = -1
                    continue

    # Last resort: try regex for object
    obj = re.search(r"\{[\s\S]*?\}", t, re.DOTALL)
    if obj:
        try:
            parsed = json.loads(obj.group(0))
            if isinstance(parsed, dict):
                return [parsed]
            return parsed
        except json.JSONDecodeError:
            pass

    # Final attempt: try parsing the whole text
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse JSON from LLM response: {str(e)}")


def _time_decay_score(*, now_epoch: int, published_epoch: Optional[int]) -> float:
    """
    time_score = exp(-days_old / 14), clamped to [0, 1].
    """
    try:
        pe = int(published_epoch or 0)
    except Exception:
        pe = 0
    if pe <= 0:
        return 0.0
    days_old = max(0.0, float(now_epoch - pe) / 86400.0)
    return max(0.0, min(1.0, float(math.exp(-days_old / 14.0))))


def _normalize_epoch_seconds(value: Any) -> Optional[int]:
    """
    Normalize a timestamp into epoch seconds.
    Accepts epoch seconds or epoch milliseconds.
    Returns None if value is missing/invalid.
    """
    if value is None:
        return None
    try:
        v = int(value)
    except Exception:
        return None

    # Heuristic: millis are usually >= 10^12
    if v >= 10**12:
        v = int(v / 1000)

    # Reject obviously invalid timestamps (before 2000-01-01)
    if v < 946684800:
        return None

    return v


def _format_relative_time(*, now_epoch: int, published_epoch: Optional[int]) -> str:
    """
    Human-ish relative time computed from normalized published_date.
    """
    pe = _normalize_epoch_seconds(published_epoch)
    if not pe:
        return ""

    delta = int(now_epoch - pe)
    if delta < 0:
        delta = 0

    days = delta // 86400
    if days <= 0:
        hours = max(1, delta // 3600)
        return f"{hours} hours ago" if hours != 1 else "1 hour ago"
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    if months == 1:
        return "1 month ago"
    if months < 24:
        return f"{months} months ago"
    years = max(1, months // 12)
    return f"{years} years ago" if years != 1 else "1 year ago"


def _news_type_weight(item: Dict[str, Any]) -> float:
    """
    news_type_weight in [0, 1].
    """
    if not isinstance(item, dict):
        return 0.6
    news_type = str(item.get("news_type") or "").strip().lower()
    news_sub_type = str(item.get("news_sub_type") or "").strip().lower()
    post_type = str(item.get("post_type") or "").strip().lower()

    if news_type == "company_news":
        return 1.0
    if "thought" in news_type:
        return 0.7
    if "repost" in news_type or "repost" in news_sub_type or post_type == "repost":
        return 0.4
    return 0.6


def _dedupe_and_diversify(
    *,
    ranked_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Post-processing:
      - Deduplicate by `news_id` only
    Expects ranked_items sorted best->worst.
    """
    seen_news_ids: set[str] = set()
    out: List[Dict[str, Any]] = []
    filtered_count = 0

    for item in ranked_items:
        news_id = str(item.get("news_id") or "").strip()
        if news_id:
            if news_id in seen_news_ids:
                filtered_count += 1
                logger.debug(
                    "Deduplication: filtered duplicate news_id='%s', title='%s', connection='%s'",
                    news_id,
                    item.get("news_title", "")[:100],
                    item.get("connection_public_identifier", ""),
                )
                continue
            seen_news_ids.add(news_id)

        out.append(item)

    if filtered_count > 0:
        logger.info(
            "Deduplication: filtered %d items by news_id from %d total items, kept %d",
            filtered_count,
            len(ranked_items),
            len(out),
        )

    return out


async def _fetch_top_20_news_from_es(
    *,
    es_client_bd,
    identifiers: List[str],
    index_name: str,
    min_published_epoch: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Fetch top 20 news items from Elasticsearch for the last 60 days.
    Returns news items sorted by published_date descending.

    Args:
        es_client_bd: Elasticsearch client
        identifiers: List of public_identifiers or company universal names
        index_name: ES index name
        min_published_epoch: Minimum published date (epoch seconds)

    Returns:
        Tuple of (List of top 20 news items, error_flag)
        - List of top 20 news items (dicts with news_id, news_title, etc.)
        - error_flag: True if ES fetch failed, False if successful
    """
    if not identifiers:
        return [], False

    try:
        source_fields = [
            "news_id",
            "news_title",
            "content",
            "link",
            "published_date",
            "linkedin_identifier",
        ]

        body = {
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"linkedin_identifier.keyword": identifiers}},
                        {
                            "range": {
                                "published_date": {"gte": int(min_published_epoch)}
                            }
                        },
                    ]
                }
            },
            "sort": [{"published_date": {"order": "desc"}}],
            "size": 20,  # Top 20 only
            "_source": source_fields,
        }

        res = await es_client_bd.search(index=index_name, body=body)
        hits = res.get("hits", {}).get("hits", []) or []

        top_20_news = []
        for h in hits:
            src = h.get("_source") or {}
            if not isinstance(src, dict):
                continue
            news_id = src.get("news_id")
            if not news_id:
                continue
            top_20_news.append(
                {
                    "news_id": str(news_id),
                    "news_title": src.get("news_title", ""),
                    "content": src.get("content", ""),
                    "link": src.get("link", ""),
                    "published_date": src.get("published_date"),
                    "linkedin_identifier": src.get("linkedin_identifier", ""),
                }
            )

        logger.info(
            f"Fetched top 20 news from ES for {len(identifiers)} identifiers: {len(top_20_news)} items"
        )
        return top_20_news, False

    except Exception as e:
        logger.error(f"Error fetching top 20 news from ES: {e}")
        return [], True


async def _fetch_bd_news_for_people(
    *,
    es_client_bd,
    people_public_identifiers: List[str],
    news_per_person: int,
    index_name: str,
    min_published_epoch: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch bd_news docs for all people at once (by `linkedin_identifier` == public_identifier).
    Returns a flat list of news docs with `connection_public_identifier` attached.
    Queries all people in a single ES query with no per-person limits.
    """
    if not people_public_identifiers:
        return []

    items: List[Dict[str, Any]] = []

    # Keep _source small for prompt + response payload
    source_fields = [
        "news_id",
        "news_title",
        "content",
        "link",
        "published_date",
        "published_date_relative",
        "news_type",
        "news_sub_type",
        "author",
        "author_description",
        "source",
        "post_type",
        # company news support
        "company_name",
        "entity_type",
        "linkedin_identifier",  # Need this to map back to connection_public_identifier
    ]

    # Build query for all people at once using terms query
    must: List[Dict[str, Any]] = [
        {"terms": {"linkedin_identifier.keyword": people_public_identifiers}}
    ]
    if min_published_epoch:
        # IMPORTANT: Force epoch_second; otherwise ES may interpret numeric values as epoch_millis.
        must.append(
            {
                "range": {
                    "published_date": {
                        "gte": int(min_published_epoch),
                    }
                }
            }
        )

    # Use large size to fetch all matching items (no per-person limit)
    # ES default max is 10000, but we'll use track_total_hits to get accurate counts
    body = {
        "query": {"bool": {"must": must}},
        "sort": [{"published_date": {"order": "desc"}}],
        "size": 10000,  # Fetch all matching items, not limited per person
        "track_total_hits": True,
        "_source": source_fields,
    }

    try:
        res = await es_client_bd.search(index=index_name, body=body)
    except ApiError as e:
        # If index doesn't exist, surface a clear error upstream.
        raise RuntimeError(f"BD ES search failed on index='{index_name}': {e}") from e

    hits = res.get("hits", {}).get("hits", []) or []
    total_hits = (
        (res.get("hits", {}).get("total", {}) or {}).get("value")
        if isinstance(res.get("hits", {}).get("total"), dict)
        else res.get("hits", {}).get("total")
    )

    logger.debug(
        "ES query for %d linkedin_identifiers: total_hits=%s, returned_hits=%d, min_published_epoch=%s",
        len(people_public_identifiers),
        total_hits,
        len(hits),
        min_published_epoch,
    )

    now_epoch = int(time.time())
    items_before_filter = len(items)

    for h in hits:
        src = h.get("_source") or {}
        if not isinstance(src, dict):
            continue

        # Get the linkedin_identifier from the source to map back to connection_public_identifier
        pid = src.get("linkedin_identifier") or ""
        if not pid:
            continue

        # Hard-enforce date validation using published_date (NOT cache_date).
        # This protects us if ES mapping/format differs or data is stored as millis.
        published_norm = _normalize_epoch_seconds(src.get("published_date"))
        if not published_norm:
            logger.debug(
                "Skipping item (no valid published_date) for identifier='%s', news_id=%s",
                pid,
                src.get("news_id"),
            )
            continue
        # Drop obviously "future" timestamps (ingestion bugs)
        if published_norm > (now_epoch + 86400):
            logger.debug(
                "Skipping item (future date) for identifier='%s', news_id=%s, published_date=%s",
                pid,
                src.get("news_id"),
                published_norm,
            )
            continue
        if min_published_epoch and published_norm < int(min_published_epoch):
            logger.debug(
                "Skipping item (too old) for identifier='%s', news_id=%s, published_date=%s (min=%s)",
                pid,
                src.get("news_id"),
                published_norm,
                min_published_epoch,
            )
            continue

        items.append(
            {
                "connection_public_identifier": pid,
                "es_id": h.get("_id"),
                **src,
                # Override to normalized values so downstream scoring is correct/consistent
                "published_date": published_norm,
                "published_date_relative": _format_relative_time(
                    now_epoch=now_epoch,
                    published_epoch=published_norm,
                ),
            }
        )

    items_after_filter = len(items) - items_before_filter
    if items_after_filter < len(hits):
        logger.info(
            "Filtered %d/%d items (date filtering removed %d items) for %d identifiers",
            items_after_filter,
            len(hits),
            len(hits) - items_after_filter,
            len(people_public_identifiers),
        )

    return items


def _profile_to_compact_text(profile: Dict[str, Any]) -> str:
    """
    Turn an ES profile dict into compact context text for LLM ranking.
    Keep this defensive; profile shapes can vary.
    """
    if not isinstance(profile, dict):
        return ""

    fields = []
    for k in [
        "full_name",
        "name",
        "headline",
        "title",
        "industry",
        "location",
        "summary",
        "about",
        "description",
    ]:
        v = profile.get(k)
        if isinstance(v, str) and v.strip():
            fields.append(f"{k}: {v.strip()}")

    # Add top experience (routes already can reduce experience to 1 item)
    exp = profile.get("experience")
    if isinstance(exp, list) and exp and isinstance(exp[0], dict):
        e0 = exp[0]
        title = e0.get("title") or ""
        company = e0.get("company") or e0.get("company_name") or e0.get("name") or ""
        if title or company:
            fields.append(f"current_experience: {title} @ {company}".strip())

    return "\n".join(fields)[:4000]


async def _score_single_news_item(
    *,
    reference_profile_text: str,
    news_item: Dict[str, Any],
    index: int,
    model: str = "openai/gpt-4o",
) -> Dict[str, Any]:
    """
    Score a single news item independently.
    Returns {index, score, reasoning} where:
      - score is a CONTENT relevance score in [0.0, 1.0]
      - index corresponds to the news item's position
    """
    title = (news_item.get("news_title") or "").strip()
    content = (news_item.get("content") or "").strip()
    content = re.sub(r"\s+", " ", content)[:800]
    link = (news_item.get("link") or "").strip()
    who = news_item.get("connection_public_identifier") or ""
    news_type = (news_item.get("news_type") or "").strip()
    published_date = news_item.get("published_date")
    author_description = (news_item.get("author_description") or "").strip()
    source = (news_item.get("source") or "").strip()

    prompt = f"""
You are a Strategic BD Analyst specializing in Executive Recruitment.

### GOAL
Evaluate the relevance of this news item for the REFERENCE PERSON. Your task is to identify "Trigger Events"—news that signals a change in human capital needs, leadership gaps, or strategic pivots where executive search services would be highly valued.

### SCORING RUBRIC (0.0 to 1.0)
- **0.9 - 1.0 (Critical):** Immediate "Trigger Event." High-level departures, M&A activity, major funding rounds (Series B+), or massive layoffs at the REFERENCE PERSON'S company.
- **0.7 - 0.8 (High):** Significant expansion, new market entry, or competitor C-suite shifts that impact the REFERENCE PERSON'S strategic positioning.
- **0.4 - 0.6 (Moderate):** Industry trends, award wins, or partnership news that serve as "Warm Icebreakers" for relationship building.
- **0.0 - 0.3 (Low):** General market noise, routine earnings (if stable), or general interest news with no executive hiring implication.

### METHODOLOGY: THINKING STEP-BY-STEP
For this news item, perform this internal logic before scoring:
1. **Identify the Trigger:** Is there a specific event (e.g., "CEO resigned", "Raised $50M")?
2. **Determine Impact:** Does this create a need for a new executive, or provide a consultative "reason to call"?
3. **Contextualize:** How close is the news to the REFERENCE PERSON'S direct influence or company?

### FEW-SHOT EXAMPLES (For Calibration)
- *Example 1 (Score 0.95):* "TechCorp's CTO steps down after 5 years." -> Reasoning: Immediate opening for a C-suite search engagement.
- *Example 2 (Score 0.20):* "TechCorp launches updated employee wellness portal." -> Reasoning: Routine internal update with no executive-level strategic impact.

---

### REFERENCE PERSON PROFILE (compact):
{reference_profile_text}

### NEWS ITEM:
{{
  "connection_public_identifier": "{who}",
  "news_title": "{title}",
  "content_snippet": "{content}",
  "news_type": "{news_type}",
  "published_date": {published_date},
  "author_description": "{author_description}",
  "source": "{source}",
  "link": "{link}"
}}

### OUTPUT REQUIREMENTS
Return ONLY a valid JSON object. No markdown, no code fences.
Format:
{{
  "score": <number from 0.0 to 1.0>,
  "reasoning": "<string starting with the identified 'Trigger' then the 'Impact'>"
}}
""".strip()

    try:
        response = await invoke(
            model=model,
            temperature=0,
            fallbacks=[
                "openai/gpt-4.1",
                "openai/gpt-4o-mini",
                "openai/gpt-4o",
                "anthropic/claude-sonnet-4-5-20250929",
                "google/gemini-2.5-flash",
                "google/gemini-2.5-pro",
            ],
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = str(response)
        logger.debug(
            "LLM response for news item %d: %d characters",
            index,
            len(response_text),
        )

        parsed = _safe_json_loads_from_text(response_text)

        # Handle both single object and array responses
        if isinstance(parsed, list) and len(parsed) > 0:
            parsed = parsed[0]

        if not isinstance(parsed, dict):
            logger.warning(
                "LLM response for item %d was not a JSON object, got type: %s",
                index,
                type(parsed),
            )
            return {
                "index": index,
                "score": 0.0,
                "reasoning": "Failed to parse LLM response",
            }

        score = parsed.get("score")
        try:
            score_f = float(score) if score is not None else 0.0
        except (ValueError, TypeError):
            score_f = 0.0

        score_f = max(0.0, min(1.0, score_f))

        return {
            "index": index,
            "score": score_f,
            "reasoning": (parsed.get("reasoning") or "").strip(),
        }
    except Exception as e:
        logger.error(
            "Error scoring news item %d: %s",
            index,
            str(e),
        )
        return {
            "index": index,
            "score": 0.0,
            "reasoning": f"Error during scoring: {str(e)}",
        }


async def _score_content_relevance_with_llm(
    *,
    reference_profile_text: str,
    candidates: List[Dict[str, Any]],
    model: str = "openai/gpt-4o",
) -> List[Dict[str, Any]]:
    """
    Score each news item independently with separate LLM calls.
    Returns list of {index, score, reasoning} where:
      - score is a CONTENT relevance score in [0.0, 1.0]
      - index corresponds to candidates[i]
    """
    if not candidates:
        return []

    logger.info(
        "Scoring %d news items independently (one LLM call per item)",
        len(candidates),
    )

    # Score each candidate independently
    # Create tasks for all items
    tasks = [
        _score_single_news_item(
            reference_profile_text=reference_profile_text,
            news_item=candidate,
            index=i,
            model=model,
        )
        for i, candidate in enumerate(candidates)
    ]

    # Execute all scoring tasks concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results and handle any exceptions
    out: List[Dict[str, Any]] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(
                "Exception scoring news item %d: %s",
                i,
                str(result),
            )
            out.append(
                {
                    "index": i,
                    "score": 0.0,
                    "reasoning": f"Error: {str(result)}",
                }
            )
        elif isinstance(result, dict):
            out.append(result)
        else:
            logger.warning(
                "Unexpected result type for item %d: %s",
                i,
                type(result),
            )
            out.append(
                {
                    "index": i,
                    "score": 0.0,
                    "reasoning": "Unexpected result format",
                }
            )

    # Sort by index to ensure order matches input
    out.sort(key=lambda x: x.get("index", 0))

    logger.info(
        "Completed independent scoring for %d news items",
        len(out),
    )

    return out


@router.post("/score_news", response_model=NewsScoringOutput)
async def score_news(
    request: NewsScoringInput,
    es_client_compressed=Depends(get_es_client_with_compression),
    es_client_bd=Depends(get_es_client_bd),
):
    """
    News scoring endpoint:
    - Input: reference `public_identifier`
    - Optionally provide `connection_public_identifier` to score news only for that specific connection
    - Either use `connection_identifiers` provided in request OR fetch top-N connections from Postgres connection_rankings_bd (cached results from /rank_connections)
    - Fetch recent BD news for those people from BD Elasticsearch (default index: bd_news)
    - Use LLM to rank news items for relevance to the reference profile
    """
    try:
        reference_public_identifier = request.public_identifier.strip()
        if not reference_public_identifier:
            return JSONResponse(
                content={"error": "public_identifier is required"}, status_code=400
            )

        # Check for cached news, but also check for new news items that aren't cached yet
        cached_rows = await fetch_latest_ranked_news_for_reference(
            reference_public_identifier=reference_public_identifier
        )

        # Filter cached rows by connection_public_identifier if provided
        if cached_rows and request.connection_public_identifier:
            target_connection = request.connection_public_identifier.strip()
            cached_rows = [
                r
                for r in cached_rows
                if r.get("connection_public_identifier") == target_connection
            ]
            logger.info(
                "Filtered cached results to connection: %s (%d items)",
                target_connection,
                len(cached_rows),
            )

        # Extract cached news_ids to check for new items
        # Convert to strings for consistent matching (news_id can be string or number)
        # IMPORTANT: Fetch ALL news_ids (not just latest batch) to prevent duplicates
        all_existing_news_ids = await fetch_all_news_ids_for_reference(
            reference_public_identifier=reference_public_identifier
        )

        # Also build set from latest cached rows for response formatting
        cached_news_ids = set()
        if cached_rows:
            cached_news_ids = {
                str(r.get("news_id"))
                for r in cached_rows
                if r.get("news_id") is not None
            }
            logger.info(
                "Score News: reference=%s, found %d cached news items in database (latest batch), %d total unique news_ids in DB",
                reference_public_identifier,
                len(cached_rows),
                len(all_existing_news_ids),
            )
            logger.debug(
                "Cached news_ids sample (first 5): %s",
                list(cached_news_ids)[:5] if cached_news_ids else [],
            )
        else:
            logger.info(
                "Score News: reference=%s, no cached news items found in database (latest batch), %d total unique news_ids in DB",
                reference_public_identifier,
                len(all_existing_news_ids),
            )

        if es_client_bd is None:
            return JSONResponse(
                content={
                    "error": "BD Elasticsearch client not initialized (missing ES_URL_BD / ES_API_KEY_BD)"
                },
                status_code=500,
            )

        # 1) Fetch reference profile
        reference_es_id, reference_profile = await fetch_profile_by_public_identifier(
            reference_public_identifier,
            es_client=es_client_compressed,
            source_fields=None,
            only_top_experience=True,
        )
        if reference_profile is None:
            return JSONResponse(
                content={
                    "error": f"Reference profile not found: {reference_public_identifier}"
                },
                status_code=404,
            )

        # 2) Get connection identifiers: either from request or fetch from cached Postgres rankings
        top_connections: List[str] = []
        rank_map: Dict[str, int] = {}

        # If connection_public_identifier is provided, use only that connection
        if request.connection_public_identifier:
            target_connection = request.connection_public_identifier.strip()
            if target_connection:
                top_connections = [target_connection]
                rank_map[target_connection] = 1
                top_n = 1
                logger.info(
                    "Score News: reference=%s, scoring news only for specific connection: %s",
                    reference_public_identifier,
                    target_connection,
                )
            else:
                return JSONResponse(
                    content={"error": "connection_public_identifier cannot be empty"},
                    status_code=400,
                )
        elif request.connection_identifiers:
            # Use provided connection identifiers
            top_connections = [
                str(pid).strip() for pid in request.connection_identifiers if pid
            ]
            # Assign sequential ranks (1-based) for provided connections
            for i, pid in enumerate(top_connections, start=1):
                rank_map[pid] = i
            # Set top_n to the number of provided connections for scoring calculations
            top_n = len(top_connections)
            logger.info(
                "Score News: reference=%s, using %d connection identifiers provided in request",
                reference_public_identifier,
                len(top_connections),
            )
        else:
            # Fetch top connections from cached Postgres rankings (fallback)
            top_n = int(request.top_connections)
            rows = await fetch_ranked_connections(
                reference_public_identifier=reference_public_identifier,
                limit=top_n,
                offset=0,
            )

            # Build (public_identifier -> rank) where rank is 1-based in descending score order
            for i, r in enumerate(rows, start=1):
                if not isinstance(r, dict):
                    continue
                pid = r.get("connection_public_identifier")
                if not pid:
                    continue
                pid_s = str(pid)
                top_connections.append(pid_s)
                rank_map[pid_s] = i
            logger.info(
                "Score News: reference=%s, fetched %d top connections from Postgres rankings",
                reference_public_identifier,
                len(top_connections),
            )

        # Log total connections received
        logger.info(
            "Score News: reference=%s, total connections received: %d, connections: %s",
            reference_public_identifier,
            len(top_connections),
            top_connections[:10] + (["..."] if len(top_connections) > 10 else []),
        )

        # Debug: map top connections -> most recent company universal name (from people index)
        top_connection_company_universal_names: Dict[str, str] = {}
        try:
            # Use the people index directly (connections are stored there).
            people_index = (os.getenv("ES_PROFILES_INDEX") or "people").strip()

            # Try multiple common field variants; whichever exists/matches will hit.
            # Note: terms query needs an exact-match field; we include .keyword variants too.
            body = {
                "query": {
                    "bool": {
                        "should": [
                            {"terms": {"public_identifier": top_connections}},
                            {"terms": {"public_identifier.keyword": top_connections}},
                            {"terms": {"publicIdentifier": top_connections}},
                            {"terms": {"publicIdentifier.keyword": top_connections}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                "_source": ["public_identifier", "publicIdentifier", "experience"],
                "size": len(top_connections),
            }

            res = await es_client_compressed.search(index=people_index, body=body)
            hits = res.get("hits", {}).get("hits", []) or []

            # Initialize with empty for stable debug payload
            for pid in top_connections:
                top_connection_company_universal_names[pid] = ""

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
                    top_connection_company_universal_names[pid] = company_universal
                    logger.info(
                        "Score News: reference=%s, connection=%s -> company_universal_name=%s",
                        reference_public_identifier,
                        pid,
                        company_universal,
                    )
        except Exception as e:
            # Debug-only; never fail scoring if this lookup fails.
            logger.warning(
                "Score News: reference=%s, error fetching company universal names: %s",
                reference_public_identifier,
                e,
            )
            top_connection_company_universal_names = {}

        if not top_connections:
            note = (
                "No connection identifiers provided in request"
                if request.connection_identifiers
                else "No cached ranked connections found in Postgres"
            )
            return JSONResponse(
                content={
                    "reference_public_identifier": reference_public_identifier,
                    "reference_es_id": reference_es_id,
                    "top_connections": [],
                    "ranked_news": [],
                    "debug": {"note": note},
                }
            )

        # 3) First: Fetch top 20 news from ES (last 60 days) and store in memory
        now_epoch = int(time.time())
        timeline_days = 60
        min_published_epoch_60_days = now_epoch - (timeline_days * 86400)

        bd_news_index = (os.getenv("ES_BD_NEWS_INDEX") or "bd_news").strip()

        # Get all identifiers (people + companies)
        all_identifiers = list(top_connections)
        company_universal_names = sorted(
            {v for v in top_connection_company_universal_names.values() if v}
        )
        all_identifiers.extend(company_universal_names)

        logger.info(
            "Score News: reference=%s, querying ES with %d connection identifiers and %d company names: connections=%s, companies=%s",
            reference_public_identifier,
            len(top_connections),
            len(company_universal_names),
            top_connections[:5] + (["..."] if len(top_connections) > 5 else []),
            company_universal_names,
        )

        # Fetch top 20 news from ES (last 60 days) - store in memory
        top_20_es_news, es_fetch_failed = await _fetch_top_20_news_from_es(
            es_client_bd=es_client_bd,
            identifiers=all_identifiers,
            index_name=bd_news_index,
            min_published_epoch=min_published_epoch_60_days,
        )

        if es_fetch_failed:
            logger.warning(
                "Score News: reference=%s, ES top-20 fetch failed; will skip ES/DB comparison",
                reference_public_identifier,
            )
        else:
            logger.info(
                "Score News: reference=%s, fetched top 20 news from ES (last 60 days) for comparison: %d items",
                reference_public_identifier,
                len(top_20_es_news),
            )

        # 4) Continue with ranking logic: Fetch and rank news
        timeline_days_for_ranking = 60
        min_published_epoch_for_ranking = now_epoch - (
            timeline_days_for_ranking * 86400
        )
        min_published_epoch = None  # No limit for fetching - get all news

        # 4a) People news (by top connections public_identifiers)
        people_candidates = await _fetch_bd_news_for_people(
            es_client_bd=es_client_bd,
            people_public_identifiers=top_connections,
            news_per_person=int(request.news_per_connection),
            index_name=bd_news_index,
            min_published_epoch=min_published_epoch,
        )

        # 4b) Company news (by top connections' company_universal_name)
        company_rank_map: Dict[str, int] = {}
        for conn_pid, comp_universal in top_connection_company_universal_names.items():
            if not comp_universal:
                continue
            r = rank_map.get(conn_pid, top_n)
            prev = company_rank_map.get(comp_universal)
            if prev is None or r < prev:
                company_rank_map[comp_universal] = r

        company_candidates: List[Dict[str, Any]] = []
        if company_universal_names:
            logger.info(
                "Score News: reference=%s, fetching company news for %d companies: %s",
                reference_public_identifier,
                len(company_universal_names),
                company_universal_names,
            )
            company_candidates = await _fetch_bd_news_for_people(
                es_client_bd=es_client_bd,
                people_public_identifiers=company_universal_names,
                news_per_person=int(request.news_per_connection),
                index_name=bd_news_index,
                min_published_epoch=min_published_epoch,
            )
            # Map company news back to relevant connection(s) who work at that company
            # Create reverse mapping: company_universal_name -> list of connection public_identifiers
            # Sort connections by rank (lower rank = higher priority) to use best connection first
            company_to_connections: Dict[str, List[str]] = {}
            for (
                conn_pid,
                comp_universal,
            ) in top_connection_company_universal_names.items():
                if comp_universal:
                    if comp_universal not in company_to_connections:
                        company_to_connections[comp_universal] = []
                    # Add connection with its rank for sorting
                    conn_rank = rank_map.get(conn_pid, top_n)
                    company_to_connections[comp_universal].append((conn_rank, conn_pid))

            # Sort each company's connections by rank (best first)
            for comp_universal in company_to_connections:
                company_to_connections[comp_universal].sort(key=lambda x: x[0])
                company_to_connections[comp_universal] = [
                    conn_pid for _, conn_pid in company_to_connections[comp_universal]
                ]

            # Mark as company candidates. Keep connection_public_identifier as company_universal_name
            # (from linkedin_identifier in bd_news) so ranked_news_bd stores company id for company news.
            for c in company_candidates:
                if not isinstance(c, dict):
                    continue
                c["_is_company"] = True
                # Ensure connection_public_identifier is the company universal name for company news
                company_id = (
                    c.get("linkedin_identifier")
                    or c.get("connection_public_identifier")
                    or ""
                )
                if company_id:
                    c["connection_public_identifier"] = company_id
                if company_id and company_id not in company_to_connections:
                    logger.debug(
                        "Company news (company_id=%s, news_id=%s) has no connection in top rankings",
                        company_id,
                        c.get("news_id"),
                    )

            logger.info(
                "Score News: reference=%s, fetched %d company news items for %d companies (%s), storing company_universal_name as identifier",
                reference_public_identifier,
                len(company_candidates),
                len(company_universal_names),
                company_universal_names,
            )

        # Combine candidates
        candidates = (people_candidates or []) + (company_candidates or [])

        if not candidates:
            logger.info(
                "Score News: reference=%s, no news candidates found",
                reference_public_identifier,
            )
            # Still check notification logic: fetch ranked news from DB (last 60 days)
            all_ranked_from_db = await fetch_latest_ranked_news_for_reference(
                reference_public_identifier=reference_public_identifier
            )
            ranked_last_60_days = [
                r
                for r in all_ranked_from_db
                if r.get("published_date")
                and r.get("published_date") >= min_published_epoch_60_days
            ]
            total_ranked_last_60_days = len(ranked_last_60_days)
            should_notify = total_ranked_last_60_days < 50

            return JSONResponse(
                content={
                    "reference_public_identifier": reference_public_identifier,
                    "reference_es_id": reference_es_id,
                    "top_connections": top_connections,
                    "ranked_news": [],
                    "should_notify": should_notify,
                    "total_ranked_last_60_days": total_ranked_last_60_days,
                }
            )

        # Filter out cached items
        # Filter out candidates that already exist in database (check ALL existing news_ids, not just latest batch)
        filtered_candidates: List[Dict[str, Any]] = []
        skipped_existing_count = 0
        for c in candidates:
            if not isinstance(c, dict):
                continue
            news_id = c.get("news_id")
            if news_id and str(news_id) in all_existing_news_ids:
                skipped_existing_count += 1
                continue
            filtered_candidates.append(c)

        if skipped_existing_count > 0:
            logger.info(
                "Score News: reference=%s, filtered out %d candidates that already exist in database",
                reference_public_identifier,
                skipped_existing_count,
            )

        candidates = filtered_candidates

        # Split candidates: last 60 days (for LLM ranking) vs older (time-based only, not stored)
        candidates_for_llm: List[Dict[str, Any]] = []
        candidates_older: List[Dict[str, Any]] = []

        for c in candidates:
            published_date = c.get("published_date")
            if published_date and published_date >= min_published_epoch_for_ranking:
                candidates_for_llm.append(c)
            else:
                candidates_older.append(c)

        logger.info(
            "Score News: reference=%s, splitting candidates: %d for LLM scoring (last 60 days, will be stored), %d older (time-based only, not stored)",
            reference_public_identifier,
            len(candidates_for_llm),
            len(candidates_older),
        )

        # 5) LLM content relevance scoring (only for last 60 days)
        llm_by_index: Dict[int, Dict[str, Any]] = {}
        if candidates_for_llm:
            reference_text = _profile_to_compact_text(reference_profile)
            llm_scored = await _score_content_relevance_with_llm(
                reference_profile_text=reference_text,
                candidates=candidates_for_llm,
                model="openai/gpt-4o",
            )

            llm_by_index = {
                int(x["index"]): x
                for x in llm_scored
                if isinstance(x, dict) and "index" in x
            }

        # 6) Weighted scoring formula
        scored_items: List[Dict[str, Any]] = []

        # Score items from last 60 days (with LLM content scores)
        for i, c in enumerate(candidates_for_llm):
            pid = str(c.get("connection_public_identifier") or "").strip()
            if bool(c.get("_is_company")):
                rank = company_rank_map.get(pid, top_n)
            else:
                rank = rank_map.get(pid, top_n)
            time_score = _time_decay_score(
                now_epoch=now_epoch, published_epoch=c.get("published_date")
            )
            type_w = _news_type_weight(c)
            llm = llm_by_index.get(i) or {}
            content_score = float(llm.get("score") or 0.0)
            content_score = max(0.0, min(1.0, content_score))
            llm_reason = (llm.get("reasoning") or "").strip()

            # Connection rank weight removed; content weight increased (0.60).
            final_score = 0.30 * time_score + 0.60 * content_score + 0.10 * type_w
            final_score = max(0.0, min(1.0, float(final_score)))

            scored_items.append(
                {
                    "connection_public_identifier": pid,
                    "news_id": c.get("news_id"),
                    "news_title": c.get("news_title"),
                    "content": c.get("content"),
                    "link": c.get("link"),
                    "published_date": c.get("published_date"),
                    "published_date_relative": c.get("published_date_relative"),
                    "news_type": c.get("news_type"),
                    "news_sub_type": c.get("news_sub_type"),
                    "_source": c.get("source"),
                    "_author": c.get("author"),
                    "_author_description": c.get("author_description"),
                    "_entity_type": c.get("entity_type"),
                    "_company_name": c.get("company_name"),
                    "llm_score": float(final_score * 100.0),
                    "llm_reasoning": (
                        f"final={final_score:.3f} "
                        f"(time={time_score:.3f}, content={content_score:.3f}, type={type_w:.3f}). "
                        f"{llm_reason}"
                    ).strip(),
                    "_final_score": final_score,
                    "_content_score": content_score,
                }
            )

        # Score older items (time-based only) - but don't store them in DB
        # Only store items from last 60 days (with LLM scoring)
        # Older items are scored for response but not persisted
        for c in candidates_older:
            pid = str(c.get("connection_public_identifier") or "").strip()
            time_score = _time_decay_score(
                now_epoch=now_epoch, published_epoch=c.get("published_date")
            )
            final_score = time_score
            final_score = max(0.0, min(1.0, float(final_score)))

            # Add to scored_items for response, but mark as not to be stored
            scored_items.append(
                {
                    "connection_public_identifier": pid,
                    "news_id": c.get("news_id"),
                    "news_title": c.get("news_title"),
                    "content": c.get("content"),
                    "link": c.get("link"),
                    "published_date": c.get("published_date"),
                    "published_date_relative": c.get("published_date_relative"),
                    "news_type": c.get("news_type"),
                    "news_sub_type": c.get("news_sub_type"),
                    "_source": c.get("source"),
                    "_author": c.get("author"),
                    "_author_description": c.get("author_description"),
                    "_entity_type": c.get("entity_type"),
                    "_company_name": c.get("company_name"),
                    "llm_score": float(final_score * 100.0),
                    "llm_reasoning": f"final={final_score:.3f} (time={time_score:.3f}). Older than {timeline_days_for_ranking} days - time-based scoring only",
                    "_final_score": final_score,
                    "_content_score": 0.0,
                    "_skip_storage": True,  # Mark older items to skip storage
                }
            )

        # Merge with cached items (for response only, marked to skip storage)
        if cached_rows:
            now_epoch = int(time.time())
            cached_items_added = 0
            for r in cached_rows:
                conn = r.get("connection_public_identifier") or ""
                news_id = r.get("news_id")
                published = r.get("published_date")
                final_score = r.get("final_score")
                try:
                    score_0_1 = float(final_score) if final_score is not None else 0.0
                except Exception:
                    score_0_1 = 0.0
                score_0_1 = max(0.0, min(1.0, score_0_1))

                scored_items.append(
                    {
                        "connection_public_identifier": conn,
                        "news_id": news_id,
                        "news_title": r.get("news_title"),
                        "content": r.get("content"),
                        "link": r.get("link"),
                        "published_date": published,
                        "published_date_relative": _format_relative_time(
                            now_epoch=now_epoch, published_epoch=published
                        ),
                        "news_type": r.get("news_type"),
                        "news_sub_type": r.get("news_sub_type"),
                        "_source": r.get("source"),
                        "_author": r.get("author"),
                        "_author_description": r.get("author_description"),
                        "_entity_type": None,
                        "_company_name": None,
                        "llm_score": float(score_0_1 * 100.0),
                        "llm_reasoning": r.get("reasoning") or "",
                        "_final_score": score_0_1,
                        "_content_score": 0.0,
                        "_from_cache": True,  # Mark cached items to ensure they're never stored
                    }
                )
                cached_items_added += 1

            logger.info(
                "Score News: reference=%s, merged %d cached items into response (will be excluded from storage)",
                reference_public_identifier,
                cached_items_added,
            )

        # Sort by final_score descending
        scored_items.sort(key=lambda x: x.get("_final_score", 0.0), reverse=True)

        # Dedupe
        post_processed = _dedupe_and_diversify(ranked_items=scored_items)

        # Store new items to database - ONLY top 20 news per connection (last 60 days, with LLM scoring)
        # Older items are excluded from storage (time-based scoring only, not persisted)
        # Cached items are excluded from storage (already in database)
        eligible: List[Dict[str, Any]] = []
        skipped_cached_count = 0
        skipped_older_count = 0

        for x in post_processed:
            if not isinstance(x, dict):
                continue
            news_id = x.get("news_id")

            # CRITICAL: Skip cached items (already in database) - multiple checks for safety
            if x.get("_from_cache"):
                skipped_cached_count += 1
                continue  # Skip items from cache

            if news_id and str(news_id) in cached_news_ids:
                skipped_cached_count += 1
                continue  # Skip already cached items (double-check by news_id)

            # Skip older items (marked with _skip_storage) - only store last 60 days
            if x.get("_skip_storage"):
                skipped_older_count += 1
                continue

            # Additional check: ensure published_date is within last 60 days
            published_date = x.get("published_date")
            if published_date and published_date < min_published_epoch_for_ranking:
                skipped_older_count += 1
                continue  # Skip items older than 60 days

            eligible.append(x)

        # Group by connection, take top 20 per connection by final_score
        TOP_NEWS_PER_CONNECTION = 20
        by_connection: Dict[str, List[Dict[str, Any]]] = {}
        for x in eligible:
            conn = str(x.get("connection_public_identifier") or "").strip()
            if conn not in by_connection:
                by_connection[conn] = []
            by_connection[conn].append(x)

        to_store: List[Dict[str, Any]] = []
        for conn, items in by_connection.items():
            items_sorted = sorted(
                items,
                key=lambda i: float(i.get("_final_score") or 0.0),
                reverse=True,
            )[:TOP_NEWS_PER_CONNECTION]
            for rank, x in enumerate(items_sorted, start=1):
                to_store.append(
                    {
                        "connection_public_identifier": conn,
                        "news_id": x.get("news_id"),
                        "link": x.get("link"),
                        "news_title": x.get("news_title"),
                        "content": x.get("content"),
                        "news_type": x.get("news_type"),
                        "news_sub_type": x.get("news_sub_type"),
                        "published_date": x.get("published_date"),
                        "source": x.get("_source"),
                        "author": x.get("_author"),
                        "author_description": x.get("_author_description"),
                        "rank": rank,
                        "final_score": x.get("_final_score"),
                        "reasoning": x.get("llm_reasoning"),
                    }
                )

        logger.info(
            "Score News: reference=%s, storage filtering: skipped %d cached items, skipped %d older items, %d new items to store (top %d per connection)",
            reference_public_identifier,
            skipped_cached_count,
            skipped_older_count,
            len(to_store),
            TOP_NEWS_PER_CONNECTION,
        )

        if to_store:
            await insert_ranked_news_for_reference(
                reference_public_identifier=reference_public_identifier,
                reference_es_id=reference_es_id,
                ranked_news=to_store,
            )
            logger.info(
                "Score News: reference=%s, stored %d new news items in database (last 60 days only, with LLM scoring)",
                reference_public_identifier,
                len(to_store),
            )
        else:
            logger.info(
                "Score News: reference=%s, no new items to store (all items already cached or older than 60 days)",
                reference_public_identifier,
            )

        # 7) Notification logic: Compare top 20 from ES vs top 20 from DB (last 60 days)
        # Fetch ranked news from DB filtered to last 60 days
        all_ranked_from_db = await fetch_latest_ranked_news_for_reference(
            reference_public_identifier=reference_public_identifier
        )

        # Filter to last 60 days
        ranked_last_60_days = [
            r
            for r in all_ranked_from_db
            if r.get("published_date")
            and r.get("published_date") >= min_published_epoch_60_days
        ]

        total_ranked_last_60_days = len(ranked_last_60_days)

        should_notify = False

        if total_ranked_last_60_days < 50:
            # Less than 50 items: notify
            should_notify = True
            logger.info(
                "Score News: reference=%s, total ranked news (last 60 days) = %d < 50, should_notify=True",
                reference_public_identifier,
                total_ranked_last_60_days,
            )
        elif es_fetch_failed:
            # ES fetch failed: skip comparison to avoid false notifications
            should_notify = False
            logger.warning(
                "Score News: reference=%s, ES top-20 fetch failed; skipping ES/DB comparison, should_notify=False",
                reference_public_identifier,
            )
        else:
            # >= 50 items: Compare top 20 from ES vs top 20 from DB
            # Sort DB results by final_score descending and take top 20
            ranked_last_60_days_sorted = sorted(
                ranked_last_60_days,
                key=lambda x: float(x.get("final_score", 0.0)),
                reverse=True,
            )[:20]

            # Extract news_ids for comparison (take up to 20 from each)
            top_20_db_news_ids = {
                str(r.get("news_id"))
                for r in ranked_last_60_days_sorted
                if r.get("news_id")
            }

            top_20_es_news_ids = {
                str(item.get("news_id"))
                for item in top_20_es_news[:20]  # Ensure we only compare top 20
                if item.get("news_id")
            }

            # Check if there's a difference (compare sets of news_ids)
            # If sets differ in size or content, notify
            if (
                len(top_20_db_news_ids) != len(top_20_es_news_ids)
                or top_20_db_news_ids != top_20_es_news_ids
            ):
                should_notify = True
                logger.info(
                    "Score News: reference=%s, top 20 news differ (ES vs DB), should_notify=True. "
                    "ES count=%d, DB count=%d. ES news_ids: %s, DB news_ids: %s",
                    reference_public_identifier,
                    len(top_20_es_news_ids),
                    len(top_20_db_news_ids),
                    sorted(list(top_20_es_news_ids))[:10],
                    sorted(list(top_20_db_news_ids))[:10],
                )
            else:
                should_notify = False
                logger.info(
                    "Score News: reference=%s, top 20 news match (ES vs DB), should_notify=False. "
                    "Both have %d news_ids",
                    reference_public_identifier,
                    len(top_20_db_news_ids),
                )

        # Format response
        ranked_news_response = []
        for item in post_processed[:50]:  # Return top 50
            ranked_news_response.append(
                {
                    "connection_public_identifier": item.get(
                        "connection_public_identifier"
                    ),
                    "news_id": item.get("news_id"),
                    "news_title": item.get("news_title"),
                    "content": item.get("content"),
                    "link": item.get("link"),
                    "published_date": item.get("published_date"),
                    "published_date_relative": item.get("published_date_relative"),
                    "news_type": item.get("news_type"),
                    "llm_score": item.get("llm_score"),
                    "llm_reasoning": item.get("llm_reasoning"),
                }
            )

        return JSONResponse(
            content={
                "reference_public_identifier": reference_public_identifier,
                "reference_es_id": reference_es_id,
                "top_connections": top_connections,
                "ranked_news": ranked_news_response,
                "should_notify": should_notify,
                "total_ranked_last_60_days": total_ranked_last_60_days,
            }
        )

    except Exception as e:
        logger.error("Unhandled exception in /score_news: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)
