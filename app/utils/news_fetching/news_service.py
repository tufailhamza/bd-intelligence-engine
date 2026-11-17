import logging
import re
import os
from datetime import datetime, timedelta
from typing import Optional
import uuid
import asyncio

from elasticsearch import AsyncElasticsearch
from app.utils.news_fetching.news_relevance import call_llm_for_news_relevance
from app.utils.news_fetching.news_utils import fetch_news, filter_news_based_on_date

logger = logging.getLogger(__name__)

NUM_RETRIES = 3

COMPANY_CACHE_TABLE = "company_cache"
COMPANY_NEWS_TABLE = "bd_news"


async def _fetch_existing_links_from_es(
    es_client_bd: Optional[AsyncElasticsearch],
    identifier_value: str,
    identifier_field: str = "linkedin_identifier",
    index_name: str = "bd_news",
) -> set:
    """
    Fetch existing links from Elasticsearch bd_news index for a given identifier.
    Returns a set of link strings.

    Args:
        es_client_bd: Elasticsearch client
        identifier_value: Value to search for (company_universal_name, entity_id, or entity_urn)
        identifier_field: Field name to search in ("linkedin_identifier", "entity_id", or "entity_urn")
        index_name: ES index name (default: "bd_news")
    """
    if not es_client_bd:
        logger.warning("ES client not available, skipping duplicate check")
        return set()

    try:
        # Build query based on identifier field
        query_clause = {"term": {f"{identifier_field}.keyword": identifier_value}}

        body = {
            "query": {"bool": {"must": [query_clause]}},
            "_source": ["link"],
            "size": 1000,  # Limit to 1000 matches
        }

        res = await es_client_bd.search(index=index_name, body=body)
        hits = res.get("hits", {}).get("hits", []) or []

        existing_links = set()
        for h in hits:
            src = h.get("_source") or {}
            link = src.get("link")
            if link:
                existing_links.add(str(link))

        logger.info(
            f"Found {len(existing_links)} existing links in ES for {identifier_field}={identifier_value}"
        )
        return existing_links
    except Exception as e:
        logger.error(f"Error fetching existing links from ES: {e}")
        return set()


