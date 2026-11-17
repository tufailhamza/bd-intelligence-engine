import asyncio
import json
import logging
import re
import time
import traceback
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.profile_fetching import (
    ProfileInput,
    ProfilesOutput,
    PublicIdentifierProfileInput,
    PublicIdentifierProfileOutput,
    GenerateScoringMetricsInput,
    GenerateScoringMetricsOutput,
    RankConnectionsInput,
    RankConnectionsOutput,
    RankedConnection,
    RankConnectionsReasoningInput,
    RankConnectionsReasoningOutput,
    TopConnectionsInput,
    TopConnectionsOutput,
    GenerateProfileDescriptionInput,
    GenerateProfileDescriptionOutput,
)
from app.routes.dependancies import (
    get_es_client,
    get_es_client_with_compression,
    get_es_client_bd,
    get_manticore_client,
)
from app.utils.profile_fetching.profile_fetching import get_profiles_from_icp
from app.utils.profile_fetching.public_identifier_fetch import (
    fetch_profile_by_public_identifier,
    fetch_profiles_by_public_identifiers,
    _select_top_experience,
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
from app.models.schemas.company_news_fetching import NewsInputCompany
from app.utils.profile_fetching.scoring_metrics_generation import (
    generate_scoring_metrics_from_profile,
)
from app.database.profile_scores import (
    insert_profile_score,
    fetch_latest_profile_score,
)
from app.database.calendar_events import (
    upsert_calendar_events_for_public_identifier,
)
from app.database.connection_rankings import (
    insert_batch_rankings,
    fetch_ranked_connections,
    fetch_ranked_connections_with_meeting_points,
    fetch_specific_ranked_connections,
)
from app.utils.connection_rankings.impact_ranking import impact_ranking_for_db_row
from app.utils.profile_fetching.simple_connection_ranking import (
    score_connection_simple,
)
from app.utils.util_functions import (
    xml_output_to_json,
    score_profile_with_llm,
    generate_profile_description,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/get_profiles_from_icp", response_model=ProfilesOutput)
async def get_profiles(
    request: ProfileInput,
    es_client=Depends(get_es_client),
    es_client_compressed=Depends(get_es_client_with_compression),
):
    try:
        icp_json = request.dict()
        uuid = icp_json.pop("uuid", None)
        start = time.perf_counter()
        uuid_to_return, company_profile_list, company_details_map = (
            await get_profiles_from_icp(
                uuid,
                icp_json,
                es_client,
                es_client_compressed,
            )
        )
        end = time.perf_counter()
        logging.info(f"Get Profiles from ICP endpoint took {end - start} seconds")

        return JSONResponse(
            content={
                "uuid": str(uuid_to_return),
                "company_profiles": company_profile_list,
                "company_details": company_details_map,
            }
        )

    except Exception as e:
        logger.error("Unhandled exception in /get_profiles_from_icp: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.post(
    "/get_profile_by_public_identifier", response_model=PublicIdentifierProfileOutput
)
async def get_profile_by_public_identifier(
    request: PublicIdentifierProfileInput,
    es_client_compressed=Depends(get_es_client_with_compression),
):
    """
    Fetch a single profile document from Elasticsearch by `public_identifier`.
    Uses the app's "compressed" ES client (same as other profile-index operations).
    """
    try:
        start = time.perf_counter()
        es_id, profile = await fetch_profile_by_public_identifier(
            request.public_identifier,
            es_client=es_client_compressed,
            source_fields=request.source_fields,
            only_top_experience=True,
        )
        end = time.perf_counter()
        logging.info(
            f"Get Profile by Public Identifier endpoint took {end - start} seconds"
        )

        if profile is None:
            return JSONResponse(
                content={
                    "public_identifier": request.public_identifier,
                    "es_id": None,
                    "profile": None,
                },
                status_code=404,
            )

        return JSONResponse(
            content={
                "public_identifier": request.public_identifier,
                "es_id": es_id,
                "profile": profile,
            }
        )

    except Exception as e:
        logger.error(
            "Unhandled exception in /get_profile_by_public_identifier: %s", str(e)
        )
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.post(
    "/generate_scoring_metrics_from_public_identifier",
    response_model=GenerateScoringMetricsOutput,
)
async def generate_scoring_metrics_from_public_identifier(
    request: GenerateScoringMetricsInput,
    es_client_compressed=Depends(get_es_client_with_compression),
    es_client=Depends(get_es_client),
    es_client_bd=Depends(get_es_client_bd),
    manticore_client=Depends(get_manticore_client),
):
    """
    Fetch the reference profile from Elasticsearch by `public_identifier`, then call OpenAI
    to generate a weighted scoring rubric (total score out of 100) that can be used to score other profiles.
    """
    try:
        start = time.perf_counter()

        # Run DB cache lookup and ES profile fetch in parallel
        async def fetch_cache():
            try:
                return await fetch_latest_profile_score(
                    public_identifier=request.public_identifier
                )
            except Exception as e:
                logger.error(
                    "DB cache lookup failed for public_identifier=%s: %s",
                    request.public_identifier,
                    str(e),
                )
                return None

        async def fetch_profile():
            logger.info(
                "Fetching reference profile for public_identifier: %s",
                request.public_identifier,
            )
            es_id, profile = await fetch_profile_by_public_identifier(
                request.public_identifier,
                es_client=es_client_compressed,
                source_fields=None,  # fetch full profile for best rubric
                only_top_experience=False,
            )
            if profile:
                profile_name = (
                    profile.get("full_name") or profile.get("name") or "Unknown"
                )
                logger.info(
                    "Fetched reference profile: name=%s, public_identifier=%s",
                    profile_name,
                    request.public_identifier,
                )
            return es_id, profile

        # Execute both operations in parallel
        results = await asyncio.gather(
            fetch_cache(), fetch_profile(), return_exceptions=True
        )

        # Handle results and exceptions from parallel execution
        metrics_entry = results[0]
        profile_result = results[1]

        if isinstance(metrics_entry, Exception):
            logger.error("DB cache lookup exception: %s", str(metrics_entry))
            metrics_entry = None

        if isinstance(profile_result, Exception):
            logger.error("ES profile fetch exception: %s", str(profile_result))
            es_id, profile = None, None
        else:
            es_id, profile = profile_result
            logger.info(
                "After unpacking profile_result: es_id=%s, profile=%s",
                es_id if es_id else "None",
                "exists" if profile else "None",
            )

        # 1) Check if cache hit
        if metrics_entry:
            rubric_json = metrics_entry.get("rubric") or {}
            # If rubric is stored as JSONB, it should come back as a dict
            if not isinstance(rubric_json, dict):
                rubric_json = {}

            end = time.perf_counter()
            logging.info(
                f"Generate Scoring Metrics (cache hit) took {end - start} seconds"
            )
            return JSONResponse(
                content={
                    "public_identifier": request.public_identifier,
                    "es_id": metrics_entry.get("es_id"),
                    "rubric_json": rubric_json,
                    "parse_error": None,
                }
            )

        # 2) Cache miss: use ES profile to generate rubric
        if profile is None:
            return JSONResponse(
                content={
                    "public_identifier": request.public_identifier,
                    "es_id": None,
                    "rubric_json": {},
                    "parse_error": "Profile not found",
                },
                status_code=404,
            )

        # Convert calendar events Pydantic models to dicts if provided
        calendar_events_dicts = None
        if request.calendar_events:
            calendar_events_dicts = [event.dict() for event in request.calendar_events]
            # Persist calendar events (best-effort; do not fail the endpoint on DB errors)
            try:
                await upsert_calendar_events_for_public_identifier(
                    public_identifier=request.public_identifier,
                    calendar_events=calendar_events_dicts,
                    provider="google",
                )
            except Exception as e:
                logger.error(
                    "Failed to persist calendar events for public_identifier=%s: %s",
                    request.public_identifier,
                    str(e),
                )

            # Extract participant public identifiers from calendar events and ingest company news
            # IMPORTANT: The reference public_identifier is ONLY used for generating scoring metrics.
            # It is EXCLUDED from company news ingestion - only calendar event participants are processed.
            try:
                participant_pids = set()
                reference_pid = (
                    request.public_identifier
                )  # Used only for metrics generation, NOT for company news

                for event in calendar_events_dicts:
                    if not isinstance(event, dict):
                        continue
                    # Extract from event public_identifier field (if present)
                    pid = event.get("public_identifier")
                    if isinstance(pid, str) and pid and pid != reference_pid:
                        participant_pids.add(pid)
                    # Extract from organizer
                    organizer = event.get("organizer")
                    if isinstance(organizer, dict):
                        opid = organizer.get("public_identifier")
                        if isinstance(opid, str) and opid and opid != reference_pid:
                            participant_pids.add(opid)
                    # Extract from attendees
                    attendees = event.get("attendees")
                    if isinstance(attendees, list):
                        for attendee in attendees:
                            if isinstance(attendee, dict):
                                apid = attendee.get("public_identifier")
                                if (
                                    isinstance(apid, str)
                                    and apid
                                    and apid != reference_pid
                                ):
                                    participant_pids.add(apid)

                # Double-check: Remove the reference public_identifier itself (safety check)
                participant_pids.discard(reference_pid)

                if participant_pids:
                    logger.info(
                        "Reference public_identifier: %s | Found %d participant public identifiers from calendar events (excluding reference), fetching profiles",
                        reference_pid,
                        len(participant_pids),
                    )
                    # Fetch profiles for participants
                    participant_profiles = await fetch_profiles_by_public_identifiers(
                        public_identifiers=list(participant_pids),
                        es_client=es_client_compressed,
                        source_fields=None,
                        only_top_experience=True,
                    )

            except Exception as e:
                logger.error(
                    "Failed to extract participants and ingest company news: %s",
                    str(e),
                )
                logger.error("Traceback:\n%s", traceback.format_exc())
                # Don't fail the endpoint if company news ingestion fails

        # Generate scoring metrics using ONLY the reference profile
        # (Reference profile is NOT used for company news ingestion - that's handled separately for participants only)
        if profile:
            profile_name = profile.get("full_name") or profile.get("name") or "Unknown"
            profile_pid = (
                profile.get("public_identifier")
                or profile.get("publicIdentifier")
                or "Unknown"
            )
            logger.info(
                "Generating scoring metrics for reference profile: name=%s, profile_pid=%s, requested_pid=%s",
                profile_name,
                profile_pid,
                request.public_identifier,
            )
            # Verify we're using the correct profile
            if profile_pid != request.public_identifier and profile_pid != "Unknown":
                logger.warning(
                    "Profile public_identifier mismatch! Requested: %s, Got: %s",
                    request.public_identifier,
                    profile_pid,
                )
        xml_output = await generate_scoring_metrics_from_profile(
            profile=profile,  # Reference profile - used ONLY for metrics generation
            model="anthropic/claude-sonnet-4-5-20250929",
            es_client=es_client_compressed,
            calendar_events=calendar_events_dicts,
        )
        # Parse rubric (XML -> JSON)
        try:
            rubric_json = xml_output_to_json(xml_output)
            parse_error = None
            logger.info(
                "Successfully parsed rubric JSON for public_identifier=%s, rubric_keys=%s",
                request.public_identifier,
                (
                    list(rubric_json.keys())
                    if isinstance(rubric_json, dict)
                    else "not_a_dict"
                ),
            )
        except Exception as e:
            rubric_json = {}
            parse_error = str(e)
            logger.error(
                "Failed to parse rubric XML for public_identifier=%s: %s",
                request.public_identifier,
                str(e),
            )

        # Save to DB (do not fail the response if DB insert fails)
        logger.info(
            "DB insertion check for public_identifier=%s: rubric_json=%s, parse_error=%s, es_id=%s",
            request.public_identifier,
            "exists" if rubric_json else "empty",
            parse_error if parse_error else "None",
            es_id if es_id else "None",
        )

        if not es_id:
            logger.warning(
                "es_id is None for public_identifier=%s - will save with NULL es_id",
                request.public_identifier,
            )

        # Save even if es_id is None (es_id is optional in the DB)
        if rubric_json and not parse_error:
            logger.info(
                "Attempting DB insertion for public_identifier=%s, es_id=%s, rubric_size=%d",
                request.public_identifier,
                es_id if es_id else "NULL",
                len(str(rubric_json)),
            )
            try:
                await insert_profile_score(
                    public_identifier=request.public_identifier,
                    es_id=es_id,
                    rubric_json=rubric_json,
                )
                logger.info(
                    "Successfully saved rubric to DB for public_identifier=%s",
                    request.public_identifier,
                )
            except Exception as e:
                logger.error(
                    "Failed to persist rubric for public_identifier=%s: %s",
                    request.public_identifier,
                    str(e),
                )
                logger.error("DB insertion traceback:\n%s", traceback.format_exc())
                # Keep rubric_json, but surface the persistence error.
                parse_error = f"DB save failed: {str(e)}"
        else:
            logger.warning(
                "Skipping DB insertion for public_identifier=%s: rubric_json=%s, parse_error=%s, es_id=%s",
                request.public_identifier,
                "empty" if not rubric_json else "exists",
                parse_error if parse_error else "None",
                es_id if es_id else "None",
            )

        end = time.perf_counter()
        logging.info(f"Generate Scoring Metrics endpoint took {end - start} seconds")

        return JSONResponse(
            content={
                "public_identifier": request.public_identifier,
                "es_id": es_id,
                "rubric_json": rubric_json,
                "parse_error": parse_error,
            }
        )

    except Exception as e:
        logger.error(
            "Unhandled exception in /generate_scoring_metrics_from_public_identifier: %s",
            str(e),
        )
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.post("/rank_connections", response_model=RankConnectionsOutput)
async def rank_connections(
    request: RankConnectionsInput,
    es_client_compressed=Depends(get_es_client_with_compression),
):
    """
    Hybrid ranking:
    - LLM-based rubric scoring (stored in Postgres) for profile fit
    - Deterministic calendar scoring (NO LLM calendar summarization):
      - Meeting scoring: +30 for first meeting, +15 for each subsequent meeting
      - Company context: if there is a meeting with anyone at a company, then all connections at that company get +30

    Accepts any number of connection public identifiers (minimum 1).

    Request field `returnCache` (default false):
    - When true: return cached rows from `connection_rankings_bd` for the requested connections
      (no LLM re-score, no ES batch for scoring). Each item includes `priority` when cached.
      Identifiers with no cached row are still returned with score 0, empty metric_scores, and
      priority null; response may include `not_cached_connection_public_identifiers`.
    - When false: full re-score and persist (same as before); `priority` is omitted from items.
    """
    try:
        start = time.perf_counter()

        def filter_response_metrics(metric_scores: dict) -> dict:
            """
            Filter out meeting_points, company_context_points, meeting_participant_score,
            and management_level_boost from metric_scores for API response. Kept in DB.
            Returns empty dict if input is None or not a dict.
            """
            if not metric_scores or not isinstance(metric_scores, dict):
                return {}
            excluded = (
                "meeting_points",
                "company_context_points",
                "meeting_participant_score",
                "management_level_boost",
            )
            return {k: v for k, v in metric_scores.items() if k not in excluded}

        def get_top_metric(metric_scores: dict) -> Optional[str]:
            """
            Return the metric name with the highest numeric score.
            """
            if not metric_scores or not isinstance(metric_scores, dict):
                return None
            best_name = None
            best_val = None
            for k, v in metric_scores.items():
                try:
                    fv = float(v)
                except Exception:
                    continue
                if best_val is None or fv > best_val:
                    best_val = fv
                    best_name = str(k)
            return best_name

        # Management levels to exclude before ranking:
        # 6=Manager, 10=Senior, 11=Mid-Level, 12=Junior.
        MANAGEMENT_LEVELS_EXCLUDED = {6, 10, 11, 12}
        # Management levels that get a boost (only when they have calendar meetup with reference).
        # Boost amounts: 0,1,2,7 → +200; 3 → +150; 4 → +50. Use max if multiple levels qualify.
        MANAGEMENT_LEVEL_BOOST_AMOUNTS = {
            0: 200.0,  # Executive/Sr.VP
            1: 200.0,  # C-Suite
            2: 200.0,  # Director
            3: 150.0,  # President
            4: 50.0,  # Vice President
            7: 200.0,  # Partner
        }
        MANAGEMENT_LEVELS_BOOST = set(MANAGEMENT_LEVEL_BOOST_AMOUNTS.keys())

        def get_latest_management_levels(
            profile: Optional[Dict[str, Any]],
        ) -> List[int]:
            """
            Return all management_level values from the latest (first) experience.
            management_level in ES can be a list (e.g. [11] or [10, 6]) or null.
            Returns empty list if missing.
            """
            if not profile or not isinstance(profile, dict):
                return []
            exp_list = profile.get("experience")
            if (
                not isinstance(exp_list, list)
                or not exp_list
                or not isinstance(exp_list[0], dict)
            ):
                return []
            latest = exp_list[0]
            ml = latest.get("management_level")
            if ml is None:
                return []
            if isinstance(ml, (int, float)):
                return [int(ml)]
            if isinstance(ml, list):
                return [
                    int(x) for x in ml if x is not None and isinstance(x, (int, float))
                ]
            return []

        def _latest_experience(
            profile: Optional[Dict[str, Any]],
        ) -> Optional[Dict[str, Any]]:
            exp_list = profile.get("experience") if isinstance(profile, dict) else None
            if (
                isinstance(exp_list, list)
                and exp_list
                and isinstance(exp_list[0], dict)
            ):
                return exp_list[0]
            return None

        def _current_organization_fields(
            profile: Optional[Dict[str, Any]],
        ) -> Dict[str, Optional[str]]:
            latest = _latest_experience(profile)
            if not latest:
                return {
                    "organization": None,
                    "company": None,
                    "company_universal_name": None,
                    "title": None,
                }
            organization = (
                latest.get("organization")
                or latest.get("organisation")
                or latest.get("organization_name")
                or latest.get("organisation_name")
            )
            company = latest.get("company") or latest.get("company_name")
            company_universal_name = (
                latest.get("company_universal_name")
                or latest.get("company_universalname")
                or latest.get("companyUniversalName")
                or latest.get("companyUniversalname")
            )
            return {
                "organization": str(organization).strip() if organization else None,
                "company": str(company).strip() if company else None,
                "company_universal_name": (
                    str(company_universal_name).strip()
                    if company_universal_name
                    else None
                ),
                "title": (
                    str(latest.get("title")).strip() if latest.get("title") else None
                ),
            }

        def _log_profile_management_and_organization(
            *,
            reference_public_identifier: str,
            profile_public_identifier: str,
            profile_role: str,
            profile: Optional[Dict[str, Any]],
        ) -> None:
            org_fields = _current_organization_fields(profile)
            logger.info(
                "Rank Connections profile context: reference_public_identifier=%s, profile_role=%s, profile_public_identifier=%s, management_levels=%s, organization=%s, company=%s, company_universal_name=%s, title=%s",
                reference_public_identifier,
                profile_role,
                profile_public_identifier,
                get_latest_management_levels(profile),
                org_fields.get("organization"),
                org_fields.get("company"),
                org_fields.get("company_universal_name"),
                org_fields.get("title"),
            )

        def should_nullify_by_management_level(levels: List[int]) -> bool:
            """
            Exclude when any current/latest level is in {6, 10, 11, 12}
            (Manager, Senior, Mid-Level, Junior).
            """
            if not levels:
                return False
            return any(lvl in MANAGEMENT_LEVELS_EXCLUDED for lvl in levels)

        def _normalize_company(value):
            if not isinstance(value, str):
                return None
            v = value.strip().lower()
            return v or None

        def _profile_with_top_experience(
            profile: Optional[Dict[str, Any]],
        ) -> Optional[Dict[str, Any]]:
            if not isinstance(profile, dict):
                return profile
            profile_copy = dict(profile)
            exp = profile_copy.get("experience")
            if isinstance(exp, list) and exp:
                top_exp = _select_top_experience(exp)
                profile_copy["experience"] = [top_exp] if top_exp is not None else []
            return profile_copy

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
            levels = get_latest_management_levels(profile)
            if should_nullify_by_management_level(levels):
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

        # Validate at least 1 connection
        if len(request.connection_public_identifiers) == 0:
            return JSONResponse(
                content={
                    "error": "Must provide at least 1 connection public identifier"
                },
                status_code=400,
            )

        if request.return_cache:
            requested_profiles_map = await fetch_profiles_by_public_identifiers(
                public_identifiers=[
                    request.reference_public_identifier,
                    *list(request.connection_public_identifiers),
                ],
                es_client=es_client_compressed,
                source_fields=None,
                only_top_experience=False,
            )
            _, cached_reference_profile = requested_profiles_map.get(
                request.reference_public_identifier, (None, None)
            )
            cached_reference_profile = _profile_with_top_experience(
                cached_reference_profile
            )
            cached_reference_company = _current_company(cached_reference_profile)
            _log_profile_management_and_organization(
                reference_public_identifier=request.reference_public_identifier,
                profile_public_identifier=request.reference_public_identifier,
                profile_role="reference",
                profile=cached_reference_profile,
            )
            excluded_cached: Dict[str, str] = {}
            for conn_id in request.connection_public_identifiers:
                _, profile = requested_profiles_map.get(conn_id, (None, None))
                profile = _profile_with_top_experience(profile)
                _log_profile_management_and_organization(
                    reference_public_identifier=request.reference_public_identifier,
                    profile_public_identifier=conn_id,
                    profile_role="connection",
                    profile=profile,
                )
                reason = _exclusion_reason(
                    profile=profile,
                    reference_company=cached_reference_company,
                )
                if reason:
                    excluded_cached[conn_id] = reason

            cached_rows = await fetch_specific_ranked_connections(
                reference_public_identifier=request.reference_public_identifier,
                connection_public_identifiers=list(
                    c
                    for c in request.connection_public_identifiers
                    if c not in excluded_cached
                ),
            )
            not_cached = [
                c
                for c in request.connection_public_identifiers
                if c not in cached_rows and c not in excluded_cached
            ]

            def _reasoning_str_from_db(value: Any) -> Optional[str]:
                if value is None:
                    return None
                if isinstance(value, str):
                    return value
                if isinstance(value, dict):
                    t = value.get("text")
                    if isinstance(t, str):
                        return t
                    return str(value)
                return str(value)

            ranked_from_cache: List[Dict[str, Any]] = []
            reference_es_id_cached: Optional[str] = None
            for conn_id in request.connection_public_identifiers:
                if conn_id in excluded_cached:
                    continue
                row = cached_rows.get(conn_id)
                if row is None:
                    ranked_from_cache.append(
                        {
                            "public_identifier": conn_id,
                            "es_id": None,
                            "score": 0.0,
                            "metric_scores": {},
                            "reasoning": "No cached ranking for this connection.",
                            "top_metric": None,
                            "nullify": True,
                            "priority": None,
                            "impact_ranking": None,
                        }
                    )
                    continue

                if reference_es_id_cached is None:
                    reference_es_id_cached = row.get("reference_es_id")

                ms_raw = row.get("metric_scores") or {}
                if isinstance(ms_raw, str):
                    try:
                        ms_raw = json.loads(ms_raw)
                    except Exception:
                        ms_raw = {}
                if not isinstance(ms_raw, dict):
                    ms_raw = {}

                metric_scores_full: Dict[str, float] = {}
                for k, v in ms_raw.items():
                    try:
                        metric_scores_full[str(k)] = float(v)
                    except Exception:
                        metric_scores_full[str(k)] = 0.0

                cached_score = float(row.get("total_score") or 0.0)
                if cached_score < 1.0 and conn_id not in excluded_cached:
                    cached_score = 1.0
                response_ms = filter_response_metrics(metric_scores_full)
                top_metric_value = (
                    None if cached_score == 0.0 else get_top_metric(response_ms)
                )

                ranked_from_cache.append(
                    {
                        "public_identifier": conn_id,
                        "es_id": row.get("connection_es_id"),
                        "score": cached_score,
                        "metric_scores": response_ms,
                        "reasoning": _clean_reasoning(
                            _reasoning_str_from_db(row.get("reasoning"))
                        ),
                        "top_metric": top_metric_value,
                        "nullify": row.get("nullify"),
                        "priority": row.get("priority"),
                        "impact_ranking": row.get("impact_ranking"),
                    }
                )

            ranked_from_cache.sort(key=lambda x: x["score"], reverse=True)
            end_cache = time.perf_counter()
            cached_count = len(request.connection_public_identifiers) - len(not_cached)
            logger.info(
                "Rank Connections (returnCache): reference_public_identifier=%s, cached=%d, not_cached=%d, took %.3fs",
                request.reference_public_identifier,
                cached_count,
                len(not_cached),
                end_cache - start,
            )
            payload: Dict[str, Any] = {
                "reference_public_identifier": request.reference_public_identifier,
                "reference_es_id": reference_es_id_cached,
                "ranked_connections": ranked_from_cache,
            }
            if not_cached:
                payload["not_cached_connection_public_identifiers"] = not_cached
            if excluded_cached:
                payload["excluded_connection_public_identifiers"] = excluded_cached
            return JSONResponse(content=payload)

        # Load LLM rubric metrics from DB. Rescore all requested connections (full path).
        metrics_entry = await fetch_latest_profile_score(
            public_identifier=request.reference_public_identifier
        )
        existing_rankings = {}

        # Handle exceptions for metrics_entry
        if isinstance(metrics_entry, Exception):
            logger.error(f"Error fetching metrics: {metrics_entry}")
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

        connections_to_score = list(request.connection_public_identifiers)

        # Track reasons for not processing connections
        skipped_reasons = {
            "excluded_before_scoring": [],
            "profile_not_found": [],
            "llm_scoring_failed": [],
        }

        logger.info(
            "Rank Connections: reference_public_identifier=%s, total_connections_requested=%d, rescoring all (returnCache=false)",
            request.reference_public_identifier,
            len(connections_to_score),
        )

        # Calendar: from request body only (endpoint always provides it).
        connection_calendar_events_dicts: Dict[str, List[Dict[str, Any]]] = {}
        if request.calendar_events:
            for conn_id, events in request.calendar_events.items():
                if events:
                    connection_calendar_events_dicts[conn_id] = [
                        event.dict() for event in events
                    ]

        # Fetch reference profile AND all connection profiles in a SINGLE batch query
        # This reduces from 2 queries to 1 query for better performance
        all_public_identifiers = [
            request.reference_public_identifier
        ] + connections_to_score
        all_profiles_map = await fetch_profiles_by_public_identifiers(
            public_identifiers=all_public_identifiers,
            es_client=es_client_compressed,
            source_fields=None,
            only_top_experience=False,  # Reference needs full experience, connections will be filtered below
        )

        # Extract reference profile (needs full experience for company extraction)
        reference_es_id, reference_profile = all_profiles_map.get(
            request.reference_public_identifier, (None, None)
        )
        reference_profile = _profile_with_top_experience(reference_profile)
        reference_company = _current_company(reference_profile)
        _log_profile_management_and_organization(
            reference_public_identifier=request.reference_public_identifier,
            profile_public_identifier=request.reference_public_identifier,
            profile_role="reference",
            profile=reference_profile,
        )

        # Extract connection profiles (only top/recent experience)
        # Process connection profiles to keep only top experience
        profiles_map = {}
        for conn_id in connections_to_score:
            es_id, profile = all_profiles_map.get(conn_id, (None, None))
            profile = _profile_with_top_experience(profile)
            profiles_map[conn_id] = (es_id, profile)
            _log_profile_management_and_organization(
                reference_public_identifier=request.reference_public_identifier,
                profile_public_identifier=conn_id,
                profile_role="connection",
                profile=profile,
            )

        # Extract profiles and es_ids from the batch result
        connection_profiles = {}
        connection_es_ids = {}
        for conn_id in connections_to_score:
            es_id, profile = profiles_map.get(conn_id, (None, None))
            if profile:
                connection_profiles[conn_id] = profile
                connection_es_ids[conn_id] = es_id
            else:
                skipped_reasons["profile_not_found"].append(conn_id)
                logger.warning(
                    "Rank Connections: reference_public_identifier=%s, connection=%s skipped (profile not found)",
                    request.reference_public_identifier,
                    conn_id,
                )

        excluded_connections: Dict[str, str] = {}
        for conn_id in connections_to_score:
            reason = _exclusion_reason(
                profile=connection_profiles.get(conn_id),
                reference_company=reference_company,
            )
            if reason:
                excluded_connections[conn_id] = reason
                skipped_reasons["excluded_before_scoring"].append(conn_id)
                logger.info(
                    "Rank Connections: reference_public_identifier=%s, connection=%s excluded before scoring: %s",
                    request.reference_public_identifier,
                    conn_id,
                    reason,
                )

        excluded_count = len(excluded_connections)
        if excluded_count > 0:
            logger.info(
                "Rank Connections: reference_public_identifier=%s, %d connections excluded before scoring: %s",
                request.reference_public_identifier,
                excluded_count,
                ", ".join(list(excluded_connections.keys())[:10])
                + ("..." if excluded_count > 10 else ""),
            )

        connections_to_score = [
            conn_id
            for conn_id in connections_to_score
            if conn_id not in excluded_connections
        ]

        ranked_connections = []
        ranked_connections_for_db = []  # Store full metric_scores for DB insertion

        def _clean_reasoning(text: Any) -> str:
            """
            Normalize reasoning for API/DB:
            - Strip whitespace
            - Drop any internal 'simple_v2' formula fragments
            - Collapse to 1-2 sentences max
            """
            if text is None:
                return ""
            raw = str(text).strip()
            if not raw:
                return ""
            # Remove any trailing simple_v2 debug segment if present.
            raw = re.sub(
                r"\n*\s*simple_v2:.*$",
                "",
                raw,
                flags=re.DOTALL | re.IGNORECASE,
            )
            raw = " ".join(raw.split())
            if not raw:
                return ""
            # Keep at most two sentences.
            parts = re.split(r"(?<=[.!?])\s+", raw)
            short = " ".join(parts[:2]).strip()
            return short

        # LLM-score connections (NO calendar summarization) + add deterministic calendar points
        scoring_tasks = []
        conn_ids = []
        for conn_id in connections_to_score:
            profile = connection_profiles.get(conn_id)
            if not profile:
                continue
            # Get calendar events for this connection if available
            events = connection_calendar_events_dicts.get(conn_id)
            scoring_tasks.append(
                score_profile_with_llm(
                    profile=profile,
                    metrics=metrics,
                    model="openai/gpt-4o-mini",
                    es_client=es_client_compressed,
                    calendar_events=events,
                    include_calendar_insights=True,  # Always include calendar insights when events are available
                    reference_profile=reference_profile,
                )
            )
            conn_ids.append(conn_id)

        scoring_results = await asyncio.gather(*scoring_tasks, return_exceptions=True)

        for i, result in enumerate(scoring_results):
            conn_id = conn_ids[i]
            profile = connection_profiles.get(conn_id)
            events = connection_calendar_events_dicts.get(conn_id)

            calendar_res = score_connection_simple(
                connection_public_identifier=conn_id,
                connection_profile=profile,
                connection_events=events,
                company_bonus_points=0.0,
            )

            if isinstance(result, Exception):
                skipped_reasons["llm_scoring_failed"].append(conn_id)
                logger.error(
                    "Rank Connections: reference_public_identifier=%s, connection=%s LLM scoring failed: %s",
                    request.reference_public_identifier,
                    conn_id,
                    str(result),
                )
                llm_metric_scores = {}
                llm_reasoning = f"LLM scoring error: {str(result)}"
                llm_nullify = True  # Nullify on error
            else:
                llm_metric_scores = result.get("metric_scores", {}) or {}
                llm_reasoning = result.get("reasoning") or ""
                llm_nullify = result.get("nullify", False)  # Extract nullify field

            # Normalize metric scores to floats
            combined_metric_scores = {}
            if isinstance(llm_metric_scores, dict):
                for k, v in llm_metric_scores.items():
                    try:
                        combined_metric_scores[str(k)] = float(v)
                    except Exception:
                        combined_metric_scores[str(k)] = 0.0

            # Calculate LLM-only score (sum of LLM metric scores)
            llm_total_score = float(sum(combined_metric_scores.values()))

            # Check if all individual LLM metric scores are zero (or very close to zero)
            # This ensures that if LLM gives zero scores, meeting points don't boost the ranking
            EPSILON = 1e-6  # Small epsilon for floating-point comparison
            all_llm_scores_zero = (
                all(abs(float(v)) < EPSILON for v in combined_metric_scores.values())
                or abs(llm_total_score) < EPSILON
            )

            # If LLM score is zero (or all individual scores are zero), total score remains zero regardless of meetings
            if all_llm_scores_zero:
                # Set meeting and company points to zero when LLM score is zero
                combined_metric_scores["meeting_points"] = 0.0
                combined_metric_scores["company_context_points"] = 0.0
                total_score = 0.0
                # API-facing reasoning: keep this concise and avoid exposing
                # low-level calendar formula labels like `simple_v2`.
                combined_reasoning = _clean_reasoning(llm_reasoning)
                logger.info(
                    "Rank Connections: reference_public_identifier=%s, connection=%s has zero LLM score (all metrics zero), setting total score to 0.0 (meeting points ignored)",
                    request.reference_public_identifier,
                    conn_id,
                )
            else:
                # Add deterministic calendar points only if LLM score is non-zero
                # (meeting_points = simple "number of meetings": +30 first, +15 each additional)
                combined_metric_scores["meeting_points"] = float(
                    calendar_res.metric_scores.get("meeting_points", 0.0)
                )
                combined_metric_scores["company_context_points"] = float(
                    calendar_res.metric_scores.get("company_context_points", 0.0)
                )
                total_score = float(sum(combined_metric_scores.values()))

                # Management boost only when BOTH: (1) calendar meetup with reference, (2) level in boost set.
                # Amounts: 0,1,2,7 → +200; 3 → +150; 4 → +50. If multiple levels, use max.
                levels = get_latest_management_levels(profile)
                has_calendar_meetup_with_reference = (
                    bool(events) if events is not None else False
                )
                boost_amount = 0.0
                if has_calendar_meetup_with_reference and levels:
                    boost_amount = max(
                        (
                            MANAGEMENT_LEVEL_BOOST_AMOUNTS.get(lvl, 0.0)
                            for lvl in levels
                            if lvl in MANAGEMENT_LEVELS_BOOST
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

            # Create response metric_scores without meeting_points and company_context_points
            response_metric_scores = filter_response_metrics(combined_metric_scores)

            # Calculate top_metric from filtered metric_scores (so it matches what's in the response)
            # If score is zero, top_metric should be None
            top_metric_value = (
                None if total_score == 0.0 else get_top_metric(response_metric_scores)
            )

            # Response version (excludes meeting_points and company_context_points)
            row_impact_ranking = impact_ranking_for_db_row(
                metric_scores=combined_metric_scores,
                profile=profile,
            )
            ranked_connections.append(
                {
                    "public_identifier": conn_id,
                    "es_id": connection_es_ids.get(conn_id),
                    "score": total_score,
                    "metric_scores": response_metric_scores,
                    "reasoning": combined_reasoning,
                    "top_metric": top_metric_value,
                    "nullify": llm_nullify,
                    "impact_ranking": row_impact_ranking,
                }
            )

            # DB version (includes meeting_points and company_context_points)
            ranked_connections_for_db.append(
                {
                    "public_identifier": conn_id,
                    "es_id": connection_es_ids.get(conn_id),
                    "score": total_score,
                    "metric_scores": combined_metric_scores,  # Full metric_scores for DB
                    "reasoning": combined_reasoning,
                    "top_metric": top_metric_value,
                    "nullify": llm_nullify,
                    "impact_ranking": row_impact_ranking,
                }
            )

        # Sort by score descending
        ranked_connections.sort(key=lambda x: x["score"], reverse=True)

        # Log summary of processing results
        total_requested = len(request.connection_public_identifiers)
        total_in_response = len(ranked_connections)
        total_excluded = len(skipped_reasons["excluded_before_scoring"])
        total_no_profile = len(skipped_reasons["profile_not_found"])
        total_llm_failed = len(skipped_reasons["llm_scoring_failed"])
        total_scored = total_in_response
        logger.info(
            "Rank Connections: reference_public_identifier=%s, processing summary - "
            "total_requested=%d, total_in_response=%d (scored=%d, excluded_before_scoring=%d), "
            "skipped: no_profile=%d, llm_failed=%d",
            request.reference_public_identifier,
            total_requested,
            total_in_response,
            total_scored,
            total_excluded,
            total_no_profile,
            total_llm_failed,
        )

        # Log details for skipped connections (if any)
        if skipped_reasons["profile_not_found"]:
            logger.warning(
                "Rank Connections: reference_public_identifier=%s, %d connections skipped (profile not found): %s",
                request.reference_public_identifier,
                len(skipped_reasons["profile_not_found"]),
                ", ".join(skipped_reasons["profile_not_found"][:10])
                + ("..." if len(skipped_reasons["profile_not_found"]) > 10 else ""),
            )
        if skipped_reasons["llm_scoring_failed"]:
            logger.error(
                "Rank Connections: reference_public_identifier=%s, %d connections skipped (LLM scoring failed): %s",
                request.reference_public_identifier,
                len(skipped_reasons["llm_scoring_failed"]),
                ", ".join(skipped_reasons["llm_scoring_failed"][:10])
                + ("..." if len(skipped_reasons["llm_scoring_failed"]) > 10 else ""),
            )

        # Persist rankings (upsert) - use full metric_scores including meeting_points and company_context_points
        total_ranked_connections = len(ranked_connections_for_db)
        for conn in ranked_connections_for_db:
            pid = conn["public_identifier"]
            conn["impact_ranking"] = impact_ranking_for_db_row(
                metric_scores=conn.get("metric_scores"),
                profile=connection_profiles.get(pid),
            )
        logger.info(
            "Rank Connections: reference_public_identifier=%s, attempting DB insertion for %d ranked connections",
            request.reference_public_identifier,
            total_ranked_connections,
        )
        try:
            inserted_count = await insert_batch_rankings(
                reference_public_identifier=request.reference_public_identifier,
                reference_es_id=reference_es_id,
                ranked_connections=ranked_connections_for_db,  # Use full metric_scores for DB
            )
            logger.info(
                "Rank Connections: reference_public_identifier=%s, successfully inserted %d/%d connections into DB",
                request.reference_public_identifier,
                inserted_count,
                total_ranked_connections,
            )
        except Exception as e:
            logger.error(
                "Rank Connections: reference_public_identifier=%s, failed to persist connection rankings: %s",
                request.reference_public_identifier,
                str(e),
            )
            logger.error("Traceback:\n%s", traceback.format_exc())
            # Continue to return response even if DB insert fails

        end = time.perf_counter()
        logging.info(f"Rank Connections endpoint took {end - start} seconds")

        return JSONResponse(
            content={
                "reference_public_identifier": request.reference_public_identifier,
                "reference_es_id": reference_es_id,
                "ranked_connections": ranked_connections,
                "excluded_connection_public_identifiers": excluded_connections,
            }
        )

    except Exception as e:
        logger.error("Unhandled exception in /rank_connections: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.post("/get_top_scored_bd_connections", response_model=TopConnectionsOutput)
async def top_connections(request: TopConnectionsInput):
    """
    Fetch the top 20 ranked connections from connection_rankings_bd for a reference profile.
    Returns connection_identifier, score, priority (High / Medium / Low), and impact_ranking
    (Strong / Moderate / Weak), ordered by score descending.
    """
    try:
        rows = await fetch_ranked_connections(
            reference_public_identifier=request.reference_public_identifier,
            limit=20,
            offset=0,
        )
        connections = [
            {
                "connection_identifier": r["connection_public_identifier"],
                "score": float(r["total_score"]),
                "priority": r.get("priority"),
                "impact_ranking": r.get("impact_ranking"),
            }
            for r in rows
            if r.get("connection_public_identifier") is not None
        ]
        return TopConnectionsOutput(connections=connections)
    except Exception as e:
        logger.error(
            "Error in /top_connections for reference=%s: %s",
            request.reference_public_identifier,
            str(e),
        )
        return JSONResponse(
            content={"error": str(e)},
            status_code=500,
        )


@router.post(
    "/get_connections_with_meetings",
    response_model=TopConnectionsOutput,
)
async def get_connections_with_meetings(request: TopConnectionsInput):
    """
    Fetch ranked connections for a reference profile where meeting_points > 0.
    Returns connection_identifier, score, priority, and impact_ranking for each,
    ordered by score descending. Same response shape as get_top_scored_bd_connections.
    """
    try:
        rows = await fetch_ranked_connections_with_meeting_points(
            reference_public_identifier=request.reference_public_identifier,
            limit=None,
            offset=None,
        )
        connections = [
            {
                "connection_identifier": r["connection_public_identifier"],
                "score": float(r["total_score"]),
                "priority": r.get("priority"),
                "impact_ranking": r.get("impact_ranking"),
            }
            for r in rows
            if r.get("connection_public_identifier") is not None
        ]
        return TopConnectionsOutput(connections=connections)
    except Exception as e:
        logger.error(
            "Error in /get_connections_with_meetings for reference=%s: %s",
            request.reference_public_identifier,
            str(e),
        )
        return JSONResponse(
            content={"error": str(e)},
            status_code=500,
        )


@router.post(
    "/generate_profile_description",
    response_model=GenerateProfileDescriptionOutput,
)
async def generate_profile_description_endpoint(
    request: GenerateProfileDescriptionInput,
    es_client_compressed=Depends(get_es_client_with_compression),
):
    """
    Generate a short, concise description of a LinkedIn profile using LLM.
    Takes a public_identifier, fetches the profile from Elasticsearch, and generates a brief professional description.
    """
    try:
        start = time.perf_counter()

        # Fetch profile from ES
        es_id, profile = await fetch_profile_by_public_identifier(
            request.public_identifier,
            es_client=es_client_compressed,
            source_fields=None,  # fetch full profile for best description
            only_top_experience=True,
        )

        if profile is None:
            return JSONResponse(
                content={
                    "public_identifier": request.public_identifier,
                    "es_id": None,
                    "description": "Profile not found",
                    "tagline": None,
                },
                status_code=404,
            )

        # Generate description using LLM
        markdown = await generate_profile_description(
            profile=profile,
            model="openai/gpt-4o",
        )

        # Extract TAGLINE into separate key and remove it from description markdown
        tagline = None
        description = markdown or ""
        if isinstance(description, str) and description:
            # Matches:
            # TAGLINE:
            # - something...
            m = re.search(
                r"(?ims)^\s*TAGLINE:\s*\n\s*-\s*(.+?)\s*(?:\n\s*\n|$)",
                description,
            )
            if m:
                tagline = (m.group(1) or "").strip()
                # Remove the tagline block from the markdown
                description = re.sub(
                    r"(?ims)^\s*TAGLINE:\s*\n\s*-\s*.+?\s*(?:\n\s*\n|$)",
                    "\n",
                    description,
                ).strip()

        end = time.perf_counter()
        logging.info(
            f"Generate Profile Description endpoint took {end - start} seconds"
        )

        return JSONResponse(
            content={
                "public_identifier": request.public_identifier,
                "es_id": es_id,
                "description": description,
                "tagline": tagline,
            }
        )

    except Exception as e:
        logger.error("Unhandled exception in /generate_profile_description: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.post(
    "/rank_connections/reasoning",
    response_model=RankConnectionsReasoningOutput,
)
async def get_rank_connections_reasoning(
    request: RankConnectionsReasoningInput,
) -> JSONResponse:
    """
    Fetch reasoning strings for specific ranked connections from connection_rankings_bd.

    - Takes a reference_public_identifier and one or more connection_public_identifiers.
    - Returns 1–2 line, cleaned reasoning per connection.
    """
    try:
        if not request.connection_public_identifiers:
            return JSONResponse(
                content={
                    "reference_public_identifier": request.reference_public_identifier,
                    "results": [],
                    "not_found": [],
                },
                status_code=200,
            )

        rows_by_conn = await fetch_specific_ranked_connections(
            reference_public_identifier=request.reference_public_identifier,
            connection_public_identifiers=request.connection_public_identifiers,
        )

        def _normalize_reasoning_value(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                t = value.get("text")
                if isinstance(t, str):
                    return t
                return str(value)
            return str(value)

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

        results: List[Dict[str, Any]] = []
        not_found: List[str] = []

        for conn_id in request.connection_public_identifiers:
            row = rows_by_conn.get(conn_id)
            if not row:
                not_found.append(conn_id)
                continue
            raw_reasoning = _normalize_reasoning_value(row.get("reasoning"))
            cleaned = _clean_reasoning(raw_reasoning)
            results.append(
                {
                    "public_identifier": conn_id,
                    "reasoning": cleaned or None,
                }
            )

        payload: Dict[str, Any] = {
            "reference_public_identifier": request.reference_public_identifier,
            "results": results,
            "not_found": not_found,
        }
        return JSONResponse(content=payload)
    except Exception as e:
        logger.error(
            "Unhandled exception in /rank_connections/reasoning: %s",
            str(e),
        )
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)
