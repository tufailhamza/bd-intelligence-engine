import json
import os
import re
import time
import asyncio
import logging
import ast
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from elasticsearch import AsyncElasticsearch
from qutils.llm.asynchronous import invoke, stream

from app.database.me_assignment_chat import (
    fetch_assignment_context,
    fetch_assignment_context_by_id,
    fetch_assignment_context_by_assignment_id,
    fetch_assignment_contexts,
    fetch_recent_chat_messages,
    get_or_create_single_session,
    insert_chat_message,
    update_chat_session_metadata,
    upsert_assignment_context,
)
from app.models.schemas.me_assignment_chat import (
    FEATURE_KEY_ASSIGNMENT_CREATION,
    FEATURE_KEY_CONTENT_CHAT,
)
from app.utils.profile_fetching.public_identifier_fetch import (
    fetch_profile_by_es_id,
    fetch_profile_by_public_identifier,
    fetch_profiles_by_public_identifiers,
)
from app.utils.me_assignment_chat.lookalike_companies import (
    LOOKALIKE_OPPORTUNITY_TYPE,
    build_default_lookalike_opportunity,
    build_review_summary_chips,
    exclude_lookalike_opportunities,
    fetch_lookalike_companies_via_comp_gen,
    pick_lookalike_opportunity,
    prepare_review_opportunities,
)
from app.utils.me_assignment_chat.prompts import (
    ASSIGNMENT_CHAT_SYSTEM_PROMPT,
    ASSIGNMENT_CHAT_USER_PROMPT_TEMPLATE,
    ASSIGNMENT_FIELD_KEYS,
    ASSIGNMENT_FIELD_LABELS,
    ASSIGNMENT_GREETING_SYSTEM_PROMPT,
    ASSIGNMENT_GREETING_USER_PROMPT_TEMPLATE,
    PROFILE_SUGGESTION_DECIDER_SYSTEM_PROMPT,
    PROFILE_SUGGESTION_DECIDER_USER_PROMPT_TEMPLATE,
    COMPANY_MAPPING_DECIDER_SYSTEM_PROMPT,
    COMPANY_MAPPING_DECIDER_USER_PROMPT_TEMPLATE,
    CLIENT_REMOVE_DECIDER_SYSTEM_PROMPT,
    CLIENT_REMOVE_DECIDER_USER_PROMPT_TEMPLATE,
    CLIENT_OPERATION_PLANNER_SYSTEM_PROMPT,
    CLIENT_OPERATION_PLANNER_USER_PROMPT_TEMPLATE,
    PROFILE_OPERATION_PLANNER_SYSTEM_PROMPT,
    PROFILE_OPERATION_PLANNER_USER_PROMPT_TEMPLATE,
    ASSIGNMENT_REVIEW_SYSTEM_PROMPT,
    ASSIGNMENT_REVIEW_USER_PROMPT_TEMPLATE,
    NEW_ASSIGNMENT_DECIDER_SYSTEM_PROMPT,
    NEW_ASSIGNMENT_DECIDER_USER_PROMPT_TEMPLATE,
    SAVE_ASSIGNMENT_DECIDER_SYSTEM_PROMPT,
    SAVE_ASSIGNMENT_DECIDER_USER_PROMPT_TEMPLATE,
    ASSIGNMENT_PERSON_IMPACT_SYSTEM_PROMPT,
    ASSIGNMENT_PERSON_IMPACT_USER_PROMPT_TEMPLATE,
    ASSIGNMENT_OPPORTUNITIES_SYSTEM_PROMPT,
    ASSIGNMENT_OPPORTUNITIES_USER_PROMPT_TEMPLATE,
    ASSIGNMENT_LOOKALIKE_OPPORTUNITY_SYSTEM_PROMPT,
    ASSIGNMENT_LOOKALIKE_OPPORTUNITY_USER_PROMPT_TEMPLATE,
    ASSIGNMENT_FEED_IMPACT_SYSTEM_PROMPT,
    ASSIGNMENT_FEED_IMPACT_USER_PROMPT_TEMPLATE,
    CONTENT_CHAT_SYSTEM_PROMPT,
    CONTENT_CHAT_USER_PROMPT_TEMPLATE,
    CONTENT_FIELD_KEYS,
    CONTENT_FIELD_LABELS,
    REQUIRED_ASSIGNMENT_FIELD_KEYS,
    REQUIRED_ASSIGNMENT_FIELD_LABELS,
    PEOPLE_NOT_FOUND_NOTE_SYSTEM_PROMPT,
    PEOPLE_NOT_FOUND_NOTE_USER_PROMPT_TEMPLATE,
    STREAM_INTERIM_ACK_SYSTEM_PROMPT,
    STREAM_INTERIM_ACK_USER_PROMPT_TEMPLATE,
    PIPELINE_THINKING_SYSTEM_PROMPT,
    PIPELINE_THINKING_USER_PROMPT_TEMPLATE,
)
from app.utils.news_fetching.news_utils import get_source_from_pegasus

logger = logging.getLogger(__name__)

# Set True locally to enable verbose [people_trace] logs for people-involved debugging.
PEOPLE_TRACE_DEBUG = True
# Set True locally to log [live_preview_timing] phase durations for stream pipeline.
LIVE_PREVIEW_TIMING_DEBUG = True

MAP_PERSON_BATCH_SIZE = 5
# Max person-linked opportunity cards before the lookalike card is appended in review.
MAX_PERSON_OPPORTUNITIES = 20


class _LivePreviewTimingTracker:
    def __init__(self, *, label: str = "assignment_stream") -> None:
        self._label = label
        self._pipeline_start = time.perf_counter()
        self._phase_start = self._pipeline_start

    def mark(self, phase: str, **fields: Any) -> None:
        if not LIVE_PREVIEW_TIMING_DEBUG:
            return
        now = time.perf_counter()
        phase_ms = (now - self._phase_start) * 1000.0
        total_ms = (now - self._pipeline_start) * 1000.0
        self._phase_start = now
        extras = " | ".join(f"{k}={v!r}" for k, v in fields.items())
        suffix = f" | {extras}" if extras else ""
        logger.info(
            "[live_preview_timing] label=%s phase=%s phase_ms=%.1f total_ms=%.1f%s",
            self._label,
            phase,
            phase_ms,
            total_ms,
            suffix,
        )

    def summary(self, phase: str = "pipeline_complete", **fields: Any) -> None:
        self.mark(phase, **fields)