async def company_news(
    size: int,
    identifier: str,
    company_type: str,
    client,
    cache_date,
    manticore,
    company_display_name: Optional[str] = None,
    entity_urn: Optional[str] = None,
    entity_id: Optional[str] = None,
    company_universal_name: Optional[str] = None,
    return_db_items: bool = False,
    es_client_bd: Optional[AsyncElasticsearch] = None,
):

    query_identifier = company_display_name if company_display_name else identifier

    # Convert cache_date to string for news fetching
    if isinstance(cache_date, int):
        last_cached_date_str = datetime.utcfromtimestamp(cache_date).date().isoformat()
    elif isinstance(cache_date, datetime):
        last_cached_date_str = cache_date.date().isoformat()
    elif isinstance(cache_date, str):
        last_cached_date_str = cache_date  # assume already YYYY-MM-DD
    else:
        last_cached_date_str = datetime.utcnow().date().isoformat()

    payload = await fetch_news(
        cached_date=last_cached_date_str,
        company_name=company_display_name,
        identifier=query_identifier,
        num_retries=NUM_RETRIES,
    )

    if isinstance(payload, dict) and payload.get("error"):
        logger.error(f"Error fetching news for {identifier}")
        return

    payload = await filter_news_based_on_date(payload)

    logger.info(
        f"Fetched {len(payload) if isinstance(payload, list) else 0} news items for company={company_universal_name or identifier}"
    )

    structured_input = ""
    reverse_lookup = {}
    for idx, item in enumerate(payload, start=1):

        title = item.get("title", "").strip()
        snippet = item.get("snippet", "").strip()
        link = item.get("link", "").strip()
        combined_title = f"{title} - {snippet[:300]}"
        structured_input += (
            f"<{idx}><title>{combined_title}</title><link>{link}</link></{idx}>\n"
        )
        reverse_lookup[str(idx)] = item

    relevance_output = await call_llm_relevance_batched(
        structured_input=structured_input, target_company=identifier, batch_size=5
    )

    if isinstance(relevance_output, dict) and relevance_output.get("error"):
        logger.error("Error from LLM relevance call")
        return

    expiration_days = 1 if company_type.lower() == "public" else 7

    # Check if relevance_output is valid
    if not relevance_output:
        logger.info("No relevant news returned by LLM.")
        return 0

    db_items = []
    irrelevant_count = 0

    for item in relevance_output:
        if not item:  # skip None or empty items
            irrelevant_count += 1
            continue

        idx = str(item.get("index"))
        is_relevant = item.get("is_relevant", False)

        if is_relevant and idx in reverse_lookup:
            news_item = reverse_lookup[idx]

            # Always use current timestamp for cache_date (when news is being ingested)
            # The cache_date parameter is only used for filtering (to determine if company needs fresh news)
            current_cache_date = int(datetime.utcnow().timestamp())

            db_item = {
                "company_name": company_display_name or identifier,
                "source": item.get("source") or news_item.get("source", ""),
                "news_title": news_item.get("title", ""),
                "content": news_item.get("snippet", ""),
                "news_id": str(uuid.uuid4()),
                "company_universal_name": company_universal_name
                or news_item.get("company_universal_name", ""),
                "entity_urn": entity_urn,
                "entity_id": entity_id,
                "link": news_item.get("link", ""),
                "published_date": news_item.get("published_date"),
                "news_type": item.get("news_type") or news_item.get("news_type", ""),
                "news_sub_type": item.get("news_subtype")
                or news_item.get("news_subtype", ""),
                "cache_date": current_cache_date,  # Always use current timestamp when ingesting
                "expiration_days": expiration_days,
                "imageCount": item.get("imageCount", 0),
                "videoCount": item.get("videoCount", 0),
            }

            db_items.append(db_item)
        else:
            irrelevant_count += 1

    if not db_items:
        logger.info(
            f"No relevant news to insert after filtering for company={company_universal_name or identifier}"
        )
        return 0

    logger.info(
        f"Processing {len(db_items)} relevant news items for company={company_universal_name or identifier}"
    )

    inserted_ids = []
    inserted_items = []
    try:
        inserted_ids = await cache_company_news_manticore(
            db_items=db_items,
            company_universal_name=company_universal_name,
            entity_urn=entity_urn,
            entity_id=entity_id,
            manticore=manticore,
            es_client_bd=es_client_bd,  # Pass ES client for duplicate checking
        )
        inserted_count = len(inserted_ids)
        logger.info(
            f"Company news ingested into Manticore: {inserted_count} items for company={company_universal_name or identifier}"
        )
        if inserted_ids:
            inserted_set = set(str(x) for x in inserted_ids)
            inserted_items = [
                x for x in db_items if str(x.get("news_id")) in inserted_set
            ]

        if inserted_ids:
            await upsert_company_cache_date(
                company_universal_name,
                entity_urn,
                manticore=manticore,
                es_client_bd=es_client_bd,
            )

    except Exception as e:
        logger.error(f"Error storing news in Manticore: {e}")
        inserted_count = 0
        inserted_ids = []
        inserted_items = []

    if return_db_items:
        return {
            "inserted_count": inserted_count,
            "inserted_ids": inserted_ids,
            "inserted_items": inserted_items,
            "company_universal_name": company_universal_name or identifier,
        }

    return inserted_count


