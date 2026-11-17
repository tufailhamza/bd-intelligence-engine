"""
POST /daily-email

Generate a structured daily email digest for the reference person.
Groups meetings by company, attaches bd_news or participant-news summaries,
and returns a subject, greeting, and per-company meeting list.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.daily_email import (
    DailyEmailCompanyMeetingsGroup,
    DailyEmailInput,
    DailyEmailMeetingInCompany,
    DailyEmailNewsSnippet,
    DailyEmailOutput,
)
from app.routes.dependancies import get_es_client_bd, get_es_client_with_compression
from app.utils.daily_email.company_fetch import fetch_company_by_universal_names
from app.utils.daily_email.domain_mapping import (
    augment_profile_with_universal,
    build_domain_mapped_universals,
    reference_employer_sets_from_profile_email,
)
from app.utils.daily_email.helpers import (
    all_current_normalized_company_names,
    build_daily_email_greeting,
    get_profile_full_name,
)
from app.utils.daily_email.meeting_digest import (
    build_grouped_daily_meetings,
    employer_company_universals_from_profile,
    profile_company_display_and_universal,
    reference_employer_company_universals,
    resolve_news_for_meeting,
)
from app.utils.daily_email.news_fetch import (
    fetch_latest_bd_news_per_linkedin_identifier,
)
from app.utils.daily_email.participant_news import (
    CompanyParticipantsFallbackEntry,
    ConnectionNewsFallbackEntry,
    build_company_participants_news_fallback,
    build_participant_news_fallback_by_event,
    make_meeting_event_key,
)
from app.utils.daily_email.subject import generate_daily_email_subject
from app.utils.profile_fetching.public_identifier_fetch import (
    fetch_profiles_by_public_identifiers,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/daily-email", response_model=DailyEmailOutput)
async def daily_email(
    request: DailyEmailInput,
    es_client_compressed=Depends(get_es_client_with_compression),
    es_client_bd=Depends(get_es_client_bd),
):
    """
    Receive reference_public_identifier, connection_public_identifiers, and calendar_events.

    - Excludes connections in the same organisation as the reference profile (normalized
      company name on **all** current experience rows, and LinkedIn company universal
      /company/{slug} across those rows). When ``reference_profile_email`` is set and
      ``company_domain_mappings`` contains that email's domain, the reference employer
      for filtering is taken from the mapping (with profile experience as fallback for
      whichever side the map does not supply).
    - Resolves company news from bd_news (company-level), falling back to an
      LLM-generated participant-news summary when no company article is found.
    - When multiple connections share the same company, generates ONE combined
      participant summary covering all of them.
    - Meeting times are expressed in the caller-supplied timezone when provided.

    Returns structured JSON: subject, greeting, and meetings grouped by company.
    Company groups with no company news and no participant-news summary are omitted.
    """
    try:
        if not request.connection_public_identifiers:
            return JSONResponse(
                content={
                    "error": "Must provide at least one connection_public_identifier"
                },
                status_code=400,
            )

        # ------------------------------------------------------------------ #
        # 1. Deserialise calendar events                                      #
        # ------------------------------------------------------------------ #
        calendar_events_by_connection: Dict[str, List[Dict[str, Any]]] = {}
        if request.calendar_events:
            for conn_id, events in request.calendar_events.items():
                if events:
                    calendar_events_by_connection[conn_id] = [
                        (
                            e.model_dump(mode="json", exclude_none=True)
                            if hasattr(e, "model_dump")
                            else e.dict()
                        )
                        for e in events
                    ]

        # ------------------------------------------------------------------ #
        # 2. Fetch profiles (reference + all connections in one batch)        #
        # ------------------------------------------------------------------ #
        all_pids = [
            request.reference_public_identifier
        ] + request.connection_public_identifiers
        profiles_map = await fetch_profiles_by_public_identifiers(
            public_identifiers=all_pids,
            es_client=es_client_compressed,
            source_fields=None,
            only_top_experience=False,
        )

        reference_profile = profiles_map.get(
            request.reference_public_identifier, (None, None)
        )[1]
        reference_full_name = get_profile_full_name(reference_profile)
        logger.info(
            "Daily email: reference profile loaded (pid=%s, profile_type=%s, has_name=%s, name_len=%d)",
            request.reference_public_identifier,
            type(reference_profile).__name__,
            bool(reference_full_name),
            len(reference_full_name),
        )
        if not reference_full_name:
            logger.warning(
                "Daily email: reference_full_name is empty (pid=%s). "
                "Output will use empty greeting name unless profile data improves.",
                request.reference_public_identifier,
            )

        profiles_by_connection: Dict[str, Any] = {}
        connection_company_names: Dict[str, Set[str]] = {}
        for pid in request.connection_public_identifiers:
            profile = profiles_map.get(pid, (None, None))[1]
            profiles_by_connection[pid] = profile
            connection_company_names[pid] = all_current_normalized_company_names(
                profile
            )

        raw_domain_mappings: Optional[Dict[str, Any]] = None
        if request.company_domain_mappings:
            raw_domain_mappings = {
                domain: entry.model_dump()
                for domain, entry in request.company_domain_mappings.items()
            }

        profile_ref_names = all_current_normalized_company_names(reference_profile)
        profile_ref_uni = reference_employer_company_universals(reference_profile)

        ref_email = (request.reference_profile_email or "").strip()
        if ref_email and raw_domain_mappings:
            dom_names, dom_uni = reference_employer_sets_from_profile_email(
                ref_email, raw_domain_mappings
            )
            if dom_names or dom_uni:
                reference_company_names = dom_names if dom_names else profile_ref_names
                ref_employer_universals = dom_uni if dom_uni else profile_ref_uni
                logger.info(
                    "Daily email: reference employer from reference_profile_email "
                    "domain (names=%s, universals=%s); missing side(s) fall back to profile.",
                    sorted(reference_company_names),
                    sorted(ref_employer_universals),
                )
            else:
                reference_company_names = profile_ref_names
                ref_employer_universals = profile_ref_uni
        else:
            reference_company_names = profile_ref_names
            ref_employer_universals = profile_ref_uni

        # ------------------------------------------------------------------ #
        # 3. Filter out same-org connections                                  #
        # ------------------------------------------------------------------ #
        connection_public_identifiers = [
            pid
            for pid in request.connection_public_identifiers
            if not (
                reference_company_names
                and connection_company_names.get(pid)
                and (reference_company_names & connection_company_names[pid])
            )
        ]
        if not connection_public_identifiers:
            logger.info(
                "Daily email early return (name/company filter removed all connections): "
                "pid=%s input_count=%d ref_name_empty=%s ref_company_name_count=%d",
                request.reference_public_identifier,
                len(request.connection_public_identifiers),
                not bool(reference_full_name),
                len(reference_company_names),
            )
            return DailyEmailOutput(
                reference_public_identifier=request.reference_public_identifier,
                reference_full_name=reference_full_name,
                subject="Your daily digest",
                greeting=build_daily_email_greeting(reference_full_name),
                meetings=[],
                message="No connections to include (all are in the same organization as you).",
            )

        # ------------------------------------------------------------------ #
        # 4. Domain mapping: override company universal from email domain     #
        # ------------------------------------------------------------------ #
        domain_mapped_universals: Dict[str, Tuple[str, Optional[str]]] = (
            build_domain_mapped_universals(
                connection_public_identifiers=connection_public_identifiers,
                calendar_events_by_connection=calendar_events_by_connection,
                company_domain_mappings=raw_domain_mappings,
            )
        )
        if domain_mapped_universals:
            logger.debug(
                "Daily email: domain mapping resolved for %d connection(s): %s",
                len(domain_mapped_universals),
                list(domain_mapped_universals.keys()),
            )

        effective_profiles_by_connection: Dict[str, Any] = {}
        for pid in connection_public_identifiers:
            profile = profiles_by_connection.get(pid)
            if pid in domain_mapped_universals:
                universal, co_name = domain_mapped_universals[pid]
                effective_profiles_by_connection[pid] = augment_profile_with_universal(
                    profile, universal, co_name
                )
            else:
                effective_profiles_by_connection[pid] = profile

        # ------------------------------------------------------------------ #
        # 4b. Exclude connections whose employer matches reference by LinkedIn #
        #     company universal (after domain mapping), not only display name. #
        # ------------------------------------------------------------------ #
        if ref_employer_universals:
            before_uni = len(connection_public_identifiers)

            connection_public_identifiers = [
                pid
                for pid in connection_public_identifiers
                if not (
                    ref_employer_universals
                    & employer_company_universals_from_profile(
                        effective_profiles_by_connection.get(pid)
                    )
                )
            ]
            dropped = before_uni - len(connection_public_identifiers)
            if dropped:
                logger.info(
                    "Daily email: excluded %d connection(s) matching reference employer "
                    "LinkedIn company universal(s) %s",
                    dropped,
                    sorted(ref_employer_universals),
                )
        if not connection_public_identifiers:
            logger.info(
                "Daily email early return (universal filter removed all connections): "
                "pid=%s ref_name_empty=%s ref_universal_count=%d",
                request.reference_public_identifier,
                not bool(reference_full_name),
                len(ref_employer_universals),
            )
            return DailyEmailOutput(
                reference_public_identifier=request.reference_public_identifier,
                reference_full_name=reference_full_name,
                subject="Your daily digest",
                greeting=build_daily_email_greeting(reference_full_name),
                meetings=[],
                message="No connections to include (all are in the same organization as you).",
            )

        # ------------------------------------------------------------------ #
        # 5. Fetch bd_news and company ES docs                                #
        # ------------------------------------------------------------------ #
        company_universals: List[str] = []
        for pid in connection_public_identifiers:
            _, universal = profile_company_display_and_universal(
                effective_profiles_by_connection.get(pid)
            )
            if universal and universal not in company_universals:
                company_universals.append(universal)

        bd_news_index = (os.getenv("ES_BD_NEWS_INDEX") or "bd_news").strip()
        company_keys = {
            str(u).strip() for u in company_universals if u and str(u).strip()
        }

        latest_bd_by_company: Dict[str, Dict[str, Any]] = {}
        if company_keys:
            try:
                merged_bd = await fetch_latest_bd_news_per_linkedin_identifier(
                    es_client_bd=es_client_bd,
                    linkedin_identifiers=sorted(company_keys),
                    index_name=bd_news_index,
                    max_hits=500,
                    max_age_days=90,
                )
                latest_bd_by_company = {
                    k: v for k, v in merged_bd.items() if k in company_keys
                }
            except Exception as e:
                logger.warning("Daily email: bd_news fetch failed: %s", e)

        company_es_by_universal: Dict[str, Dict[str, Any]] = {}
        if company_universals:
            try:
                company_es_by_universal = await fetch_company_by_universal_names(
                    es_client_compressed, company_universals
                )
            except Exception as e:
                logger.warning("Daily email: company ES fetch failed: %s", e)

        # ------------------------------------------------------------------ #
        # 6. Per-event participant news fallback (single-connection summaries)#
        # ------------------------------------------------------------------ #
        participant_fallback_candidates: Dict[str, ConnectionNewsFallbackEntry] = {}
        for conn_id in connection_public_identifiers:
            profile = effective_profiles_by_connection.get(conn_id)
            _, company_universal = profile_company_display_and_universal(profile)
            if not isinstance(company_universal, str) or not company_universal.strip():
                continue
            u_key = company_universal.strip()
            if not isinstance(company_es_by_universal.get(u_key), dict):
                continue
            if (
                resolve_news_for_meeting(u_key, latest_bd_by_company).get("source")
                or "none"
            ) != "none":
                continue
            display_name = (get_profile_full_name(profile) or "").strip()
            if not display_name:
                continue
            for ev in calendar_events_by_connection.get(conn_id) or []:
                if not isinstance(ev, dict):
                    continue
                event_label = (
                    str(ev.get("summary") or ev.get("title") or "").strip()
                    or f"Meeting with {conn_id}"
                )
                participant_fallback_candidates[make_meeting_event_key(conn_id, ev)] = (
                    ConnectionNewsFallbackEntry(
                        event_label=event_label,
                        connection_public_identifier=conn_id,
                        connection_display_name=display_name,
                    )
                )

        participant_news_by_event: Dict[str, Dict[str, Any]] = {}
        if participant_fallback_candidates:
            try:
                participant_news_by_event = (
                    await build_participant_news_fallback_by_event(
                        es_client_bd=es_client_bd,
                        index_name=bd_news_index,
                        meetings_participants_map=participant_fallback_candidates,
                    )
                )
            except Exception as e:
                logger.warning("Daily email: per-event participant news failed: %s", e)
        if participant_news_by_event:
            logger.debug(
                "Daily email: per-event participant summaries attached (count=%d)",
                len(participant_news_by_event),
            )

        # ------------------------------------------------------------------ #
        # 7. Company-level combined participant news                          #
        #    (one summary for all connections sharing the same company)       #
        # ------------------------------------------------------------------ #
        _company_to_pids: Dict[str, List] = {}
        _company_to_display: Dict[str, str] = {}
        for conn_id in connection_public_identifiers:
            profile = effective_profiles_by_connection.get(conn_id)
            company_display, company_universal = profile_company_display_and_universal(
                profile
            )
            if not isinstance(company_universal, str) or not company_universal.strip():
                continue
            u_key = company_universal.strip()
            if not isinstance(company_es_by_universal.get(u_key), dict):
                continue
            if (
                resolve_news_for_meeting(u_key, latest_bd_by_company).get("source")
                or "none"
            ) != "none":
                continue
            display_name = (get_profile_full_name(profile) or "").strip()
            if not display_name:
                continue
            _company_to_pids.setdefault(u_key, []).append((conn_id, display_name))
            _company_to_display[u_key] = company_display

        company_participants_candidates: Dict[str, CompanyParticipantsFallbackEntry] = {
            u_key: CompanyParticipantsFallbackEntry(
                company_display_name=_company_to_display[u_key],
                participants=participants,
            )
            for u_key, participants in _company_to_pids.items()
        }

        company_participant_news_by_universal: Dict[str, Any] = {}
        if company_participants_candidates:
            logger.info(
                "Daily email: building company-level participant news for %d company(ies): %s",
                len(company_participants_candidates),
                {
                    u: len(e.participants)
                    for u, e in company_participants_candidates.items()
                },
            )
            try:
                company_participant_news_by_universal = (
                    await build_company_participants_news_fallback(
                        es_client_bd=es_client_bd,
                        index_name=bd_news_index,
                        company_participants_map=company_participants_candidates,
                    )
                )
            except Exception as e:
                logger.warning(
                    "Daily email: company-level participant news failed: %s", e
                )

        # ------------------------------------------------------------------ #
        # 8. Build grouped meeting output                                     #
        # ------------------------------------------------------------------ #
        reference_timezone: Optional[str] = (
            request.timezone.strip()
            if isinstance(request.timezone, str) and request.timezone.strip()
            else None
        )
        if reference_timezone:
            logger.info(
                "Daily email: using reference timezone=%s for all meeting times",
                reference_timezone,
            )

        (
            meeting_groups_raw,
            flat_meeting_count,
            unique_calendar_event_count,
            es_matched_meeting_row_count,
        ) = build_grouped_daily_meetings(
            connection_public_identifiers=connection_public_identifiers,
            calendar_events_by_connection=calendar_events_by_connection,
            profiles_by_connection=effective_profiles_by_connection,
            latest_bd_by_company=latest_bd_by_company,
            participant_news_by_event=participant_news_by_event,
            company_es_by_universal=company_es_by_universal,
            company_participant_news_by_universal=company_participant_news_by_universal,
            reference_timezone=reference_timezone,
            domain_mapped_connection_ids=frozenset(domain_mapped_universals.keys()),
        )

        digest_message: Optional[str] = None
        if not meeting_groups_raw and flat_meeting_count > 0:
            if es_matched_meeting_row_count == 0:
                digest_message = (
                    "No meetings are shown because none of the companies linked to "
                    "these events were found in the company directory (Elasticsearch companies index)."
                )
            else:
                digest_message = (
                    "No meetings are shown because no company or participant news "
                    "was available for these meetings (bd_news / summaries)."
                )

        # ------------------------------------------------------------------ #
        # 9. Serialise to response schema                                     #
        # ------------------------------------------------------------------ #
        meetings = [
            DailyEmailCompanyMeetingsGroup(
                company_name=g["company_name"],
                company_universal_name=g["company_universal_name"],
                company_website_url=g.get("company_website_url"),
                news=DailyEmailNewsSnippet(**g["news"]),
                meetings=[
                    DailyEmailMeetingInCompany(
                        connection_public_identifier=row[
                            "connection_public_identifier"
                        ],
                        event_title=row.get("event_title") or [],
                        meeting_time=row["meeting_time"],
                    )
                    for row in g["meetings"]
                ],
            )
            for g in meeting_groups_raw
        ]

        greeting_text = build_daily_email_greeting(reference_full_name)
        logger.info(
            "Daily email greeting debug: reference_public_identifier=%s, greeting=%r",
            request.reference_public_identifier,
            greeting_text,
        )

        return DailyEmailOutput(
            reference_public_identifier=request.reference_public_identifier,
            reference_full_name=reference_full_name,
            subject=await generate_daily_email_subject(
                reference_full_name=reference_full_name,
                meeting_groups=meeting_groups_raw,
                unique_calendar_event_count=unique_calendar_event_count,
            ),
            greeting=greeting_text,
            meetings=meetings,
            message=digest_message,
        )

    except Exception as e:
        logger.exception(
            "Unhandled exception in /daily-email: %s (pid=%s, connection_count=%d, has_reference_profile_email=%s)",
            str(e),
            request.reference_public_identifier,
            len(request.connection_public_identifiers),
            bool((request.reference_profile_email or "").strip()),
        )
        return JSONResponse(
            content={"error": "Internal Server Error"},
            status_code=500,
        )