def _safe_json_loads_from_text(text: str) -> Any:
    if not text:
        raise ValueError("Empty response")
    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", t).replace("```", "").strip()
    obj = re.search(r"\{[\s\S]*\}", t)
    return json.loads(obj.group(0) if obj else t)


def _eligible_people_for_opportunities(
    people_details: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    eligible: List[Dict[str, Any]] = []
    seen_public_identifiers: set[str] = set()
    for person in people_details or []:
        if not isinstance(person, dict):
            continue
        public_identifier = _to_clean_string(person.get("public_identifier"))
        if not public_identifier or public_identifier in seen_public_identifiers:
            continue
        seen_public_identifiers.add(public_identifier)
        eligible.append(person)
    return eligible


def _normalize_opportunity_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    action = _to_clean_string(item.get("action"))
    title = _to_clean_string(item.get("title"))
    if not action and not title:
        return None
    related_person_public_identifier = _to_clean_string(
        item.get("related_person_public_identifier")
    )
    related_person_name = _to_clean_string(item.get("related_person_name"))
    return {
        "type": _to_clean_string(item.get("type")) or "OPPORTUNITY",
        "title": title or action or "",
        "action": action or title or "",
        "why_now": _to_clean_string(item.get("why_now")) or "",
        "related_person_name": related_person_name,
        "related_person_public_identifier": related_person_public_identifier,
        "cta_label": (
            _to_clean_string(item.get("cta_label"))
            if related_person_public_identifier
            else None
        ),
    }


def _opportunity_covers_person(
    opportunity: Dict[str, Any],
    *,
    public_identifier: str,
    name: Optional[str] = None,
) -> bool:
    related_public_identifier = _to_clean_string(
        opportunity.get("related_person_public_identifier")
    )
    if related_public_identifier and related_public_identifier == public_identifier:
        return True
    if not name:
        return False
    related_name = _to_clean_string(opportunity.get("related_person_name"))
    if related_name and _person_names_match(name, related_name):
        return True
    return False


def _build_fallback_opportunity_for_person(person: Dict[str, Any]) -> Dict[str, Any]:
    name = _to_clean_string(person.get("name")) or "Contact"
    public_identifier = _to_clean_string(person.get("public_identifier"))
    company = _to_clean_string(person.get("company"))
    why_now = (
        _to_clean_string(person.get("why_this_person_matters"))
        or "They were involved in this completed assignment."
    )

    first_action: Optional[str] = None
    actions = person.get("possible_actions")
    if isinstance(actions, list):
        for item in actions:
            first_action = _to_clean_string(item)
            if first_action:
                break

    if first_action:
        title = first_action
        action = (
            f"{first_action} with {name} at {company}."
            if company
            else f"{first_action} with {name}."
        )
    else:
        title = f"Follow up with {name}"
        action = (
            f"Reach out to {name} at {company} about this assignment."
            if company
            else f"Reach out to {name} about this assignment."
        )

    return {
        "type": "REFERRAL ASK",
        "title": title,
        "action": action,
        "why_now": why_now,
        "related_person_name": name,
        "related_person_public_identifier": public_identifier,
        "cta_label": "Send Message",
    }


def _ensure_opportunities_cover_people(
    opportunities: List[Dict[str, Any]],
    *,
    people_details: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    lookalike = pick_lookalike_opportunity(opportunities)
    person_opportunities = [
        dict(opportunity)
        for opportunity in exclude_lookalike_opportunities(opportunities)
        if isinstance(opportunity, dict)
    ]

    for person in _eligible_people_for_opportunities(people_details):
        public_identifier = _to_clean_string(person.get("public_identifier"))
        name = _to_clean_string(person.get("name"))
        if not public_identifier:
            continue
        if any(
            _opportunity_covers_person(
                opportunity,
                public_identifier=public_identifier,
                name=name,
            )
            for opportunity in person_opportunities
        ):
            continue
        if len(person_opportunities) >= MAX_PERSON_OPPORTUNITIES:
            break
        person_opportunities.append(_build_fallback_opportunity_for_person(person))

    if lookalike is not None:
        return [*person_opportunities, dict(lookalike)]
    return person_opportunities


async def _generate_assignment_opportunities(
    *,
    assignment_json: str,
    people_details: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    eligible_people = _eligible_people_for_opportunities(people_details)
    prompt = ASSIGNMENT_OPPORTUNITIES_USER_PROMPT_TEMPLATE.format(
        assignment_json=assignment_json,
        verified_people_count=len(eligible_people),
        people_details_json=json.dumps(
            people_details or [], ensure_ascii=False, indent=2
        ),
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1",
            temperature=0.1,
            fallbacks=["anthropic/claude-sonnet-4-5-20250929"],
            messages=[
                {
                    "role": "system",
                    "content": ASSIGNMENT_OPPORTUNITIES_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return _ensure_opportunities_cover_people([], people_details=people_details)
        raw_items = parsed.get("opportunities")
        if not isinstance(raw_items, list):
            return _ensure_opportunities_cover_people([], people_details=people_details)

        opportunities: List[Dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_opportunity_item(item)
            if normalized is not None:
                opportunities.append(normalized)
        return _ensure_opportunities_cover_people(
            opportunities,
            people_details=people_details,
        )
    except Exception:
        logger.exception("Failed to generate assignment opportunities")
        return _ensure_opportunities_cover_people([], people_details=people_details)


async def _generate_lookalike_card_copy(*, assignment_json: str) -> Dict[str, str]:
    prompt = ASSIGNMENT_LOOKALIKE_OPPORTUNITY_USER_PROMPT_TEMPLATE.format(
        assignment_json=assignment_json,
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1",
            temperature=0.1,
            fallbacks=["anthropic/claude-sonnet-4-5-20250929"],
            messages=[
                {
                    "role": "system",
                    "content": ASSIGNMENT_LOOKALIKE_OPPORTUNITY_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return {}
        title = _to_clean_string(parsed.get("title")) or ""
        action = _to_clean_string(parsed.get("action")) or ""
        if not title and not action:
            return {}
        return {
            "title": title or action,
            "action": action or title,
        }
    except Exception:
        logger.exception("Failed to generate lookalike card copy")
        return {}


async def _detect_links_with_llm(message: str) -> Dict[str, Any]:
    """
    Use an LLM to detect content links inside a free-form user message.

    Returns:
        {
          "links": [url, ...],
          "primary_link": url or None
        }
    """
    if not isinstance(message, str) or not message.strip():
        return {"links": [], "primary_link": None}

    prompt = f"""
You are a careful URL detection assistant.

Task:
- Read the following user message.
- Extract all distinct HTTP/HTTPS URLs contained anywhere in the text.
- Identify the single PRIMARY link that the message is mainly about.
  - When there is only one link, it is the primary_link.
  - When there are multiple, choose the one the user is clearly referring to
    as the main article, post, or content to discuss.

User message:
{message}

Output JSON only, with this exact shape:
{{
  "links": ["https://...", "..."],
  "primary_link": "https://..."  // or null when no clear primary link
}}
""".strip()

    try:
        response = await invoke(
            messages=[{"role": "user", "content": prompt}],
            model="openai/gpt-4o-mini",
            temperature=0.0,
            fallbacks=["openai/gpt-4.1-mini"],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return {"links": [], "primary_link": None}
        links = parsed.get("links") or []
        if not isinstance(links, list):
            links = []
        clean_links: List[str] = []
        seen = set()
        for item in links:
            if not isinstance(item, str):
                continue
            url = item.strip()
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            clean_links.append(url)
        primary = parsed.get("primary_link")
        if isinstance(primary, str):
            primary = primary.strip() or None
        else:
            primary = None
        if primary and primary not in clean_links:
            clean_links.insert(0, primary)
        elif not primary and clean_links:
            primary = clean_links[0]
        return {"links": clean_links, "primary_link": primary}
    except Exception:
        logger.exception("Failed to detect links with LLM; falling back to no links")
        return {"links": [], "primary_link": None}


async def review_assignment_payload(
    raw_payload: Any,
    es_client: Optional[AsyncElasticsearch] = None,
) -> Dict[str, Any]:
    def _normalize_payload(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            draft = payload.get("assignment_draft")
            if isinstance(draft, dict):
                return draft
            return payload
        return {}

    def _parse_client_names(value: Any) -> List[str]:
        names: List[str] = []
        seen = set()

        def _add(v: Any) -> None:
            s = _to_clean_string(v)
            if not s:
                return
            key = s.lower()
            if key in seen:
                return
            seen.add(key)
            names.append(s)

        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _add(
                        item.get("display_name")
                        or item.get("li_name")
                        or item.get("universal_name")
                        or item.get("li_universalname")
                    )
                else:
                    _add(item)
            return names

        text = _to_clean_string(value)
        if not text:
            return names

        parsed: Any = None
        try:
            parsed = json.loads(text)
        except Exception:
            try:
                parsed = ast.literal_eval(text)
            except Exception:
                parsed = None

        if isinstance(parsed, list):
            return _parse_client_names(parsed)

        for part in [p.strip() for p in text.split(";") if p.strip()]:
            _add(part)
        return names

    def _split_people_text(text: str) -> List[str]:
        return [
            part.strip()
            for part in re.split(r"\s*(?:,|;|\band\b)\s*", text)
            if part.strip()
        ]

    def _parse_people_names(value: Any) -> List[str]:
        names: List[str] = []
        seen = set()

        def _add(v: Any) -> None:
            s = _to_clean_string(v)
            if not s:
                return
            key = s.lower()
            if key in seen:
                return
            seen.add(key)
            names.append(s)

        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _add(item.get("name"))
                else:
                    _add(item)
            return names

        text = _to_clean_string(value)
        if not text:
            return names

        parsed: Any = None
        try:
            parsed = json.loads(text)
        except Exception:
            try:
                parsed = ast.literal_eval(text)
            except Exception:
                parsed = None

        if isinstance(parsed, list):
            return _parse_people_names(parsed)

        for part in _split_people_text(text):
            _add(part)
        return names

    def _parse_people_entries(value: Any) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []

        def _append_from_dict(item: Dict[str, Any]) -> None:
            public_identifier = _to_clean_string(item.get("public_identifier"))
            name = _to_clean_string(
                item.get("name") or item.get("full_name") or public_identifier
            )
            if not name and not public_identifier:
                return
            entries.append(
                {
                    "name": name or public_identifier,
                    "company": _to_clean_string(item.get("company")),
                    "id": _to_clean_string(item.get("id")),
                    "public_identifier": public_identifier,
                }
            )

        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _append_from_dict(item)
                else:
                    name = _to_clean_string(item)
                    if name:
                        entries.append(
                            {
                                "name": name,
                                "company": None,
                                "id": None,
                                "public_identifier": name,
                            }
                        )
            return entries

        text = _to_clean_string(value)
        if not text:
            return entries

        parsed: Any = None
        try:
            parsed = json.loads(text)
        except Exception:
            try:
                parsed = ast.literal_eval(text)
            except Exception:
                parsed = None

        if isinstance(parsed, list):
            return _parse_people_entries(parsed)

        for part in _split_people_text(text):
            entries.append(
                {
                    "name": part,
                    "company": None,
                    "id": None,
                    "public_identifier": part,
                }
            )
        return entries

    def _fallback_review(payload: Any) -> Dict[str, Any]:
        data = _normalize_payload(payload)
        if not data:
            return {
                "summary": "",
            }

        assignment_type = _to_clean_string(data.get("assignment_type"))
        assignment_name = _to_clean_string(
            data.get("assignment_name")
            or data.get("assignment_title")
            or data.get("title")
            or data.get("name")
        )
        mandate = _to_clean_string(data.get("mandate"))
        outcome = _to_clean_string(data.get("outcome"))
        completion_time = _to_clean_string(data.get("completion_time"))
        core_problem = _to_clean_string(data.get("core_problem_solved"))
        reusable_proof_point = _to_clean_string(data.get("reusable_proof_point"))
        speed_advantage = _to_clean_string(data.get("speed_advantage"))
        win_reason = _to_clean_string(data.get("win_reason"))
        trust_factor = _to_clean_string(data.get("trust_factor"))
        primary_insight = _to_clean_string(data.get("primary_insight"))
        leadership_angle = _to_clean_string(data.get("leadership_angle"))
        reusable_content_angle = _to_clean_string(data.get("reusable_content_angle"))
        business_problem = _to_clean_string(data.get("business_problem"))
        leadership_need = _to_clean_string(data.get("leadership_need"))
        decision_context = _to_clean_string(data.get("decision_context"))
        future_trigger_relevance = _to_clean_string(
            data.get("future_trigger_relevance")
        )
        client_context = _to_clean_string(data.get("client_context"))
        industry = _to_clean_string(data.get("industry"))

        client_names = _parse_client_names(data.get("client"))
        people_names = _parse_people_names(data.get("people_involved"))

        what_parts: List[str] = []
        if assignment_name:
            what_parts.append(f"Completed {assignment_name}")
        if assignment_type:
            what_parts.append(f"an {assignment_type} assignment")
        if mandate:
            what_parts.append(f"focused on {mandate}")
        if core_problem:
            what_parts.append(f"to solve {core_problem}")
        if outcome:
            what_parts.append(f"with outcome: {outcome}")
        if completion_time:
            what_parts.append(f"in {completion_time}")
        what_was_completed = ". ".join(what_parts).strip()
        if what_was_completed and not what_was_completed.endswith("."):
            what_was_completed += "."

        why_parts = [
            p
            for p in [
                business_problem,
                leadership_need,
                decision_context,
                speed_advantage,
                reusable_proof_point,
                future_trigger_relevance,
            ]
            if _to_clean_string(p)
        ]
        why_it_mattered = "; ".join(why_parts)

        why_winnable = "; ".join(
            p
            for p in [win_reason, speed_advantage, trust_factor, reusable_proof_point]
            if _to_clean_string(p)
        )
        reusable_market_insight = "; ".join(
            p
            for p in [primary_insight, leadership_angle, reusable_content_angle]
            if _to_clean_string(p)
        )
        what_can_it_unlock_next = "; ".join(
            p
            for p in [
                future_trigger_relevance,
                reusable_content_angle,
                leadership_angle,
            ]
            if _to_clean_string(p)
        )
        additional_sections = [
            {"heading": heading, "content": content}
            for heading, content in [
                ("Why this was winnable", why_winnable),
                ("Reusable market insight", reusable_market_insight),
                ("What can it unlock next?", what_can_it_unlock_next),
            ]
            if _to_clean_string(content)
        ]

        # Build a richer 1–2 sentence description of the client and context.
        primary_client = ", ".join(client_names[:3]) if client_names else None
        org_descriptors: List[str] = []
        if industry:
            org_descriptors.append(f"{industry} company")
        # Detect simple PE-backed / ownership hints from context fields.
        ownership_hints = []
        for field in (
            client_context,
            business_problem,
            leadership_need,
            decision_context,
        ):
            text = _to_clean_string(field) or ""
            if "pe-backed" in text.lower() and "PE-backed" not in ownership_hints:
                ownership_hints.append("PE-backed")
        if ownership_hints:
            org_descriptors.insert(0, ", ".join(ownership_hints))
        org_descriptor = ", ".join(org_descriptors) if org_descriptors else None

        first_sentence_parts: List[str] = []
        if primary_client:
            if org_descriptor:
                first_sentence_parts.append(f"{primary_client}, a {org_descriptor}")
            else:
                first_sentence_parts.append(primary_client)
        elif client_context:
            first_sentence_parts.append(client_context)
        if not first_sentence_parts and industry:
            first_sentence_parts.append(f"A {industry} organization")
        first_sentence = (
            (". ".join(first_sentence_parts)).strip() if first_sentence_parts else ""
        )
        if first_sentence and not first_sentence.endswith("."):
            first_sentence += "."

        context_clauses: List[str] = []
        for field in (business_problem, leadership_need, decision_context):
            text = _to_clean_string(field)
            if text:
                context_clauses.append(text)
        context_sentence = " ".join(context_clauses).strip()
        if context_sentence and not context_sentence.endswith("."):
            context_sentence += "."

        who_was_it_for = " ".join(
            part for part in [first_sentence, context_sentence] if part
        ).strip()

        who_was_involved = ", ".join(people_names[:12])

        summary_parts = [
            p
            for p in [
                what_was_completed,
                why_it_mattered,
                *[section["content"] for section in additional_sections],
                who_was_it_for,
                f"Involved people: {who_was_involved}" if who_was_involved else "",
            ]
            if _to_clean_string(p)
        ]
        summary = " ".join(summary_parts).strip()

        return {
            "summary": summary,
        }

    async def _generate_person_impact(
        *,
        assignment_json: str,
        person_entry: Dict[str, Any],
        profile_context: str,
    ) -> Dict[str, Any]:
        prompt = ASSIGNMENT_PERSON_IMPACT_USER_PROMPT_TEMPLATE.format(
            assignment_json=assignment_json,
            person_json=json.dumps(person_entry, ensure_ascii=False, indent=2),
            profile_context=profile_context,
        )
        response = await invoke(
            model="openai/gpt-4.1",
            temperature=0.1,
            fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4o"],
            messages=[
                {"role": "system", "content": ASSIGNMENT_PERSON_IMPACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            parsed = {}
        actions = parsed.get("possible_actions")
        if not isinstance(actions, list):
            actions = []
        return {
            "why_this_person_matters": _to_clean_string(
                parsed.get("why_this_person_matters")
            )
            or "",
            "possible_actions": [
                action
                for action in (_to_clean_string(item) for item in actions)
                if action
            ],
        }

    async def _build_people_involved_details(
        *,
        assignment_data: Dict[str, Any],
        assignment_json: str,
    ) -> List[Dict[str, Any]]:
        people_entries = _parse_people_entries(assignment_data.get("people_involved"))
        people_entries.extend(
            _parse_people_entries(assignment_data.get("who_was_involved"))
        )
        people_entries = _dedupe_people_entries(people_entries)
        if not people_entries:
            return []

        public_identifiers = [
            public_identifier
            for public_identifier in (
                _to_clean_string(entry.get("public_identifier"))
                for entry in people_entries
            )
            if public_identifier
        ]
        profile_map: Dict[str, Tuple[Optional[str], Optional[Dict[str, Any]]]] = {}
        if es_client is not None and public_identifiers:
            try:
                profile_map = await fetch_profiles_by_public_identifiers(
                    public_identifiers=list(dict.fromkeys(public_identifiers)),
                    es_client=es_client,
                    source_fields=None,
                    only_top_experience=False,
                )
            except Exception:
                logger.exception(
                    "Failed to fetch people profiles for assignment review"
                )
                profile_map = {}

        def _profile_display_fields(
            profile: Optional[Dict[str, Any]],
        ) -> Dict[str, Optional[str]]:
            if not isinstance(profile, dict):
                return {"name": None, "headline": None, "title": None, "company": None}

            headline = _to_clean_string(profile.get("headline"))
            name = _to_clean_string(profile.get("full_name") or profile.get("name"))
            title = None
            company = None
            exp = profile.get("experience")
            if isinstance(exp, list):
                for item in exp:
                    if not isinstance(item, dict):
                        continue
                    title = _to_clean_string(item.get("title"))
                    company = _to_clean_string(
                        item.get("company") or item.get("company_name")
                    )
                    if title or company:
                        break

            return {
                "name": name,
                "headline": headline,
                "title": title,
                "company": company,
            }

        async def _build_one(entry: Dict[str, Any]) -> Dict[str, Any]:
            public_identifier = _to_clean_string(entry.get("public_identifier"))
            profile_es_id: Optional[str] = None
            profile: Optional[Dict[str, Any]] = None
            if public_identifier:
                profile_es_id, profile = profile_map.get(
                    public_identifier, (None, None)
                )

            display_fields = _profile_display_fields(profile)
            display_name = display_fields.get("name") or _to_clean_string(
                entry.get("name")
            )
            display_company = display_fields.get("company") or _to_clean_string(
                entry.get("company")
            )
            display_title = display_fields.get("title")
            display_headline = display_fields.get("headline")
            role_descriptor = display_headline
            if display_title and display_company:
                role_descriptor = f"{display_title} at {display_company}"
            elif display_title:
                role_descriptor = display_title

            person_payload = {
                "name": display_name or "",
                "company": display_company,
                "title": display_title,
                "headline": display_headline,
                "role_descriptor": role_descriptor,
                "id": _to_clean_string(entry.get("id")),
                "public_identifier": public_identifier,
                "profile_es_id": profile_es_id,
            }
            profile_context = _compact_profile_context(profile) if profile else "N/A"
            try:
                impact = await _generate_person_impact(
                    assignment_json=assignment_json,
                    person_entry=person_payload,
                    profile_context=profile_context,
                )
            except Exception:
                logger.exception(
                    "Failed to generate person impact for assignment review: %s",
                    person_payload.get("public_identifier")
                    or person_payload.get("name"),
                )
                impact = {"why_this_person_matters": "", "possible_actions": []}
            return {**person_payload, **impact}

        return await asyncio.gather(*[_build_one(entry) for entry in people_entries])

    async def _generate_opportunities(
        *,
        assignment_json: str,
        people_details: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return await _generate_assignment_opportunities(
            assignment_json=assignment_json,
            people_details=people_details,
        )

    async def _generate_feed_impact(
        *,
        assignment_json: str,
        people_details: List[Dict[str, Any]],
        opportunities: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        prompt = ASSIGNMENT_FEED_IMPACT_USER_PROMPT_TEMPLATE.format(
            assignment_json=assignment_json,
            people_details_json=json.dumps(
                people_details or [], ensure_ascii=False, indent=2
            ),
            opportunities_json=json.dumps(
                opportunities or [], ensure_ascii=False, indent=2
            ),
        )
        fallback = {
            "summary": "",
            "will_increase_priority_for": [],
            "muted_by_default": [],
            "trigger_rules_created": [],
        }
        try:
            response = await invoke(
                model="openai/gpt-4.1",
                temperature=0.1,
                fallbacks=["anthropic/claude-sonnet-4-5-20250929"],
                messages=[
                    {
                        "role": "system",
                        "content": ASSIGNMENT_FEED_IMPACT_SYSTEM_PROMPT,
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            parsed = _safe_json_loads_from_text(str(response))
            if not isinstance(parsed, dict):
                return fallback

            def _string_list(value: Any) -> List[str]:
                if not isinstance(value, list):
                    return []
                return [
                    item
                    for item in (_to_clean_string(entry) for entry in value)
                    if item
                ]

            def _trigger_rules(value: Any) -> List[Dict[str, str]]:
                if not isinstance(value, list):
                    return []
                rules: List[Dict[str, str]] = []
                seen = set()
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    title = _to_clean_string(
                        item.get("title") or item.get("name") or item.get("heading")
                    )
                    description = _to_clean_string(
                        item.get("description")
                        or item.get("rule")
                        or item.get("content")
                        or item.get("text")
                    )
                    if not title or not description:
                        continue
                    key = title.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    rules.append({"title": title, "description": description})
                    if len(rules) >= 6:
                        break
                return rules

            return {
                "summary": _to_clean_string(parsed.get("summary")) or "",
                "will_increase_priority_for": _string_list(
                    parsed.get("will_increase_priority_for")
                ),
                "muted_by_default": _string_list(parsed.get("muted_by_default")),
                "trigger_rules_created": _trigger_rules(
                    parsed.get("trigger_rules_created")
                ),
            }
        except Exception:
            logger.exception("Failed to generate assignment feed impact")
            return fallback

    def _human_join(items: List[str]) -> str:
        cleaned = [item for item in (_to_clean_string(i) for i in items) if item]
        if not cleaned:
            return ""
        if len(cleaned) == 1:
            return cleaned[0]
        if len(cleaned) == 2:
            return f"{cleaned[0]} and {cleaned[1]}"
        return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"

    def _format_who_was_involved(
        *,
        people_details: List[Dict[str, Any]],
        fallback: Optional[str],
    ) -> str:
        descriptors: List[str] = []
        seen = set()
        for person in people_details:
            if not isinstance(person, dict):
                continue
            public_identifier = _to_clean_string(person.get("public_identifier"))
            descriptor = _to_clean_string(person.get("role_descriptor"))
            name = _to_clean_string(person.get("name"))

            # Prefer role/title/company context from ES over raw public identifiers.
            value = descriptor
            if not value or value == public_identifier:
                value = name if name and name != public_identifier else None
            if not value:
                continue

            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            descriptors.append(value)

        joined = _human_join(descriptors[:12])
        if joined:
            return f"The assignment involved {joined}."
        return _to_clean_string(fallback) or ""

    def _parse_additional_sections(value: Any) -> List[Dict[str, str]]:
        if not isinstance(value, list):
            return []

        sections: List[Dict[str, str]] = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            heading = _to_clean_string(
                item.get("heading") or item.get("title") or item.get("label")
            )
            content = _to_clean_string(
                item.get("content") or item.get("body") or item.get("text")
            )
            if not heading or not content:
                continue
            key = heading.lower()
            if key in seen:
                continue
            seen.add(key)
            sections.append({"heading": heading, "content": content})
            if len(sections) >= 4:
                break

        return sections

    assignment_data = _normalize_payload(raw_payload)
    raw_json = json.dumps(raw_payload, ensure_ascii=False, indent=2, default=str)

    # Kick off lookalike companies fetch in the background immediately so it
    # runs in parallel with the LLM review call below.
    lookalike_companies_task = asyncio.create_task(
        fetch_lookalike_companies_via_comp_gen(
            assignment_data=assignment_data,
            es_client=es_client,
        )
    )

    prompt = ASSIGNMENT_REVIEW_USER_PROMPT_TEMPLATE.format(raw_json=raw_json)
    try:
        response = await invoke(
            model="openai/gpt-4.1",
            temperature=0.1,
            fallbacks=["anthropic/claude-sonnet-4-5-20250929"],
            messages=[
                {"role": "system", "content": ASSIGNMENT_REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            parsed = {}

        def _get_value(key: str) -> str:
            return _to_clean_string(parsed.get(key)) or ""

        result: Dict[str, Any] = {
            "summary": _get_value("summary"),
        }
    except Exception:
        logger.exception(
            "Assignment review LLM invocation failed; using deterministic fallback"
        )
        result = _fallback_review(raw_payload)

    people_involved_details = await _build_people_involved_details(
        assignment_data=assignment_data,
        assignment_json=raw_json,
    )
    result["people_involved_details"] = people_involved_details
    if people_involved_details:
        result["who_was_involved"] = _format_who_was_involved(
            people_details=people_involved_details,
            fallback=result.get("who_was_involved"),
        )
    result["opportunities"] = prepare_review_opportunities(
        await _generate_opportunities(
            assignment_json=raw_json,
            people_details=people_involved_details,
        ),
        assignment_data=assignment_data,
    )
    result["feed_impact"] = await _generate_feed_impact(
        assignment_json=raw_json,
        people_details=people_involved_details,
        opportunities=result["opportunities"],
    )
    feed_impact = result.get("feed_impact") or {}
    result["summary_chips"] = build_review_summary_chips(
        opportunities=result.get("opportunities") or [],
        feed_impact=feed_impact,
    )
    return result


def _parse_assignment_draft(raw_draft: Any) -> Dict[str, Any]:
    if isinstance(raw_draft, dict):
        return raw_draft
    if isinstance(raw_draft, str):
        try:
            parsed = json.loads(raw_draft)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


async def build_lookalike_companies_opportunity(
    assignment_id: str,
    *,
    es_client: Optional[AsyncElasticsearch] = None,
) -> Dict[str, Any]:
    """
    Load assignment by id and return lookalike card: type, title, action, companies.
    Returns an error dict with found=False when assignment is missing.
    """
    assignment_id = (assignment_id or "").strip()
    if not assignment_id:
        return {"found": False, "error": "assignment_id is required"}

    context = await fetch_assignment_context_by_assignment_id(assignment_id)
    if not context:
        logger.warning(
            "[lookalike] no assignment found for assignment_id=%s", assignment_id
        )
        return {
            "assignment_id": assignment_id,
            "found": False,
            "error": "Assignment not found",
        }

    assignment_data = _parse_assignment_draft(context.get("assignment_draft"))
    assignment_json = json.dumps(
        assignment_data, ensure_ascii=False, indent=2, default=str
    )

    lookalike_companies_task = asyncio.create_task(
        fetch_lookalike_companies_via_comp_gen(
            assignment_data=assignment_data,
            es_client=es_client,
        )
    )
    card_copy_task = asyncio.create_task(
        _generate_lookalike_card_copy(assignment_json=assignment_json)
    )

    card_copy, companies = await asyncio.gather(
        card_copy_task,
        lookalike_companies_task,
    )
    if not card_copy:
        card_copy = build_default_lookalike_opportunity(assignment_data)

    return {
        "type": LOOKALIKE_OPPORTUNITY_TYPE,
        "title": card_copy.get("title") or "",
        "action": card_copy.get("action") or "",
        "companies": companies or [],
    }


def _to_clean_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _session_metadata(session: Dict[str, Any]) -> Dict[str, Any]:
    raw = session.get("metadata") if isinstance(session, dict) else None
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _active_assignment_feature_key(
    *,
    session: Dict[str, Any],
    base_feature_key: str,
) -> str:
    metadata = _session_metadata(session)
    active = _to_clean_string(metadata.get("active_assignment_feature_key"))
    if active and (
        active == base_feature_key or active.startswith(f"{base_feature_key}:")
    ):
        return active
    return base_feature_key


async def _set_active_assignment_feature_key(
    *,
    session: Dict[str, Any],
    assignment_feature_key: str,
) -> None:
    metadata = _session_metadata(session)
    metadata["active_assignment_feature_key"] = assignment_feature_key
    await update_chat_session_metadata(
        session_id=str(session.get("id")),
        metadata=metadata,
    )


def _count_prior_blocked_save_turns(
    recent_messages: List[Dict[str, Any]],
    *,
    assignment_feature_key: str,
) -> int:
    """How many prior assistant turns blocked a save request for this assignment."""
    count = 0
    for msg in recent_messages or []:
        if str(msg.get("role") or "").lower() != "assistant":
            continue
        meta = msg.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            continue
        if (
            _to_clean_string(meta.get("assignment_feature_key"))
            != assignment_feature_key
        ):
            continue
        if meta.get("save_requested") and not meta.get("ready_to_save"):
            count += 1
    return count


def _load_prior_save_blocked_count(
    *,
    session: Dict[str, Any],
    recent_messages: List[Dict[str, Any]],
    assignment_feature_key: str,
) -> int:
    metadata = _session_metadata(session)
    all_state = metadata.get("assignment_save_state")
    if isinstance(all_state, dict):
        state = all_state.get(assignment_feature_key)
        if isinstance(state, dict) and state.get("blocked_count") is not None:
            return int(state.get("blocked_count") or 0)
    return _count_prior_blocked_save_turns(
        recent_messages,
        assignment_feature_key=assignment_feature_key,
    )


async def _persist_save_retry_state(
    *,
    session: Dict[str, Any],
    assignment_feature_key: str,
    save_requested: bool,
    ready_to_save: bool,
) -> None:
    metadata = _session_metadata(session)
    all_state = metadata.get("assignment_save_state")
    if not isinstance(all_state, dict):
        all_state = {}
    state = dict(all_state.get(assignment_feature_key) or {})
    blocked = int(state.get("blocked_count") or 0)
    if save_requested and not ready_to_save:
        state["blocked_count"] = blocked + 1
    elif ready_to_save:
        state["blocked_count"] = 0
    all_state[assignment_feature_key] = state
    metadata["assignment_save_state"] = all_state
    await update_chat_session_metadata(
        session_id=str(session.get("id")),
        metadata=metadata,
    )


def _resolve_save_turn_outcome(
    *,
    draft: Dict[str, Any],
    save_decision: Dict[str, Any],
    prior_blocked_save_count: int,
) -> Dict[str, Any]:
    """
    Apply save retry policy: after one blocked save, repeat save requests auto-waive
    the one remaining skippable gap (never People Involved; at most one field blank).
    """
    save_requested = bool(save_decision.get("should_save"))
    explicit_skip_remaining = bool(save_decision.get("explicit_skip_remaining"))
    auto_waived_on_retry = bool(
        save_requested and prior_blocked_save_count >= 1 and not explicit_skip_remaining
    )
    effective_explicit_skip = explicit_skip_remaining or auto_waived_on_retry
    ready_to_save, missing_labels, waived_labels = _resolve_ready_to_save(
        draft=draft,
        save_requested=save_requested,
        explicit_skip_remaining=effective_explicit_skip,
        prior_blocked_save_count=prior_blocked_save_count,
    )
    return {
        "save_requested": save_requested,
        "explicit_skip_remaining": explicit_skip_remaining,
        "effective_explicit_skip": effective_explicit_skip,
        "auto_waived_on_retry": auto_waived_on_retry,
        "ready_to_save": ready_to_save,
        "missing_labels": missing_labels,
        "waived_labels": waived_labels,
        "prior_blocked_save_count": prior_blocked_save_count,
    }


def _new_assignment_feature_key(base_feature_key: str) -> str:
    return f"{base_feature_key}:{uuid.uuid4().hex}"


def _empty_assignment_draft() -> Dict[str, Optional[str]]:
    return {k: None for k in ASSIGNMENT_FIELD_KEYS}


def _empty_content_draft() -> Dict[str, Optional[str]]:
    return {k: None for k in CONTENT_FIELD_KEYS}


def _merge_assignment_draft(
    *,
    existing: Dict[str, Any],
    extracted: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    # Keep canonical keys, but allow dynamic keys to evolve with conversation.
    merged = {k: v for k, v in _empty_assignment_draft().items()}

    for key, value in (existing or {}).items():
        key_str = _to_clean_string(key)
        if not key_str:
            continue
        merged[key_str] = _to_clean_string(value)

    for key, value in (extracted or {}).items():
        key_str = _to_clean_string(key)
        if not key_str:
            continue

        # When the model explicitly includes a key with an empty/None value,
        # interpret that as an instruction to CLEAR that field.
        if value is None or (isinstance(value, str) and value.strip() == ""):
            merged[key_str] = None
            continue

        cleaned_val = _to_clean_string(value)
        if cleaned_val:
            merged[key_str] = cleaned_val

    # Preserve original completion behavior for the canonical key set only.
    for key in ASSIGNMENT_FIELD_KEYS:
        merged.setdefault(key, None)

    return merged


def _has_required_assignment_field(draft: Dict[str, Any], key: str) -> bool:
    if key == "people_involved":
        entries = _normalize_people_involved_entries(
            _to_clean_string(draft.get("people_involved"))
        )
        return any(
            _to_clean_string(entry.get("id"))
            and _to_clean_string(entry.get("public_identifier"))
            for entry in entries
            if isinstance(entry, dict)
        )
    return bool(_to_clean_string(draft.get(key)))


def _missing_field_labels(draft: Dict[str, Any]) -> List[str]:
    """Returns labels of REQUIRED fields not yet filled. Empty list means ready to save."""
    missing = []
    for key in REQUIRED_ASSIGNMENT_FIELD_KEYS:
        if not _has_required_assignment_field(draft, key):
            missing.append(REQUIRED_ASSIGNMENT_FIELD_LABELS[key])
    return missing


def _completion_score_content(draft: Dict[str, Any]) -> float:
    total_fields = len(CONTENT_FIELD_KEYS)
    if not total_fields:
        return 0.0
    filled = sum(
        1 for k in CONTENT_FIELD_KEYS if _to_clean_string(draft.get(k))
    )
    return float(filled) / float(total_fields)


def _completion_score(draft: Dict[str, Any]) -> float:
    """Completion score based on required fields only (0.0–1.0)."""
    total = len(REQUIRED_ASSIGNMENT_FIELD_KEYS)
    present = sum(
        1
        for k in REQUIRED_ASSIGNMENT_FIELD_KEYS
        if _has_required_assignment_field(draft, k)
    )
    return float(present / total) if total else 0.0


def _people_verification_failures(
    *,
    people_outcome_requested_names: Optional[List[str]] = None,
    after_people_involved: Optional[str] = None,
) -> List[str]:
    """Names sent to person mapping this turn that were not verified into people_involved."""
    if not people_outcome_requested_names:
        return []
    _, failed_names = _split_people_add_outcome_names(
        requested_names=people_outcome_requested_names,
        after_people_involved=after_people_involved,
    )
    return failed_names


def _should_show_add_people_cta(missing_labels: List[str]) -> bool:
    labels = missing_labels or []
    return (
        len(labels) <= 3
        and REQUIRED_ASSIGNMENT_FIELD_LABELS["people_involved"] in labels
    )


def _should_show_add_people_cta_after_mapping_failure(
    *,
    people_outcome_requested_names: Optional[List[str]] = None,
    after_people_involved: Optional[str] = None,
) -> bool:
    """Stream/chat only: show CTA when person mapping was attempted and any name failed."""
    if not people_outcome_requested_names:
        return False
    return bool(
        _people_verification_failures(
            people_outcome_requested_names=people_outcome_requested_names,
            after_people_involved=after_people_involved,
        )
    )


def _has_prior_user_turn(messages: List[Dict[str, Any]]) -> bool:
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = _to_clean_string(msg.get("role"))
        if role and role.lower() == "user":
            return True
    return False


def _has_minimum_assignment_signal(draft: Dict[str, Any]) -> bool:
    """
    Guardrail for early-save flow: allow save-with-missing-fields only when
    there is at least some meaningful assignment content in the draft.
    """
    if not isinstance(draft, dict):
        return False
    signal_keys = {
        "assignment_name",
        "assignment_type",
        "client",
        "mandate",
        "outcome",
        "business_problem",
        "leadership_need",
        "win_reason",
        "primary_insight",
    }
    return any(_to_clean_string(draft.get(key)) for key in signal_keys)


_MAX_SKIPPABLE_MISSING_FIELDS_ON_EXPLICIT_SAVE = 1

# When the user insists on save, waive less-critical gaps first (never people_involved).
_WAIVE_ON_INSIST_FIELD_PREFERENCE = [
    "primary_insight",
    "win_reason",
    "business_problem",
    "leadership_need",
    "outcome",
    "assignment_type",
    "client",
]


def _pick_label_to_waive_on_insist(missing_labels: List[str]) -> Optional[str]:
    label_by_key = REQUIRED_ASSIGNMENT_FIELD_LABELS
    for key in _WAIVE_ON_INSIST_FIELD_PREFERENCE:
        label = label_by_key.get(key)
        if label and label in missing_labels:
            return label
    return missing_labels[0] if missing_labels else None


def _waived_labels_on_insistent_save(
    *,
    missing_labels: List[str],
    prior_blocked_save_count: int,
    save_requested: bool,
    explicit_skip_remaining: bool,
) -> List[str]:
    """After a prior blocked save, waive extra gaps so at most one blank remains."""
    people_label = REQUIRED_ASSIGNMENT_FIELD_LABELS["people_involved"]
    if not save_requested or not explicit_skip_remaining:
        return []
    if people_label in missing_labels:
        return []
    if prior_blocked_save_count < 1:
        return []
    skippable = [label for label in missing_labels if label != people_label]
    waived: List[str] = []
    remaining = list(skippable)
    while len(remaining) > _MAX_SKIPPABLE_MISSING_FIELDS_ON_EXPLICIT_SAVE:
        pick = _pick_label_to_waive_on_insist(remaining)
        if not pick:
            break
        waived.append(pick)
        remaining = [label for label in remaining if label != pick]
    return waived


def _resolve_ready_to_save(
    *,
    draft: Dict[str, Any],
    save_requested: bool,
    explicit_skip_remaining: bool,
    prior_blocked_save_count: int = 0,
) -> Tuple[bool, List[str], List[str]]:
    """
    ready_to_save is true when all required fields are present, OR when the user
    explicitly asked to save and skip on a draft with meaningful content — at most
    one required field may remain blank (People Involved is never waivable).
    """
    missing_labels = _missing_field_labels(draft)
    if not missing_labels:
        return True, missing_labels, []
    people_label = REQUIRED_ASSIGNMENT_FIELD_LABELS["people_involved"]
    if people_label in missing_labels:
        return False, missing_labels, []

    waived_labels = _waived_labels_on_insistent_save(
        missing_labels=missing_labels,
        prior_blocked_save_count=prior_blocked_save_count,
        save_requested=save_requested,
        explicit_skip_remaining=explicit_skip_remaining,
    )
    skippable = [label for label in missing_labels if label != people_label]
    effective_missing = [label for label in skippable if label not in waived_labels]

    if (
        save_requested
        and explicit_skip_remaining
        and _has_minimum_assignment_signal(draft)
        and len(effective_missing) <= _MAX_SKIPPABLE_MISSING_FIELDS_ON_EXPLICIT_SAVE
    ):
        return True, missing_labels, waived_labels
    return False, missing_labels, waived_labels


def _should_emit_stream_interim_ack(
    *,
    save_requested: bool,
    explicit_skip_remaining: bool,
    suppress_stream_deltas: bool,
    should_fetch_people: bool,
) -> bool:
    """Fast ack is only useful on slow people-mapping turns, not save/confirm turns."""
    if save_requested or explicit_skip_remaining:
        return False
    return bool(suppress_stream_deltas or should_fetch_people)


def _compact_profile_context(profile: Optional[Dict[str, Any]]) -> str:
    if not isinstance(profile, dict):
        return "N/A"

    lines: List[str] = []
    name = profile.get("full_name") or profile.get("name") or ""
    headline = profile.get("headline") or ""
    summary = profile.get("summary") or ""
    if name:
        lines.append(f"Name: {name}")
    if headline:
        lines.append(f"Headline: {headline}")
    if summary:
        lines.append(f"Summary: {summary}")

    exp = profile.get("experience")
    if isinstance(exp, list) and exp:
        lines.append("Recent Experience:")
        for e in exp[:2]:
            if not isinstance(e, dict):
                continue
            title = e.get("title") or ""
            company = e.get("company") or e.get("company_name") or ""
            if title or company:
                lines.append(f"- {title} @ {company}".strip())

    return "\n".join(lines)[:2500] if lines else "N/A"


def _derive_first_name(*, profile: Optional[Dict[str, Any]], entity_urn: str) -> str:
    if isinstance(profile, dict):
        full_name = _to_clean_string(profile.get("full_name") or profile.get("name"))
        if full_name:
            parts = full_name.split()
            if parts:
                return parts[0]

    fallback = _to_clean_string(entity_urn) or "there"
    # Handle slug-like names: david-jones -> David
    first_token = fallback.split("-")[0].split("_")[0].split()[0]
    return first_token.capitalize() if first_token else "there"


def _build_content_initial_greeting_fallback(*, first_name: str) -> str:
    return (
        f"Hi {first_name}, let's turn your content into a relationship trigger.\n\n"
        "Paste a link to a podcast, YouTube video, article, newsletter, or any other "
        "content you want Triggers to use. I'll read the source, detect the themes, "
        "identify who in your network may care, and help you turn it into personalized outreach.\n\n"
        "What would you like to add?"
    )


def _build_content_welcome_back_greeting(
    *,
    first_name: str,
    content_draft: Dict[str, Any],
) -> str:
    content_title = _to_clean_string(content_draft.get("title")) or "this content"
    return (
        f"Hi {first_name}, welcome back. You're continuing work on {content_title}.\n\n"
        "Paste another link or tell me what you'd like to update, and I'll refresh "
        "the content preview and triggers."
    )


async def _generate_initial_greeting(
    *,
    entity_urn: str,
    first_name: str,
    profile_context: str,
    chat_type: str = "assignment",
) -> str:
    is_content = chat_type.lower() == "content"
    if is_content:
        return _build_content_initial_greeting_fallback(first_name=first_name)

    system_prompt = ASSIGNMENT_GREETING_SYSTEM_PROMPT
    prompt = ASSIGNMENT_GREETING_USER_PROMPT_TEMPLATE.format(
        entity_urn=entity_urn,
        first_name=first_name,
        profile_context=profile_context,
    )
    response = await invoke(
        model="openai/gpt-4.1",
        temperature=0.2,
        fallbacks=["openai/gpt-4o", "anthropic/claude-sonnet-4-5-20250929"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    )
    text = _to_clean_string(response)
    if text:
        return text
    return (
        f"Hi {first_name}!\n\n"
        "Let's turn your completed assignment into future opportunities. "
        "I'll ask a few questions about the work you completed, who it involved, why it mattered, "
        "and what it could unlock next.\n\n"
        "To start, what assignment did you complete? You can describe the type of work, "
        "the client problem, and the outcome."
    )


async def fetch_profile_by_entity_urn(
    *,
    entity_urn: str,
    es_client: AsyncElasticsearch,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    body = {
        "query": {
            "bool": {
                "should": [
                    {"term": {"entity_urn.keyword": entity_urn}},
                    {"term": {"entity_urn": entity_urn}},
                    {"term": {"entityUrn.keyword": entity_urn}},
                    {"term": {"entityUrn": entity_urn}},
                ],
                "minimum_should_match": 1,
            }
        },
        "size": 1,
    }
    # Use explicit index env lookup here to keep utility independent.
    from os import getenv

    index = (getenv("ES_PROFILES_INDEX") or "").strip()
    if not index:
        raise RuntimeError("ES_PROFILES_INDEX env var is not set")

    res = await es_client.search(index=index, body=body)
    hits = res.get("hits", {}).get("hits", []) or []
    if not hits:
        return None, None
    hit = hits[0]
    src = hit.get("_source")
    return hit.get("_id"), src if isinstance(src, dict) else None


def _format_chat_history(messages: List[Dict[str, Any]]) -> str:
    if not messages:
        return "N/A"
    lines: List[str] = []
    for m in messages[-20:]:
        role = str(m.get("role") or "unknown").strip()
        content = str(m.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)[:6000] if lines else "N/A"


def _profile_suggestion_request_from_llm(parsed: Dict[str, Any]) -> Dict[str, Any]:
    req = parsed.get("profile_suggestion_request") or {}
    if not isinstance(req, dict):
        return {
            "should_fetch": False,
            "names": [],
            "titles": [],
            "companies": [],
            "timeline": "current",
        }

    def _as_str_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for item in value:
            s = _to_clean_string(item)
            if s:
                out.append(s)
        return out

    return {
        "should_fetch": bool(req.get("should_fetch")),
        "names": _as_str_list(req.get("names")),
        "titles": _as_str_list(req.get("titles")),
        "companies": _as_str_list(req.get("companies")),
        "timeline": _to_clean_string(req.get("timeline")) or "current",
    }


def _build_profile_payload_for_person(
    *,
    name: str,
    title: Optional[str],
    company: Optional[str],
    timeline: str = "current",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"name": name}
    if title:
        payload["title"] = title
    if company:
        payload["company"] = company
    return payload


def _map_person_api_headers() -> Dict[str, str]:
    raw_api_key = (os.getenv("FAST_COMP_API_KEY") or "").strip()
    api_key = raw_api_key
    if raw_api_key.lower().startswith("bearer "):
        api_key = raw_api_key.split(" ", 1)[1].strip()

    headers = {"Content-Type": "application/json", "accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


_MAP_PERSON_RESPONSE_META_KEYS = frozenset(
    {"profile", "results", "suggestions", "items", "error", "message", "status"}
)


def _extract_compact_map_person_profile(
    response: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Compact map_person shape: {public_identifier: es_id}."""
    if len(response) != 1:
        return None
    pid_key, es_id_value = next(iter(response.items()))
    pid = _to_clean_string(pid_key)
    es_id = _to_clean_string(es_id_value)
    if not pid or not es_id:
        return None
    if pid.lower() in _MAP_PERSON_RESPONSE_META_KEYS:
        return None
    return {"public_identifier": pid, "id": es_id}


def _normalize_map_person_profile(response: Any) -> Optional[Dict[str, Any]]:
    if isinstance(response, list) and response:
        first = response[0]
        return first if isinstance(first, dict) else None
    if not isinstance(response, dict) or not response:
        return None

    profile = response.get("profile")
    if isinstance(profile, dict) and profile:
        return dict(profile)

    for key in ("results", "suggestions", "items"):
        value = response.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                return first

    compact = _extract_compact_map_person_profile(response)
    if compact:
        return compact

    if _to_clean_string(response.get("public_identifier")):
        return response

    return None


def _has_profile_suggestion_results(response: Any) -> bool:
    return _normalize_map_person_profile(response) is not None


def _normalize_people_involved_entries(
    people_involved: Optional[str],
) -> List[Dict[str, Any]]:
    raw = _to_clean_string(people_involved)
    if not raw:
        return []

    def _is_valid_person_name(name: str) -> bool:
        n = name.strip()
        if not n:
            return False
        # Reject serialized/json-like blobs mistakenly passed as names.
        if any(ch in n for ch in ["{", "}", "[", "]", '"', ":"]):
            return False
        if not re.search(r"[A-Za-z]", n):
            return False
        return True

    def _name_candidates(value: Any) -> List[str]:
        if isinstance(value, list):
            out: List[str] = []
            for v in value:
                if not isinstance(v, str):
                    continue
                s = _to_clean_string(v)
                if s and _is_valid_person_name(s):
                    out.append(s)
            return out
        text = _to_clean_string(value)
        if not text:
            return []
        # Handle list-literal strings like "['Muhammad Talha', 'Muhammad Yousuf']".
        if text.startswith("[") and text.endswith("]"):
            parsed_list: Any = None
            try:
                parsed_list = json.loads(text)
            except Exception:
                try:
                    parsed_list = ast.literal_eval(text)
                except Exception:
                    parsed_list = None
            if isinstance(parsed_list, list):
                out: List[str] = []
                for v in parsed_list:
                    if not isinstance(v, str):
                        continue
                    s = _to_clean_string(v)
                    if s and _is_valid_person_name(s):
                        out.append(s)
                return out
        return [text] if _is_valid_person_name(text) else []

    def _normalized_entries(item: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for name in _name_candidates(item.get("name")):
            entries.append(
                {
                    "name": name,
                    "company": _to_clean_string(item.get("company")),
                    "id": _to_clean_string(item.get("id")),
                    "public_identifier": _to_clean_string(
                        item.get("public_identifier")
                    ),
                }
            )
        return entries

    def _from_serialized_list() -> List[Dict[str, Any]]:
        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception:
            try:
                parsed = ast.literal_eval(raw)
            except Exception:
                return []
        if not isinstance(parsed, list):
            return []

        entries: List[Dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            entries.extend(_normalized_entries(item))
        return entries

    parsed_entries = _from_serialized_list()
    if parsed_entries:
        return parsed_entries

    # Backward-compat parser for older semicolon string format.
    entries: List[Dict[str, Any]] = []
    chunks = [part.strip() for part in raw.split(";") if part.strip()]
    for chunk in chunks:
        enriched_match = re.match(
            r'^(?P<name>.+?)\s+\[(?P<id>[^\]]+)\](?:\s+\{"public_identifier":"(?P<public_identifier>[^"]+)"\})?$',
            chunk,
        )
        if enriched_match:
            entries.append(
                {
                    "name": _to_clean_string(enriched_match.group("name")),
                    "company": None,
                    "id": _to_clean_string(enriched_match.group("id")),
                    "public_identifier": _to_clean_string(
                        enriched_match.group("public_identifier")
                    ),
                }
            )
            continue

        company_match = re.match(r"^(?P<name>.+?)\s+\((?P<company>.+?)\)$", chunk)
        if company_match:
            entries.append(
                {
                    "name": _to_clean_string(company_match.group("name")),
                    "company": _to_clean_string(company_match.group("company")),
                    "id": None,
                    "public_identifier": None,
                }
            )
            continue

        entries.append(
            {
                "name": chunk,
                "company": None,
                "id": None,
                "public_identifier": None,
            }
        )
    return entries


def _dedupe_people_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_public_identifier: Dict[str, Dict[str, Any]] = {}
    by_name_company: Dict[Tuple[str, str], Dict[str, Any]] = {}
    ordered_keys: List[Tuple[str, str]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _to_clean_string(entry.get("name"))
        if not name:
            continue
        normalized_entry = {
            "name": name,
            "company": _to_clean_string(entry.get("company")),
            "id": _to_clean_string(entry.get("id")),
            "public_identifier": _to_clean_string(entry.get("public_identifier")),
        }
        public_identifier = normalized_entry["public_identifier"]
        key = (
            normalized_entry["name"].lower(),
            (normalized_entry["company"] or "").lower(),
        )
        if key not in by_name_company:
            ordered_keys.append(key)

        if public_identifier:
            if public_identifier in by_public_identifier:
                continue
            by_public_identifier[public_identifier] = normalized_entry
            existing = by_name_company.get(key)
            # Prefer enriched profile entries over plain entries for same name/company.
            if existing is None or not _to_clean_string(
                existing.get("public_identifier")
            ):
                by_name_company[key] = normalized_entry
            continue

        if key not in by_name_company:
            by_name_company[key] = normalized_entry

    deduped: List[Dict[str, Any]] = []
    seen_public_identifiers = set()
    for key in ordered_keys:
        entry = by_name_company.get(key)
        if not entry:
            continue
        public_identifier = _to_clean_string(entry.get("public_identifier"))
        if public_identifier:
            if public_identifier in seen_public_identifiers:
                continue
            seen_public_identifiers.add(public_identifier)
        deduped.append(entry)
    return deduped


def _merge_people_involved_values(
    *,
    existing_value: Optional[str],
    latest_value: Optional[str],
) -> Optional[str]:
    merged_entries = _normalize_people_involved_entries(existing_value)
    merged_entries.extend(_normalize_people_involved_entries(latest_value))
    deduped = _dedupe_people_entries(merged_entries)
    if not deduped:
        return None
    return json.dumps(deduped, ensure_ascii=False)


def _has_valid_people_update(latest_value: Optional[str]) -> bool:
    return len(_normalize_people_involved_entries(latest_value)) > 0


def _unique_people_names(entries: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _to_clean_string(entry.get("name"))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _finalize_people_fields(draft: Dict[str, Any]) -> None:
    before_entries = _summarize_people_entries_for_trace(
        _to_clean_string(draft.get("people_involved"))
    )
    entries = _normalize_people_involved_entries(
        _to_clean_string(draft.get("people_involved"))
    )
    if not entries:
        draft["people_involved"] = None
        draft["people"] = None
        _people_trace_log(
            "finalize_people_fields cleared",
            before_entries=before_entries,
            after_entries=[],
            reason="no_entries",
        )
        return

    verified = [
        entry
        for entry in entries
        if _to_clean_string(entry.get("id"))
        and _to_clean_string(entry.get("public_identifier"))
    ]
    if verified:
        draft["people_involved"] = json.dumps(
            _dedupe_people_entries(verified), ensure_ascii=False
        )
        draft["people"] = None
        _people_trace_log(
            "finalize_people_fields kept verified",
            before_entries=before_entries,
            after_entries=_summarize_people_entries_for_trace(
                draft.get("people_involved")
            ),
            dropped_unverified_count=len(entries) - len(verified),
        )
        return

    # No fallback: people_involved must contain verified profiles only.
    draft["people_involved"] = None
    draft["people"] = None
    _people_trace_log(
        "finalize_people_fields cleared",
        before_entries=before_entries,
        after_entries=[],
        reason="no_verified_entries",
        unverified_entries=_summarize_people_entries_for_trace(
            json.dumps(entries, ensure_ascii=False)
        ),
    )


def _profile_company_from_top_experience(
    profile: Optional[Dict[str, Any]],
) -> Optional[str]:
    if not isinstance(profile, dict):
        return None
    exp = profile.get("experience")
    if not isinstance(exp, list):
        return None
    for item in exp:
        if not isinstance(item, dict):
            continue
        company = _to_clean_string(item.get("company") or item.get("company_name"))
        if company:
            return company
    return None


def _build_people_involved_entry_from_selection(
    *,
    public_identifier: str,
    selected_person: Optional[Dict[str, Any]],
    es_id: Optional[str],
    profile: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    selected_person = selected_person if isinstance(selected_person, dict) else {}
    profile = profile if isinstance(profile, dict) else {}

    pid = (
        _to_clean_string(selected_person.get("public_identifier"))
        or _to_clean_string(public_identifier)
        or _to_clean_string(profile.get("public_identifier"))
    )
    profile_id = (
        _to_clean_string(selected_person.get("id"))
        or _to_clean_string(selected_person.get("es_id"))
        or _to_clean_string(profile.get("id"))
        or _to_clean_string(es_id)
    )
    name = (
        _to_clean_string(selected_person.get("name"))
        or _to_clean_string(selected_person.get("full_name"))
        or _to_clean_string(profile.get("full_name") or profile.get("name"))
        or pid
    )
    company = _to_clean_string(
        selected_person.get("company") or selected_person.get("company_name")
    ) or _profile_company_from_top_experience(profile)

    if not pid or not profile_id or not name:
        _people_trace_log(
            "build_people_entry skipped",
            reason="missing_required_fields",
            pid=pid,
            profile_id=profile_id,
            name=name,
            es_id=es_id,
            selected_person_keys=(
                sorted(selected_person.keys())
                if isinstance(selected_person, dict)
                else []
            ),
            profile_keys=sorted(profile.keys()) if isinstance(profile, dict) else [],
        )
        return None

    entry = {
        "name": name,
        "company": company,
        "id": profile_id,
        "public_identifier": pid,
    }
    _people_trace_log("build_people_entry ok", entry=entry)
    return entry


def _format_people_names_for_chat(entries: List[Dict[str, Any]]) -> str:
    labels: List[str] = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = _to_clean_string(entry.get("name"))
        if not name:
            continue
        company = _to_clean_string(entry.get("company"))
        label = f"{name} ({company})" if company else name
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return ", ".join(labels)


async def add_people_involved_to_assignment(
    *,
    assignment_id: str,
    people: Optional[List[Dict[str, Any]]] = None,
    public_identifiers: Optional[List[str]] = None,
    es_client: AsyncElasticsearch,
) -> Dict[str, Any]:
    assignment_id = _to_clean_string(assignment_id)
    if not assignment_id:
        raise ValueError("assignment_id is required")

    selected_people = [p for p in (people or []) if isinstance(p, dict)]
    selected_by_pid: Dict[str, Dict[str, Any]] = {}
    ordered_public_identifiers: List[str] = []
    seen = set[Any]()

    def _add_pid(
        pid_value: Any,
        selected_person: Optional[Dict[str, Any]] = None,
    ) -> None:
        pid = _to_clean_string(pid_value)
        if not pid:
            return
        key = pid.lower()
        if key not in seen:
            seen.add(key)
            ordered_public_identifiers.append(pid)
        if selected_person:
            selected_by_pid[key] = selected_person

    for person in selected_people:
        _add_pid(person.get("public_identifier"), person)

    for pid in public_identifiers or []:
        _add_pid(pid)

    if not ordered_public_identifiers:
        raise ValueError("At least one selected person public_identifier is required")

    existing_context = await fetch_assignment_context_by_assignment_id(assignment_id)
    if not existing_context:
        return {
            "found": False,
            "error": "Assignment not found",
            "assignment_id": assignment_id,
        }

    profile_map: Dict[str, Tuple[Optional[str], Optional[Dict[str, Any]]]] = {}
    try:
        profile_map = await fetch_profiles_by_public_identifiers(
            public_identifiers=ordered_public_identifiers,
            es_client=es_client,
            source_fields=None,
            only_top_experience=True,
        )
    except Exception:
        logger.exception(
            "Failed to fetch selected people for assignment_id=%s",
            assignment_id,
        )
        profile_map = {}
    profile_map_by_lower = {
        str(pid).strip().lower(): value
        for pid, value in profile_map.items()
        if _to_clean_string(pid)
    }

    added_entries: List[Dict[str, Any]] = []
    skipped_public_identifiers: List[str] = []

    for pid in ordered_public_identifiers:
        selected_person = selected_by_pid.get(pid.lower())
        es_id, profile = profile_map.get(
            pid,
            profile_map_by_lower.get(pid.lower(), (None, None)),
        )
        entry = _build_people_involved_entry_from_selection(
            public_identifier=pid,
            selected_person=selected_person,
            es_id=es_id,
            profile=profile,
        )
        if entry:
            added_entries.append(entry)
        else:
            skipped_public_identifiers.append(pid)

    if not added_entries:
        draft = dict(existing_context.get("assignment_draft") or {})
        _finalize_people_fields(draft)
        missing_labels = _missing_field_labels(draft)
        return {
            "found": True,
            "assignment_id": assignment_id,
            "assignment_draft": draft,
            "people_involved": draft.get("people_involved"),
            "added_people": [],
            "skipped_public_identifiers": skipped_public_identifiers,
            "missing_fields": missing_labels,
            "completion_score": _completion_score(draft),
            "ready_to_save": len(missing_labels) == 0,
            "show_add_people_cta": _should_show_add_people_cta(missing_labels),
        }

    draft = dict(existing_context.get("assignment_draft") or {})
    existing_entries = _normalize_people_involved_entries(
        _to_clean_string(draft.get("people_involved"))
    )
    merged_entries = _dedupe_people_entries([*existing_entries, *added_entries])
    draft["people_involved"] = json.dumps(merged_entries, ensure_ascii=False)
    _finalize_people_fields(draft)

    completion = _completion_score(draft)
    missing_labels = _missing_field_labels(draft)
    entity_urn = str(existing_context.get("entity_urn") or "")
    feature_key = (
        _to_clean_string(existing_context.get("feature_key"))
        or FEATURE_KEY_ASSIGNMENT_CREATION
    )
    await upsert_assignment_context(
        entity_urn=entity_urn,
        feature_key=feature_key,
        assignment_draft=draft,
        completion_score=completion,
        last_user_message=existing_context.get("last_user_message"),
        context_id=assignment_id,
    )

    added_people_text = _format_people_names_for_chat(added_entries)
    if entity_urn and added_people_text:
        session = await get_or_create_single_session(
            entity_urn=entity_urn,
            feature_key=feature_key,
            title="Assignment Intake",
        )
        session_id = str(session.get("id"))
        metadata = {
            "assignment_feature_key": assignment_id,
            "type": "manual_people_involved_add",
            "source": "people_involved_add_api",
            "public_identifiers": [
                entry.get("public_identifier")
                for entry in added_entries
                if isinstance(entry, dict) and entry.get("public_identifier")
            ],
        }
        await insert_chat_message(
            session_id=session_id,
            entity_urn=entity_urn,
            feature_key=feature_key,
            role="user",
            content=(
                "Manually added these people to people involved: "
                f"{added_people_text}."
            ),
            metadata=metadata,
        )
        await insert_chat_message(
            session_id=session_id,
            entity_urn=entity_urn,
            feature_key=feature_key,
            role="assistant",
            content=f"Added {added_people_text} to people involved.",
            metadata={
                **metadata,
                "ready_to_save": len(missing_labels) == 0,
                "show_add_people_cta": _should_show_add_people_cta(missing_labels),
            },
        )

    final_people_entries = _normalize_people_involved_entries(
        _to_clean_string(draft.get("people_involved"))
    )
    return {
        "found": True,
        "assignment_id": assignment_id,
        "assignment_draft": draft,
        "people_involved": draft.get("people_involved"),
        "people_involved_entries": final_people_entries,
        "added_people": added_entries,
        "skipped_public_identifiers": skipped_public_identifiers,
        "missing_fields": missing_labels,
        "completion_score": completion,
        "ready_to_save": len(missing_labels) == 0,
        "show_add_people_cta": _should_show_add_people_cta(missing_labels),
    }


def _augment_profile_request_from_people_involved(
    *,
    request: Dict[str, Any],
    people_involved_value: Optional[str],
) -> Dict[str, Any]:
    merged = {
        "should_fetch": bool(request.get("should_fetch")),
        "names": list(request.get("names") or []),
        "titles": list(request.get("titles") or []),
        "companies": list(request.get("companies") or []),
        "timeline": _to_clean_string(request.get("timeline")) or "current",
    }

    # If decider already extracted explicit names for this latest turn,
    # do not expand to historical people_involved entries.
    explicit_names = [n for n in merged["names"] if _to_clean_string(n)]
    if explicit_names:
        return merged

    entries = _normalize_people_involved_entries(people_involved_value)
    if not entries:
        return merged

    existing_names = {
        str(n).strip().lower() for n in merged["names"] if _to_clean_string(n)
    }
    for entry in entries:
        # Backfill only unresolved people (no public_identifier yet).
        if _to_clean_string(entry.get("public_identifier")):
            continue
        name = _to_clean_string(entry.get("name"))
        if not name:
            continue
        if name.strip().lower() in existing_names:
            continue
        existing_names.add(name.strip().lower())
        merged["names"].append(name)
        merged["titles"].append("")
        merged["companies"].append(_to_clean_string(entry.get("company")) or "")

    return merged


def _extract_client_entries_from_suggestions(
    people_involved_suggestions: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if not isinstance(people_involved_suggestions, list):
        return []
    entries: List[Dict[str, Any]] = []
    seen = set()
    for item in people_involved_suggestions:
        if not isinstance(item, dict):
            continue
        first_profile = _extract_first_profile_from_response(item.get("suggestions"))
        if not isinstance(first_profile, dict):
            continue
        matched_experiences = first_profile.get("matched_experiences")
        if not isinstance(matched_experiences, list):
            continue
        for exp in matched_experiences:
            if not isinstance(exp, dict):
                continue
            universal = _to_clean_string(exp.get("company_universal_name"))
            if not universal:
                continue
            universal = universal.lower()
            key = universal.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                {
                    "universal_name": universal,
                    "display_name": _to_clean_string(
                        exp.get("company_name") or exp.get("company")
                    )
                    or universal,
                    "employee_count": None,
                    "es_id": None,
                }
            )
    return entries


def _normalize_client_entries(value: Optional[str]) -> List[Dict[str, Any]]:
    base = _to_clean_string(value)
    if not base:
        return []

    parsed: Any = None
    try:
        parsed = json.loads(base)
    except Exception:
        parsed = None

    entries: List[Dict[str, Any]] = []
    # Support both a single serialized dict and a list of dicts/strings.
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                universal = _to_clean_string(
                    item.get("universal_name")
                    or item.get("company_universal_name")
                    or item.get("li_universalname")
                    or item.get("name")
                )
                if not universal:
                    continue
                universal = universal.lower()
                display = (
                    _to_clean_string(
                        item.get("display_name")
                        or item.get("li_name")
                        or item.get("company_name")
                    )
                    or universal
                )
                employee_count = item.get("employee_count")
                if employee_count is None:
                    employee_count = item.get("li_size")
                entries.append(
                    {
                        "universal_name": universal,
                        "display_name": display,
                        "employee_count": employee_count,
                        "es_id": _to_clean_string(item.get("es_id")),
                    }
                )
            else:
                cleaned = _to_clean_string(item)
                if cleaned:
                    entries.append(
                        {
                            "universal_name": cleaned.lower(),
                            "display_name": cleaned,
                            "employee_count": None,
                            "es_id": None,
                        }
                    )
    else:
        for part in [p.strip() for p in base.split(";") if p.strip()]:
            entries.append(
                {
                    "universal_name": part.lower(),
                    "display_name": part,
                    "employee_count": None,
                    "es_id": None,
                }
            )
    return entries


def _merge_company_candidates(*company_lists: List[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for companies in company_lists:
        for company in companies:
            value = _to_clean_string(company)
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(value)
    return merged


def _extract_company_candidates_from_client_value(value: Optional[str]) -> List[str]:
    entries = _normalize_client_entries(value)
    companies: List[str] = []
    seen = set()
    for entry in entries:
        universal = _to_clean_string(entry.get("universal_name"))
        if not universal:
            continue
        key = universal.lower()
        if key in seen:
            continue
        seen.add(key)
        companies.append(universal)
    return companies


def _client_entries_from_unmapped_targets(targets: List[str]) -> List[Dict[str, Any]]:
    """
    Build provisional client entries directly from user-provided company names.
    This preserves clearly mentioned clients in live preview when mapping lookup
    does not resolve a canonical record in this turn.
    """
    entries: List[Dict[str, Any]] = []
    seen = set()
    for target in targets:
        name = _to_clean_string(target)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "universal_name": name,
                "display_name": name,
                "employee_count": None,
                "es_id": None,
            }
        )
    return entries


def _append_assistant_note(assistant_message: str, note: str) -> str:
    clean_note = _to_clean_string(note)
    if not clean_note:
        return assistant_message
    if not assistant_message:
        return clean_note
    separator = "\n" if assistant_message.endswith("\n") else "\n"
    return assistant_message + separator + clean_note


def _person_names_match(requested: str, saved: str) -> bool:
    requested_parts = [p for p in requested.lower().split() if p]
    saved_parts = [p for p in saved.lower().split() if p]
    if not requested_parts or not saved_parts:
        return False
    if " ".join(requested_parts) == " ".join(saved_parts):
        return True
    if requested_parts[-1] != saved_parts[-1]:
        return False
    if len(requested_parts) == 1 or len(saved_parts) == 1:
        return True
    return (
        requested_parts[0] == saved_parts[0]
        or requested_parts[0][0] == saved_parts[0][0]
    )


def _format_person_name_list(names: List[str]) -> str:
    clean = [_to_clean_string(n) for n in names if _to_clean_string(n)]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return ", ".join(clean[:-1]) + f", and {clean[-1]}"


def _split_people_add_outcome_names(
    *,
    requested_names: List[str],
    after_people_involved: Optional[str],
) -> Tuple[List[str], List[str]]:
    requested = [_to_clean_string(n) for n in requested_names if _to_clean_string(n)]
    if not requested:
        return [], []

    after_entries = _normalize_people_involved_entries(after_people_involved)
    saved_names: List[str] = []
    failed_names: List[str] = []
    for req in requested:
        matched_name: Optional[str] = None
        for entry in after_entries:
            if not isinstance(entry, dict):
                continue
            entry_name = _to_clean_string(entry.get("name"))
            if entry_name and _person_names_match(req, entry_name):
                matched_name = entry_name
                break
        if matched_name:
            saved_names.append(matched_name)
        else:
            failed_names.append(req)
    return saved_names, failed_names


def _build_people_add_outcome_line(
    *,
    requested_names: List[str],
    after_people_involved: Optional[str],
) -> Optional[str]:
    saved_names, failed_names = _split_people_add_outcome_names(
        requested_names=requested_names,
        after_people_involved=after_people_involved,
    )
    if not saved_names and not failed_names:
        return None

    if saved_names and not failed_names:
        return f"People involved: added {_format_person_name_list(saved_names)}."
    if saved_names and failed_names:
        return (
            f"People involved: added {_format_person_name_list(saved_names)} only; "
            f"couldn't verify {_format_person_name_list(failed_names)}."
        )
    return f"People involved: couldn't verify {_format_person_name_list(failed_names)}."


def _prepend_people_outcome_line(
    assistant_message: str,
    outcome_line: Optional[str],
) -> str:
    line = _to_clean_string(outcome_line)
    if not line:
        return assistant_message
    if not assistant_message:
        return line
    return line + "\n\n" + assistant_message


def _apply_people_add_outcome_to_message(
    *,
    assistant_message: str,
    requested_names: List[str],
    after_people_involved: Optional[str],
) -> str:
    outcome_line = _build_people_add_outcome_line(
        requested_names=requested_names,
        after_people_involved=after_people_involved,
    )
    return _prepend_people_outcome_line(assistant_message, outcome_line)


async def _append_dynamic_entity_update_note(
    *,
    assistant_message: str,
    latest_message: str,
    entity_type: str,
    outcome: str,
    targets: List[str],
) -> str:
    """
    Append a short, natural acknowledgement note for client/people add attempts.

    entity_type: "client" | "people"
    outcome: "added" | "not_found"
    """
    clean_targets = [_to_clean_string(t) for t in targets if _to_clean_string(t)]
    if not clean_targets:
        return assistant_message

    system_prompt = (
        "You are a follow-up assistant for assignment-intake chat.\n\n"
        "Task:\n"
        "- Write one short, natural sentence to append to the assistant's reply.\n"
        "- Acknowledge whether requested entities were successfully added or not found.\n"
        "- If not found, ask for one concrete disambiguation detail.\n\n"
        "Rules:\n"
        "- Output plain text only.\n"
        "- One sentence preferred; max two short sentences.\n"
        "- Keep tone helpful and human.\n"
        "- Mention the provided target names directly.\n"
    )
    user_prompt = (
        f"LATEST USER MESSAGE:\n{latest_message}\n\n"
        f"ENTITY TYPE:\n{entity_type}\n\n"
        f"OUTCOME:\n{outcome}\n\n"
        f"TARGETS:\n{', '.join(clean_targets[:5])}\n\n"
        f"CURRENT ASSISTANT REPLY:\n{assistant_message}\n\n"
        "Write the follow-up note now."
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1-mini",
            temperature=0.2,
            fallbacks=["openai/gpt-4.1", "openai/gpt-4o-mini"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        note = _to_clean_string(response)
        if not note:
            return assistant_message
        return _append_assistant_note(assistant_message, note)
    except Exception:
        logger.exception("Failed to generate dynamic entity update note")
        return assistant_message


class _StreamingJsonStringFieldExtractor:
    """Decode one JSON string field from a growing LLM JSON stream."""

    def __init__(self, field_name: str) -> None:
        self._key_re = re.compile(rf'"{re.escape(field_name)}"\s*:\s*')
        self._raw = ""
        self._key_found = False
        self._done = False
        self._empty = False
        self._scan_idx = 0
        self._pending_escape = False
        self._unicode_buffer: Optional[str] = None
        self._text = ""
        self._emitted_len = 0

    @property
    def is_empty_field(self) -> bool:
        return self._empty

    @property
    def has_meaningful_content(self) -> bool:
        """True once thinking contains non-whitespace text."""
        return bool(self._text.strip())

    def feed(self, chunk: str) -> str:
        if self._done or self._empty:
            return ""

        self._raw += chunk
        if not self._key_found:
            match = self._key_re.search(self._raw)
            if not match:
                return ""
            self._key_found = True
            self._scan_idx = match.end()
            remainder = self._raw[self._scan_idx :]
            if remainder.startswith('""'):
                self._empty = True
                self._done = True
                return ""
            if not remainder.startswith('"'):
                return ""
            self._scan_idx += 1

        while self._key_found and not self._done and self._scan_idx < len(self._raw):
            ch = self._raw[self._scan_idx]
            self._scan_idx += 1

            if self._unicode_buffer is not None:
                if ch.lower() in "0123456789abcdef":
                    self._unicode_buffer += ch
                    if len(self._unicode_buffer) == 4:
                        try:
                            self._text += chr(int(self._unicode_buffer, 16))
                        except Exception:
                            self._text += "\\u" + self._unicode_buffer
                        self._unicode_buffer = None
                        self._pending_escape = False
                else:
                    self._text += "\\u" + self._unicode_buffer + ch
                    self._unicode_buffer = None
                    self._pending_escape = False
                continue

            if self._pending_escape:
                if ch == "n":
                    self._text += "\n"
                elif ch == "t":
                    self._text += "\t"
                elif ch == "r":
                    self._text += "\r"
                elif ch == "b":
                    self._text += "\b"
                elif ch == "f":
                    self._text += "\f"
                elif ch in ['"', "\\", "/"]:
                    self._text += ch
                elif ch == "u":
                    self._unicode_buffer = ""
                    continue
                else:
                    self._text += ch
                self._pending_escape = False
                continue

            if ch == "\\":
                self._pending_escape = True
                continue
            if ch == '"':
                self._done = True
                if not _thinking_is_meaningful(self._text):
                    self._empty = True
                break

            self._text += ch

        delta = self._text[self._emitted_len :]
        self._emitted_len = len(self._text)
        return delta


class _ReasoningParagraphEmitter:
    """Split thinking deltas into paragraphs (0-based index per blank line)."""

    def __init__(self) -> None:
        self._paragraph_idx = 0
        self._hold = ""

    def feed(self, delta: str) -> List[Tuple[int, str]]:
        if not delta:
            return []

        buf = self._hold + delta
        self._hold = ""
        out: List[Tuple[int, str]] = []

        while "\n\n" in buf:
            segment, _, buf = buf.partition("\n\n")
            if segment:
                out.append((self._paragraph_idx, segment))
            self._paragraph_idx += 1

        if buf.endswith("\n") and not buf.endswith("\n\n"):
            self._hold = "\n"
            buf = buf[:-1]

        if buf:
            out.append((self._paragraph_idx, buf))
        return out

    def flush(self) -> List[Tuple[int, str]]:
        if not self._hold:
            return []
        out = [(self._paragraph_idx, self._hold)]
        self._hold = ""
        return out


_THINKING_PLACEHOLDER_RE = re.compile(
    r"^[\s.\-_,;:!?…'\"`]*$",
    re.UNICODE,
)


