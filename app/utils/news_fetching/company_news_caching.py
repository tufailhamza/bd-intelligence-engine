import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from elasticsearch import AsyncElasticsearch

logger = logging.getLogger(__name__)


async def _fetch_company_cache_date_from_es(
    es_client_bd: Optional[AsyncElasticsearch],
    company_universal_name: Optional[str] = None,
    entity_urn: Optional[str] = None,
    index_name: str = "bd_news",
) -> Optional[int]:
    """
    Fetch the latest cache_date from Elasticsearch bd_news index for a company.
    Returns the maximum cache_date (epoch timestamp) or None if no news found.

    Args:
        es_client_bd: Elasticsearch client
        company_universal_name: Company universal name (optional)
        entity_urn: Entity URN (optional)
        index_name: ES index name (default: "bd_news")

    Returns:
        Latest cache_date as epoch timestamp (int) or None
    """
    if not es_client_bd:
        return None

    if not company_universal_name and not entity_urn:
        return None

    try:
        # Build query to find company news
        must_clauses = []
        if company_universal_name:
            must_clauses.append(
                {"term": {"linkedin_identifier.keyword": company_universal_name}}
            )
        if entity_urn:
            must_clauses.append({"term": {"entity_urn.keyword": entity_urn}})

        body = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"entity_type.keyword": "company_search"}},
                        *must_clauses,
                    ]
                }
            },
            "size": 0,
            "aggs": {"max_cache_date": {"max": {"field": "cache_date"}}},
        }

        res = await es_client_bd.search(index=index_name, body=body)
        max_cache_date = (
            res.get("aggregations", {}).get("max_cache_date", {}).get("value")
        )

        if max_cache_date is not None:
            return int(max_cache_date)

        return None
    except Exception as e:
        logger.error(f"Error fetching cache_date from ES: {e}")
        return None


async def fetch_company_cache_dates(
    companies: Dict[str, Dict[str, Any]],
    es_client_bd: Optional[AsyncElasticsearch] = None,
    manticore=None,  # Kept for backward compatibility but not used
) -> List[Dict[str, Any]]:
    """
    Fetch cache_date timestamps for given companies from Elasticsearch bd_news index.

    Args:
        companies: Dict of companies keyed by name, each value is a dict with LinkedIn info
        es_client_bd: Elasticsearch client for BD news index
        manticore: Deprecated - kept for backward compatibility but not used

    Returns:
        List of dicts, each with:
            - All original company fields
            - company_universal_name
            - company_urn
            - cache_date (epoch timestamp int or None)
    """
    results: List[Dict[str, Any]] = []

    for key, company in companies.items():
        # normalize keys
        universal_name = company.get("li_universalname", "")
        urn = company.get("li_urn") or company.get("company_urn") or ""

        cache_date = None

        if universal_name or urn:
            cache_date = await _fetch_company_cache_date_from_es(
                es_client_bd=es_client_bd,
                company_universal_name=universal_name if universal_name else None,
                entity_urn=urn if urn else None,
                index_name="bd_news",
            )

        results.append(
            {
                **company,
                "company_universal_name": universal_name,
                "company_urn": urn,
                "cache_date": cache_date,
            }
        )

    return results


# async def process_company_cache_dates(
#     companies: Dict[str, Dict[str, Any]], manticore: ManticoreConnection
# ) -> List[Dict[str, Any]]:
#     """
#     Fetch cache_date and filter companies based on staff count and cache freshness.

#     Rules:
#         - If no cache_date:
#             - staff > 10000 → assign 1 day ago
#             - else → assign 1 week ago
#         - If cache_date exists:
#             - staff > 10000 → keep only if cache_date older than 1 day
#             - staff <= 10000 → keep only if cache_date older than 1 week

#     Returns:
#         Filtered list of companies that need re-caching, with all original fields preserved.
#     """
#     companies_with_cache = await fetch_company_cache_dates(companies, manticore)
#     now = datetime.utcnow()
#     filtered: List[Dict[str, Any]] = []

#     for comp in companies_with_cache:
#         staffcount = comp.get("li_staffcount") or comp.get("staffcount") or 0
#         cache_date = comp.get("cache_date")

#         if isinstance(cache_date, str):
#             try:
#                 cache_date = datetime.fromisoformat(cache_date)
#             except Exception:
#                 cache_date = None

