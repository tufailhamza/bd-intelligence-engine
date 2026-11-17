import logging
import traceback
import asyncio
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from typing import Any, Dict, List

from app.core.manticore_client import ManticoreConnection
from app.utils.person_news.person_news_processing import cache_person_news_manticore
from app.routes.dependancies import get_manticore_client, get_es_client_bd
from app.utils.person_news.person_news_indexing import (
    build_bd_news_docs_from_processed_posts,
    bulk_index_bd_news,
)
from app.utils.notifications import notify_news_ingested

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/cache_linkedin_news")
async def cache_linkedin_news(
    data: Dict[str, Any],
    manticore=Depends(get_manticore_client),
    es_client_bd=Depends(get_es_client_bd),
):
    """
    Endpoint to ingest LinkedIn posts for a person,
    process them via LLM, and store into Manticore.
    """
    try:
        user_id = data.get("public_identifier")
        posts = data.get("posts", [])
        entity_urn = data.get("entity_urn")
        entity_id = data.get("entity_id")
        last_author_name = data.get("last_author_name")
        last_author_description = data.get("last_author_description")

        if not user_id or not posts or not entity_urn or not entity_id:
            return JSONResponse(
                content={
                    "success": False,
                    "error": "Missing required fields: user_id/public_identifier, posts, entity_urn, entity_id",
                },
                status_code=400,
            )

        logger.info(
            f"Starting LinkedIn news ingestion for user_id={user_id}, received {len(posts)} posts"
        )

        # Process via LLM and save to Manticore (keep existing behavior)
        manticore_result = await cache_person_news_manticore(
            user_id=user_id,
            posts=posts,
            entity_urn=entity_urn,
            entity_id=entity_id,
            manticore=manticore,
            last_author_name=last_author_name,
            last_author_description=last_author_description,
            return_processed_posts=True,
            es_client_bd=es_client_bd,  # Pass ES client for duplicate checking
        )

        # Backward/defensive handling: should be a dict when return_processed_posts=True,
        # but older paths may return a list.
        if isinstance(manticore_result, dict):
            inserted_ids = manticore_result.get("inserted_ids", []) or []
            processed_posts = manticore_result.get("processed_posts", []) or []
        else:
            inserted_ids = manticore_result or []
            processed_posts = []

        logger.info(
            f"News ingested into Manticore: {len(inserted_ids)} items for user_id={user_id}"
        )

        # Also ingest into BD Elasticsearch (bd_news index) using BD credentials
        es_indexed = 0
        if es_client_bd:
            try:
                # Only index newly inserted items
                inserted_set = set(str(x) for x in inserted_ids)
                processed_posts = [
                    p
                    for p in processed_posts
                    if str(p.get("activityUrn") or "") in inserted_set
                ]

                docs = build_bd_news_docs_from_processed_posts(
                    user_id=user_id,
                    entity_urn=entity_urn,
                    entity_id=entity_id,
                    processed_posts=processed_posts,
                    last_author_name=last_author_name or "",
                    last_author_description=last_author_description or "",
                )
                bulk_res = await bulk_index_bd_news(
                    es_client_bd=es_client_bd,
                    docs=docs,
                    index_name="bd_news",
                )
                es_indexed = int(bulk_res.get("indexed", 0))
                logger.info(
                    f"News indexed into Elasticsearch: {es_indexed} items for user_id={user_id}"
                )

                # Notify that news has been ingested (fire-and-forget, non-blocking)
                if es_indexed > 0:
                    asyncio.create_task(
                        notify_news_ingested(user_id=user_id, entity_urn=entity_urn)
                    )
            except Exception as e:
                logger.error(f"Error indexing LinkedIn news to BD ES: {e}")

        logger.info(
            f"LinkedIn news ingestion completed for user_id={user_id}: Manticore={len(inserted_ids)}, Elasticsearch={es_indexed}"
        )

        return JSONResponse(
            content={
                "success": True,
                "inserted_count": len(inserted_ids),
                "es_indexed_count": es_indexed,
            }
        )

    except Exception as e:
        logger.error(f"Error in cache_linkedin_news endpoint: {e}")
        traceback.print_exc()
        return JSONResponse(
            content={"success": False, "error": str(e)}, status_code=500
        )


async def _fetch_existing_news_docs_from_es(
    es_client_bd,
    news_ids: List[str],
    index_name: str = "bd_news",
) -> List[Dict[str, Any]]:
    """
    Fetch full documents from bd_news ES index by document _id
    (which equals news_id at index time). Uses mget for exact, reliable lookup.
    Returns the full list of _source dicts for found documents.
    """
    if not es_client_bd or not news_ids:
        return []
    try:
        body = {"ids": news_ids}
        res = await es_client_bd.mget(index=index_name, body=body)
        docs = res.get("docs") or []
        return [d["_source"] for d in docs if d.get("found") and d.get("_source")]
    except Exception as e:
        logger.error(f"Error fetching existing news docs from ES via mget: {e}")
        return []