def _thinking_is_meaningful(text: Optional[str]) -> bool:
    """True when thinking is real prose, not empty/whitespace/placeholder."""
    cleaned = _to_clean_string(text)
    if not cleaned or _THINKING_PLACEHOLDER_RE.match(cleaned):
        return False
    return len(cleaned) >= 8


_SIMPLE_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|thanks|thank you|ok|okay|sure|yes|no)[!.?\s]*$",
    re.IGNORECASE,
)
_ASSIGNMENT_DETAIL_SIGNAL_RE = re.compile(
    r"\b("
    r"search|mandate|assignment|client|company|role|outcome|insight|"
    r"business\s+problem|leadership|win\s+reason|talent\s+pool|people|"
    r"executive|cfo|ceo|cto|vp|director|president|chief|placement|"
    r"procurement|supply\s+chain|hurdle|awarded|evaluated|mapped"
    r")\b",
    re.IGNORECASE,
)
_NAMED_PERSON_RE = re.compile(r"[A-Z][a-z]+(?:['\-\.][A-Z][a-z]+)?\s+[A-Z][a-z]+")


def _thinking_required_for_message(latest_message: str) -> bool:
    """
    Substantive assignment/search turns get pipeline progress thinking.
    Skip for greetings and save/skip-only commands with no new content.
    """
    message = _to_clean_string(latest_message)
    if not message:
        return False
    if _SIMPLE_GREETING_RE.match(message):
        return False
    if (
        _SAVE_INTENT_RE.search(message) or _SKIP_REMAINING_IN_MESSAGE_RE.search(message)
    ) and len(message) <= 48:
        if not _ASSIGNMENT_DETAIL_SIGNAL_RE.search(
            message
        ) and not _NAMED_PERSON_RE.search(message):
            return False
    if len(message) >= 80:
        return True
    if _ASSIGNMENT_DETAIL_SIGNAL_RE.search(message):
        return True
    if _NAMED_PERSON_RE.search(message):
        return True
    return len(message) >= 40


