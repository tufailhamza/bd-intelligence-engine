import asyncio
import logging
import re
import time
import traceback
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.profile_fetching import (
    UpdateRankConnectionsInput,
    UpdateRankConnectionsOutput,
)
from app.routes.dependancies import get_es_client_with_compression
from app.utils.profile_fetching.public_identifier_fetch import (
    fetch_profiles_by_public_identifiers,
    _select_top_experience,
)
from app.database.profile_scores import fetch_latest_profile_score
from app.database.connection_rankings import (
    insert_batch_rankings,
    fetch_specific_ranked_connections,
)
from app.utils.profile_fetching.simple_connection_ranking import score_connection_simple
from app.utils.util_functions import score_profile_with_llm
from app.utils.connection_rankings.impact_ranking import impact_ranking_for_db_row

logger = logging.getLogger(__name__)
router = APIRouter()

# Management levels to exclude before ranking:
# 6=Manager, 10=Senior, 11=Mid-Level, 12=Junior.
_MANAGEMENT_LEVELS_EXCLUDED = {6, 10, 11, 12}

# Management levels that receive a boost when they have a calendar meetup with the reference.
_MANAGEMENT_LEVEL_BOOST_AMOUNTS = {
    0: 200.0,  # Executive/Sr.VP
    1: 200.0,  # C-Suite
    2: 200.0,  # Director
    3: 150.0,  # President
    4: 50.0,  # Vice President
    7: 200.0,  # Partner
}
_MANAGEMENT_LEVELS_BOOST = set(_MANAGEMENT_LEVEL_BOOST_AMOUNTS.keys())

_RESPONSE_EXCLUDED_METRICS = (
    "meeting_points",
    "company_context_points",
    "meeting_participant_score",
    "management_level_boost",
)


def _filter_response_metrics(metric_scores: dict) -> dict:
    if not metric_scores or not isinstance(metric_scores, dict):
        return {}
    return {
        k: v for k, v in metric_scores.items() if k not in _RESPONSE_EXCLUDED_METRICS
    }


def _get_top_metric(metric_scores: dict) -> Optional[str]:
    if not metric_scores or not isinstance(metric_scores, dict):
        return None
    best_name, best_val = None, None
    for k, v in metric_scores.items():
        try:
            fv = float(v)
        except Exception:
            continue
        if best_val is None or fv > best_val:
            best_val = fv
            best_name = str(k)
    return best_name


def _get_latest_management_levels(profile: Optional[Dict[str, Any]]) -> List[int]:
    if not profile or not isinstance(profile, dict):
        return []
    exp_list = profile.get("experience")
    if (
        not isinstance(exp_list, list)
        or not exp_list
        or not isinstance(exp_list[0], dict)
    ):
        return []
    ml = exp_list[0].get("management_level")
    if ml is None:
        return []
    if isinstance(ml, (int, float)):
        return [int(ml)]
    if isinstance(ml, list):
        return [int(x) for x in ml if x is not None and isinstance(x, (int, float))]
    return []


def _should_nullify_by_management_level(levels: List[int]) -> bool:
    if not levels:
        return False
    return any(lvl in _MANAGEMENT_LEVELS_EXCLUDED for lvl in levels)


