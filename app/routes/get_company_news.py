import logging
import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from types import SimpleNamespace
from pydantic import BaseModel, Field
from app.models.schemas.company_news_fetching import NewsInput
from app.routes.dependancies import (
    get_es_client,
    get_es_client_bd,
    get_manticore_client,
)
from app.utils.news_fetching.company_news_fetching import (
    fetch_company_names_bulk,
    to_number,
)

from app.utils.news_fetching.company_news_caching import process_company_cache_dates
from app.utils.news_fetching.news_service import company_news
from app.utils.news_fetching.company_news_indexing import (
    build_bd_news_docs_from_company_items,
)
from app.utils.person_news.person_news_indexing import bulk_index_bd_news
from app.utils.notifications import notify_news_ingested_batch

logger = logging.getLogger(__name__)


router = APIRouter()


@router.post("/ingest_company_news")
async def get_company_news(
    request: NewsInput,
    es_client=Depends(get_es_client),
    es_client_bd=Depends(get_es_client_bd),
    manticore_client=Depends(get_manticore_client),
):

    try:
        company_info_map = await fetch_company_names_bulk(request.companies, es_client)
        companies_to_fetch = await process_company_cache_dates(
            company_info_map, es_client_bd=es_client_bd, manticore=manticore_client
        )

        logger.info(
            f"Starting company news ingestion for {len(companies_to_fetch)} companies"
        )

        result_dict = {}
        companies_to_notify = []  # Collect company IDs for batch notification

        # Wrap dicts into objects with ownership_types
        prepared_companies = [
            SimpleNamespace(**company_dict, ownership_types=["public"])
            for company_dict in companies_to_fetch
        ]

        for company_obj in prepared_companies:
            # Use company_info_map if available
            company_info = (
                company_info_map.get(company_obj.company_universal_name)
                if company_info_map
                else None
            )

            # Mirror fetch_company_news_for_one_company() but return inserted items for ES indexing
            company_size = company_info.get("li_size") if company_info else None
            company_staffcount = (
                company_info.get("li_staffcount") if company_info else None
            )
            numeric_values = filter(
                None, map(to_number, [company_size, company_staffcount])
            )
            company_size_final = max(numeric_values, default=None)

            res = await company_news(
                identifier=company_obj.company_universal_name,
                company_display_name=(
                    company_info.get("li_name") if company_info else None
                ),
                company_type=(
                    company_obj.ownership_types[0]
                    if company_obj.ownership_types
                    else "private"
                ),
                client=es_client,
                size=company_size_final,
                cache_date=company_obj.cache_date,
                manticore=manticore_client,
                entity_urn=company_obj.company_urn,
                entity_id=(
                    company_info.get("entity_id")
                    if company_info
                    else company_obj.entity_id
                ),
                company_universal_name=company_obj.company_universal_name,
                return_db_items=True,
                es_client_bd=es_client_bd,  # Pass ES client for duplicate checking
            )

            inserted_count = (
                int(res.get("inserted_count", 0)) if isinstance(res, dict) else 0
            )
            result_dict[company_obj.company_universal_name] = inserted_count

            logger.info(
                f"Company news ingested into Manticore: {inserted_count} items for company={company_obj.company_universal_name}"
            )

            # ALSO index into BD Elasticsearch (bd_news index) if client available
            es_indexed = 0
            if es_client_bd and isinstance(res, dict):
                inserted_items = res.get("inserted_items") or []
                docs = build_bd_news_docs_from_company_items(
                    company_universal_name=company_obj.company_universal_name,
                    entity_urn=company_obj.company_urn,
                    entity_id=(
                        company_info.get("entity_id")
                        if company_info
                        else company_obj.entity_id
                    ),
                    company_items=inserted_items,
                )
                try:
                    bulk_res = await bulk_index_bd_news(
                        es_client_bd=es_client_bd,
                        docs=docs,
                        index_name="bd_news",
                    )
                    es_indexed = int(bulk_res.get("indexed", 0))
                    logger.info(
                        f"Company news indexed into Elasticsearch: {es_indexed} items for company={company_obj.company_universal_name}"
                    )

                    # Collect company ID for batch notification (non-blocking)
                    if es_indexed > 0:
                        company_id = company_obj.company_universal_name or (
                            company_info.get("entity_id")
                            if company_info
                            else company_obj.entity_id
                        )
                        companies_to_notify.append(company_id)
                except Exception as e:
                    logger.error(f"Error indexing company news to BD ES: {e}")

            logger.info(
                f"Company news ingestion completed for company={company_obj.company_universal_name}: Manticore={inserted_count}, Elasticsearch={es_indexed}"
            )

        total_ingested = sum(result_dict.values())
        logger.info(
            f"Company news ingestion completed for all companies: {len(result_dict)} companies processed, {total_ingested} total news items ingested"
        )

        # Send batch notifications asynchronously (non-blocking)
        if companies_to_notify:
            asyncio.create_task(notify_news_ingested_batch(companies_to_notify))
            logger.info(
                f"Scheduled batch notification for {len(companies_to_notify)} companies"
            )

        return {"news_counts": result_dict}

    except Exception as e:
        logger.error(f"Error ingesting company news: {e}")
        return {"news_counts": {}}