#         if not cache_date:
#             # no cache record → assign default stale value
#             if staffcount > 10000:
#                 comp["cache_date"] = now - timedelta(days=1)
#             else:
#                 comp["cache_date"] = now - timedelta(weeks=1)
#             filtered.append(comp)
#         else:
#             if staffcount > 10000:
#                 if cache_date <= now - timedelta(days=1):
#                     filtered.append(comp)
#             else:
#                 if cache_date <= now - timedelta(weeks=1):
#                     filtered.append(comp)

#     return filtered

# async def process_company_cache_dates(
#     companies: Dict[str, Dict[str, Any]], manticore: ManticoreConnection
# ) -> List[Dict[str, Any]]:
#     """
#     Fetch cache_date and filter companies based on staff count and cache freshness.
#     """
#     companies_with_cache = await fetch_company_cache_dates(companies, manticore)
#     now = int(datetime.utcnow().timestamp())
#     filtered: List[Dict[str, Any]] = []

#     for comp in companies_with_cache:
#         staffcount = comp.get("li_staffcount") or comp.get("staffcount") or 0
#         cache_date = comp.get("cache_date")

#         if isinstance(cache_date, str):
#             try:
#                 cache_date = datetime.fromisoformat(cache_date)
#             except Exception as e:
#                 print(f"[DEBUG] Failed to parse cache_date '{cache_date}': {e}")
#                 cache_date = None

#         print(f"[DEBUG] Company: {comp.get('li_name') or comp.get('name')}, "
#               f"Staff: {staffcount}, Cache: {cache_date}")

#         if not cache_date:
#             # no cache record → assign default stale value
#             if staffcount > 10000:
#                 comp["cache_date"] = now - timedelta(days=1)
#                 print(f"[DEBUG] No cache, staff>10000 → set cache_date to {comp['cache_date']}")
#             else:
#                 comp["cache_date"] = now - timedelta(weeks=1)
#                 print(f"[DEBUG] No cache, staff<=10000 → set cache_date to {comp['cache_date']}")
#             filtered.append(comp)
#             print(f"[DEBUG] Company added to filtered list (no cache)")
#         else:
#             if staffcount > 10000:
#                 if cache_date <= now - timedelta(days=1):
#                     filtered.append(comp)
#                     print(f"[DEBUG] Staff>10000 and cache stale → company added")
#                 else:
#                     print(f"[DEBUG] Staff>10000 and cache fresh → company skipped")
#             else:
#                 if cache_date <= now - timedelta(weeks=1):
#                     filtered.append(comp)
#                     print(f"[DEBUG] Staff<=10000 and cache stale → company added")
#                 else:
#                     print(f"[DEBUG] Staff<=10000 and cache fresh → company skipped")

#     print(f"[DEBUG] Total companies after filtering: {len(filtered)}")
#     return filtered


async def process_company_cache_dates(
    companies: Dict[str, Dict[str, Any]],
    es_client_bd=None,
    manticore=None,  # Deprecated - kept for backward compatibility
) -> List[Dict[str, Any]]:
    companies_with_cache = await fetch_company_cache_dates(
        companies, es_client_bd=es_client_bd, manticore=manticore
    )
    now = int(datetime.utcnow().timestamp())  # epoch integer
    filtered: List[Dict[str, Any]] = []

    for comp in companies_with_cache:
        staffcount = comp.get("li_staffcount") or comp.get("staffcount") or 0
        cache_date = comp.get("cache_date")

        # Convert string datetime to epoch
        if isinstance(cache_date, str):
            try:
                cache_date = int(datetime.fromisoformat(cache_date).timestamp())
            except Exception as e:
                logger.debug(f"[DEBUG] Failed to parse cache_date '{cache_date}': {e}")
                cache_date = None
        elif isinstance(cache_date, datetime):
            cache_date = int(cache_date.timestamp())
        elif not isinstance(cache_date, int):
            cache_date = None

        print(
            f"[DEBUG] Company: {comp.get('li_name') or comp.get('name')}, "
            f"Staff: {staffcount}, Cache: {cache_date}"
        )

        if not cache_date:
            if staffcount > 10000:
                comp["cache_date"] = now - 86400  # 1 day ago
            else:
                comp["cache_date"] = now - 604800  # 1 week ago
            filtered.append(comp)
        else:
            if staffcount > 10000 and cache_date <= now - 86400:
                filtered.append(comp)
            elif staffcount <= 10000 and cache_date <= now - 604800:
                filtered.append(comp)

    return filtered