def _normalize_company(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v or None


def _current_company(profile: Optional[Dict[str, Any]]) -> Optional[str]:
    exp = profile.get("experience") if isinstance(profile, dict) else None
    if isinstance(exp, list) and exp and isinstance(exp[0], dict):
        company = exp[0].get("company") or exp[0].get("company_name")
        return _normalize_company(company) if company else None
    return None


def _exclusion_reason(
    *,
    profile: Optional[Dict[str, Any]],
    reference_company: Optional[str],
) -> Optional[str]:
    levels = _get_latest_management_levels(profile)
    if _should_nullify_by_management_level(levels):
        return (
            "Excluded before ranking because latest experience management_level "
            f"{levels} contains one of 6, 10, 11, or 12."
        )
    conn_company = _current_company(profile)
    if reference_company and conn_company and conn_company == reference_company:
        return (
            "Excluded before ranking because connection belongs to the same "
            f"organization as the reference profile ({reference_company})."
        )
    return None


@router.post("/update_rank_connections", response_model=UpdateRankConnectionsOutput)
async def update_rank_connections(
    request: UpdateRankConnectionsInput,
    es_client_compressed=Depends(get_es_client_with_compression),
):
    """
    Update the score of already-ranked connections using updated calendar information.

    Accepts the same inputs as /rank_connections (reference identifier, connection identifiers,
    calendar events). Only connections that already exist in the DB are re-scored; connections
    not yet ranked are reported in `not_found_in_db` and left unchanged.

    The scoring logic is identical to /rank_connections but always forces a fresh re-score
    using the provided calendar_events, regardless of what was previously stored.
    """
    try:
        start = time.perf_counter()

        if len(request.connection_public_identifiers) == 0:
            return JSONResponse(
                content={
                    "error": "Must provide at least 1 connection public identifier"
                },
                status_code=400,
            )

        # Load LLM rubric metrics and existing DB rankings in parallel
        metrics_entry, existing_rankings_result = await asyncio.gather(
            fetch_latest_profile_score(
                public_identifier=request.reference_public_identifier
            ),
            fetch_specific_ranked_connections(
                reference_public_identifier=request.reference_public_identifier,
                connection_public_identifiers=request.connection_public_identifiers,
            ),
            return_exceptions=True,
        )

        if isinstance(metrics_entry, Exception):
            logger.error("Error fetching metrics: %s", metrics_entry)
            return JSONResponse(
                content={
                    "error": f"Error loading metrics for reference profile: {request.reference_public_identifier}"
                },
                status_code=500,
            )

        if not metrics_entry:
            return JSONResponse(
                content={
                    "error": f"No metrics found for reference profile: {request.reference_public_identifier}"
                },
                status_code=404,
            )

        if isinstance(existing_rankings_result, Exception):
            logger.warning(
                "Error fetching existing rankings: %s.", existing_rankings_result
            )
            return JSONResponse(
                content={"error": "Failed to fetch existing rankings from DB"},
                status_code=500,
            )

        existing_rankings: Dict[str, Any] = existing_rankings_result or {}

        rubric_json = metrics_entry.get("rubric", {}) or {}
        metrics = (
            (rubric_json.get("metrics", []) or [])
            if isinstance(rubric_json, dict)
            else []
        )
        if not metrics:
            return JSONResponse(
                content={"error": "No metrics found in stored rubric"},
                status_code=400,
            )

        # Split: connections already in DB vs. those that have never been ranked
        connections_in_db = [
            pid
            for pid in request.connection_public_identifiers
            if pid in existing_rankings
        ]
        not_found_in_db = [
            pid
            for pid in request.connection_public_identifiers
            if pid not in existing_rankings
        ]

        if not_found_in_db:
            logger.info(
                "Update Rank Connections: reference=%s, %d connections not found in DB (never ranked): %s",
                request.reference_public_identifier,
                len(not_found_in_db),
                ", ".join(not_found_in_db[:10])
                + ("..." if len(not_found_in_db) > 10 else ""),
            )

        # Build calendar events dict from request body
        connection_calendar_events_dicts: Dict[str, List[Dict[str, Any]]] = {}
        if request.calendar_events:
            for conn_id, events in request.calendar_events.items():
                if events:
                    connection_calendar_events_dicts[conn_id] = [
                        event.dict() for event in events
                    ]

        # Batch-fetch reference profile + all in-DB connection profiles in one query
        all_public_identifiers = [
            request.reference_public_identifier
        ] + connections_in_db
        all_profiles_map = await fetch_profiles_by_public_identifiers(
            public_identifiers=all_public_identifiers,
            es_client=es_client_compressed,
            source_fields=None,
            only_top_experience=False,
        )

        reference_es_id, reference_profile = all_profiles_map.get(
            request.reference_public_identifier, (None, None)
        )

        reference_company = _current_company(reference_profile)

        # Build per-connection profile map (top experience only)
        connection_profiles: Dict[str, Any] = {}
        connection_es_ids: Dict[str, Optional[str]] = {}
        for conn_id in connections_in_db:
            es_id, profile = all_profiles_map.get(conn_id, (None, None))
            if profile and isinstance(profile, dict):
                profile_copy = dict(profile)
                exp = profile_copy.get("experience")
                if isinstance(exp, list) and exp:
                    top_exp = _select_top_experience(exp)
                    profile_copy["experience"] = (
                        [top_exp] if top_exp is not None else []
                    )
                connection_profiles[conn_id] = profile_copy
                connection_es_ids[conn_id] = es_id
            else:
                # Fall back to the es_id stored in the existing DB ranking
                connection_es_ids[conn_id] = existing_rankings[conn_id].get(
                    "connection_es_id"
                )
                logger.warning(
                    "Update Rank Connections: reference=%s, connection=%s ES profile not found — keeping cached DB score",
                    request.reference_public_identifier,
                    conn_id,
                )

        excluded_connections: Dict[str, str] = {}
        for conn_id in connections_in_db:
            reason = _exclusion_reason(
                profile=connection_profiles.get(conn_id),
                reference_company=reference_company,
            )
            if reason:
                excluded_connections[conn_id] = reason
                logger.info(
                    "Update Rank Connections: reference=%s, connection=%s excluded before rescoring: %s",
                    request.reference_public_identifier,
                    conn_id,
                    reason,
                )

        connections_to_rescore = [
            pid for pid in connections_in_db if pid not in excluded_connections
        ]

        updated_connections: List[Dict[str, Any]] = []
        updated_connections_for_db: List[Dict[str, Any]] = []

        def _clean_reasoning(text: Any) -> str:
            if text is None:
                return ""
            raw = str(text).strip()
            if not raw:
                return ""
            raw = re.sub(
                r"\n*\s*simple_v2:.*$",
                "",
                raw,
                flags=re.DOTALL | re.IGNORECASE,
            )
            raw = " ".join(raw.split())
            if not raw:
                return ""
            parts = re.split(r"(?<=[.!?])\s+", raw)
            short = " ".join(parts[:2]).strip()
            return short

        # Re-score connections with LLM (same logic as /rank_connections)
        scoring_tasks: List = []
        conn_ids_to_score: List[str] = []

        for conn_id in connections_to_rescore:
            profile = connection_profiles.get(conn_id)
            if not profile:
                # No ES profile — preserve the existing DB entry unchanged
                row = existing_rankings[conn_id]
                ms = row.get("metric_scores", {}) or {}
                cached_score = float(row.get("total_score", 0.0))
                response_ms = _filter_response_metrics(ms)
                top_metric_value = (
                    None if cached_score == 0.0 else _get_top_metric(response_ms)
                )
                cached_nullify = row.get("nullify", False) if cached_score > 0 else True
                updated_connections.append(
                    {
                        "public_identifier": conn_id,
                        "es_id": connection_es_ids.get(conn_id),
                        "score": cached_score,
                        "metric_scores": response_ms,
                        "reasoning": _clean_reasoning(row.get("reasoning")),
                        "top_metric": top_metric_value,
                        "nullify": cached_nullify,
                    }
                )
                updated_connections_for_db.append(
                    {
                        "public_identifier": conn_id,
                        "es_id": connection_es_ids.get(conn_id),
                        "score": cached_score,
                        "metric_scores": ms,
                        "reasoning": _clean_reasoning(row.get("reasoning")),
                        "top_metric": top_metric_value,
                        "nullify": cached_nullify,
                    }
                )
                continue

            events = connection_calendar_events_dicts.get(conn_id)
            scoring_tasks.append(
                score_profile_with_llm(
                    profile=profile,
                    metrics=metrics,
                    model="openai/gpt-4o-mini",
                    es_client=es_client_compressed,
                    calendar_events=events,
                    include_calendar_insights=True,
                    reference_profile=reference_profile,
                )
            )
            conn_ids_to_score.append(conn_id)

        scoring_results = await asyncio.gather(*scoring_tasks, return_exceptions=True)

        EPSILON = 1e-6
        for i, result in enumerate(scoring_results):
            conn_id = conn_ids_to_score[i]
            profile = connection_profiles.get(conn_id)
            events = connection_calendar_events_dicts.get(conn_id)

            calendar_res = score_connection_simple(
                connection_public_identifier=conn_id,
                connection_profile=profile,
                connection_events=events,
                company_bonus_points=0.0,
            )

            if isinstance(result, Exception):
                logger.error(
                    "Update Rank Connections: reference=%s, connection=%s LLM re-scoring failed: %s",
                    request.reference_public_identifier,
                    conn_id,
                    str(result),
                )
                llm_metric_scores: Dict[str, Any] = {}
                llm_reasoning = f"LLM scoring error: {str(result)}"
                llm_nullify = True
            else:
                llm_metric_scores = result.get("metric_scores", {}) or {}
                llm_reasoning = result.get("reasoning") or ""
                llm_nullify = result.get("nullify", False)

            combined_metric_scores: Dict[str, float] = {}
            if isinstance(llm_metric_scores, dict):
                for k, v in llm_metric_scores.items():
                    try:
                        combined_metric_scores[str(k)] = float(v)
                    except Exception:
                        combined_metric_scores[str(k)] = 0.0

            llm_total_score = float(sum(combined_metric_scores.values()))
            all_llm_scores_zero = (
                all(abs(float(v)) < EPSILON for v in combined_metric_scores.values())
                or abs(llm_total_score) < EPSILON
            )

            if all_llm_scores_zero:
                combined_metric_scores["meeting_points"] = 0.0
                combined_metric_scores["company_context_points"] = 0.0
                total_score = 0.0
                # Keep reasoning concise and hide internal simple_v2 formula text.
                combined_reasoning = _clean_reasoning(llm_reasoning)
            else:
                combined_metric_scores["meeting_points"] = float(
                    calendar_res.metric_scores.get("meeting_points", 0.0)
                )
                combined_metric_scores["company_context_points"] = float(
                    calendar_res.metric_scores.get("company_context_points", 0.0)
                )
                total_score = float(sum(combined_metric_scores.values()))

                levels = _get_latest_management_levels(profile)
                has_calendar_meetup = bool(events) if events is not None else False
                boost_amount = 0.0
                if has_calendar_meetup and levels:
                    boost_amount = max(
                        (
                            _MANAGEMENT_LEVEL_BOOST_AMOUNTS.get(lvl, 0.0)
                            for lvl in levels
                            if lvl in _MANAGEMENT_LEVELS_BOOST
                        ),
                        default=0.0,
                    )
                if boost_amount > 0:
                    total_score += boost_amount
                    combined_metric_scores["management_level_boost"] = boost_amount
                    combined_reasoning = _clean_reasoning(
                        (llm_reasoning or "").strip()
                        + " "
                        + f"(management level boost of +{int(boost_amount)} applied due to calendar relationship)."
                    )
                else:
                    combined_reasoning = _clean_reasoning(llm_reasoning)

            if total_score < 1.0 and conn_id not in excluded_connections:
                total_score = 1.0

            response_metric_scores = _filter_response_metrics(combined_metric_scores)
            top_metric_value = (
                None if total_score == 0.0 else _get_top_metric(response_metric_scores)
            )

            updated_connections.append(
                {
                    "public_identifier": conn_id,
                    "es_id": connection_es_ids.get(conn_id),
                    "score": total_score,
                    "metric_scores": response_metric_scores,
                    "reasoning": combined_reasoning,
                    "top_metric": top_metric_value,
                    "nullify": llm_nullify,
                }
            )
            updated_connections_for_db.append(
                {
                    "public_identifier": conn_id,
                    "es_id": connection_es_ids.get(conn_id),
                    "score": total_score,
                    "metric_scores": combined_metric_scores,
                    "reasoning": combined_reasoning,
                    "top_metric": top_metric_value,
                    "nullify": llm_nullify,
                }
            )

        updated_connections.sort(key=lambda x: x["score"], reverse=True)

        logger.info(
            "Update Rank Connections: reference=%s — total_requested=%d, in_db=%d, not_in_db=%d, re-scored=%d",
            request.reference_public_identifier,
            len(request.connection_public_identifiers),
            len(connections_in_db),
            len(not_found_in_db),
            len(conn_ids_to_score),
        )

        # Persist updated rankings (upsert), including impact_ranking on the same row
        if updated_connections_for_db:
            for conn in updated_connections_for_db:
                pid = conn["public_identifier"]
                conn["impact_ranking"] = impact_ranking_for_db_row(
                    metric_scores=conn.get("metric_scores"),
                    profile=connection_profiles.get(pid),
                )
            try:
                inserted_count = await insert_batch_rankings(
                    reference_public_identifier=request.reference_public_identifier,
                    reference_es_id=reference_es_id,
                    ranked_connections=updated_connections_for_db,
                )
                logger.info(
                    "Update Rank Connections: reference=%s, upserted %d/%d connections into DB",
                    request.reference_public_identifier,
                    inserted_count,
                    len(updated_connections_for_db),
                )
            except Exception as e:
                logger.error(
                    "Update Rank Connections: reference=%s, failed to persist updated rankings: %s",
                    request.reference_public_identifier,
                    str(e),
                )
                logger.error("Traceback:\n%s", traceback.format_exc())

        end = time.perf_counter()
        logger.info("Update Rank Connections endpoint took %.2f seconds", end - start)

        return JSONResponse(
            content={
                "reference_public_identifier": request.reference_public_identifier,
                "reference_es_id": reference_es_id,
                "updated_connections": updated_connections,
                "not_found_in_db": not_found_in_db,
                "excluded_connection_public_identifiers": excluded_connections,
            }
        )

    except Exception as e:
        logger.error("Unhandled exception in /update_rank_connections: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)