async def cache_company_news_manticore(
    db_items: list,
    company_universal_name: Optional[str],
    entity_urn: Optional[str],
    entity_id: Optional[str],
    manticore,
    es_client_bd: Optional[AsyncElasticsearch] = None,
):
    """
    Store company news items in Manticore (bd_news table), removing duplicates based on link.
    Duplicate checking is done against Elasticsearch bd_news index instead of Manticore.
    """
    if not db_items:
        return []

    inserted_ids = []
    cache_date_str = int(datetime.utcnow().timestamp())

    # Step 1: Determine identifier field and value for ES query
    if company_universal_name:
        identifier_field = "linkedin_identifier"
        identifier_value = company_universal_name
    elif entity_id:
        identifier_field = "entity_id"
        identifier_value = entity_id
    elif entity_urn:
        identifier_field = "entity_urn"
        identifier_value = entity_urn
    else:
        logger.warning(
            "No identifier available to check duplicates. Inserting all items."
        )
        existing_links = set()

    # Step 2: Fetch existing links from Elasticsearch bd_news index
    if company_universal_name or entity_id or entity_urn:
        existing_links = await _fetch_existing_links_from_es(
            es_client_bd=es_client_bd,
            identifier_value=identifier_value,
            identifier_field=identifier_field,
            index_name="bd_news",
        )
    else:
        existing_links = set()

    # Step 2: Filter out duplicates
    filtered_items = [
        item for item in db_items if item.get("link") not in existing_links
    ]

    logger.info(f"{len(db_items) - len(filtered_items)} duplicate news items removed.")
    if not filtered_items:
        logger.info("No new news items to insert after filtering duplicates.")
        return []

    # Step 3: Insert filtered items one by one
    for item in filtered_items:
        try:
            query = f"""
                INSERT INTO {COMPANY_NEWS_TABLE} (
                    news_id,
                    linkedin_identifier,
                    entity_urn,
                    entity_id,
                    entity_type,
                    company_name,
                    author,
                    author_description,
                    post_type,
                    published_date,
                    published_date_relative,
                    news_title,
                    content,
                    link,
                    news_type,
                    news_sub_type,
                    source,
                    cache_date,
                    expiration_days,
                    original_author_linkedin_identifier,
                    original_author_entity_id,
                    original_author_entity_urn,
                    repost_original_entity_type,
                    imageCount,
                    videoCount
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            args = (
                item["news_id"],
                item.get("company_universal_name"),  # renamed to linkedin_identifier
                item.get("entity_urn"),
                item.get("entity_id"),
                "company_search",  # hardcoded entity_type
                item.get("company_name"),
                "",  # author not available
                "",  # author_description not available
                "",  # post_type not available
                item.get("published_date"),
                "",  # published_date_relative not available
                item.get("news_title"),
                item.get("content"),
                item.get("link"),
                item.get("news_type"),
                item.get("news_sub_type"),
                item.get("source"),
                cache_date_str,
                item.get("expiration_days"),
                "",  # original_author_linkedin_identifier
                "",  # original_author_entity_id
                "",  # original_author_entity_urn
                "",  # repost_original_entity_type
                item.get("imageCount", 0),
                item.get("videoCount", 0),
            )
            await manticore.execute(query, args)
            inserted_ids.append(item["news_id"])
        except Exception as e:
            logger.error(f"Error inserting news {item.get('news_title')}: {e}")

    logger.info(f"Inserted {len(inserted_ids)} new company news items.")
    return inserted_ids


async def upsert_company_cache_date(
    company_universal_name: Optional[str],
    entity_urn: Optional[str],
    manticore=None,  # Deprecated - kept for backward compatibility but not used
    es_client_bd: Optional[AsyncElasticsearch] = None,
):
    """
    Update cache_date for a company. Since we're using ES for cache tracking,
    the cache_date is automatically updated when news items are inserted (they have cache_date field).
    This function is kept for backward compatibility but does nothing - cache_date is derived from ES news items.
    """
    # Cache date is now tracked via the cache_date field in ES news items
    # No separate cache table needed - we query ES for max cache_date when needed
    logger.debug(
        f"upsert_company_cache_date called for company_universal_name={company_universal_name}, "
        f"entity_urn={entity_urn} - cache_date is now tracked in ES news items"
    )


async def call_llm_relevance_batched(
    structured_input: str, target_company: str, batch_size: int = 5
):
    """
    Split structured_input into batches of `batch_size` items,
    call LLM concurrently, and accumulate results safely.
    Gracefully handles errors, misformatted responses, or empty outputs.
    """
    # Split into individual items like <1>...</1>
    items = [line for line in structured_input.strip().split("\n") if line.strip()]
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]

    tasks = []
    for batch in batches:
        batch_input = "\n".join(batch)
        tasks.append(call_llm_for_news_relevance(batch_input, target_company))

    # Run all batches concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    accumulated = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.warning(f"Batch {i} failed with exception: {res}")
            continue
        if not res:  # handles None, "", [], etc.
            continue
        # Only include well-formed entries (already filtered in call_llm_for_news_relevance)
        accumulated.extend(res)

    return accumulated