class CompanyNewsFetchRequest(BaseModel):
    linkedin_identifier: str = Field(
        ..., description="LinkedIn company slug, e.g. 'google'"
    )
    page: int = Field(1, ge=1, description="1-based page number")
    page_size: int = Field(20, ge=1, le=100, description="Number of results per page")
    sort_order: Optional[str] = Field(
        "desc", description="Sort order for published_date: 'asc' or 'desc'"
    )


_COMPANY_NEWS_SOURCE_FIELDS = [
    "news_id",
    "news_title",
    "content",
    "link",
    "published_date",
    "published_date_relative",
    "post_type",
    "news_type",
    "news_sub_type",
    "author",
    "author_description",
    "last_author_name",
    "last_author_description",
    "source",
    "entity_urn",
    "entity_id",
    "linkedin_identifier",
    "imagecount",
    "videocount",
    "cache_date",
]


@router.post("/get_company_news")
async def get_company_news_paginated(
    request: CompanyNewsFetchRequest,
    es_client_bd=Depends(get_es_client_bd),
):
    """
    Fetch paginated company news from bd_news ES index filtered by linkedin_identifier.
    """
    try:
        if not es_client_bd:
            return JSONResponse(
                content={"success": False, "error": "ES BD client not available"},
                status_code=503,
            )

        sort_order = (
            request.sort_order if request.sort_order in ("asc", "desc") else "desc"
        )
        from_offset = (request.page - 1) * request.page_size

        body: Dict[str, Any] = {
            "query": {
                "term": {"linkedin_identifier.keyword": request.linkedin_identifier}
            },
            "sort": [{"published_date": {"order": sort_order}}],
            "from": from_offset,
            "size": request.page_size,
            "_source": _COMPANY_NEWS_SOURCE_FIELDS,
        }

        res = await es_client_bd.search(index="bd_news", body=body)
        hits = res.get("hits", {})
        total_value = hits.get("total", {})
        total = (
            total_value.get("value", 0)
            if isinstance(total_value, dict)
            else int(total_value or 0)
        )
        raw_hits = hits.get("hits", []) or []

        news_items: List[Dict[str, Any]] = []
        for h in raw_hits:
            src = h.get("_source") or {}
            if isinstance(src, dict):
                news_items.append(src)

        total_pages = max(1, (total + request.page_size - 1) // request.page_size)

        logger.info(
            f"Fetched company news for linkedin_identifier={request.linkedin_identifier}: "
            f"page={request.page}, page_size={request.page_size}, "
            f"returned={len(news_items)}, total={total}"
        )

        return JSONResponse(
            content={
                "success": True,
                "linkedin_identifier": request.linkedin_identifier,
                "page": request.page,
                "page_size": request.page_size,
                "total": total,
                "total_pages": total_pages,
                "news": news_items,
            }
        )

    except Exception as e:
        logger.error(f"Error fetching company news: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)}, status_code=500
        )