def _plan_pipeline_thinking_steps(
    *,
    latest_message: str,
    missing_field_count: int,
    should_fetch_people: bool,
    people_names: List[str],
    early_save_requested: bool,
) -> List[Dict[str, Any]]:
    """Structured pipeline steps passed to the fast thinking LLM (wording is dynamic)."""
    steps: List[Dict[str, Any]] = []
    substantive = _thinking_required_for_message(latest_message)

    if not substantive:
        if early_save_requested:
            steps.append({"action": "check_save", "context": {}})
        return steps

    steps.append(
        {
            "action": "read_message",
            "context": {"missing_field_count": missing_field_count},
        }
    )
    clean_names = [_to_clean_string(n) for n in people_names if _to_clean_string(n)]
    if should_fetch_people and clean_names:
        steps.append(
            {
                "action": "verify_people",
                "context": {"count": len(clean_names)},
            }
        )
    steps.append({"action": "process_assignment", "context": {}})
    if early_save_requested:
        steps.append({"action": "check_save", "context": {}})
    steps.append({"action": "prepare_response", "context": {}})
    return steps


async def _generate_pipeline_thinking_sentences(
    *,
    latest_message: str,
    steps: List[Dict[str, Any]],
) -> List[str]:
    if not steps:
        return []
    user_prompt = PIPELINE_THINKING_USER_PROMPT_TEMPLATE.format(
        user_snippet=_trim_text_for_stream_ack_context(latest_message, limit=500),
        steps_json=json.dumps(steps, ensure_ascii=False, indent=2),
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1-nano",
            temperature=0.2,
            fallbacks=["openai/gpt-4.1-mini", "openai/gpt-4o-mini"],
            messages=[
                {"role": "system", "content": PIPELINE_THINKING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if isinstance(parsed, dict):
            raw_sentences = parsed.get("sentences")
            if isinstance(raw_sentences, list):
                sentences = [
                    _to_clean_string(s) for s in raw_sentences if _to_clean_string(s)
                ]
                if sentences:
                    return sentences[: len(steps)]
    except Exception:
        logger.exception("Failed to generate pipeline thinking sentences")
    return []


def _should_defer_assistant_stream(
    *,
    latest_message: str,
    save_requested: bool,
    explicit_skip_remaining: bool,
    suppress_stream_deltas: bool,
) -> bool:
    """Hold assistant text until save/edit state is resolved; emit one final chunk."""
    if suppress_stream_deltas:
        return True
    if save_requested or explicit_skip_remaining:
        return True
    message = _to_clean_string(latest_message)
    if _FIELD_EDIT_WITHOUT_SAVE_RE.search(message) and not _SAVE_INTENT_RE.search(
        message
    ):
        return True
    return False


def _format_missing_fields_list(missing_fields: List[str]) -> str:
    labels = [_to_clean_string(f) for f in missing_fields if _to_clean_string(f)]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _missing_fields_grammar(missing_fields: List[str]) -> Tuple[str, str, str]:
    """Return (formatted list, is/are, it/them) for natural copy."""
    fmt = _format_missing_fields_list(missing_fields)
    if len(missing_fields) == 1:
        return fmt, "is", "it"
    return fmt, "are", "them"


_SAVE_STATUS_CLAIM_RE = re.compile(
    r"(?i)\b("
    r"ready to save|saved as is|saved without|go ahead and save|"
    r"everything required|all required|assignment is complete|"
    r"now complete and saved|since you requested to save|"
    r"save isn'?t available|intentionally left blank as you requested"
    r")\b"
)

_INTAKE_BULLET_CHAIN_RE = re.compile(r"\?\s*-\s+")


def _normalize_intake_bullet_format(text: str) -> str:
    """Ensure each intake question is on its own Markdown bullet line."""
    message = text or ""
    if not message.strip() or "- " not in message:
        return message

    normalized = _INTAKE_BULLET_CHAIN_RE.sub("?\n- ", message)
    result_lines: List[str] = []
    for line in normalized.split("\n"):
        stripped = line.strip()
        if not stripped:
            result_lines.append("")
            continue
        if stripped.count("- ") >= 2 and stripped.startswith("- "):
            for segment in re.split(r"\s+-\s+", stripped):
                segment = segment.strip()
                if not segment:
                    continue
                if not segment.startswith("- "):
                    segment = "- " + segment.lstrip("- ").strip()
                result_lines.append(segment)
            continue
        result_lines.append(stripped)
    return "\n".join(result_lines).strip()


def _strip_save_status_claims(text: str) -> str:
    """Remove sentences that make save/complete claims inconsistent with backend."""
    message = _to_clean_string(text)
    if not message:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", message)
    kept: List[str] = []
    for part in parts:
        chunk = part.strip()
        if not chunk:
            continue
        if _SAVE_STATUS_CLAIM_RE.search(chunk):
            continue
        kept.append(chunk)
    return " ".join(kept).strip()


def _build_deterministic_save_status_message(
    *,
    missing_fields: List[str],
    ready_to_save: bool,
    save_requested: bool,
    explicit_skip_remaining: bool,
    latest_message: str,
    prior_blocked_save_count: int = 0,
    auto_waived_on_retry: bool = False,
    waived_labels: Optional[List[str]] = None,
) -> Optional[str]:
    missing_fmt = _format_missing_fields_list(missing_fields)
    message = _to_clean_string(latest_message)
    is_field_edit = bool(
        _FIELD_EDIT_WITHOUT_SAVE_RE.search(message)
        and not _SAVE_INTENT_RE.search(message)
    )
    people_label = REQUIRED_ASSIGNMENT_FIELD_LABELS["people_involved"]
    waived = list(waived_labels or [])
    skippable = [f for f in missing_fields if f != people_label]
    effective_blank = [f for f in skippable if f not in waived]

    if ready_to_save and save_requested:
        if effective_blank:
            blank_fmt = _format_missing_fields_list(effective_blank)
            return f"Got it — ready to save with {blank_fmt} left blank."
        if explicit_skip_remaining or auto_waived_on_retry:
            return "This assignment is complete and ready to save."

    if (
        ready_to_save
        and not missing_fields
        and (save_requested or explicit_skip_remaining)
    ):
        return "This assignment is complete and ready to save."

    if save_requested and not ready_to_save and missing_fields:
        if people_label in missing_fields:
            other = [f for f in missing_fields if f != people_label]
            if other:
                other_fmt = _format_missing_fields_list(other)
                return (
                    f"Still need {people_label} — add who's involved and I can "
                    f"save with {other_fmt} left blank."
                )
            return f"Still need {people_label} — add who's involved and I'll save."

        if len(skippable) > _MAX_SKIPPABLE_MISSING_FIELDS_ON_EXPLICIT_SAVE:
            skippable_fmt = _format_missing_fields_list(skippable)
            return (
                f"I can save with one field left blank, but still need "
                f"{skippable_fmt}. Share either one, or save again to leave "
                f"the rest blank."
            )

        missing_fmt, verb, pronoun = _missing_fields_grammar(missing_fields)
        if explicit_skip_remaining:
            return (
                f"I can't save yet — {missing_fmt} {verb} still missing. "
                "I can leave one field blank once only one gap remains."
            )

        object_pronoun = "them" if pronoun == "them" else "it"
        return (
            f"I can't save yet — {missing_fmt} {verb} still missing. "
            f"Share {object_pronoun} when you're ready, or save again to leave "
            f"one field blank."
        )

    if is_field_edit and missing_fields:
        edit_targets = _infer_field_edit_target_labels(latest_message)
        target_fmt = _format_missing_fields_list(
            edit_targets if edit_targets else missing_fields[:1]
        )
        return (
            f"Got it — I've left {target_fmt} blank. "
            "It's still shown as missing until you add it or choose to "
            "save without it."
        )

    if is_field_edit and not missing_fields:
        return "Got it — I've updated that field."

    return None


async def _finalize_assistant_message(
    *,
    assistant_message: str,
    missing_fields: List[str],
    ready_to_save: bool,
    save_requested: bool,
    explicit_skip_remaining: bool,
    latest_message: str,
    suppress_stream_deltas: bool = False,
    prior_blocked_save_count: int = 0,
    auto_waived_on_retry: bool = False,
    waived_labels: Optional[List[str]] = None,
    people_outcome_requested_names: Optional[List[str]] = None,
    after_people_involved: Optional[str] = None,
) -> str:
    """
    Align assistant text with backend save state using deterministic status lines.
    Falls back to a light LLM pass only for ordinary missing-field intake turns.
    """
    defer_stream = _should_defer_assistant_stream(
        latest_message=latest_message,
        save_requested=save_requested,
        explicit_skip_remaining=explicit_skip_remaining,
        suppress_stream_deltas=suppress_stream_deltas,
    )
    status_message = _build_deterministic_save_status_message(
        missing_fields=missing_fields,
        ready_to_save=ready_to_save,
        save_requested=save_requested,
        explicit_skip_remaining=explicit_skip_remaining,
        latest_message=latest_message,
        prior_blocked_save_count=prior_blocked_save_count,
        auto_waived_on_retry=auto_waived_on_retry,
        waived_labels=waived_labels,
    )

    body = assistant_message
    if (
        missing_fields
        or save_requested
        or explicit_skip_remaining
        or auto_waived_on_retry
        or defer_stream
    ):
        body = _strip_save_status_claims(body)

    if status_message and (
        defer_stream
        or save_requested
        or explicit_skip_remaining
        or auto_waived_on_retry
    ):
        body = status_message
    elif status_message:
        body = f"{body}\n\n{status_message}".strip() if body else status_message
    elif missing_fields and not ready_to_save and not defer_stream:
        body = await _normalize_assistant_message_consistency(
            assistant_message=body or assistant_message,
            missing_fields=missing_fields,
            ready_to_save=ready_to_save,
            save_requested=save_requested,
            explicit_skip_remaining=explicit_skip_remaining,
            people_outcome_requested_names=people_outcome_requested_names,
            after_people_involved=after_people_involved,
        )

    if people_outcome_requested_names:
        body = _apply_people_add_outcome_to_message(
            assistant_message=body,
            requested_names=people_outcome_requested_names,
            after_people_involved=after_people_involved,
        )
    return _normalize_intake_bullet_format(body)


async def _normalize_assistant_message_consistency(
    *,
    assistant_message: str,
    missing_fields: List[str],
    ready_to_save: bool,
    save_requested: bool = False,
    explicit_skip_remaining: bool = False,
    people_outcome_requested_names: Optional[List[str]] = None,
    after_people_involved: Optional[str] = None,
) -> str:
    """
    Post-process assistant_message so that statements about readiness to save
    are consistent with the backend's notion of missing required fields.
    """
    # Reconcile the LLM's early reply with backend post-processing. Entity
    # lookups can satisfy fields after the reply was drafted, so the final
    # message must be based on missing_fields, not the original model guess.
    missing_list = ", ".join(missing_fields) if missing_fields else "none"
    people_verified: List[str] = []
    people_unverified: List[str] = []
    if people_outcome_requested_names:
        people_verified, people_unverified = _split_people_add_outcome_names(
            requested_names=people_outcome_requested_names,
            after_people_involved=after_people_involved,
        )

    system_prompt = (
        "You are editing an assistant reply for an assignment-intake chat.\n\n"
        "Goal:\n"
        "- Make sure the message's comments about being 'complete' or 'ready to save' "
        "match the backend state.\n"
        "- Make sure the message asks ONLY for fields that are still listed in "
        "MISSING REQUIRED FIELD LABELS.\n"
        "- If the current reply asks for details about a field that is no longer "
        "missing, remove that question.\n"
        "- In particular, if People Involved is NOT missing, do not ask for a "
        "person's title, current company, employer, or mapping details.\n"
        "- If there are missing REQUIRED fields and READY_TO_SAVE is false, the message "
        "MUST NOT say everything is complete or ready to save. Instead, it should "
        "briefly acknowledge what was captured and ask for the remaining missing "
        "fields, OR explain that save is blocked until those fields are provided or "
        "the user confirms they want to save without them.\n"
        "- When missing_fields is empty and ready_to_save is true, do not ask for "
        "more required details; briefly acknowledge that the latest update was "
        "captured and the assignment is ready.\n"
        "- When READY_TO_SAVE is true but MISSING REQUIRED FIELD LABELS is not "
        "'none', the user chose to save without filling the remaining gap(s). "
        "Confirm the assignment is ready to save as-is and name the missing "
        "field(s) as intentionally left blank. Do NOT say all required fields "
        "are complete, filled, or present.\n"
        "- When READY_TO_SAVE is false, do NOT say the assignment is saved, "
        "complete, finalized, or ready to save.\n"
        "- When SAVE_REQUESTED is true, READY_TO_SAVE is false, and missing fields "
        "remain, explain that save is not available yet and name the remaining "
        "field(s). Do NOT repeat the same intake questions the user already "
        "refused. If more than one field is missing, explain that only one field "
        "can be left blank on save. Offer a short path forward — do not loop.\n"
        "- When SAVE_REQUESTED is true and EXPLICIT_SKIP_REMAINING is true, do NOT "
        "ask for the missing fields again — the save status line handles the reply.\n"
        "- Do NOT repeat or paraphrase a 'People involved:' outcome line; that is "
        "prepended separately after your edit.\n"
        "- Do NOT list individual people names when a people outcome line will be "
        "prepended; keep the body focused on assignment readiness or missing fields.\n"
    )
    if people_verified or people_unverified:
        system_prompt += (
            "- When PEOPLE VERIFICATION RESULTS are provided, the reply body must "
            "match them exactly.\n"
            "- Do NOT thank, confirm, or say you added anyone listed under "
            "PEOPLE NOT VERIFIED.\n"
            "- If you mention people at all, only reference names under "
            "PEOPLE VERIFIED.\n"
            "- Do NOT include a 'People involved:' status line; that will be "
            "prepended separately after your edit.\n"
            "- Focus on asking for the remaining missing assignment fields.\n"
        )
    system_prompt += (
        "\nOutput:\n"
        "- Return the full, final assistant reply as plain text (no JSON).\n"
        "- Keep it short and conversational.\n"
    )

    user_prompt = (
        f"CURRENT ASSISTANT REPLY:\n{assistant_message}\n\n"
        f"MISSING REQUIRED FIELD LABELS:\n{missing_list}\n\n"
        f"READY_TO_SAVE FLAG:\n{ready_to_save}\n\n"
        f"SAVE_REQUESTED:\n{save_requested}\n\n"
        f"EXPLICIT_SKIP_REMAINING:\n{explicit_skip_remaining}\n\n"
    )
    if people_verified or people_unverified:
        user_prompt += (
            f"PEOPLE VERIFIED:\n{_format_person_name_list(people_verified) or 'none'}\n\n"
            f"PEOPLE NOT VERIFIED:\n{_format_person_name_list(people_unverified) or 'none'}\n\n"
        )
    user_prompt += (
        "Rewrite the assistant reply so that it is consistent with the missing fields "
        "and ready_to_save flag"
    )
    if people_verified or people_unverified:
        user_prompt += " and the people verification results"
    user_prompt += ".\n"
    try:
        response = await invoke(
            model="openai/gpt-4.1-mini",
            temperature=0.2,
            fallbacks=["openai/gpt-4.1", "openai/gpt-4o-mini"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        rewritten = _to_clean_string(response)
        return rewritten or assistant_message
    except Exception:
        logger.exception("Failed to normalize assistant message consistency")
        return assistant_message


def _profile_names_from_suggestions(
    people_involved_suggestions: Optional[List[Dict[str, Any]]],
) -> List[str]:
    if not isinstance(people_involved_suggestions, list):
        return []
    names: List[str] = []
    seen = set()
    for item in people_involved_suggestions:
        if not isinstance(item, dict):
            continue
        profile = _extract_first_profile_from_response(item.get("suggestions"))
        if not isinstance(profile, dict):
            continue
        name = _to_clean_string(profile.get("full_name") or profile.get("name"))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _append_client_entries_to_client(
    *,
    current_client: Optional[str],
    client_entries: List[Dict[str, Any]],
    replace_targets: Optional[List[str]] = None,
) -> Optional[str]:
    existing = _normalize_client_entries(current_client)
    by_universal: Dict[str, Dict[str, Any]] = {}
    ordered_keys: List[str] = []
    replace_keys = {
        t.strip().lower() for t in (replace_targets or []) if _to_clean_string(t)
    }

    for entry in existing:
        universal = _to_clean_string(entry.get("universal_name"))
        if not universal:
            continue
        display_name = _to_clean_string(entry.get("display_name")) or universal
        if replace_keys and (
            universal.lower() in replace_keys or display_name.lower() in replace_keys
        ):
            # Skip old client entry that user asked to replace.
            continue
        key = universal.lower()
        if key not in by_universal:
            ordered_keys.append(key)
        by_universal[key] = {
            "universal_name": universal,
            "display_name": display_name,
            "employee_count": entry.get("employee_count"),
            "es_id": _to_clean_string(entry.get("es_id")),
        }

    for entry in client_entries:
        if not isinstance(entry, dict):
            continue
        universal = _to_clean_string(entry.get("universal_name"))
        if not universal:
            continue
        key = universal.lower()
        if key not in by_universal:
            ordered_keys.append(key)
            by_universal[key] = {
                "universal_name": universal,
                "display_name": _to_clean_string(entry.get("display_name"))
                or universal,
                "employee_count": entry.get("employee_count"),
                "es_id": _to_clean_string(entry.get("es_id")),
            }
            continue
        existing_entry = by_universal[key]
        incoming_display = _to_clean_string(entry.get("display_name"))
        if incoming_display:
            existing_entry["display_name"] = incoming_display
        incoming_employee_count = entry.get("employee_count")
        if incoming_employee_count is not None:
            existing_entry["employee_count"] = incoming_employee_count
        incoming_es_id = _to_clean_string(entry.get("es_id"))
        if incoming_es_id:
            existing_entry["es_id"] = incoming_es_id

    merged = [by_universal[k] for k in ordered_keys if k in by_universal]
    if not merged:
        return None
    return json.dumps(merged, ensure_ascii=False)


def _extract_client_replace_targets_from_update(
    update: Optional[Dict[str, Any]],
) -> List[str]:
    if not isinstance(update, dict):
        return []
    key = _to_clean_string(update.get("key"))
    if not key or key.strip().lower() != "client":
        return []

    current_value = _to_clean_string(update.get("current_value"))
    if not current_value:
        return []

    targets: List[str] = []
    seen = set()
    parsed_entries = _normalize_client_entries(current_value)
    if parsed_entries:
        for entry in parsed_entries:
            for candidate in (
                _to_clean_string(entry.get("universal_name")),
                _to_clean_string(entry.get("display_name")),
            ):
                if not candidate:
                    continue
                candidate_key = candidate.strip().lower()
                if candidate_key in seen:
                    continue
                seen.add(candidate_key)
                targets.append(candidate)
        return targets

    raw_key = current_value.strip().lower()
    if raw_key:
        targets.append(current_value)
    return targets


def _extract_client_entry_from_mapping_response(
    response: Any,
) -> Optional[Dict[str, Any]]:
    if isinstance(response, dict):
        source = response.get("source")
        if isinstance(source, dict):
            universal = _to_clean_string(source.get("li_universalname"))
            if universal:
                universal = universal.lower()
                return {
                    "universal_name": universal,
                    "display_name": _to_clean_string(source.get("li_name"))
                    or universal,
                    "employee_count": source.get("li_size"),
                    "es_id": _to_clean_string(response.get("es_id")),
                }
    return None


async def get_company_mapping(company_name: str, user_query: str) -> Optional[Any]:
    url = (
        os.getenv("COMPANY_MAPPING_URL") or os.getenv("OMPANY_MAPPING_URL") or ""
    ).strip()
    if not url:
        return None

    raw_api_key = (os.getenv("COMPANY_MAPPING_API") or "").strip()
    api_key = raw_api_key
    if raw_api_key.lower().startswith("bearer "):
        api_key = raw_api_key.split(" ", 1)[1].strip()

    headers = {"Content-Type": "application/json", "accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key

    payload = {
        "company_name": company_name,
        "user_query": user_query,
        "relevant_industries": [],
    }

    try:
        logger.info("Company mapping call started: company_name=%s", company_name)
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        def _send() -> Tuple[int, Any]:
            with urllib.request.urlopen(req, timeout=40) as resp:
                status = int(getattr(resp, "status", 200))
                body = resp.read().decode("utf-8")
                parsed = json.loads(body) if body else None
                return status, parsed

        status, parsed = await asyncio.to_thread(_send)
        logger.info(
            "Company mapping call finished: status=%s company_name=%s universal_name=%s",
            status,
            company_name,
            (
                (_extract_client_entry_from_mapping_response(parsed) or {}).get(
                    "universal_name"
                )
            ),
        )
        return parsed
    except Exception:
        logger.exception(
            "Failed to fetch company mapping for company_name=%s", company_name
        )
        return None


async def get_company_mapping_multi(
    *,
    companies: List[str],
    user_query: str,
) -> List[Dict[str, Any]]:
    if not companies:
        return []

    seen = set()
    normalized_companies: List[str] = []
    for company in companies:
        value = _to_clean_string(company)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_companies.append(value)

    results: List[Dict[str, Any]] = []
    total = len(normalized_companies)
    for idx, company in enumerate(normalized_companies, start=1):
        logger.info("Company mapping target: %d/%d company=%s", idx, total, company)
        response = await get_company_mapping(company, user_query)
        results.append({"company": company, "mapping": response})
    return results


async def get_profile_suggestions(payload: Dict[str, Any]) -> Optional[Any]:
    url = (os.getenv("MAP_PERSON_API") or "").strip()
    if not url:
        logger.warning("MAP_PERSON_API is not configured; skipping person mapping")
        return None

    headers = _map_person_api_headers()

    try:
        logger.info(
            "Map person call started: name=%s company=%s title=%s",
            payload.get("name"),
            payload.get("company"),
            payload.get("title"),
        )
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        def _send() -> Tuple[int, Any]:
            with urllib.request.urlopen(req, timeout=40) as resp:
                status = int(getattr(resp, "status", 200))
                body = resp.read().decode("utf-8")
                parsed = json.loads(body) if body else None
                return status, parsed

        status, parsed = await asyncio.to_thread(_send)
        if PEOPLE_TRACE_DEBUG:
            response_summary = _summarize_map_person_response(parsed)
            logger.info(
                "Map person call finished: status=%s name=%s has_results=%s response_summary=%s",
                status,
                payload.get("name"),
                _has_profile_suggestion_results(parsed),
                response_summary,
            )
            _people_trace_log(
                "map_person_response",
                name=payload.get("name"),
                status=status,
                summary=response_summary,
                raw_response=_people_trace_json(parsed),
            )
        else:
            logger.info(
                "Map person call finished: status=%s name=%s has_results=%s",
                status,
                payload.get("name"),
                _has_profile_suggestion_results(parsed),
            )
        return parsed
    except Exception:
        logger.exception("Failed to map person via MAP_PERSON_API")
        return None


async def _map_person_multi_item(
    *,
    idx: int,
    total: int,
    name: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    logger.info(
        "Profile suggestions person: %d/%d name=%s payload=%s",
        idx,
        total,
        name,
        payload,
    )
    map_started = time.perf_counter()
    response = await get_profile_suggestions(payload)
    if LIVE_PREVIEW_TIMING_DEBUG:
        logger.info(
            "[live_preview_timing] label=map_person phase=item index=%d/%d name=%r phase_ms=%.1f",
            idx,
            total,
            name,
            (time.perf_counter() - map_started) * 1000.0,
        )

    _people_trace_log(
        "map_person_multi_item",
        index=idx,
        total=total,
        name=name,
        response_summary=_summarize_map_person_response(response),
        extracted_profile=_summarize_map_person_response(
            _extract_first_profile_from_response(response)
        ),
    )
    return {
        "name": name,
        "suggestions": response,
        "payload_used": payload,
    }


async def get_profile_suggestions_multi(
    request: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Make one profile-suggestions call per extracted person using structured fields only.

    When multiple names are present (talent-pool style), use name-only lookups to avoid
    mis-mapping from shared title/company hints. Calls run in parallel batches of up to
    MAP_PERSON_BATCH_SIZE.
    """
    names = list(request.get("names") or [])
    titles = list(request.get("titles") or [])
    companies = list(request.get("companies") or [])
    timeline = _to_clean_string(request.get("timeline")) or "current"
    name_only = bool(request.get("name_only")) or len(names) >= 2
    if not names:
        return []

    total = len(names)
    items: List[Tuple[int, str, Dict[str, Any]]] = []
    for idx, name in enumerate(names, start=1):
        title = (
            None if name_only else (titles[idx - 1] if idx - 1 < len(titles) else None)
        )
        company = (
            None
            if name_only
            else (companies[idx - 1] if idx - 1 < len(companies) else None)
        )
        payload = _build_profile_payload_for_person(
            name=name,
            title=_to_clean_string(title),
            company=_to_clean_string(company),
            timeline=timeline,
        )
        items.append((idx, name, payload))

    results: List[Dict[str, Any]] = []
    for batch_start in range(0, len(items), MAP_PERSON_BATCH_SIZE):
        batch = items[batch_start : batch_start + MAP_PERSON_BATCH_SIZE]
        batch_started = time.perf_counter()
        batch_results = await asyncio.gather(
            *[
                _map_person_multi_item(
                    idx=idx,
                    total=total,
                    name=name,
                    payload=payload,
                )
                for idx, name, payload in batch
            ]
        )
        results.extend(batch_results)
        if LIVE_PREVIEW_TIMING_DEBUG:
            logger.info(
                "[live_preview_timing] label=map_person phase=batch size=%d start_index=%d phase_ms=%.1f",
                len(batch),
                batch[0][0],
                (time.perf_counter() - batch_started) * 1000.0,
            )

    _people_trace_log(
        "map_person_multi_done",
        total=total,
        result_count=len(results),
        names=[item.get("name") for item in results if isinstance(item, dict)],
    )
    return results


def _extract_first_profile_from_response(response: Any) -> Optional[Dict[str, Any]]:
    return _normalize_map_person_profile(response)


async def _fetch_mapped_person_profile_from_es(
    *,
    es_client: AsyncElasticsearch,
    public_identifier: str,
    mapped_es_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    es_id: Optional[str] = None
    profile: Optional[Dict[str, Any]] = None

    if mapped_es_id:
        try:
            es_id, profile = await fetch_profile_by_es_id(
                mapped_es_id,
                es_client=es_client,
                source_fields=None,
                only_top_experience=True,
            )
        except Exception:
            logger.exception(
                "Failed ES lookup for mapped person es_id=%s",
                mapped_es_id,
            )

    if profile is None and public_identifier:
        try:
            es_id, profile = await fetch_profile_by_public_identifier(
                public_identifier,
                es_client=es_client,
                source_fields=None,
                only_top_experience=True,
            )
        except Exception:
            logger.exception(
                "Failed ES lookup for mapped person public_identifier=%s",
                public_identifier,
            )

    return es_id, profile


async def _build_people_entry_from_suggestion_item(
    *,
    item_idx: int,
    item: Dict[str, Any],
    es_client: Optional[AsyncElasticsearch] = None,
    es_lookup: Optional[Tuple[Optional[str], Optional[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
    first_profile = _extract_first_profile_from_response(item.get("suggestions"))
    if not isinstance(first_profile, dict):
        _people_trace_log(
            "append_people_item_skipped",
            item_idx=item_idx,
            name=item.get("name"),
            reason="no_extracted_profile",
            response_summary=_summarize_map_person_response(item.get("suggestions")),
        )
        return None

    profile_public_identifier = _to_clean_string(first_profile.get("public_identifier"))
    if not profile_public_identifier:
        _people_trace_log(
            "append_people_item_skipped",
            item_idx=item_idx,
            name=item.get("name"),
            reason="missing_public_identifier",
            profile_keys=sorted(first_profile.keys()),
            profile_preview=_people_trace_json(first_profile, limit=400),
        )
        return None

    payload_used = (
        item.get("payload_used") if isinstance(item.get("payload_used"), dict) else {}
    )
    payload_company = _company_from_map_person_payload(payload_used)
    request_name = _to_clean_string(item.get("name"))

    if es_client is not None:
        mapped_es_id = _to_clean_string(first_profile.get("id"))
        if es_lookup is None:
            _people_trace_log(
                "append_people_es_lookup_start",
                item_idx=item_idx,
                name=item.get("name"),
                public_identifier=profile_public_identifier,
                mapped_es_id=mapped_es_id,
            )
            es_id, profile = await _fetch_mapped_person_profile_from_es(
                es_client=es_client,
                public_identifier=profile_public_identifier,
                mapped_es_id=mapped_es_id,
            )
        else:
            es_id, profile = es_lookup
            _people_trace_log(
                "append_people_es_lookup_start",
                item_idx=item_idx,
                name=item.get("name"),
                public_identifier=profile_public_identifier,
                mapped_es_id=mapped_es_id,
            )

        _people_trace_log(
            "append_people_es_lookup_done",
            item_idx=item_idx,
            name=item.get("name"),
            public_identifier=profile_public_identifier,
            mapped_es_id=mapped_es_id,
            es_id=es_id,
            has_profile=isinstance(profile, dict),
            profile_keys=(sorted(profile.keys()) if isinstance(profile, dict) else []),
        )

        if not isinstance(profile, dict):
            _people_trace_log(
                "append_people_item_skipped",
                item_idx=item_idx,
                name=item.get("name"),
                reason="es_profile_not_found",
                public_identifier=profile_public_identifier,
                mapped_es_id=mapped_es_id,
            )
            return None

        profile_name = _to_clean_string(profile.get("full_name") or profile.get("name"))
        if (
            request_name
            and profile_name
            and not _person_names_match(request_name, profile_name)
        ):
            _people_trace_log(
                "append_people_item_skipped",
                item_idx=item_idx,
                name=request_name,
                reason="es_profile_name_mismatch",
                profile_name=profile_name,
                public_identifier=profile_public_identifier,
            )
            return None

        selected_person = dict(first_profile)
        if request_name and not _to_clean_string(
            selected_person.get("full_name") or selected_person.get("name")
        ):
            selected_person["name"] = request_name
        if payload_company:
            selected_person["company"] = payload_company
        entry = _build_people_involved_entry_from_selection(
            public_identifier=profile_public_identifier,
            selected_person=selected_person,
            es_id=es_id,
            profile=profile,
        )
        if not entry:
            _people_trace_log(
                "append_people_item_skipped",
                item_idx=item_idx,
                name=item.get("name"),
                reason="build_entry_returned_none",
                public_identifier=profile_public_identifier,
                es_id=es_id,
            )
        return entry

    profile_name = _to_clean_string(
        first_profile.get("full_name") or first_profile.get("name")
    )
    profile_id = _to_clean_string(first_profile.get("id"))
    if profile_name and profile_id:
        entry = {
            "name": profile_name,
            "company": payload_company,
            "id": profile_id,
            "public_identifier": profile_public_identifier,
        }
        _people_trace_log(
            "append_people_item_built_no_es",
            item_idx=item_idx,
            entry=entry,
        )
        return entry

    _people_trace_log(
        "append_people_item_skipped",
        item_idx=item_idx,
        name=item.get("name"),
        reason="missing_name_or_id_without_es_client",
        profile_name=profile_name,
        profile_id=profile_id,
    )
    return None


def _normalize_people_involved_entry_dict(
    item: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    name = _to_clean_string(item.get("name"))
    if not name:
        return None
    return {
        "name": name,
        "company": _to_clean_string(item.get("company")),
        "id": _to_clean_string(item.get("id")),
        "public_identifier": _to_clean_string(item.get("public_identifier")),
    }


def _parse_people_involved_update_entries(
    current_value: str,
) -> List[Dict[str, Any]]:
    """
    Parse people_involved chip/update payloads.

    UI chips may send either a single profile object or a one-item array.
    """
    raw = _to_clean_string(current_value)
    if not raw:
        return []

    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except Exception:
        try:
            parsed = ast.literal_eval(raw)
        except Exception:
            parsed = None

    if isinstance(parsed, dict):
        entry = _normalize_people_involved_entry_dict(parsed)
        return [entry] if entry else []

    if isinstance(parsed, list):
        entries: List[Dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            entry = _normalize_people_involved_entry_dict(item)
            if entry:
                entries.append(entry)
        return entries

    return _normalize_people_involved_entries(raw)


def _extract_people_replace_targets_from_update(
    update: Optional[Dict[str, Any]],
) -> Dict[str, set]:
    targets = {"names": set(), "public_identifiers": set(), "ids": set()}
    if not isinstance(update, dict):
        return targets

    key = _to_clean_string(update.get("key"))
    if not key or key.strip().lower() != "people_involved":
        return targets

    current_value = _to_clean_string(update.get("current_value"))
    if not current_value:
        return targets

    parsed_entries = _parse_people_involved_update_entries(current_value)
    if parsed_entries:
        for entry in parsed_entries:
            name = _to_clean_string(entry.get("name"))
            public_identifier = _to_clean_string(entry.get("public_identifier"))
            pid = _to_clean_string(entry.get("id"))
            if name:
                targets["names"].add(name.lower())
            if public_identifier:
                targets["public_identifiers"].add(public_identifier.lower())
            if pid:
                targets["ids"].add(pid.lower())
        return targets

    # Backward-compatible plain-text fallback (single name/public identifier/id).
    text = current_value.strip()
    if text and not text.startswith(("{", "[")):
        targets["names"].add(text.lower())
        targets["public_identifiers"].add(text.lower())
        targets["ids"].add(text.lower())
    return targets


_CHIP_PEOPLE_REMOVE_RE = re.compile(
    r"\b(remove|delete|take\s+off|drop|clear)\b",
    re.IGNORECASE,
)
_CHAT_PEOPLE_REMOVE_FROM_COMPANY_RE = re.compile(
    r"\b(?:remove|delete|take\s+off|drop)\s+(.+?)\s+(?:from|at)\s+(.+?)\s*$",
    re.IGNORECASE,
)


def _augment_remove_target_specs_from_message(
    *,
    latest_message: str,
    specs: List[Dict[str, Any]],
    current_people_involved: Optional[str],
) -> List[Dict[str, Any]]:
    """
    When the user says "remove X from Y", enforce a name+company remove spec even
    if the planner only returned a bare name.
    """
    message = _to_clean_string(latest_message) or ""
    match = _CHAT_PEOPLE_REMOVE_FROM_COMPANY_RE.search(message)
    if not match:
        return specs

    raw_name = _to_clean_string(match.group(1))
    raw_company = _to_clean_string(match.group(2))
    if not raw_name or not raw_company:
        return specs

    name_lower = raw_name.lower()
    company_lower = raw_company.lower()
    entries = _normalize_people_involved_entries(current_people_involved)

    resolved_name = raw_name
    resolved_company = raw_company
    resolved_id: Optional[str] = None
    resolved_public_identifier: Optional[str] = None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_name = (_to_clean_string(entry.get("name")) or "").lower()
        entry_company = (_to_clean_string(entry.get("company")) or "").lower()
        if not entry_name:
            continue
        name_matches = name_lower == entry_name or name_lower in entry_name
        if not name_matches:
            continue
        if company_lower not in entry_company and entry_company not in company_lower:
            continue
        resolved_name = _to_clean_string(entry.get("name")) or raw_name
        resolved_company = _to_clean_string(entry.get("company")) or raw_company
        resolved_id = _to_clean_string(entry.get("id"))
        resolved_public_identifier = _to_clean_string(entry.get("public_identifier"))
        break

    return [
        {
            "name": resolved_name,
            "company": resolved_company,
            "id": resolved_id,
            "public_identifier": resolved_public_identifier,
        }
    ]


def _extract_chip_people_entry_from_update(
    update: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not isinstance(update, dict):
        return None
    key = _to_clean_string(update.get("key"))
    if not key or key.strip().lower() != "people_involved":
        return None
    current_value = _to_clean_string(update.get("current_value"))
    if not current_value:
        return None
    entries = _parse_people_involved_update_entries(current_value)
    if len(entries) != 1:
        return None
    entry = entries[0]
    return entry if isinstance(entry, dict) else None


def _is_chip_people_remove_turn(
    *,
    latest_message: str,
    update: Optional[Dict[str, Any]],
) -> bool:
    chip_entry = _extract_chip_people_entry_from_update(update)
    if not chip_entry:
        return False
    message = _to_clean_string(latest_message) or ""
    return bool(_CHIP_PEOPLE_REMOVE_RE.search(message))


def _entry_matches_chip_person(
    entry: Dict[str, Any],
    chip_entry: Dict[str, Any],
) -> bool:
    chip_public_identifier = (
        _to_clean_string(chip_entry.get("public_identifier")) or ""
    ).lower()
    chip_id = (_to_clean_string(chip_entry.get("id")) or "").lower()
    entry_public_identifier = (
        _to_clean_string(entry.get("public_identifier")) or ""
    ).lower()
    entry_id = (_to_clean_string(entry.get("id")) or "").lower()

    if chip_public_identifier and entry_public_identifier:
        return entry_public_identifier == chip_public_identifier
    if chip_id and entry_id:
        return entry_id == chip_id

    chip_name = (_to_clean_string(chip_entry.get("name")) or "").lower()
    chip_company = (_to_clean_string(chip_entry.get("company")) or "").lower()
    entry_name = (_to_clean_string(entry.get("name")) or "").lower()
    entry_company = (_to_clean_string(entry.get("company")) or "").lower()
    if not chip_name or chip_name != entry_name:
        return False
    if chip_company:
        return chip_company == entry_company
    return True


def _remove_single_chip_person(
    entries: List[Dict[str, Any]],
    chip_entry: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Remove at most one list entry that matches the selected UI chip identity."""
    filtered: List[Dict[str, Any]] = []
    removed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not removed and _entry_matches_chip_person(entry, chip_entry):
            removed = True
            continue
        filtered.append(entry)
    return filtered


def _normalize_people_remove_target_specs(
    raw_targets: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(raw_targets, list):
        return []

    specs: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for raw in raw_targets:
        if isinstance(raw, str):
            name = _to_clean_string(raw)
            if not name:
                continue
            spec = {
                "name": name,
                "company": None,
                "id": None,
                "public_identifier": None,
            }
        elif isinstance(raw, dict):
            name = _to_clean_string(raw.get("name"))
            if not name:
                continue
            spec = {
                "name": name,
                "company": _to_clean_string(raw.get("company")) or None,
                "id": _to_clean_string(raw.get("id")) or None,
                "public_identifier": _to_clean_string(raw.get("public_identifier"))
                or None,
            }
        else:
            continue

        dedupe_key = (
            spec["name"].lower(),
            (spec["company"] or "").lower(),
            (spec["id"] or "").lower(),
            (spec["public_identifier"] or "").lower(),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        specs.append(spec)
    return specs


def _remove_people_entries_by_target_specs(
    entries: List[Dict[str, Any]],
    specs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not specs:
        return entries

    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if any(_entry_matches_chip_person(entry, spec) for spec in specs):
            continue
        filtered.append(entry)
    return filtered


def _filter_people_for_remove_action(
    *,
    entries: List[Dict[str, Any]],
    chip_people_remove: bool,
    chip_entry: Optional[Dict[str, Any]],
    people_remove_target_specs: List[Dict[str, Any]],
    legacy_remove_targets: Dict[str, set],
) -> List[Dict[str, Any]]:
    if chip_people_remove and chip_entry:
        filtered = _remove_single_chip_person(entries, chip_entry)
        _people_trace_log(
            "chip_people_remove_applied",
            before_count=len(entries),
            after_count=len(filtered),
            chip_entry=_people_trace_json(chip_entry),
        )
        return filtered

    if people_remove_target_specs:
        filtered = _remove_people_entries_by_target_specs(
            entries, people_remove_target_specs
        )
        _people_trace_log(
            "chat_people_remove_applied",
            before_count=len(entries),
            after_count=len(filtered),
            remove_target_specs=_people_trace_json(people_remove_target_specs),
        )
        if legacy_remove_targets:
            before_legacy = len(filtered)
            filtered = _remove_people_entries_by_targets(
                filtered, legacy_remove_targets
            )
            if len(filtered) != before_legacy:
                _people_trace_log(
                    "legacy_people_remove_targets_applied",
                    before_count=before_legacy,
                    after_count=len(filtered),
                    legacy_targets=_summarize_remove_targets_for_trace(
                        legacy_remove_targets
                    ),
                )
        return filtered

    return _remove_people_entries_by_targets(entries, legacy_remove_targets)


async def _resolve_people_involved_operation(
    *,
    latest_message: str,
    update: Optional[Dict[str, Any]],
    people_replace_targets: Dict[str, set],
    client_action: str,
    merged_draft: Dict[str, Any],
    chat_history: str,
    update_context: Optional[str],
) -> Tuple[
    Dict[str, Any],
    str,
    List[Any],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, set],
    bool,
    Optional[Dict[str, Any]],
]:
    chip_entry = _extract_chip_people_entry_from_update(update)
    chip_people_remove = _is_chip_people_remove_turn(
        latest_message=latest_message,
        update=update,
    )

    if chip_people_remove:
        profile_plan = {"action": "REMOVE", "remove_targets": [], "add_people": []}
        _people_trace_log(
            "chip_people_remove_forced",
            chip_entry=_people_trace_json(chip_entry),
        )
    elif client_action != "KEEP":
        _people_trace_log(
            "profile_planner_skipped",
            client_action=client_action,
            reason="client_field_not_keep",
        )
        profile_plan = {"action": "KEEP", "remove_targets": [], "add_people": []}
    else:
        profile_plan = await _plan_profile_operation(
            latest_message=latest_message,
            current_people_involved=_to_clean_string(
                merged_draft.get("people_involved")
            ),
            current_assignment_draft=merged_draft,
            chat_history=chat_history,
            update_context=update_context,
        )

    profile_action: str = profile_plan.get("action") or "KEEP"
    plan_remove_people: List[Any] = profile_plan.get("remove_targets") or []
    plan_remove_target_specs: List[Dict[str, Any]] = profile_plan.get(
        "remove_target_specs"
    ) or _normalize_people_remove_target_specs(plan_remove_people)
    if not chip_people_remove and profile_action == "REMOVE":
        plan_remove_target_specs = _augment_remove_target_specs_from_message(
            latest_message=latest_message,
            specs=plan_remove_target_specs,
            current_people_involved=_to_clean_string(
                merged_draft.get("people_involved")
            ),
        )
    plan_add_people: List[Dict[str, Any]] = profile_plan.get("add_people") or []

    if chip_people_remove:
        all_people_remove_targets = people_replace_targets
    else:
        all_people_remove_targets = people_replace_targets

    return (
        profile_plan,
        profile_action,
        plan_remove_people,
        plan_remove_target_specs,
        plan_add_people,
        all_people_remove_targets,
        chip_people_remove,
        chip_entry,
    )


def _remove_people_entries_by_targets(
    entries: List[Dict[str, Any]],
    targets: Dict[str, set],
) -> List[Dict[str, Any]]:
    if not targets:
        return entries
    target_names = targets.get("names") or set()
    target_public_identifiers = targets.get("public_identifiers") or set()
    target_ids = targets.get("ids") or set()
    if not target_names and not target_public_identifiers and not target_ids:
        return entries

    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = (_to_clean_string(entry.get("name")) or "").lower()
        public_identifier = (
            _to_clean_string(entry.get("public_identifier")) or ""
        ).lower()
        pid = (_to_clean_string(entry.get("id")) or "").lower()
        if (
            (name and name in target_names)
            or (public_identifier and public_identifier in target_public_identifiers)
            or (pid and pid in target_ids)
        ):
            continue
        filtered.append(entry)
    return filtered


def _build_people_remove_targets_from_names(names: List[str]) -> Dict[str, set]:
    """Convert a plain list of name strings into a remove-targets dict."""
    return {
        "names": {n.strip().lower() for n in names if n.strip()},
        "public_identifiers": set(),
        "ids": set(),
    }


def _merge_people_targets(
    t1: Dict[str, set],
    t2: Dict[str, set],
) -> Dict[str, set]:
    """Merge two people-target dicts (e.g. from UI chip and planner)."""
    return {
        "names": (t1.get("names") or set()) | (t2.get("names") or set()),
        "public_identifiers": (t1.get("public_identifiers") or set())
        | (t2.get("public_identifiers") or set()),
        "ids": (t1.get("ids") or set()) | (t2.get("ids") or set()),
    }


def _build_profile_lookup_request_from_plan(
    add_people: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Convert the planner's add_people list to the request dict expected by
    get_profile_suggestions_multi: {should_fetch, names, titles, companies, timeline}.
    """
    names: List[str] = []
    titles: List[str] = []
    companies: List[str] = []
    for person in add_people:
        if not isinstance(person, dict):
            continue
        name = _to_clean_string(person.get("name"))
        if not name:
            continue
        names.append(name)
        titles.append(_to_clean_string(person.get("title")) or "")
        companies.append(_to_clean_string(person.get("company")) or "")
    return {
        "should_fetch": bool(names),
        "names": names,
        "titles": titles,
        "companies": companies,
        "timeline": "current",
    }


_PEOPLE_TRACE_LOG_LIMIT = 1200


def _people_trace_json(value: Any, limit: int = _PEOPLE_TRACE_LOG_LIMIT) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _people_trace_log(message: str, **fields: Any) -> None:
    if not PEOPLE_TRACE_DEBUG:
        return
    parts = [message]
    for key, value in fields.items():
        if isinstance(value, set):
            value = sorted(value)
        if isinstance(value, (dict, list, tuple)):
            parts.append(f"{key}={_people_trace_json(value)}")
        else:
            parts.append(f"{key}={value!r}")
    logger.info("[people_trace] %s", " | ".join(parts))


def _summarize_map_person_response(response: Any) -> Dict[str, Any]:
    if response is None:
        return {"response_type": "none"}
    if isinstance(response, list):
        return {"response_type": "list", "len": len(response)}
    if isinstance(response, dict):
        profile = response.get("profile")
        summary: Dict[str, Any] = {
            "response_type": "dict",
            "top_keys": sorted(response.keys()),
            "has_profile": isinstance(profile, dict),
        }
        compact = _extract_compact_map_person_profile(response)
        if compact:
            summary["format"] = "compact_pid_to_id"
            summary["public_identifier"] = compact.get("public_identifier")
            summary["id"] = compact.get("id")
            return summary
        if isinstance(profile, dict):
            summary["profile_keys"] = sorted(profile.keys())
            summary["public_identifier"] = _to_clean_string(
                profile.get("public_identifier")
            )
            summary["full_name"] = _to_clean_string(
                profile.get("full_name") or profile.get("name")
            )
            summary["id"] = _to_clean_string(profile.get("id"))
        else:
            normalized = _normalize_map_person_profile(response)
            if isinstance(normalized, dict):
                summary["format"] = "normalized_profile"
                summary["public_identifier"] = _to_clean_string(
                    normalized.get("public_identifier")
                )
                summary["full_name"] = _to_clean_string(
                    normalized.get("full_name") or normalized.get("name")
                )
                summary["id"] = _to_clean_string(normalized.get("id"))
        return summary
    return {"response_type": type(response).__name__}


def _summarize_people_entries_for_trace(
    people_involved: Optional[str],
) -> List[Dict[str, Optional[str]]]:
    entries = _normalize_people_involved_entries(people_involved)
    return [
        {
            "name": _to_clean_string(entry.get("name")),
            "company": _to_clean_string(entry.get("company")),
            "id": _to_clean_string(entry.get("id")),
            "public_identifier": _to_clean_string(entry.get("public_identifier")),
        }
        for entry in entries
        if isinstance(entry, dict)
    ]


def _summarize_remove_targets_for_trace(
    targets: Optional[Dict[str, set]],
) -> Dict[str, List[str]]:
    if not targets:
        return {"names": [], "public_identifiers": [], "ids": []}
    return {
        "names": sorted(targets.get("names") or []),
        "public_identifiers": sorted(targets.get("public_identifiers") or []),
        "ids": sorted(targets.get("ids") or []),
    }


def _log_people_planner_context(
    *,
    phase: str,
    client_action: str,
    profile_action: str,
    profile_plan: Dict[str, Any],
    people_replace_targets: Dict[str, set],
    all_people_remove_targets: Dict[str, set],
    existing_draft_people: Optional[str],
    merged_draft_people: Optional[str],
) -> None:
    _people_trace_log(
        f"planner_context phase={phase}",
        client_action=client_action,
        profile_action=profile_action,
        profile_plan=profile_plan,
        ui_replace_targets=_summarize_remove_targets_for_trace(people_replace_targets),
        all_remove_targets=_summarize_remove_targets_for_trace(
            all_people_remove_targets
        ),
        existing_draft_entries=_summarize_people_entries_for_trace(
            existing_draft_people
        ),
        merged_draft_entries=_summarize_people_entries_for_trace(merged_draft_people),
    )


def _company_from_map_person_payload(payload_used: Dict[str, Any]) -> Optional[str]:
    company = _to_clean_string(payload_used.get("company"))
    if company:
        return company
    payload_companies = payload_used.get("companies")
    if isinstance(payload_companies, list) and payload_companies:
        return _to_clean_string(payload_companies[0])
    return None


async def _append_profile_ids_to_people_involved(
    *,
    current_people_involved: Optional[str],
    people_involved_suggestions: Optional[List[Dict[str, Any]]],
    replace_targets: Optional[Dict[str, set]] = None,
    replace_target_specs: Optional[List[Dict[str, Any]]] = None,
    es_client: Optional[AsyncElasticsearch] = None,
) -> Optional[str]:
    base_entries = _normalize_people_involved_entries(current_people_involved)
    _people_trace_log(
        "append_people_start",
        current_people_involved=current_people_involved,
        base_entries=_summarize_people_entries_for_trace(current_people_involved),
        suggestions_count=(
            len(people_involved_suggestions)
            if isinstance(people_involved_suggestions, list)
            else 0
        ),
        replace_targets=_summarize_remove_targets_for_trace(replace_targets),
        has_es_client=es_client is not None,
    )

    if not isinstance(people_involved_suggestions, list):
        if not base_entries:
            _people_trace_log(
                "append_people_end", result=None, reason="no_suggestions_no_base"
            )
            return None
        result = json.dumps(_dedupe_people_entries(base_entries), ensure_ascii=False)
        _people_trace_log(
            "append_people_end",
            result_entries=_summarize_people_entries_for_trace(result),
            reason="no_suggestions_preserved_base",
        )
        return result

    new_entries: List[Dict[str, Any]] = []
    suggestion_items: List[Tuple[int, Dict[str, Any]]] = []
    for item_idx, item in enumerate(people_involved_suggestions, start=1):
        if not isinstance(item, dict):
            _people_trace_log(
                "append_people_item_skipped",
                item_idx=item_idx,
                reason="item_not_dict",
                item_type=type(item).__name__,
            )
            continue
        suggestion_items.append((item_idx, item))

    if es_client is not None and suggestion_items:
        es_lookup_targets: List[Tuple[int, Dict[str, Any], str, Optional[str]]] = []
        for item_idx, item in suggestion_items:
            first_profile = _extract_first_profile_from_response(
                item.get("suggestions")
            )
            if not isinstance(first_profile, dict):
                continue
            profile_public_identifier = _to_clean_string(
                first_profile.get("public_identifier")
            )
            if not profile_public_identifier:
                continue
            es_lookup_targets.append(
                (
                    item_idx,
                    item,
                    profile_public_identifier,
                    _to_clean_string(first_profile.get("id")) or None,
                )
            )

        es_lookup_started = time.perf_counter()
        es_lookups = await asyncio.gather(
            *[
                _fetch_mapped_person_profile_from_es(
                    es_client=es_client,
                    public_identifier=profile_public_identifier,
                    mapped_es_id=mapped_es_id,
                )
                for _, _, profile_public_identifier, mapped_es_id in es_lookup_targets
            ]
        )
        if LIVE_PREVIEW_TIMING_DEBUG:
            logger.info(
                "[live_preview_timing] label=append_people phase=es_lookup_batch count=%d phase_ms=%.1f",
                len(es_lookup_targets),
                (time.perf_counter() - es_lookup_started) * 1000.0,
            )

        es_lookup_by_item_idx = {
            item_idx: es_lookup
            for (item_idx, _, _, _), es_lookup in zip(es_lookup_targets, es_lookups)
        }

        for item_idx, item in suggestion_items:
            entry = await _build_people_entry_from_suggestion_item(
                item_idx=item_idx,
                item=item,
                es_client=es_client,
                es_lookup=es_lookup_by_item_idx.get(item_idx),
            )
            if entry:
                new_entries.append(entry)
                _people_trace_log(
                    "append_people_item_added",
                    item_idx=item_idx,
                    entry=entry,
                )
    else:
        for item_idx, item in suggestion_items:
            entry = await _build_people_entry_from_suggestion_item(
                item_idx=item_idx,
                item=item,
                es_client=es_client,
            )
            if entry:
                new_entries.append(entry)
                _people_trace_log(
                    "append_people_item_added",
                    item_idx=item_idx,
                    entry=entry,
                )

    base_before_replace = list(base_entries)
    # In people update flow, replace targets only when we have new verified entries to add.
    if (replace_targets or replace_target_specs) and new_entries:
        if replace_target_specs:
            base_entries = _remove_people_entries_by_target_specs(
                base_entries, replace_target_specs
            )
        if replace_targets:
            base_entries = _remove_people_entries_by_targets(
                base_entries, replace_targets
            )
        _people_trace_log(
            "append_people_replace_targets_applied",
            base_before_replace=_summarize_people_entries_for_trace(
                json.dumps(base_before_replace, ensure_ascii=False)
            ),
            base_after_replace=_summarize_people_entries_for_trace(
                json.dumps(base_entries, ensure_ascii=False)
            ),
            replace_target_specs=_people_trace_json(replace_target_specs or []),
            replace_targets=_summarize_remove_targets_for_trace(replace_targets),
            new_entries_count=len(new_entries),
        )
    elif (replace_targets or replace_target_specs) and not new_entries:
        _people_trace_log(
            "append_people_replace_targets_skipped",
            reason="no_new_entries_to_trigger_replace",
            replace_target_specs=_people_trace_json(replace_target_specs or []),
            replace_targets=_summarize_remove_targets_for_trace(replace_targets),
        )

    base_entries.extend(new_entries)

    deduped = _dedupe_people_entries(base_entries)
    if not deduped:
        _people_trace_log("append_people_end", result=None, reason="deduped_empty")
        return None
    result = json.dumps(deduped, ensure_ascii=False)
    _people_trace_log(
        "append_people_end",
        result_entries=_summarize_people_entries_for_trace(result),
        new_entries_count=len(new_entries),
        total_entries=len(deduped),
    )
    return result


async def _decide_profile_suggestion_request(
    *,
    latest_message: str,
    people_involved: Optional[str],
    chat_history: str,
    update_context: Optional[str],
) -> Dict[str, Any]:
    prompt = PROFILE_SUGGESTION_DECIDER_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        people_involved=people_involved or "N/A",
        update_context=update_context or "N/A",
        chat_history=chat_history,
    )
    response = await invoke(
        model="openai/gpt-4.1",
        temperature=0.0,
        fallbacks=["openai/gpt-4o-mini"],
        messages=[
            {"role": "system", "content": PROFILE_SUGGESTION_DECIDER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    parsed = _safe_json_loads_from_text(str(response))
    if not isinstance(parsed, dict):
        return {
            "should_fetch": False,
            "names": [],
            "titles": [],
            "companies": [],
            "timeline": "current",
        }
    return {
        "should_fetch": bool(parsed.get("should_fetch")),
        "names": [s for s in (parsed.get("names") or []) if _to_clean_string(s)],
        "titles": [s for s in (parsed.get("titles") or []) if _to_clean_string(s)],
        "companies": [
            s for s in (parsed.get("companies") or []) if _to_clean_string(s)
        ],
        "timeline": _to_clean_string(parsed.get("timeline")) or "current",
    }


def _log_profile_decider_result(decider_request: Dict[str, Any]) -> None:
    _people_trace_log("profile_suggestion_decider_result", request=decider_request)


def _build_people_mapping_prompt_context(decider_request: Dict[str, Any]) -> str:
    if not decider_request.get("should_fetch"):
        return "N/A"
    names = [
        _to_clean_string(n)
        for n in decider_request.get("names") or []
        if _to_clean_string(n)
    ]
    if not names:
        return "N/A"
    names_line = ", ".join(names)
    return (
        "BACKEND PEOPLE MAPPING (AUTHORITATIVE — this turn):\n"
        f"The following people were named and WILL be verified/mapped automatically: {names_line}.\n"
        "MANDATORY RULES FOR THIS TURN:\n"
        "- people_involved is NOT missing — do NOT ask for it in any form.\n"
        "- Do NOT ask the user to list, confirm, or re-state these names or their companies.\n"
        '- Do NOT ask about "your team", "your side", or "client stakeholders" when these candidates were already named.\n'
        "- Talent pool / candidate / external-pool names count as people_involved for this product.\n"
        "- Do NOT thank, congratulate, or restate the assignment in assistant_message.\n"
        "- Do NOT say people will be mapped or verified in the background; a separate outcome line handles results.\n"
        "- assistant_message for this turn = Markdown bullets for remaining missing fields ONLY. No intro. No outro. No client-added confirmation.\n"
        "- Never include people_involved in extracted_fields."
    )


async def _prepare_pre_llm_people_mapping(
    *,
    latest_message: str,
    people_involved: Optional[str],
    chat_history: str,
    update_context: Optional[str],
) -> Tuple[str, Optional[Dict[str, Any]], bool]:
    decider_request = await _decide_profile_suggestion_request(
        latest_message=latest_message,
        people_involved=people_involved,
        chat_history=chat_history,
        update_context=update_context,
    )
    decider_request = _augment_profile_request_from_people_involved(
        request=decider_request,
        people_involved_value=people_involved,
    )
    _people_trace_log("pre_llm_people_mapping", request=decider_request)
    prompt_context = _build_people_mapping_prompt_context(decider_request)
    names = [
        _to_clean_string(n)
        for n in decider_request.get("names") or []
        if _to_clean_string(n)
    ]
    suppress_stream_deltas = bool(decider_request.get("should_fetch") and names)
    cached_request = dict(decider_request) if suppress_stream_deltas else None
    return prompt_context, cached_request, suppress_stream_deltas


def _trim_text_for_stream_ack_context(text: str, *, limit: int = 1200) -> str:
    clean = _to_clean_string(text)
    if not clean:
        return "none"
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "...(truncated)"


async def _generate_stream_interim_ack_message(
    *,
    latest_message: str,
    update_context: str = "N/A",
    chat_history: str = "",
) -> Optional[str]:
    user_prompt = STREAM_INTERIM_ACK_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        update_context=_trim_text_for_stream_ack_context(update_context),
        chat_history=_trim_text_for_stream_ack_context(chat_history),
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1-mini",
            temperature=0.2,
            fallbacks=["openai/gpt-4o-mini", "openai/gpt-4.1"],
            messages=[
                {"role": "system", "content": STREAM_INTERIM_ACK_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        ack = _to_clean_string(response)
        return ack or None
    except Exception:
        logger.exception("Failed to generate stream interim ack message")
        return None


async def _append_people_not_found_note(
    *,
    assistant_message: str,
    latest_message: str,
    requested_names: List[str],
) -> str:
    """
    Use a small LLM to generate a short, natural note when
    profile suggestions for people_involved fail to resolve.
    """
    clean_names = [_to_clean_string(n) for n in requested_names if _to_clean_string(n)]
    if not clean_names:
        return assistant_message

    names_str = ", ".join(clean_names)
    user_prompt = PEOPLE_NOT_FOUND_NOTE_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        requested_names=names_str,
        current_reply=assistant_message,
    )
    response = await invoke(
        model="openai/gpt-4.1-mini",
        temperature=0.2,
        fallbacks=["openai/gpt-4.1", "openai/gpt-4o-mini"],
        messages=[
            {"role": "system", "content": PEOPLE_NOT_FOUND_NOTE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    note = _to_clean_string(response)
    if not note:
        return assistant_message

    # Ensure there is a line break before appending the note when needed.
    if assistant_message and not assistant_message.endswith("\n"):
        return assistant_message + "\n" + note
    return assistant_message + note


async def _decide_company_mapping_request(
    *,
    latest_message: str,
    current_client: Optional[str],
    chat_history: str,
    update_context: Optional[str],
) -> Dict[str, Any]:
    prompt = COMPANY_MAPPING_DECIDER_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        current_client=current_client or "N/A",
        update_context=update_context or "N/A",
        chat_history=chat_history,
    )
    response = await invoke(
        model="openai/gpt-4.1",
        temperature=0.0,
        fallbacks=["openai/gpt-4o-mini"],
        messages=[
            {"role": "system", "content": COMPANY_MAPPING_DECIDER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    parsed = _safe_json_loads_from_text(str(response))
    if not isinstance(parsed, dict):
        return {"should_fetch": False, "companies": []}
    return {
        "should_fetch": bool(parsed.get("should_fetch")),
        "companies": [
            s for s in (parsed.get("companies") or []) if _to_clean_string(s)
        ],
    }


async def _decide_client_remove_request(
    *,
    latest_message: str,
    current_client: Optional[str],
    chat_history: str,
) -> Dict[str, Any]:
    """
    Detects whether the user wants to remove specific companies from the client field.
    Returns {should_remove: bool, targets: List[str]} where targets are the display
    names of companies to remove (matched against universal_name and display_name).
    """
    prompt = CLIENT_REMOVE_DECIDER_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        current_client=current_client or "N/A",
        chat_history=chat_history,
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1",
            temperature=0.0,
            fallbacks=["openai/gpt-4o-mini"],
            messages=[
                {"role": "system", "content": CLIENT_REMOVE_DECIDER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return {"should_remove": False, "targets": []}
        return {
            "should_remove": bool(parsed.get("should_remove")),
            "targets": [
                s for s in (parsed.get("targets") or []) if _to_clean_string(s)
            ],
        }
    except Exception:
        logger.exception("Failed to decide client remove request")
        return {"should_remove": False, "targets": []}


async def _plan_client_operation(
    *,
    latest_message: str,
    current_client: Optional[str],
    chat_history: str,
    update_context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Central planner that determines the single intended operation on the client field.

    Returns:
        {
            "action": "ADD" | "REMOVE" | "REPLACE" | "CLEAR" | "KEEP",
            "remove_targets": List[str],   # company names/aliases to remove
            "add_companies":  List[str],   # company names to look up and add
        }

    This replaces the pair of _decide_company_mapping_request +
    _decide_client_remove_request with one authoritative call, eliminating
    the race condition where both deciders could fire and produce conflicting
    instructions (e.g. remove intent + accidental add from mapping).
    """
    prompt = CLIENT_OPERATION_PLANNER_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        current_client=current_client or "N/A",
        update_context=update_context or "N/A",
        chat_history=chat_history,
    )
    try:
        response = await invoke(
            model="openai/gpt-5.1",
            temperature=0.0,
            fallbacks=["openai/gpt-4.1", "openai/gpt-4o-mini"],
            messages=[
                {
                    "role": "system",
                    "content": CLIENT_OPERATION_PLANNER_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return {"action": "KEEP", "remove_targets": [], "add_companies": []}
        action = _to_clean_string(parsed.get("action")) or "KEEP"
        if action not in ("ADD", "REMOVE", "REPLACE", "CLEAR", "KEEP"):
            action = "KEEP"
        return {
            "action": action,
            "remove_targets": [
                s for s in (parsed.get("remove_targets") or []) if _to_clean_string(s)
            ],
            "add_companies": [
                s for s in (parsed.get("add_companies") or []) if _to_clean_string(s)
            ],
        }
    except Exception:
        logger.exception("Failed to plan client operation")
        return {"action": "KEEP", "remove_targets": [], "add_companies": []}


async def _plan_profile_operation(
    *,
    latest_message: str,
    current_people_involved: Optional[str],
    current_assignment_draft: Optional[Dict[str, Any]],
    chat_history: str,
    update_context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Central planner that determines the ONE intended operation on the
    people_involved field.

    Returns:
        {
            "action": "ADD" | "REMOVE" | "REPLACE" | "CLEAR" | "KEEP",
            "remove_targets": List[str | Dict],              # planner output
            "remove_target_specs": List[{name, company, ...}],
            "add_people":     List[{name, title, company}],  # people to look up
        }

    Mirrors _plan_client_operation for the people field.  Replaces the old
    _decide_profile_suggestion_request + _augment_profile_request_from_people_involved
    for explicit ADD / REMOVE / REPLACE / CLEAR turns, while preserving the
    background-enrichment path for KEEP turns.
    """
    assignment_context_keys = [
        "assignment_name",
        "assignment_type",
        "client",
        "industry",
        "mandate",
        "client_context",
        "outcome",
        "business_problem",
        "leadership_need",
        "decision_context",
    ]
    assignment_context = {
        key: current_assignment_draft.get(key)
        for key in assignment_context_keys
        if isinstance(current_assignment_draft, dict)
        and _to_clean_string(current_assignment_draft.get(key))
    }
    prompt = PROFILE_OPERATION_PLANNER_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        current_people_involved=current_people_involved or "N/A",
        assignment_context=(
            json.dumps(assignment_context, ensure_ascii=False, indent=2)
            if assignment_context
            else "N/A"
        ),
        update_context=update_context or "N/A",
        chat_history=chat_history,
    )
    try:
        response = await invoke(
            model="openai/gpt-5.1",
            temperature=0.0,
            fallbacks=["openai/gpt-4.1", "openai/gpt-4o-mini"],
            messages=[
                {
                    "role": "system",
                    "content": PROFILE_OPERATION_PLANNER_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return {"action": "KEEP", "remove_targets": [], "add_people": []}
        action = _to_clean_string(parsed.get("action")) or "KEEP"
        if action not in ("ADD", "REMOVE", "REPLACE", "CLEAR", "KEEP"):
            action = "KEEP"
        add_people = [
            p
            for p in (parsed.get("add_people") or [])
            if isinstance(p, dict) and _to_clean_string(p.get("name"))
        ]
        raw_remove_targets = parsed.get("remove_targets") or []
        remove_target_specs = _normalize_people_remove_target_specs(raw_remove_targets)
        plan = {
            "action": action,
            "remove_targets": raw_remove_targets,
            "remove_target_specs": remove_target_specs,
            "add_people": add_people,
        }
        _people_trace_log("profile_operation_planner_result", plan=plan)
        return plan
    except Exception:
        logger.exception("Failed to plan profile operation")
        return {"action": "KEEP", "remove_targets": [], "add_people": []}


async def _decide_new_assignment_request(
    *,
    latest_message: str,
    current_draft: Dict[str, Any],
    chat_history: str,
    update_context: Optional[str],
    locked_to_entity_id: bool,
    entity_id: Optional[str],
) -> Dict[str, Any]:
    prompt = NEW_ASSIGNMENT_DECIDER_USER_PROMPT_TEMPLATE.format(
        locked_to_entity_id="true" if locked_to_entity_id else "false",
        entity_id=_to_clean_string(entity_id) or "N/A",
        latest_message=latest_message,
        current_draft_json=json.dumps(
            current_draft or {}, ensure_ascii=False, indent=2
        ),
        update_context=update_context or "N/A",
        chat_history=chat_history,
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1",
            temperature=0.0,
            fallbacks=["openai/gpt-4o-mini"],
            messages=[
                {"role": "system", "content": NEW_ASSIGNMENT_DECIDER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return {"should_start_new_assignment": False, "reason": ""}
        return {
            "should_start_new_assignment": bool(
                parsed.get("should_start_new_assignment")
            ),
            "reason": _to_clean_string(parsed.get("reason")) or "",
        }
    except Exception:
        logger.exception("Failed to decide whether to start a new assignment")
        return {"should_start_new_assignment": False, "reason": ""}


_SAVE_INTENT_RE = re.compile(
    r"\b(save(?:\s+it|\s+as\s+is|\s+now|\s+this|\s+without(?:\s+\w+)?)?|"
    r"finalize|submit|finish(?:\s+this|\s+it)?|"
    r"complete\s+it|looks\s+good|go\s+ahead|no\s+more\s+changes)\b",
    re.IGNORECASE,
)
_SKIP_REMAINING_IN_MESSAGE_RE = re.compile(
    r"\b("
    r"save\s+as\s+is|save\s+without|leave\s+(?:it\s+)?blank|skip\s+it|"
    r"nope\s+just\s+save|don'?t\s+want\s+to\s+add|no\s+enrichment|"
    r"without\s+primary\s+insight|intentionally\s+left\s+blank|"
    r"do\s+not\s+want\s+to\s+add|don'?t\s+want\s+to\s+add\s+(?:anything|one|further)"
    r")\b",
    re.IGNORECASE,
)
_FIELD_EDIT_WITHOUT_SAVE_RE = re.compile(
    r"\b(remove|clear|delete|wipe|reset|leave\s+(?:it\s+)?blank)\b",
    re.IGNORECASE,
)
_BULK_FIELD_CLEAR_RE = re.compile(
    r"\b("
    r"clear\s+all|remove\s+all|wipe\s+everything|reset\s+(?:this\s+)?assignment|"
    r"all\s+fields|everything"
    r")\b",
    re.IGNORECASE,
)
_FIELD_PHRASE_TO_KEY: List[Tuple[str, str]] = sorted(
    [
        ("primary insight", "primary_insight"),
        ("main takeaway", "primary_insight"),
        ("key insight", "primary_insight"),
        ("people involved", "people_involved"),
        ("business problem", "business_problem"),
        ("leadership need", "leadership_need"),
        ("core problem solved", "core_problem_solved"),
        ("assignment type", "assignment_type"),
        ("assignment name", "assignment_name"),
        ("client context", "client_context"),
        ("completion time", "completion_time"),
        ("win reason", "win_reason"),
        ("speed advantage", "speed_advantage"),
        ("trust factor", "trust_factor"),
        ("reusable proof point", "reusable_proof_point"),
        ("leadership angle", "leadership_angle"),
        ("reusable content angle", "reusable_content_angle"),
        ("future trigger relevance", "future_trigger_relevance"),
        ("decision context", "decision_context"),
        ("outcome", "outcome"),
        ("mandate", "mandate"),
        ("client", "client"),
        ("industry", "industry"),
    ],
    key=lambda item: len(item[0]),
    reverse=True,
)


def _is_bulk_field_clear_message(latest_message: str) -> bool:
    return bool(_BULK_FIELD_CLEAR_RE.search(_to_clean_string(latest_message) or ""))


def _is_targeted_field_edit_message(latest_message: str) -> bool:
    message = _to_clean_string(latest_message) or ""
    if not message:
        return False
    if _is_bulk_field_clear_message(message):
        return False
    return bool(
        _FIELD_EDIT_WITHOUT_SAVE_RE.search(message)
        and not _SAVE_INTENT_RE.search(message)
    )


def _infer_field_edit_target_keys(latest_message: str) -> List[str]:
    message = (_to_clean_string(latest_message) or "").lower()
    if not message or _is_bulk_field_clear_message(message):
        return []
    found: List[str] = []
    for phrase, key in _FIELD_PHRASE_TO_KEY:
        if phrase in message and key not in found:
            found.append(key)
    return found


def _infer_field_edit_target_labels(latest_message: str) -> List[str]:
    labels: List[str] = []
    for key in _infer_field_edit_target_keys(latest_message):
        label = ASSIGNMENT_FIELD_LABELS.get(
            key
        ) or REQUIRED_ASSIGNMENT_FIELD_LABELS.get(key)
        if label and label not in labels:
            labels.append(label)
    return labels


def _sanitize_field_edit_extracted_fields(
    *,
    latest_message: str,
    extracted_fields: Dict[str, Any],
    update: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    On targeted remove/clear turns, prevent the main LLM from wiping unrelated fields.
    """
    if not isinstance(extracted_fields, dict):
        return {}
    if not _is_targeted_field_edit_message(latest_message):
        return dict(extracted_fields)

    if isinstance(update, dict) and update.get("key"):
        update_key = _to_clean_string(update.get("key"))
        if update_key:
            return {update_key: extracted_fields.get(update_key)}

    target_keys = _infer_field_edit_target_keys(latest_message)
    if target_keys:
        sanitized: Dict[str, Any] = {}
        for key in target_keys:
            if key in extracted_fields:
                sanitized[key] = extracted_fields[key]
            else:
                sanitized[key] = ""
        return sanitized

    cleared_keys = [
        key
        for key, value in extracted_fields.items()
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if len(cleared_keys) > 1:
        return {}
    return dict(extracted_fields)


def _post_process_save_decision(
    *,
    latest_message: str,
    save_decision: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize save decider output — trust the LLM, do not override with regex."""
    should_save = bool(save_decision.get("should_save"))
    explicit_skip = bool(save_decision.get("explicit_skip_remaining"))
    if explicit_skip and not should_save:
        explicit_skip = False
    return {
        "should_save": should_save,
        "explicit_skip_remaining": explicit_skip,
        "reason": _to_clean_string(save_decision.get("reason")) or "",
    }


async def _decide_save_assignment_request(
    *,
    latest_message: str,
    current_draft: Dict[str, Any],
    missing_fields: List[str],
    chat_history: str,
    update_context: Optional[str],
    prior_blocked_save_count: int = 0,
) -> Dict[str, Any]:
    prompt = SAVE_ASSIGNMENT_DECIDER_USER_PROMPT_TEMPLATE.format(
        latest_message=latest_message,
        prior_blocked_save_count=prior_blocked_save_count,
        current_draft_json=json.dumps(
            current_draft or {}, ensure_ascii=False, indent=2
        ),
        missing_fields_json=json.dumps(missing_fields or [], ensure_ascii=False),
        update_context=update_context or "N/A",
        chat_history=chat_history,
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1",
            temperature=0.0,
            fallbacks=["openai/gpt-4o-mini"],
            messages=[
                {"role": "system", "content": SAVE_ASSIGNMENT_DECIDER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = _safe_json_loads_from_text(str(response))
        if not isinstance(parsed, dict):
            return {
                "should_save": False,
                "explicit_skip_remaining": False,
                "reason": "",
            }
        raw = {
            "should_save": bool(parsed.get("should_save")),
            "explicit_skip_remaining": bool(parsed.get("explicit_skip_remaining")),
            "reason": _to_clean_string(parsed.get("reason")) or "",
        }
        return _post_process_save_decision(
            latest_message=latest_message,
            save_decision=raw,
        )
    except Exception:
        logger.exception("Failed to decide whether to save assignment")
        return {
            "should_save": False,
            "explicit_skip_remaining": False,
            "reason": "",
        }


def _extract_client_entries_from_company_mapping(
    company_mapping_results: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if not isinstance(company_mapping_results, list):
        return []
    entries: List[Dict[str, Any]] = []
    seen = set()
    for item in company_mapping_results:
        if not isinstance(item, dict):
            continue
        entry = _extract_client_entry_from_mapping_response(item.get("mapping"))
        if not isinstance(entry, dict):
            continue
        universal_name = _to_clean_string(entry.get("universal_name"))
        if not universal_name:
            continue
        key = universal_name.lower()
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    return entries


async def process_assignment_chat_turn(
    *,
    entity_urn: str,
    message: str,
    update: Optional[Dict[str, Any]] = None,
    feature_key: str = FEATURE_KEY_ASSIGNMENT_CREATION,
    es_client: AsyncElasticsearch,
    client_context: Optional[Dict[str, Any]] = None,
    external_entity_id: Optional[str] = None,
) -> Dict[str, Any]:
    external_entity_id = _to_clean_string(external_entity_id)
    if not external_entity_id:
        raise ValueError(
            "entity_id is required for assignment chat; backend context id generation is disabled."
        )

    session = await get_or_create_single_session(
        entity_urn=entity_urn,
        feature_key=feature_key,
        title="Assignment Intake",
    )
    session_id = str(session.get("id"))
    update_context = "N/A"
    if isinstance(update, dict) and update:
        update_context = json.dumps(update, ensure_ascii=False)

    if external_entity_id:
        # Use external_entity_id as the logical "assignment_feature_key" for this chat session,
        # but persist the context row with feature_key fixed to the high-level feature label.
        assignment_feature_key = external_entity_id
        recent_messages = await fetch_recent_chat_messages(
            session_id=session_id,
            limit=10,
            assignment_feature_key=assignment_feature_key,
        )
        existing_context = await fetch_assignment_context_by_id(
            entity_urn=entity_urn,
            context_id=assignment_feature_key,
        )
        existing_draft = (
            dict(existing_context.get("assignment_draft") or {})
            if existing_context
            else _empty_assignment_draft()
        )
        formatted_chat_history = _format_chat_history(recent_messages)
        is_first_substantive_turn = not _has_prior_user_turn(recent_messages)
        draft_has_no_required_data = _completion_score(existing_draft) == 0.0
        # In edit mode (external_entity_id is provided), we NEVER allow starting a new
        # assignment from this chat. However, we still use the decider to detect when
        # the user is trying to do so, so we can respond explicitly.
        if is_first_substantive_turn and draft_has_no_required_data:
            # First user turn with an effectively empty draft should be treated as
            # normal assignment capture, not as a forbidden "new assignment" switch.
            _new_assignment_decision = {
                "should_start_new_assignment": False,
                "reason": "first_turn_empty_draft",
            }
        else:
            _new_assignment_decision = await _decide_new_assignment_request(
                latest_message=message,
                current_draft=existing_draft,
                chat_history=formatted_chat_history,
                update_context=update_context,
                locked_to_entity_id=True,
                entity_id=assignment_feature_key,
            )
        is_new_assignment = False
    else:
        raise ValueError(
            "entity_id is required for assignment chat; backend context id generation is disabled."
        )

    _, profile = await fetch_profile_by_entity_urn(
        entity_urn=entity_urn,
        es_client=es_client,
    )

    message_metadata = {
        **(client_context or {}),
        "assignment_feature_key": assignment_feature_key,
    }
    await insert_chat_message(
        session_id=session_id,
        entity_urn=entity_urn,
        feature_key=feature_key,
        role="user",
        content=message,
        metadata=message_metadata,
    )

    # Edit-only guardrail: when external_entity_id is provided and the decider says
    # this turn is trying to start a new/separate assignment, do NOT mutate the draft.
    # Instead, explain that this flow only edits the current assignment.
    if external_entity_id and _new_assignment_decision.get(
        "should_start_new_assignment"
    ):
        assistant_message = (
            "I can only help you customize this specific assignment in this view. "
            "To create a completely new assignment from scratch, please start a new assignment "
            "from the main BD workspace and then open its chat separately."
        )
        completion = _completion_score(existing_draft)
        missing_labels = _missing_field_labels(existing_draft)
        ready_to_save = len(missing_labels) == 0
        show_add_people_cta = _should_show_add_people_cta(missing_labels)

        await upsert_assignment_context(
            entity_urn=entity_urn,
            feature_key=feature_key,
            assignment_draft=existing_draft,
            completion_score=completion,
            last_user_message=message,
            context_id=assignment_feature_key,
        )

        await insert_chat_message(
            session_id=session_id,
            entity_urn=entity_urn,
            feature_key=feature_key,
            role="assistant",
            content=assistant_message,
            metadata={
                "ready_to_save": ready_to_save,
                "used_profile_suggestions": False,
                "used_company_mapping": False,
                "started_new_assignment": False,
                "save_requested": False,
                "assignment_feature_key": assignment_feature_key,
            },
        )

        return {
            "entity_urn": entity_urn,
            "session_id": session_id,
            "assignment_feature_key": assignment_feature_key,
            "assistant_message": assistant_message,
            "assignment_draft": existing_draft,
            "missing_fields": missing_labels,
            "completion_score": completion,
            "ready_to_save": ready_to_save,
            "show_add_people_cta": show_add_people_cta,
            "started_new_assignment": False,
            "save_requested": False,
        }

    people_mapping_context, prefetched_decider_request, _ = (
        await _prepare_pre_llm_people_mapping(
            latest_message=message,
            people_involved=_to_clean_string(existing_draft.get("people_involved")),
            chat_history=formatted_chat_history,
            update_context=update_context,
        )
    )

    user_prompt = ASSIGNMENT_CHAT_USER_PROMPT_TEMPLATE.format(
        entity_urn=entity_urn,
        profile_context=_compact_profile_context(profile),
        locked_to_entity_id="true",
        entity_id=assignment_feature_key,
        current_draft_json=json.dumps(existing_draft, ensure_ascii=False, indent=2),
        chat_history=formatted_chat_history,
        latest_message=message,
        update_context=update_context,
        people_mapping_context=people_mapping_context,
    )

    llm_response = await invoke(
        model="openai/gpt-4.1",
        temperature=0.2,
        fallbacks=["openai/gpt-4o", "anthropic/claude-sonnet-4-5-20250929"],
        messages=[
            {"role": "system", "content": ASSIGNMENT_CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    parsed = _safe_json_loads_from_text(str(llm_response))
    if not isinstance(parsed, dict):
        raise ValueError("Invalid LLM output; expected JSON object")

    assistant_message = _to_clean_string(parsed.get("assistant_message")) or (
        "Got it. I captured what I could. Please share a bit more detail so I can complete the assignment draft."
    )
    llm_ready_to_save = bool(parsed.get("ready_to_save"))
    extracted_fields = parsed.get("extracted_fields") or {}
    if not isinstance(extracted_fields, dict):
        extracted_fields = {}
    # When the frontend sends an explicit update, restrict LLM field updates
    # to ONLY that key so other fields are not unintentionally changed.
    if isinstance(update, dict) and update.get("key"):
        update_key = _to_clean_string(update.get("key"))
        if update_key:
            extracted_fields = {update_key: extracted_fields.get(update_key)}
    extracted_fields = _sanitize_field_edit_extracted_fields(
        latest_message=message,
        extracted_fields=extracted_fields,
        update=update,
    )
    # Disallow non-canonical freeform people key from LLM output.
    extracted_fields.pop("people", None)
    # Do not trust direct people_involved extraction from chat LLM.
    # People are persisted only after profile-suggestion verification (id + public_identifier).
    extracted_fields.pop("people_involved", None)
    people_replace_targets = _extract_people_replace_targets_from_update(update)
    extracted_client_candidates = _extract_company_candidates_from_client_value(
        _to_clean_string(extracted_fields.get("client"))
    )
    client_replace_targets = _extract_client_replace_targets_from_update(update)

    merged_draft = _merge_assignment_draft(
        existing=existing_draft,
        extracted=extracted_fields,
    )
    completion = _completion_score(merged_draft)
    missing_labels = _missing_field_labels(merged_draft)
    # Big agent proposes ready_to_save; backend enforces required-field gating.
    ready_to_save = llm_ready_to_save and len(missing_labels) == 0

    # --- Client-field planner -------------------------------------------------
    # Run client planning before profile planning. If this turn is about the
    # client/company field, skip profile lookup entirely so company names are not
    # also sent through people search.
    client_plan = await _plan_client_operation(
        latest_message=message,
        current_client=_to_clean_string(merged_draft.get("client")),
        chat_history=formatted_chat_history,
        update_context=update_context,
    )
    client_action: str = client_plan.get("action") or "KEEP"
    plan_remove_targets: List[str] = client_plan.get("remove_targets") or []
    plan_add_companies: List[str] = client_plan.get("add_companies") or []
    if (
        client_action == "KEEP"
        and extracted_client_candidates
        and not _to_clean_string(existing_draft.get("client"))
    ):
        # The extractor found a client value. Treat it as a client add rather
        # than letting a conservative planner keep the client empty.
        client_action = "ADD"
        plan_add_companies = _merge_company_candidates(
            plan_add_companies,
            extracted_client_candidates,
        )

    # --- People-involved planner ----------------------------------------------
    # A single planner determines the intended operation (ADD / REMOVE / REPLACE
    # / CLEAR / KEEP) so that removal intents are never accidentally overridden
    # by a background profile lookup.
    (
        profile_plan,
        profile_action,
        plan_remove_people,
        plan_remove_target_specs,
        plan_add_people,
        all_people_remove_targets,
        chip_people_remove,
        chip_entry,
    ) = await _resolve_people_involved_operation(
        latest_message=message,
        update=update,
        people_replace_targets=people_replace_targets,
        client_action=client_action,
        merged_draft=merged_draft,
        chat_history=formatted_chat_history,
        update_context=update_context,
    )
    _log_people_planner_context(
        phase="before_people_action",
        client_action=client_action,
        profile_action=profile_action,
        profile_plan=profile_plan,
        people_replace_targets=people_replace_targets,
        all_people_remove_targets=all_people_remove_targets,
        existing_draft_people=_to_clean_string(existing_draft.get("people_involved")),
        merged_draft_people=_to_clean_string(merged_draft.get("people_involved")),
    )

    people_involved_suggestions = None
    company_mapping_results = None
    people_outcome_requested_names: Optional[List[str]] = None

    if profile_action == "CLEAR":
        merged_draft["people_involved"] = None
    elif profile_action == "REMOVE":
        # Pure removal — filter the existing list directly; no profile lookup.
        existing_people = _normalize_people_involved_entries(
            _to_clean_string(merged_draft.get("people_involved"))
        )
        filtered = _filter_people_for_remove_action(
            entries=existing_people,
            chip_people_remove=chip_people_remove,
            chip_entry=chip_entry,
            people_remove_target_specs=plan_remove_target_specs,
            legacy_remove_targets=all_people_remove_targets,
        )
        deduped = _dedupe_people_entries(filtered)
        merged_draft["people_involved"] = (
            json.dumps(deduped, ensure_ascii=False) if deduped else None
        )
    elif profile_action in ("ADD", "REPLACE"):
        # Look up new people from the planner's list, then merge into
        # the existing list.  For REPLACE, replace_targets strip the old
        # person once new verified profiles are confirmed.
        lookup_request = _build_profile_lookup_request_from_plan(plan_add_people)
        if lookup_request.get("names"):
            people_involved_suggestions = await get_profile_suggestions_multi(
                lookup_request
            )
        merged_people_involved = await _append_profile_ids_to_people_involved(
            current_people_involved=_to_clean_string(
                merged_draft.get("people_involved")
            ),
            people_involved_suggestions=people_involved_suggestions,
            replace_targets=(
                all_people_remove_targets if people_involved_suggestions else None
            ),
            replace_target_specs=(
                plan_remove_target_specs if people_involved_suggestions else None
            ),
            es_client=es_client,
        )
        if merged_people_involved:
            merged_draft["people_involved"] = merged_people_involved
        requested_names = [
            _to_clean_string(p.get("name"))
            for p in plan_add_people
            if _to_clean_string(p.get("name"))
        ]
        if requested_names:
            people_outcome_requested_names = requested_names
    else:
        # KEEP — run the existing background-enrichment path to resolve any
        # unresolved (id-less) people already in the list.
        if prefetched_decider_request is not None:
            decider_request = dict(prefetched_decider_request)
            _people_trace_log(
                "profile_suggestion_decider_reused",
                request=decider_request,
                source="pre_llm_prefetch",
            )
        else:
            decider_request = await _decide_profile_suggestion_request(
                latest_message=message,
                people_involved=_to_clean_string(merged_draft.get("people_involved")),
                chat_history=formatted_chat_history,
                update_context=update_context,
            )
            decider_request = _augment_profile_request_from_people_involved(
                request=decider_request,
                people_involved_value=_to_clean_string(
                    merged_draft.get("people_involved")
                ),
            )
            _log_profile_decider_result(decider_request)
        if decider_request.get("should_fetch"):
            people_involved_suggestions = await get_profile_suggestions_multi(
                decider_request
            )
        merged_people_involved = await _append_profile_ids_to_people_involved(
            current_people_involved=_to_clean_string(
                merged_draft.get("people_involved")
            ),
            people_involved_suggestions=people_involved_suggestions,
            replace_targets=(
                people_replace_targets if people_involved_suggestions else None
            ),
            es_client=es_client,
        )
        if merged_people_involved:
            merged_draft["people_involved"] = merged_people_involved
        if decider_request.get("should_fetch"):
            requested_names = [
                _to_clean_string(n)
                for n in decider_request.get("names") or []
                if _to_clean_string(n)
            ]
            if requested_names:
                people_outcome_requested_names = requested_names
    _people_trace_log(
        "people_action_complete",
        profile_action=profile_action,
        merged_draft_entries=_summarize_people_entries_for_trace(
            _to_clean_string(merged_draft.get("people_involved"))
        ),
    )
    # --------------------------------------------------------------------------

    client_entries: List[Dict[str, Any]] = []
    # Run company mapping only for ADD / REPLACE.  Merge planner's explicit
    # company list with any names the main LLM extracted as a fallback.
    company_mapping_results = None
    if client_action in ("ADD", "REPLACE"):
        company_mapping_targets = _merge_company_candidates(
            plan_add_companies,
            extracted_client_candidates,
        )
        if company_mapping_targets:
            company_mapping_results = await get_company_mapping_multi(
                companies=company_mapping_targets,
                user_query=message,
            )
    mapped_client_entries = _extract_client_entries_from_company_mapping(
        company_mapping_results
    )
    if mapped_client_entries:
        client_entries.extend(mapped_client_entries)
    elif client_action in ("ADD", "REPLACE"):
        company_mapping_targets = _merge_company_candidates(
            plan_add_companies,
            extracted_client_candidates,
        )
        client_entries.extend(
            _client_entries_from_unmapped_targets(company_mapping_targets)
        )

    # Combine UI chip replace-targets with planner remove-targets.
    all_remove_targets = list(
        dict.fromkeys((client_replace_targets or []) + (plan_remove_targets or []))
    )

    # Apply the plan.
    if client_action == "CLEAR":
        merged_draft["client"] = None
    elif client_action in ("REMOVE", "REPLACE") or all_remove_targets:
        # Remove targeted companies then append any new entries.
        merged_draft["client"] = _append_client_entries_to_client(
            current_client=_to_clean_string(existing_draft.get("client")),
            client_entries=client_entries,
            replace_targets=(all_remove_targets if not is_new_assignment else None),
        )
    elif client_action == "ADD" and client_entries:
        merged_draft["client"] = _append_client_entries_to_client(
            current_client=_to_clean_string(existing_draft.get("client")),
            client_entries=client_entries,
            replace_targets=None,
        )
        added_names = [
            _to_clean_string(entry.get("display_name") or entry.get("universal_name"))
            for entry in client_entries
            if isinstance(entry, dict)
            and _to_clean_string(
                entry.get("display_name") or entry.get("universal_name")
            )
        ]
        if added_names:
            assistant_message = await _append_dynamic_entity_update_note(
                assistant_message=assistant_message,
                latest_message=message,
                entity_type="client",
                outcome="added",
                targets=added_names,
            )
    elif client_action in ("ADD", "REPLACE") and plan_add_companies:
        merged_draft["client"] = _to_clean_string(existing_draft.get("client"))
        assistant_message = await _append_dynamic_entity_update_note(
            assistant_message=assistant_message,
            latest_message=message,
            entity_type="client",
            outcome="not_found",
            targets=plan_add_companies[:3],
        )
    else:
        # KEEP or no actionable change — preserve existing canonical value.
        merged_draft["client"] = _to_clean_string(existing_draft.get("client"))
    # --------------------------------------------------------------------------
    _finalize_people_fields(merged_draft)
    completion = _completion_score(merged_draft)
    missing_labels = _missing_field_labels(merged_draft)
    prior_blocked_save_count = _load_prior_save_blocked_count(
        session=session,
        recent_messages=recent_messages,
        assignment_feature_key=assignment_feature_key,
    )
    save_decision = await _decide_save_assignment_request(
        latest_message=message,
        current_draft=merged_draft,
        missing_fields=missing_labels,
        chat_history=formatted_chat_history,
        update_context=update_context,
        prior_blocked_save_count=prior_blocked_save_count,
    )
    save_outcome = _resolve_save_turn_outcome(
        draft=merged_draft,
        save_decision=save_decision,
        prior_blocked_save_count=prior_blocked_save_count,
    )
    save_requested = bool(save_outcome.get("save_requested"))
    explicit_skip_remaining = bool(save_outcome.get("explicit_skip_remaining"))
    auto_waived_on_retry = bool(save_outcome.get("auto_waived_on_retry"))
    ready_to_save = bool(save_outcome.get("ready_to_save"))
    missing_labels = list(save_outcome.get("missing_labels") or [])
    waived_labels = list(save_outcome.get("waived_labels") or [])
    show_add_people_cta = _should_show_add_people_cta(missing_labels)
    assistant_message = await _finalize_assistant_message(
        assistant_message=assistant_message,
        missing_fields=missing_labels,
        ready_to_save=ready_to_save,
        save_requested=save_requested,
        explicit_skip_remaining=explicit_skip_remaining,
        latest_message=message,
        prior_blocked_save_count=prior_blocked_save_count,
        auto_waived_on_retry=auto_waived_on_retry,
        waived_labels=waived_labels,
        people_outcome_requested_names=people_outcome_requested_names,
        after_people_involved=_to_clean_string(merged_draft.get("people_involved")),
    )

    await upsert_assignment_context(
        entity_urn=entity_urn,
        feature_key=feature_key,
        assignment_draft=merged_draft,
        completion_score=completion,
        last_user_message=message,
        context_id=assignment_feature_key,
    )

    await insert_chat_message(
        session_id=session_id,
        entity_urn=entity_urn,
        feature_key=feature_key,
        role="assistant",
        content=assistant_message,
        metadata={
            "ready_to_save": ready_to_save,
            "used_profile_suggestions": people_involved_suggestions is not None,
            "used_company_mapping": company_mapping_results is not None,
            "started_new_assignment": is_new_assignment,
            "save_requested": save_requested,
            "explicit_skip_remaining": explicit_skip_remaining,
            "auto_waived_on_retry": auto_waived_on_retry,
            "assignment_feature_key": assignment_feature_key,
        },
    )
    await _persist_save_retry_state(
        session=session,
        assignment_feature_key=assignment_feature_key,
        save_requested=save_requested,
        ready_to_save=ready_to_save,
    )

    return {
        "entity_urn": entity_urn,
        "session_id": session_id,
        "assignment_feature_key": assignment_feature_key,
        "assistant_message": assistant_message,
        "assignment_draft": merged_draft,
        "missing_fields": missing_labels,
        "completion_score": completion,
        "ready_to_save": ready_to_save,
        "show_add_people_cta": show_add_people_cta,
        "started_new_assignment": is_new_assignment,
        "save_requested": save_requested,
        "explicit_skip_remaining": explicit_skip_remaining,
    }


async def process_content_chat_turn(
    *,
    entity_urn: str,
    message: str,
    update: Optional[Dict[str, Any]] = None,
    feature_key: str = FEATURE_KEY_CONTENT_CHAT,
    es_client: AsyncElasticsearch,
    client_context: Optional[Dict[str, Any]] = None,
    external_entity_id: Optional[str] = None,
) -> Dict[str, Any]:
    external_entity_id = _to_clean_string(external_entity_id)
    if not external_entity_id:
        raise ValueError(
            "entity_id is required for content chat; backend context id generation is disabled."
        )

    session = await get_or_create_single_session(
        entity_urn=entity_urn,
        feature_key=feature_key,
        title="Content Intake",
    )
    session_id = str(session.get("id"))

    context_feature_key = external_entity_id

    existing_context = await fetch_assignment_context_by_id(
        entity_urn=entity_urn,
        context_id=context_feature_key,
    )
    existing_draft = (
        dict(existing_context.get("assignment_draft") or {})
        if existing_context
        else _empty_content_draft()
    )

    recent_messages = await fetch_recent_chat_messages(
        session_id=session_id,
        limit=10,
    )
    formatted_chat_history = _format_chat_history(recent_messages)

    _, profile = await fetch_profile_by_entity_urn(
        entity_urn=entity_urn,
        es_client=es_client,
    )

    update_context = (
        json.dumps(update, ensure_ascii=False)
        if isinstance(update, dict) and update
        else "N/A"
    )

    # Use LLM to detect primary link inside the free-form message.
    link_detection = await _detect_links_with_llm(message)
    primary_link = _to_clean_string(link_detection.get("primary_link"))
    all_links: List[str] = [
        _to_clean_string(l) for l in link_detection.get("links") or []
    ]
    all_links = [l for l in all_links if l]

    fetched_content_context: Dict[str, Any] = {}
    if primary_link:
        try:
            pegasus_response = await get_source_from_pegasus(
                params={"link": primary_link}
            )
        except Exception:
            logger.exception("Pegasus fetch failed for primary_link=%s", primary_link)
            pegasus_response = None

        html_text: Optional[str] = None
        if isinstance(pegasus_response, dict):
            html_text = _to_clean_string(pegasus_response.get("link_result"))

        title: Optional[str] = None
        snippet: Optional[str] = None
        source: Optional[str] = None

        if html_text:
            try:
                from bs4 import BeautifulSoup
                from urllib.parse import urlparse

                soup = BeautifulSoup(html_text, "html.parser")
                if soup.title and soup.title.string:
                    title = _to_clean_string(soup.title.string)

                # Prefer og:description or meta description
                og_desc = soup.find("meta", property="og:description")
                if og_desc and og_desc.get("content"):
                    snippet = _to_clean_string(og_desc["content"])
                if not snippet:
                    meta_desc = soup.find("meta", attrs={"name": "description"})
                    if meta_desc and meta_desc.get("content"):
                        snippet = _to_clean_string(meta_desc["content"])

                if not snippet:
                    # Fallback: first non-trivial paragraph
                    for p in soup.find_all("p"):
                        text = _to_clean_string(p.get_text())
                        if text and len(text.split()) >= 8:
                            snippet = text
                            break

                # Source domain from URL
                try:
                    parsed = urlparse(primary_link)
                    source = _to_clean_string(parsed.netloc)
                except Exception:
                    source = None
            except Exception:
                logger.exception(
                    "Failed to parse HTML for primary_link=%s", primary_link
                )

        fetched_content_context = {
            "primary_link": primary_link,
            "all_links": all_links,
            "source_domain": source,
            "html_title": title,
            "html_snippet": snippet,
        }

    message_metadata = {
        **(client_context or {}),
        "chat_type": "content",
        "update_context": update_context,
    }
    await insert_chat_message(
        session_id=session_id,
        entity_urn=entity_urn,
        feature_key=feature_key,
        role="user",
        content=message,
        metadata=message_metadata,
    )

    user_prompt = CONTENT_CHAT_USER_PROMPT_TEMPLATE.format(
        entity_urn=entity_urn,
        profile_context=_compact_profile_context(profile),
        current_content_json=json.dumps(
            existing_draft or {}, ensure_ascii=False, indent=2
        ),
        fetched_content_context=json.dumps(
            fetched_content_context or {}, ensure_ascii=False, indent=2
        ),
        chat_history=formatted_chat_history,
        latest_message=message,
        client_context=json.dumps(client_context or {}, ensure_ascii=False, indent=2),
    )

    llm_response = await invoke(
        model="openai/gpt-4.1",
        temperature=0.2,
        fallbacks=["openai/gpt-4o", "anthropic/claude-sonnet-4-5-20250929"],
        messages=[
            {"role": "system", "content": CONTENT_CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    parsed = _safe_json_loads_from_text(str(llm_response))
    if not isinstance(parsed, dict):
        parsed = {}

    assistant_message = _to_clean_string(parsed.get("assistant_message")) or (
        "Got it. I captured what I could from this content. Tell me more about how you want to use it so we can shape the triggers together."
    )

    preview = parsed.get("content_preview") or {}
    if not isinstance(preview, dict):
        preview = {}
    metadata = parsed.get("content_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    merged_draft: Dict[str, Optional[str]] = {
        k: _to_clean_string(v) for k, v in (_empty_content_draft()).items()
    }

    for source in (existing_draft or {}, preview, metadata):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            key_str = _to_clean_string(key)
            if not key_str:
                continue
            merged_draft[key_str] = _to_clean_string(value)

    total_fields = len(CONTENT_FIELD_KEYS)
    completion = _completion_score_content(merged_draft) if total_fields else 0.0

    await upsert_assignment_context(
        entity_urn=entity_urn,
        feature_key=FEATURE_KEY_CONTENT_CHAT,
        assignment_draft=merged_draft,
        completion_score=completion,
        last_user_message=message,
        context_id=context_feature_key,
    )

    await insert_chat_message(
        session_id=session_id,
        entity_urn=entity_urn,
        feature_key=feature_key,
        role="assistant",
        content=assistant_message,
        metadata={
            "chat_type": "content",
            "completion_score": completion,
        },
    )

    return {
        "entity_urn": entity_urn,
        "session_id": session_id,
        "assistant_message": assistant_message,
        "content_draft": merged_draft,
        "completion_score": completion,
    }


async def _consume_prefetched_map_person_task(
    task: Optional[asyncio.Task],
    *,
    reuse: bool,
) -> Optional[List[Dict[str, Any]]]:
    if task is None:
        return None
    if not reuse:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return None
    return await task


async def stream_assignment_chat_turn(
    *,
    entity_urn: str,
    message: str,
    update: Optional[Dict[str, Any]] = None,
    feature_key: str = FEATURE_KEY_ASSIGNMENT_CREATION,
    es_client: AsyncElasticsearch,
    client_context: Optional[Dict[str, Any]] = None,
    external_entity_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    timing = _LivePreviewTimingTracker()
    external_entity_id = _to_clean_string(external_entity_id)
    if not external_entity_id:
        raise ValueError(
            "entity_id is required for assignment chat stream; backend context id generation is disabled."
        )

    session = await get_or_create_single_session(
        entity_urn=entity_urn,
        feature_key=feature_key,
        title="Assignment Intake",
    )
    session_id = str(session.get("id"))
    update_context = "N/A"
    if isinstance(update, dict) and update:
        update_context = json.dumps(update, ensure_ascii=False)

    if external_entity_id:
        assignment_feature_key = external_entity_id
        recent_messages, existing_context, profile_result = await asyncio.gather(
            fetch_recent_chat_messages(
                session_id=session_id,
                limit=10,
                assignment_feature_key=assignment_feature_key,
            ),
            fetch_assignment_context_by_id(
                entity_urn=entity_urn,
                context_id=assignment_feature_key,
            ),
            fetch_profile_by_entity_urn(
                entity_urn=entity_urn,
                es_client=es_client,
            ),
        )
        _, profile = profile_result
        existing_draft = (
            dict(existing_context.get("assignment_draft") or {})
            if existing_context
            else _empty_assignment_draft()
        )
        formatted_chat_history = _format_chat_history(recent_messages)
        is_first_substantive_turn = not _has_prior_user_turn(recent_messages)
        draft_has_no_required_data = _completion_score(existing_draft) == 0.0
        if is_first_substantive_turn and draft_has_no_required_data:
            # First user turn with an effectively empty draft should be treated as
            # normal assignment capture, not as a forbidden "new assignment" switch.
            _new_assignment_decision = {
                "should_start_new_assignment": False,
                "reason": "first_turn_empty_draft",
            }
        else:
            _new_assignment_decision = await _decide_new_assignment_request(
                latest_message=message,
                current_draft=existing_draft,
                chat_history=formatted_chat_history,
                update_context=update_context,
                locked_to_entity_id=True,
                entity_id=assignment_feature_key,
            )
        is_new_assignment = False
    else:
        raise ValueError(
            "entity_id is required for assignment chat stream; backend context id generation is disabled."
        )
    timing.mark("context_load", session_id=session_id)

    message_metadata = {
        **(client_context or {}),
        "assignment_feature_key": assignment_feature_key,
    }

    if external_entity_id and _new_assignment_decision.get(
        "should_start_new_assignment"
    ):
        assistant_message = (
            "I can only help you customize this specific assignment in this view. "
            "To create a completely new assignment from scratch, please start a new assignment "
            "from the main BD workspace and then open its chat separately."
        )
        completion = _completion_score(existing_draft)
        missing_labels = _missing_field_labels(existing_draft)
        ready_to_save = len(missing_labels) == 0
        show_add_people_cta = _should_show_add_people_cta(missing_labels)

        await upsert_assignment_context(
            entity_urn=entity_urn,
            feature_key=feature_key,
            assignment_draft=existing_draft,
            completion_score=completion,
            last_user_message=message,
            context_id=assignment_feature_key,
        )

        await insert_chat_message(
            session_id=session_id,
            entity_urn=entity_urn,
            feature_key=feature_key,
            role="assistant",
            content=assistant_message,
            metadata={
                "ready_to_save": ready_to_save,
                "used_profile_suggestions": False,
                "used_company_mapping": False,
                "started_new_assignment": False,
                "save_requested": False,
                "assignment_feature_key": assignment_feature_key,
            },
        )

        yield {
            "type": "assistant_final",
            "text": assistant_message,
        }
        timing.summary("early_return_new_assignment_blocked")
        yield {
            "type": "result",
            "result": {
                "entity_urn": entity_urn,
                "session_id": session_id,
                "assignment_feature_key": assignment_feature_key,
                "assistant_message": assistant_message,
                "assignment_draft": existing_draft,
                "missing_fields": missing_labels,
                "completion_score": completion,
                "ready_to_save": ready_to_save,
                "show_add_people_cta": show_add_people_cta,
                "started_new_assignment": False,
                "save_requested": False,
            },
        }
        return

    persist_user_task = asyncio.create_task(
        insert_chat_message(
            session_id=session_id,
            entity_urn=entity_urn,
            feature_key=feature_key,
            role="user",
            content=message,
            metadata=message_metadata,
        )
    )
    missing_labels_pre = _missing_field_labels(existing_draft)
    prior_blocked_save_count = _load_prior_save_blocked_count(
        session=session,
        recent_messages=recent_messages,
        assignment_feature_key=assignment_feature_key,
    )
    pre_llm_people_task = asyncio.create_task(
        _prepare_pre_llm_people_mapping(
            latest_message=message,
            people_involved=_to_clean_string(existing_draft.get("people_involved")),
            chat_history=formatted_chat_history,
            update_context=update_context,
        )
    )
    early_save_decision_task = asyncio.create_task(
        _decide_save_assignment_request(
            latest_message=message,
            current_draft=existing_draft,
            missing_fields=missing_labels_pre,
            chat_history=formatted_chat_history,
            update_context=update_context,
            prior_blocked_save_count=prior_blocked_save_count,
        )
    )
    (
        people_mapping_context,
        prefetched_decider_request,
        suppress_stream_deltas,
    ) = await pre_llm_people_task
    early_save_decision = await early_save_decision_task
    early_save_requested = bool(early_save_decision.get("should_save"))
    early_explicit_skip = bool(early_save_decision.get("explicit_skip_remaining"))
    should_fetch_people = bool(
        isinstance(prefetched_decider_request, dict)
        and prefetched_decider_request.get("should_fetch")
    )
    defer_assistant_stream = True
    timing.mark(
        "pre_llm_people_mapping",
        suppress_stream_deltas=suppress_stream_deltas,
        defer_assistant_stream=defer_assistant_stream,
        has_mapping_context=people_mapping_context != "N/A",
        early_save_requested=early_save_requested,
    )

    people_names_pre = []
    if isinstance(prefetched_decider_request, dict):
        people_names_pre = [
            _to_clean_string(n)
            for n in prefetched_decider_request.get("names") or []
            if _to_clean_string(n)
        ]
    pipeline_steps = _plan_pipeline_thinking_steps(
        latest_message=message,
        missing_field_count=len(missing_labels_pre),
        should_fetch_people=should_fetch_people,
        people_names=people_names_pre,
        early_save_requested=early_save_requested,
    )
    pipeline_thinking_task: Optional[asyncio.Task] = None
    if pipeline_steps:
        pipeline_thinking_task = asyncio.create_task(
            _generate_pipeline_thinking_sentences(
                latest_message=message,
                steps=pipeline_steps,
            )
        )

    await persist_user_task
    timing.mark("persist_user_message")

    prefetched_map_person_task: Optional[asyncio.Task] = None
    if (
        suppress_stream_deltas
        and prefetched_decider_request
        and prefetched_decider_request.get("should_fetch")
    ):
        prefetched_map_person_task = asyncio.create_task(
            get_profile_suggestions_multi(dict(prefetched_decider_request))
        )
        timing.mark(
            "prefetch_map_person_started",
            name_count=len(prefetched_decider_request.get("names") or []),
        )

    pipeline_sentences: List[str] = []
    if pipeline_thinking_task is not None:
        try:
            pipeline_sentences = await pipeline_thinking_task
        except Exception:
            logger.exception("pipeline_thinking_task failed")
        logger.info(
            "[pipeline_thinking] steps=%d sentences=%d",
            len(pipeline_steps),
            len(pipeline_sentences),
        )

    main_llm_started = False
    pipeline_paragraph_index = 0
    for sentence in pipeline_sentences:
        if main_llm_started:
            break
        clean_sentence = _to_clean_string(sentence)
        if not clean_sentence:
            continue
        yield {
            "type": "reasoning_delta",
            "text": clean_sentence,
            "paragraph_index": pipeline_paragraph_index,
        }
        pipeline_paragraph_index += 1
        await asyncio.sleep(0)

    user_prompt = ASSIGNMENT_CHAT_USER_PROMPT_TEMPLATE.format(
        entity_urn=entity_urn,
        profile_context=_compact_profile_context(profile),
        locked_to_entity_id="true",
        entity_id=assignment_feature_key,
        current_draft_json=json.dumps(existing_draft, ensure_ascii=False, indent=2),
        chat_history=formatted_chat_history,
        latest_message=message,
        update_context=update_context,
        people_mapping_context=people_mapping_context,
    )

    llm_chunks: List[str] = []

    async for part in stream(
        model="openai/gpt-5.1",
        temperature=0.2,
        fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4.1"],
        messages=[
            {"role": "system", "content": ASSIGNMENT_CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    ):
        if not main_llm_started:
            main_llm_started = True
        chunk_text = str(part or "")
        if chunk_text == "":
            continue
        llm_chunks.append(chunk_text)

    timing.mark("main_llm_stream", chunk_count=len(llm_chunks))

    parsed = _safe_json_loads_from_text("".join(llm_chunks))
    if not isinstance(parsed, dict):
        raise ValueError("Invalid LLM output; expected JSON object")

    assistant_message = _to_clean_string(parsed.get("assistant_message")) or (
        "Got it. I captured what I could. Please share a bit more detail so I can complete the assignment draft."
    )
    llm_ready_to_save = bool(parsed.get("ready_to_save"))
    extracted_fields = parsed.get("extracted_fields") or {}
    if not isinstance(extracted_fields, dict):
        extracted_fields = {}
    if isinstance(update, dict) and update.get("key"):
        update_key = _to_clean_string(update.get("key"))
        if update_key:
            extracted_fields = {update_key: extracted_fields.get(update_key)}
    extracted_fields = _sanitize_field_edit_extracted_fields(
        latest_message=message,
        extracted_fields=extracted_fields,
        update=update,
    )
    # Disallow non-canonical freeform people key from LLM output.
    extracted_fields.pop("people", None)
    # Do not trust direct people_involved extraction from chat LLM.
    # People are persisted only after profile-suggestion verification (id + public_identifier).
    extracted_fields.pop("people_involved", None)
    people_replace_targets = _extract_people_replace_targets_from_update(update)
    extracted_client_candidates = _extract_company_candidates_from_client_value(
        _to_clean_string(extracted_fields.get("client"))
    )
    client_replace_targets = _extract_client_replace_targets_from_update(update)

    merged_draft = _merge_assignment_draft(
        existing=existing_draft,
        extracted=extracted_fields,
    )
    completion = _completion_score(merged_draft)
    missing_labels = _missing_field_labels(merged_draft)
    # Big agent proposes ready_to_save; backend enforces required-field gating.
    ready_to_save = llm_ready_to_save and len(missing_labels) == 0
    timing.mark(
        "draft_merge_and_field_extract",
        missing_field_count=len(missing_labels),
    )

    # --- Client-field planner -------------------------------------------------
    # Run client planning before profile planning. If this turn is about the
    # client/company field, skip profile lookup entirely so company names are not
    # also sent through people search.
    client_plan = await _plan_client_operation(
        latest_message=message,
        current_client=_to_clean_string(merged_draft.get("client")),
        chat_history=formatted_chat_history,
        update_context=update_context,
    )
    client_action: str = client_plan.get("action") or "KEEP"
    plan_remove_targets: List[str] = client_plan.get("remove_targets") or []
    plan_add_companies: List[str] = client_plan.get("add_companies") or []
    if (
        client_action == "KEEP"
        and extracted_client_candidates
        and not _to_clean_string(existing_draft.get("client"))
    ):
        # The extractor found a client value. Treat it as a client add rather
        # than letting a conservative planner keep the client empty.
        client_action = "ADD"
        plan_add_companies = _merge_company_candidates(
            plan_add_companies,
            extracted_client_candidates,
        )
    timing.mark("client_planner", client_action=client_action)

    # --- People-involved planner ----------------------------------------------
    # A single planner determines the intended operation (ADD / REMOVE / REPLACE
    # / CLEAR / KEEP) so that removal intents are never accidentally overridden
    # by a background profile lookup.
    (
        profile_plan,
        profile_action,
        plan_remove_people,
        plan_remove_target_specs,
        plan_add_people,
        all_people_remove_targets,
        chip_people_remove,
        chip_entry,
    ) = await _resolve_people_involved_operation(
        latest_message=message,
        update=update,
        people_replace_targets=people_replace_targets,
        client_action=client_action,
        merged_draft=merged_draft,
        chat_history=formatted_chat_history,
        update_context=update_context,
    )
    _log_people_planner_context(
        phase="before_people_action",
        client_action=client_action,
        profile_action=profile_action,
        profile_plan=profile_plan,
        people_replace_targets=people_replace_targets,
        all_people_remove_targets=all_people_remove_targets,
        existing_draft_people=_to_clean_string(existing_draft.get("people_involved")),
        merged_draft_people=_to_clean_string(merged_draft.get("people_involved")),
    )

    people_involved_suggestions = None
    company_mapping_results = None
    people_outcome_requested_names: Optional[List[str]] = None

    if profile_action == "CLEAR":
        await _consume_prefetched_map_person_task(
            prefetched_map_person_task,
            reuse=False,
        )
        merged_draft["people_involved"] = None
    elif profile_action == "REMOVE":
        await _consume_prefetched_map_person_task(
            prefetched_map_person_task,
            reuse=False,
        )
        # Pure removal — filter the existing list directly; no profile lookup.
        existing_people = _normalize_people_involved_entries(
            _to_clean_string(merged_draft.get("people_involved"))
        )
        filtered = _filter_people_for_remove_action(
            entries=existing_people,
            chip_people_remove=chip_people_remove,
            chip_entry=chip_entry,
            people_remove_target_specs=plan_remove_target_specs,
            legacy_remove_targets=all_people_remove_targets,
        )
        deduped = _dedupe_people_entries(filtered)
        merged_draft["people_involved"] = (
            json.dumps(deduped, ensure_ascii=False) if deduped else None
        )
    elif profile_action in ("ADD", "REPLACE"):
        await _consume_prefetched_map_person_task(
            prefetched_map_person_task,
            reuse=False,
        )
        # Look up new people from the planner's list, then merge into
        # the existing list.  For REPLACE, replace_targets strip the old
        # person once new verified profiles are confirmed.
        lookup_request = _build_profile_lookup_request_from_plan(plan_add_people)
        if lookup_request.get("names"):
            people_involved_suggestions = await get_profile_suggestions_multi(
                lookup_request
            )
        merged_people_involved = await _append_profile_ids_to_people_involved(
            current_people_involved=_to_clean_string(
                merged_draft.get("people_involved")
            ),
            people_involved_suggestions=people_involved_suggestions,
            replace_targets=(
                all_people_remove_targets if people_involved_suggestions else None
            ),
            replace_target_specs=(
                plan_remove_target_specs if people_involved_suggestions else None
            ),
            es_client=es_client,
        )
        if merged_people_involved:
            merged_draft["people_involved"] = merged_people_involved
        requested_names = [
            _to_clean_string(p.get("name"))
            for p in plan_add_people
            if _to_clean_string(p.get("name"))
        ]
        if requested_names:
            people_outcome_requested_names = requested_names
    else:
        # KEEP — run the existing background-enrichment path to resolve any
        # unresolved (id-less) people already in the list.
        if prefetched_decider_request is not None:
            decider_request = dict(prefetched_decider_request)
            _people_trace_log(
                "profile_suggestion_decider_reused",
                request=decider_request,
                source="pre_llm_prefetch",
            )
        else:
            decider_request = await _decide_profile_suggestion_request(
                latest_message=message,
                people_involved=_to_clean_string(merged_draft.get("people_involved")),
                chat_history=formatted_chat_history,
                update_context=update_context,
            )
            decider_request = _augment_profile_request_from_people_involved(
                request=decider_request,
                people_involved_value=_to_clean_string(
                    merged_draft.get("people_involved")
                ),
            )
            _log_profile_decider_result(decider_request)
        prefetch_started = time.perf_counter()
        people_involved_suggestions = await _consume_prefetched_map_person_task(
            prefetched_map_person_task,
            reuse=True,
        )
        if people_involved_suggestions is not None:
            _people_trace_log(
                "map_person_prefetch_reused",
                result_count=len(people_involved_suggestions),
            )
            if LIVE_PREVIEW_TIMING_DEBUG:
                logger.info(
                    "[live_preview_timing] label=map_person phase=prefetch_reused phase_ms=%.1f",
                    (time.perf_counter() - prefetch_started) * 1000.0,
                )
        elif decider_request.get("should_fetch"):
            people_involved_suggestions = await get_profile_suggestions_multi(
                decider_request
            )
        merged_people_involved = await _append_profile_ids_to_people_involved(
            current_people_involved=_to_clean_string(
                merged_draft.get("people_involved")
            ),
            people_involved_suggestions=people_involved_suggestions,
            replace_targets=(
                people_replace_targets if people_involved_suggestions else None
            ),
            es_client=es_client,
        )
        if merged_people_involved:
            merged_draft["people_involved"] = merged_people_involved
        if decider_request.get("should_fetch"):
            requested_names = [
                _to_clean_string(n)
                for n in decider_request.get("names") or []
                if _to_clean_string(n)
            ]
            if requested_names:
                people_outcome_requested_names = requested_names
    _people_trace_log(
        "people_action_complete",
        profile_action=profile_action,
        merged_draft_entries=_summarize_people_entries_for_trace(
            _to_clean_string(merged_draft.get("people_involved"))
        ),
    )
    timing.mark(
        "people_pipeline",
        profile_action=profile_action,
        map_person_count=(
            len(people_involved_suggestions)
            if isinstance(people_involved_suggestions, list)
            else 0
        ),
    )
    # --------------------------------------------------------------------------

    client_entries: List[Dict[str, Any]] = []
    # Run company mapping only for ADD / REPLACE.  Merge planner's explicit
    # company list with any names the main LLM extracted as a fallback.
    company_mapping_results = None
    if client_action in ("ADD", "REPLACE"):
        company_mapping_targets = _merge_company_candidates(
            plan_add_companies,
            extracted_client_candidates,
        )
        if company_mapping_targets:
            company_mapping_results = await get_company_mapping_multi(
                companies=company_mapping_targets,
                user_query=message,
            )
    mapped_client_entries = _extract_client_entries_from_company_mapping(
        company_mapping_results
    )
    if mapped_client_entries:
        client_entries.extend(mapped_client_entries)
    elif client_action in ("ADD", "REPLACE"):
        company_mapping_targets = _merge_company_candidates(
            plan_add_companies,
            extracted_client_candidates,
        )
        client_entries.extend(
            _client_entries_from_unmapped_targets(company_mapping_targets)
        )

    # Combine UI chip replace-targets with planner remove-targets.
    all_remove_targets = list(
        dict.fromkeys((client_replace_targets or []) + (plan_remove_targets or []))
    )

    # Apply the plan.
    if client_action == "CLEAR":
        merged_draft["client"] = None
    elif client_action in ("REMOVE", "REPLACE") or all_remove_targets:
        # Remove targeted companies then append any new entries.
        merged_draft["client"] = _append_client_entries_to_client(
            current_client=_to_clean_string(existing_draft.get("client")),
            client_entries=client_entries,
            replace_targets=(all_remove_targets if not is_new_assignment else None),
        )
    elif client_action == "ADD" and client_entries:
        merged_draft["client"] = _append_client_entries_to_client(
            current_client=_to_clean_string(existing_draft.get("client")),
            client_entries=client_entries,
            replace_targets=None,
        )
        added_names = [
            _to_clean_string(entry.get("display_name") or entry.get("universal_name"))
            for entry in client_entries
            if isinstance(entry, dict)
            and _to_clean_string(
                entry.get("display_name") or entry.get("universal_name")
            )
        ]
        if added_names:
            assistant_message = await _append_dynamic_entity_update_note(
                assistant_message=assistant_message,
                latest_message=message,
                entity_type="client",
                outcome="added",
                targets=added_names,
            )
    elif client_action in ("ADD", "REPLACE") and plan_add_companies:
        merged_draft["client"] = _to_clean_string(existing_draft.get("client"))
        assistant_message = await _append_dynamic_entity_update_note(
            assistant_message=assistant_message,
            latest_message=message,
            entity_type="client",
            outcome="not_found",
            targets=plan_add_companies[:3],
        )
    else:
        # KEEP or no actionable change — preserve existing canonical value.
        merged_draft["client"] = _to_clean_string(existing_draft.get("client"))
    # --------------------------------------------------------------------------
    timing.mark("company_mapping_and_client_apply", client_action=client_action)
    _finalize_people_fields(merged_draft)
    completion = _completion_score(merged_draft)
    missing_labels = _missing_field_labels(merged_draft)
    prior_blocked_save_count = _load_prior_save_blocked_count(
        session=session,
        recent_messages=recent_messages,
        assignment_feature_key=assignment_feature_key,
    )
    save_decision = await _decide_save_assignment_request(
        latest_message=message,
        current_draft=merged_draft,
        missing_fields=missing_labels,
        chat_history=formatted_chat_history,
        update_context=update_context,
        prior_blocked_save_count=prior_blocked_save_count,
    )
    save_outcome = _resolve_save_turn_outcome(
        draft=merged_draft,
        save_decision=save_decision,
        prior_blocked_save_count=prior_blocked_save_count,
    )
    save_requested = bool(save_outcome.get("save_requested"))
    explicit_skip_remaining = bool(save_outcome.get("explicit_skip_remaining"))
    auto_waived_on_retry = bool(save_outcome.get("auto_waived_on_retry"))
    ready_to_save = bool(save_outcome.get("ready_to_save"))
    missing_labels = list(save_outcome.get("missing_labels") or [])
    waived_labels = list(save_outcome.get("waived_labels") or [])
    after_people_involved = _to_clean_string(merged_draft.get("people_involved"))
    unverified_people = _people_verification_failures(
        people_outcome_requested_names=people_outcome_requested_names,
        after_people_involved=after_people_involved,
    )
    show_add_people_cta = _should_show_add_people_cta_after_mapping_failure(
        people_outcome_requested_names=people_outcome_requested_names,
        after_people_involved=after_people_involved,
    )
    if people_outcome_requested_names:
        logger.info(
            "[people_cta] mapping_attempted=%s verified_count=%d unverified=%r show_cta=%s",
            True,
            len(people_outcome_requested_names) - len(unverified_people),
            unverified_people,
            show_add_people_cta,
        )
    timing.mark(
        "save_decision",
        save_requested=save_requested,
        explicit_skip_remaining=explicit_skip_remaining,
        auto_waived_on_retry=auto_waived_on_retry,
        prior_blocked_save_count=prior_blocked_save_count,
    )
    assistant_message = await _finalize_assistant_message(
        assistant_message=assistant_message,
        missing_fields=missing_labels,
        ready_to_save=ready_to_save,
        save_requested=save_requested,
        explicit_skip_remaining=explicit_skip_remaining,
        latest_message=message,
        suppress_stream_deltas=suppress_stream_deltas,
        prior_blocked_save_count=prior_blocked_save_count,
        auto_waived_on_retry=auto_waived_on_retry,
        waived_labels=waived_labels,
        people_outcome_requested_names=people_outcome_requested_names,
        after_people_involved=_to_clean_string(merged_draft.get("people_involved")),
    )
    timing.mark("message_normalize_and_outcome")

    await upsert_assignment_context(
        entity_urn=entity_urn,
        feature_key=feature_key,
        assignment_draft=merged_draft,
        completion_score=completion,
        last_user_message=message,
        context_id=assignment_feature_key,
    )

    await insert_chat_message(
        session_id=session_id,
        entity_urn=entity_urn,
        feature_key=feature_key,
        role="assistant",
        content=assistant_message,
        metadata={
            "ready_to_save": ready_to_save,
            "used_profile_suggestions": people_involved_suggestions is not None,
            "used_company_mapping": company_mapping_results is not None,
            "started_new_assignment": is_new_assignment,
            "save_requested": save_requested,
            "explicit_skip_remaining": explicit_skip_remaining,
            "auto_waived_on_retry": auto_waived_on_retry,
            "assignment_feature_key": assignment_feature_key,
        },
    )
    await _persist_save_retry_state(
        session=session,
        assignment_feature_key=assignment_feature_key,
        save_requested=save_requested,
        ready_to_save=ready_to_save,
    )
    timing.mark("persist_assistant_and_draft")
    timing.summary(
        "ready_for_live_preview",
        completion_score=completion,
        ready_to_save=ready_to_save,
    )

    if defer_assistant_stream:
        yield {
            "type": "assistant_final",
            "text": assistant_message,
        }
    yield {
        "type": "result",
        "result": {
            "entity_urn": entity_urn,
            "session_id": session_id,
            "assignment_feature_key": assignment_feature_key,
            "assistant_message": assistant_message,
            "assignment_draft": merged_draft,
            "missing_fields": missing_labels,
            "completion_score": completion,
            "ready_to_save": ready_to_save,
            "show_add_people_cta": show_add_people_cta,
            "started_new_assignment": is_new_assignment,
            "save_requested": save_requested,
            "explicit_skip_remaining": explicit_skip_remaining,
        },
    }


async def initialize_assignment_chat(
    *,
    entity_urn: str,
    feature_key: str = FEATURE_KEY_ASSIGNMENT_CREATION,
    es_client: AsyncElasticsearch,
    client_context: Optional[Dict[str, Any]] = None,
    external_entity_id: Optional[str] = None,
    chat_type: str = "assignment",
) -> Dict[str, Any]:
    external_entity_id = _to_clean_string(external_entity_id)
    if not external_entity_id:
        raise ValueError(
            "entity_id is required for assignment chat init; backend context id generation is disabled."
        )

    is_content = (chat_type or "").lower() == "content" or (
        feature_key == FEATURE_KEY_CONTENT_CHAT
    )
    session_title = "Content Intake" if is_content else "Assignment Intake"

    session = await get_or_create_single_session(
        entity_urn=entity_urn,
        feature_key=feature_key,
        title=session_title,
    )
    session_id = str(session.get("id"))
    assignment_feature_key = external_entity_id

    recent_messages = await fetch_recent_chat_messages(
        session_id=session_id,
        limit=10,
        assignment_feature_key=assignment_feature_key,
    )
    existing_context = await fetch_assignment_context_by_id(
        entity_urn=entity_urn,
        context_id=assignment_feature_key,
    )
    if existing_context:
        existing_draft = dict(existing_context.get("assignment_draft") or {})
    elif is_content:
        existing_draft = _empty_content_draft()
    else:
        existing_draft = _empty_assignment_draft()

    # If there is already assistant/user interaction, don't create duplicate greeting.
    existing_assistant = next(
        (
            _to_clean_string(m.get("content"))
            for m in reversed(recent_messages)
            if str(m.get("role") or "").strip() == "assistant"
        ),
        None,
    )
    if existing_assistant:
        return {
            "entity_urn": entity_urn,
            "session_id": session_id,
            "assignment_feature_key": assignment_feature_key,
            "greeting_message": existing_assistant,
            "chat_type": "content" if is_content else "assignment",
        }

    _, profile = await fetch_profile_by_entity_urn(
        entity_urn=entity_urn,
        es_client=es_client,
    )
    first_name = _derive_first_name(profile=profile, entity_urn=entity_urn)
    if external_entity_id and existing_context:
        if is_content:
            greeting_message = _build_content_welcome_back_greeting(
                first_name=first_name,
                content_draft=existing_draft,
            )
        else:
            assignment_name = (
                _to_clean_string((existing_draft or {}).get("assignment_name"))
                or "this assignment"
            )
            greeting_message = (
                f"Hi {first_name}, welcome back. You're continuing the draft for {assignment_name}. "
                "Tell me what you'd like to review, update, or add, and I'll adjust the draft accordingly."
            )
    else:
        greeting_message = await _generate_initial_greeting(
            entity_urn=entity_urn,
            first_name=first_name,
            profile_context=_compact_profile_context(profile),
            chat_type="content" if is_content else "assignment",
        )

    await insert_chat_message(
        session_id=session_id,
        entity_urn=entity_urn,
        feature_key=feature_key,
        role="assistant",
        content=greeting_message,
        metadata={
            "type": "initial_greeting",
            "chat_type": "content" if is_content else "assignment",
            "assignment_feature_key": assignment_feature_key,
            **(client_context or {}),
        },
    )

    # Ensure context row exists even before first user message.
    if existing_context:
        completion = float(existing_context.get("completion_score") or 0.0)
    elif is_content:
        completion = _completion_score_content(existing_draft)
    else:
        completion = _completion_score(existing_draft)

    await upsert_assignment_context(
        entity_urn=entity_urn,
        feature_key=feature_key,
        assignment_draft=existing_draft,
        completion_score=completion,
        last_user_message=(
            existing_context.get("last_user_message") if existing_context else None
        ),
        context_id=assignment_feature_key,
    )

    return {
        "entity_urn": entity_urn,
        "session_id": session_id,
        "assignment_feature_key": assignment_feature_key,
        "greeting_message": greeting_message,
        "chat_type": "content" if is_content else "assignment",
    }


async def fetch_assignment_chat_state(
    *,
    entity_urn: str,
    feature_key: str = FEATURE_KEY_ASSIGNMENT_CREATION,
) -> Dict[str, Any]:
    session = await get_or_create_single_session(
        entity_urn=entity_urn,
        feature_key=feature_key,
        title="Assignment Intake",
    )
    session_id = str(session.get("id"))
    assignment_feature_key = _active_assignment_feature_key(
        session=session,
        base_feature_key=feature_key,
    )
    messages = await fetch_recent_chat_messages(
        session_id=session_id,
        limit=50,
        assignment_feature_key=assignment_feature_key,
    )
    existing_context = await fetch_assignment_context(
        entity_urn=entity_urn,
        feature_key=assignment_feature_key,
    )
    draft = (
        dict(existing_context.get("assignment_draft") or {})
        if existing_context
        else _empty_assignment_draft()
    )
    completion = (
        float(existing_context.get("completion_score") or 0.0)
        if existing_context
        else _completion_score(draft)
    )
    missing_labels = _missing_field_labels(draft)
    return {
        "entity_urn": entity_urn,
        "feature_key": assignment_feature_key,
        "base_feature_key": feature_key,
        "session_id": session_id,
        "assignment_draft": draft,
        "missing_fields": missing_labels,
        "completion_score": completion,
        "ready_to_save": len(missing_labels) == 0,
        "show_add_people_cta": _should_show_add_people_cta(missing_labels),
        "messages": messages,
    }


async def fetch_assignment_chat_assignments(
    *,
    entity_urn: str,
    feature_key: str = FEATURE_KEY_ASSIGNMENT_CREATION,
) -> Dict[str, Any]:
    session = await get_or_create_single_session(
        entity_urn=entity_urn,
        feature_key=feature_key,
        title="Assignment Intake",
    )
    active_feature_key = _active_assignment_feature_key(
        session=session,
        base_feature_key=feature_key,
    )
    contexts = await fetch_assignment_contexts(
        entity_urn=entity_urn,
        feature_key_prefix=feature_key,
        limit=50,
    )

    assignments: List[Dict[str, Any]] = []
    for ctx in contexts:
        draft = ctx.get("assignment_draft") or {}
        if not isinstance(draft, dict):
            draft = {}
        missing_labels = _missing_field_labels(draft)
        assignment_feature_key = _to_clean_string(ctx.get("feature_key")) or feature_key
        assignments.append(
            {
                "assignment_feature_key": assignment_feature_key,
                "is_active": assignment_feature_key == active_feature_key,
                "assignment_name": _to_clean_string(draft.get("assignment_name")),
                "assignment_draft": draft,
                "missing_fields": missing_labels,
                "completion_score": float(
                    ctx.get("completion_score") or _completion_score(draft)
                ),
                "ready_to_save": len(missing_labels) == 0,
                "status": _to_clean_string(ctx.get("status")),
                "last_user_message": _to_clean_string(ctx.get("last_user_message")),
                "created_at": ctx.get("created_at"),
                "updated_at": ctx.get("updated_at"),
            }
        )

    return {
        "entity_urn": entity_urn,
        "active_assignment_feature_key": active_feature_key,
        "assignments": assignments,
    }
