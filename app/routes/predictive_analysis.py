import logging
import os
import time
import traceback

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.database.ranked_news import delete_predictive_ranked_news
from app.models.schemas.predictive_analysis import PredictiveAnalysisInput
from app.routes.dependancies import (
    get_es_client_bd,
    get_es_client_with_compression,
)
from app.utils.person_news.person_news_indexing import bulk_index_bd_news
from app.utils.predictive_analysis.service import (
    NEWS_TYPE,
    build_experience_fingerprint,
    build_predictive_trigger_docs,
    build_profile_packet,
    cached_docs_for_experience,
    existing_docs_by_trigger_type,
    enrich_triggers_with_people_to_reach_out,
    fetch_existing_predictive_trigger_docs,
    generate_predictive_triggers,
    trigger_payloads_from_docs,
)
from app.utils.profile_fetching.public_identifier_fetch import fetch_profile_by_es_id

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/predictive_analysis")
async def predictive_analysis(
    request: PredictiveAnalysisInput,
    es_client_compressed=Depends(get_es_client_with_compression),
    es_client_bd=Depends(get_es_client_bd),
):
    try:
        started = time.perf_counter()
        profile_id = request.profile_id.strip()
        if not profile_id:
            return JSONResponse(
                content={"error": "profile_id is required"},
                status_code=400,
            )

        es_id, profile = await fetch_profile_by_es_id(
            profile_id,
            es_client=es_client_compressed,
            source_fields=None,
            only_top_experience=False,
        )
        if profile is None:
            return JSONResponse(
                content={
                    "profile_id": profile_id,
                    "error": f"Profile not found: {profile_id}",
                },
                status_code=404,
            )

        profile_packet = build_profile_packet(profile_id, profile)
        public_identifier = str(profile.get("public_identifier") or "").strip()

        if es_client_bd is None:
            return JSONResponse(
                content={
                    "profile_id": profile_id,
                    "error": "BD Elasticsearch client not initialized (missing ES_URL_BD / ES_API_KEY_BD)",
                    "triggers": [],
                    "indexed_count": 0,
                },
                status_code=500,
            )

        bd_news_index = (os.getenv("ES_BD_NEWS_INDEX") or "bd_news").strip()
        experience_fingerprint = build_experience_fingerprint(profile_packet)
        existing_docs = await fetch_existing_predictive_trigger_docs(
            es_client_bd=es_client_bd,
            profile_id=profile_id,
            index_name=bd_news_index,
        )
        cached_docs = cached_docs_for_experience(
            existing_docs,
            experience_fingerprint,
        )
        if cached_docs:
            cached_profile_summary, cached_triggers = trigger_payloads_from_docs(
                cached_docs
            )
            cached_has_people_to_reach_out = all(
                "people_to_reach_out" in doc for doc in cached_docs
            )
            cached_has_person_ctas = all(
                "predictive_trigger_person_ctas" in doc for doc in cached_docs
            )
            cached_people_counts = [
                len(doc.get("people_to_reach_out") or []) for doc in cached_docs
            ]
            cached_stakeholder_counts = [
                len(
                    [
                        person
                        for person in (doc.get("people_to_reach_out") or [])
                        if isinstance(person, dict)
                        and not person.get("is_original_profile")
                    ]
                )
                for doc in cached_docs
            ]
            logger.info(
                "Predictive analysis cache check profile_id=%s cached_docs=%d has_people=%s has_person_ctas=%s people_counts=%s stakeholder_counts=%s",
                profile_id,
                len(cached_docs),
                cached_has_people_to_reach_out,
                cached_has_person_ctas,
                cached_people_counts,
                cached_stakeholder_counts,
            )
            if (
                cached_triggers
                and cached_has_people_to_reach_out
                and cached_has_person_ctas
            ):
                logger.info(
                    "Predictive analysis profile_id=%s cache hit with %d triggers",
                    profile_id,
                    len(cached_triggers),
                )
                return JSONResponse(
                    content={
                        "profile_id": profile_id,
                        "es_id": es_id,
                        "public_identifier": public_identifier,
                        "profile_summary": cached_profile_summary,
                        "triggers": cached_triggers,
                        "indexed_count": 0,
                        "indexed_news_ids": [
                            doc.get("news_id") or doc.get("id") or doc.get("_id")
                            for doc in cached_docs
                        ],
                        "dropped_triggers": [],
                        "cache_hit": True,
                        "experience_fingerprint": experience_fingerprint,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    }
                )
            if cached_triggers:
                profile_summary = cached_profile_summary
                triggers = cached_triggers
                dropped = []
            else:
                profile_summary, triggers, dropped, _raw_response = (
                    await generate_predictive_triggers(profile_packet)
                )
        else:
            profile_summary, triggers, dropped, _raw_response = (
                await generate_predictive_triggers(profile_packet)
            )

        if not triggers:
            logger.info(
                "Predictive analysis profile_id=%s produced no valid triggers. dropped=%d",
                profile_id,
                len(dropped),
            )
            return JSONResponse(
                content={
                    "profile_id": profile_id,
                    "es_id": es_id,
                    "public_identifier": public_identifier,
                    "profile_summary": profile_summary,
                    "triggers": [],
                    "indexed_count": 0,
                    "dropped_triggers": dropped,
                    "cache_hit": False,
                    "experience_fingerprint": experience_fingerprint,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            )

        triggers = await enrich_triggers_with_people_to_reach_out(
            triggers=triggers,
            profile=profile,
            profile_id=profile_id,
            es_client=es_client_compressed,
        )

        existing_by_type = existing_docs_by_trigger_type(existing_docs)
        docs = build_predictive_trigger_docs(
            profile_id=profile_id,
            profile=profile,
            profile_packet=profile_packet,
            profile_summary=profile_summary,
            triggers=triggers,
            existing_docs_by_type=existing_by_type,
        )
        bulk_result = await bulk_index_bd_news(
            es_client_bd=es_client_bd,
            docs=docs,
            index_name=bd_news_index,
        )
        errors = bulk_result.get("errors") or []
        indexed_count = int(bulk_result.get("indexed", 0))

        logger.info(
            "Predictive analysis profile_id=%s triggers=%d indexed=%d dropped=%d",
            profile_id,
            len(triggers),
            indexed_count,
            len(dropped),
        )

        return JSONResponse(
            content={
                "profile_id": profile_id,
                "es_id": es_id,
                "public_identifier": public_identifier,
                "profile_summary": profile_summary,
                "triggers": triggers,
                "indexed_count": indexed_count,
                "indexed_news_ids": [doc["news_id"] for doc in docs],
                "dropped_triggers": dropped,
                "indexing_errors": errors[:3] if errors else [],
                "cache_hit": False,
                "experience_fingerprint": experience_fingerprint,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
        )

    except Exception as exc:
        logger.error("Unhandled exception in /predictive_analysis: %s", str(exc))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(
            content={"error": "Internal Server Error"},
            status_code=500,
        )


@router.delete("/predictive_analysis/news/scored")
async def delete_predictive_analysis_news():
    try:
        db_deleted = await delete_predictive_ranked_news(
            news_type=NEWS_TYPE,
        )

        logger.info(
            "Deleted predictive analysis scored news: ranked_news_deleted=%d seen_rows_deleted=%d",
            db_deleted.get("ranked_news_deleted", 0),
            db_deleted.get("seen_rows_deleted", 0),
        )

        return JSONResponse(
            content={
                "deleted": True,
                "news_type": NEWS_TYPE,
                "ranked_news_deleted": db_deleted.get("ranked_news_deleted", 0),
                "seen_rows_deleted": db_deleted.get("seen_rows_deleted", 0),
            }
        )
    except Exception as exc:
        logger.error(
            "Unhandled exception in DELETE /predictive_analysis/news: %s",
            str(exc),
            exc_info=True,
        )
        return JSONResponse(
            content={"error": "Internal Server Error", "deleted": False},
            status_code=500,
        )