@router.post("/cache_linkedin_news_using_bd")
async def cache_linkedin_news_using_bd(
    data: Dict[str, Any],
    manticore=Depends(get_manticore_client),
    es_client_bd=Depends(get_es_client_bd),
):
    """
    Same as /cache_linkedin_news but also returns the full payload for
    posts that already exist in bd_news (duplicates are not re-inserted).
    """
    try:
        user_id = data.get("public_identifier")
        posts = data.get("posts", [])
        entity_urn = data.get("entity_urn")
        entity_id = data.get("entity_id")
        last_author_name = data.get("last_author_name")
        last_author_description = data.get("last_author_description")

        if not user_id or not posts or not entity_urn or not entity_id:
            return JSONResponse(
                content={
                    "success": False,
                    "error": "Missing required fields: user_id/public_identifier, posts, entity_urn, entity_id",
                },
                status_code=400,
            )

        logger.info(
            f"Starting LinkedIn news ingestion (bd) for user_id={user_id}, received {len(posts)} posts"
        )

        # Identify which incoming posts already exist before processing
        incoming_ids = [
            str(p.get("activityUrn")) for p in posts if p.get("activityUrn")
        ]

        # Process via LLM and save to Manticore + ES (same as existing endpoint)
        manticore_result = await cache_person_news_manticore(
            user_id=user_id,
            posts=posts,
            entity_urn=entity_urn,
            entity_id=entity_id,
            manticore=manticore,
            last_author_name=last_author_name,
            last_author_description=last_author_description,
            return_processed_posts=True,
            es_client_bd=es_client_bd,
        )

        if isinstance(manticore_result, dict):
            inserted_ids = manticore_result.get("inserted_ids", []) or []
            processed_posts = manticore_result.get("processed_posts", []) or []
        else:
            inserted_ids = manticore_result or []
            processed_posts = []

        logger.info(
            f"News ingested into Manticore: {len(inserted_ids)} items for user_id={user_id}"
        )

        # Index newly inserted items into BD ES
        es_indexed = 0
        if es_client_bd:
            try:
                inserted_set = set(str(x) for x in inserted_ids)
                new_posts = [
                    p
                    for p in processed_posts
                    if str(p.get("activityUrn") or "") in inserted_set
                ]

                docs = build_bd_news_docs_from_processed_posts(
                    user_id=user_id,
                    entity_urn=entity_urn,
                    entity_id=entity_id,
                    processed_posts=new_posts,
                    last_author_name=last_author_name or "",
                    last_author_description=last_author_description or "",
                )
                bulk_res = await bulk_index_bd_news(
                    es_client_bd=es_client_bd,
                    docs=docs,
                    index_name="bd_news",
                )
                es_indexed = int(bulk_res.get("indexed", 0))
                logger.info(
                    f"News indexed into Elasticsearch: {es_indexed} items for user_id={user_id}"
                )

                if es_indexed > 0:
                    asyncio.create_task(
                        notify_news_ingested(user_id=user_id, entity_urn=entity_urn)
                    )
            except Exception as e:
                logger.error(f"Error indexing LinkedIn news to BD ES: {e}")

        # Determine which incoming ids were already existing (not newly inserted)
        inserted_id_strs = set(str(x) for x in inserted_ids)
        already_existing_ids = [
            nid for nid in incoming_ids if nid not in inserted_id_strs
        ]

        # Fetch full payloads for the already-existing posts from ES
        existing_news_docs: List[Dict[str, Any]] = []
        if already_existing_ids and es_client_bd:
            existing_news_docs = await _fetch_existing_news_docs_from_es(
                es_client_bd=es_client_bd,
                news_ids=already_existing_ids,
                index_name="bd_news",
            )

        logger.info(
            f"LinkedIn news ingestion (bd) completed for user_id={user_id}: "
            f"Manticore={len(inserted_ids)}, Elasticsearch={es_indexed}, "
            f"already_existing={len(existing_news_docs)}"
        )

        return JSONResponse(
            content={
                "success": True,
                "inserted_count": len(inserted_ids),
                "es_indexed_count": es_indexed,
                "already_existing_count": len(existing_news_docs),
                "already_existing_news": existing_news_docs,
            }
        )

    except Exception as e:
        logger.error(f"Error in cache_linkedin_news_using_bd endpoint: {e}")
        traceback.print_exc()
        return JSONResponse(
            content={"success": False, "error": str(e)}, status_code=500
        )
