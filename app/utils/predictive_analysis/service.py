import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from qutils.llm.asynchronous import invoke

from app.utils.predictive_analysis.prompts import (
    PREDICTIVE_ANALYSIS_SYSTEM_PROMPT,
    PREDICTIVE_ANALYSIS_USER_PROMPT_TEMPLATE,
    PREDICTIVE_REACHOUT_VALIDATION_SYSTEM_PROMPT,
    PREDICTIVE_REACHOUT_VALIDATION_USER_PROMPT_TEMPLATE,
    PREDICTIVE_STAKEHOLDER_TITLES_SYSTEM_PROMPT,
    PREDICTIVE_STAKEHOLDER_TITLES_USER_PROMPT_TEMPLATE,
)
from app.utils.profile_fetching.public_identifier_fetch import fetch_profile_by_es_id

logger = logging.getLogger(__name__)

MIN_TRIGGER_CONFIDENCE = 0.70
NEWS_TYPE = "predictive_profile_trigger"
ENTITY_TYPE = "person_predictive_trigger"
SOURCE = "Predictive Analysis"

CANONICAL_TRIGGER_TYPES = {
    "role_tenure_inflection",
    "industry_authority",
    "board_governance",
    "enterprise_scale_operator",
    "transformation_operator",
    "cross_sector_leader",
    "succession_candidate",
    "strategic_network_trigger",
    "other_high_conviction_trigger",
}

TRIGGER_TYPE_ALIASES = {
    "strategic_network_signal": "strategic_network_trigger",
    "strategic_network": "strategic_network_trigger",
    "enterprise_operator": "enterprise_scale_operator",
    "scale_operator": "enterprise_scale_operator",
    "board_signal": "board_governance",
    "governance_signal": "board_governance",
    "role_tenure": "role_tenure_inflection",
    "tenure_inflection": "role_tenure_inflection",
    "industry_expertise": "industry_authority",
    "sector_authority": "industry_authority",
    "transformation_leader": "transformation_operator",
    "cross_sector": "cross_sector_leader",
    "succession": "succession_candidate",
}


def _clean_text(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _clean_list(value: Any, *, max_items: int = 20, item_limit: int = 240) -> List[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in raw_items:
        text = _clean_text(item, item_limit)
        if text:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _safe_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_json_loads_from_text(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", raw).replace("```", "").strip()

    try:
        return json.loads(raw)
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(raw[start : end + 1])

    raise ValueError("Could not parse JSON object from LLM response")


def _compact_headquarter(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "city": _clean_text(value.get("city"), 120),
        "state": _clean_text(value.get("geographic_area"), 120),
        "country": _clean_text(value.get("country"), 120),
    }


def _compact_experience(exp: Dict[str, Any]) -> Dict[str, Any]:
    start = exp.get("start")
    end = exp.get("end", None)
    return {
        "index": exp.get("index"),
        "title": _clean_text(exp.get("title"), 300),
        "company": _clean_text(exp.get("company") or exp.get("company_name"), 300),
        "company_universal_name": _clean_text(
            exp.get("company_universal_name")
            or exp.get("company_universalname")
            or exp.get("companyUniversalName")
            or exp.get("companyUniversalname"),
            300,
        ),
        "company_industry": _clean_list(exp.get("company_industry"), max_items=8),
        "company_speciality": _clean_list(exp.get("company_speciality"), max_items=20),
        "company_description": _clean_text(exp.get("company_description"), 900),
        "company_type": _clean_text(exp.get("company_type"), 120),
        "company_ownership": _clean_list(exp.get("company_ownership"), max_items=5),
        "company_size": exp.get("company_size"),
        "staff_count_range": exp.get("staff_count_range"),
        "company_headquarter": _compact_headquarter(exp.get("company_headquarter")),
        "location": _clean_text(exp.get("location"), 240),
        "start": _clean_text(start, 80),
        "end": _clean_text(end, 80) if end else "Present",
        "tenure_days": _safe_number(exp.get("tenure")),
        "tenure_at_each_company_days": _safe_number(exp.get("tenure_at_each_company")),
        "management_level": exp.get("management_level"),
        "functional_area": _clean_text(exp.get("functional_area"), 160),
        "job_summary": _clean_text(exp.get("job_summary"), 900),
    }


def _compact_education_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": item.get("index"),
        "school": _clean_text(item.get("school"), 240),
        "degree": _clean_text(item.get("degree"), 180),
        "major": _clean_text(item.get("major"), 180),
        "major_degree": _clean_text(item.get("major_degree"), 240),
        "start": _clean_text(item.get("start"), 80),
        "end": _clean_text(item.get("end"), 80),
    }


def build_profile_packet(profile_id: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    current_date = datetime.now(timezone.utc).date().isoformat()
    experiences_raw = profile.get("experience")
    experience_items = experiences_raw if isinstance(experiences_raw, list) else []
    experiences = [
        _compact_experience(exp) for exp in experience_items if isinstance(exp, dict)
    ]

    education_raw = profile.get("education")
    education_items = education_raw if isinstance(education_raw, list) else []
    education = [
        _compact_education_item(item)
        for item in education_items[:8]
        if isinstance(item, dict)
    ]

    return {
        "current_date": current_date,
        "profile_id": profile_id,
        "identity": {
            "public_identifier": _clean_text(profile.get("public_identifier"), 240),
            "entity_urn": _clean_text(profile.get("entity_urn"), 240),
            "full_name": _clean_text(
                profile.get("full_name") or profile.get("name"), 240
            ),
            "first_name": _clean_text(profile.get("first_name"), 120),
            "last_name": _clean_text(profile.get("last_name"), 120),
            "headline": _clean_text(profile.get("headline"), 400),
            "summary": _clean_text(profile.get("summary"), 1400),
            "age": profile.get("age"),
            "location": _clean_text(profile.get("location"), 300),
            "location_full_path": _clean_text(profile.get("location_full_path"), 400),
            "country": _clean_text(profile.get("country"), 120),
            "state": _clean_text(profile.get("state"), 120),
            "metro_area": _clean_text(profile.get("metro_area"), 160),
            "follower_count": profile.get("follower_count"),
            "connection_count": profile.get("connection_count"),
            "total_tenure_days": _safe_number(profile.get("total_tenure")),
            "is_verified": profile.get("is_verified"),
            "is_premium": profile.get("is_premium"),
            "updated_at": _clean_text(profile.get("updated_at"), 80),
        },
        "experience_count": len(experiences),
        "experience": experiences,
        "education": education,
    }


def _current_operating_experience(profile: Dict[str, Any]) -> Dict[str, Any]:
    experiences = profile.get("experience")
    if not isinstance(experiences, list):
        return {}

    current_items = [
        exp for exp in experiences if isinstance(exp, dict) and not exp.get("end")
    ]
    pool = current_items or [exp for exp in experiences if isinstance(exp, dict)]
    if not pool:
        return {}

    for exp in pool:
        title = _clean_text(exp.get("title"), 300).lower()
        has_company = bool(
            _clean_text(
                exp.get("company_universal_name"),
                300,
            )
        )
        if not has_company:
            continue
        if any(word in title for word in ("ceo", "chief", "president", "founder")):
            return exp
        if not any(word in title for word in ("board", "trustee", "advisor")):
            return exp

    return pool[0]


def _clean_title_groups(value: Any) -> List[List[str]]:
    if not isinstance(value, list):
        return []
    groups: List[List[str]] = []
    for group in value:
        titles = _clean_list(group, max_items=6, item_limit=120)
        titles = list(dict.fromkeys(titles))
        if titles:
            groups.append(titles)
        if len(groups) >= 3:
            break
    return groups


async def _stakeholder_title_groups(
    trigger: Dict[str, Any], current_title: str
) -> List[List[str]]:
    if trigger.get("outreach_audience") not in {"manager_stakeholder", "both"}:
        return []
    if not trigger.get("manager_outreach_angle"):
        return []

    prompt = PREDICTIVE_STAKEHOLDER_TITLES_USER_PROMPT_TEMPLATE.format(
        current_title=current_title,
        trigger_json=json.dumps(
            {
                "trigger_type": trigger.get("trigger_type"),
                "trigger_title": trigger.get("trigger_title"),
                "trigger_description": trigger.get("trigger_description"),
                "manager_outreach_angle": trigger.get("manager_outreach_angle"),
                "search_relevance": trigger.get("search_relevance"),
            },
            ensure_ascii=False,
        ),
    )
    try:
        response = await invoke(
            model="openai/gpt-4.1-mini",
            temperature=0.1,
            fallbacks=["openai/gpt-4.1", "anthropic/claude-3-5-haiku-latest"],
            messages=[
                {
                    "role": "system",
                    "content": PREDICTIVE_STAKEHOLDER_TITLES_SYSTEM_PROMPT,
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=500,
        )
        parsed = _safe_json_loads_from_text(str(response or ""))
    except Exception as exc:
        logger.warning("Stakeholder title generation failed: %s", exc)
        return []

    if not isinstance(parsed, dict):
        return []
    title_groups = _clean_title_groups(parsed.get("title_groups"))
    if title_groups:
        return title_groups
    titles = _clean_list(parsed.get("titles"), max_items=6, item_limit=120)
    return [list(dict.fromkeys(titles))] if titles else []


async def _profile_ids_by_title_and_company(
    *,
    titles: List[str],
    company_universal_name: str,
) -> List[str]:
    url = (
        os.getenv("PROFILE_BY_TITLE_AND_COMPANY_URL")
        or "http://localhost:3000/m2m/ai/profileByTitleAndCompany"
    )
    titles = list(dict.fromkeys(_clean_list(titles, max_items=8, item_limit=120)))
    if not url or not titles or not company_universal_name:
        return []

    headers = {}
    bearer = (os.getenv("M2M_BEARER") or "").strip()
    if bearer:
        token = (
            bearer.split(None, 1)[1] if bearer.lower().startswith("bearer ") else bearer
        )
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                json={
                    "titles": titles,
                    "companyUniversalName": company_universal_name,
                },
                headers=headers,
            )
        if response.status_code != 200:
            logger.warning(
                "profileByTitleAndCompany failed status=%s titles=%s company=%s response=%s",
                response.status_code,
                titles,
                company_universal_name,
                response.text[:1000],
            )
            return []
        data = response.json()
    except Exception as exc:
        logger.warning("profileByTitleAndCompany request failed: %s", exc)
        return []

    ids = data if isinstance(data, list) else []
    if not isinstance(ids, list):
        return []
    out: List[str] = []
    for item in ids:
        profile_id = item.get("profile_id") if isinstance(item, dict) else item
        profile_id = _clean_text(profile_id, 120)
        if profile_id and profile_id not in out:
            out.append(profile_id)
    logger.info(
        "profileByTitleAndCompany mapped titles=%s company=%s ids_count=%d ids=%s",
        titles,
        company_universal_name,
        len(out),
        out[:10],
    )
    return out


def _compact_reachout_candidate(
    profile_id: str, profile: Dict[str, Any]
) -> Dict[str, Any]:
    current = _current_operating_experience(profile)
    return {
        "profile_id": str(profile_id),
        "public_identifier": _clean_text(profile.get("public_identifier"), 240),
        "full_name": _clean_text(profile.get("full_name") or profile.get("name"), 240),
        "headline": _clean_text(profile.get("headline"), 400),
        "summary": _clean_text(profile.get("summary"), 600),
        "current_title": _clean_text(current.get("title"), 240),
        "current_company": _clean_text(current.get("company"), 240),
        "current_company_universal_name": _clean_text(
            current.get("company_universal_name"),
            240,
        ),
    }


def _validate_people_to_reach_out_payload(
    parsed: Any,
    candidates_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return []
    raw_people = parsed.get("people_to_reach_out")
    if not isinstance(raw_people, list):
        return []

    out: List[Dict[str, Any]] = []
    for item in raw_people:
        if not isinstance(item, dict):
            continue
        profile_id = _clean_text(item.get("profile_id"), 120)
        candidate = candidates_by_id.get(profile_id)
        if not candidate:
            continue
        confidence = _safe_number(item.get("confidence")) or 0.0
        if confidence < MIN_TRIGGER_CONFIDENCE:
            continue
        ctas = _clean_list(item.get("ctas"), max_items=4, item_limit=240)
        if not ctas:
            continue
        out.append(
            {
                "profile_id": profile_id,
                "outreach_role": _clean_text(item.get("outreach_role"), 160),
                "reasoning": _clean_text(item.get("reasoning"), 700),
                "ctas": ctas,
                "confidence": round(max(0.0, min(confidence, 1.0)), 4),
            }
        )
        if len(out) >= 4:
            break
    return out


def _minimal_people_to_reach_out(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        profile_id = _clean_text(item.get("profile_id"), 120)
        ctas = _clean_list(item.get("ctas"), max_items=4, item_limit=240)
        if not profile_id or not ctas:
            continue
        confidence = _safe_number(item.get("confidence"))
        row = {
            "profile_id": profile_id,
            "outreach_role": _clean_text(item.get("outreach_role"), 160),
            "reasoning": _clean_text(item.get("reasoning"), 700),
            "ctas": ctas,
            "is_original_profile": bool(item.get("is_original_profile")),
        }
        if confidence is not None:
            row["confidence"] = round(max(0.0, min(confidence, 1.0)), 4)
        out.append(row)
        if len(out) >= 6:
            break
    return out


def _people_to_reach_out_with_original(
    *,
    profile_id: str,
    trigger: Dict[str, Any],
) -> List[Dict[str, Any]]:
    original_ctas = _clean_list(
        trigger.get("person_ctas") or trigger.get("person_outreach_ctas"),
        max_items=4,
        item_limit=240,
    )
    if not original_ctas:
        original_ctas = ["Explore a discreet leadership market conversation"]

    people = [
        {
            "profile_id": str(profile_id),
            "outreach_role": "reference_executive",
            "reasoning": _clean_text(
                trigger.get("person_outreach_angle")
                or trigger.get("reasoning")
                or trigger.get("search_relevance")
                or trigger.get("trigger_title"),
                700,
            ),
            "ctas": original_ctas,
            "confidence": trigger.get("confidence"),
            "is_original_profile": True,
        }
    ]
    for person in trigger.get("people_to_reach_out") or []:
        if not isinstance(person, dict):
            continue
        if _clean_text(person.get("profile_id"), 120) == str(profile_id):
            continue
        row = dict(person)
        row["is_original_profile"] = False
        people.append(row)
    return _minimal_people_to_reach_out(people)


async def _validate_people_to_reach_out(
    *,
    reference_profile: Dict[str, Any],
    trigger: Dict[str, Any],
    candidate_profiles: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not candidate_profiles:
        return []

    candidates_by_id = {
        str(candidate.get("profile_id")): candidate
        for candidate in candidate_profiles
        if candidate.get("profile_id")
    }
    reference_current = _current_operating_experience(reference_profile)
    reference_payload = {
        "full_name": _clean_text(
            reference_profile.get("full_name") or reference_profile.get("name"), 240
        ),
        "headline": _clean_text(reference_profile.get("headline"), 400),
        "current_title": _clean_text(reference_current.get("title"), 240),
        "current_company": _clean_text(reference_current.get("company"), 240),
    }
    user_prompt = PREDICTIVE_REACHOUT_VALIDATION_USER_PROMPT_TEMPLATE.format(
        reference_profile_json=json.dumps(reference_payload, ensure_ascii=False),
        trigger_json=json.dumps(trigger, ensure_ascii=False),
        candidate_profiles_json=json.dumps(candidate_profiles, ensure_ascii=False),
    )
    try:
        response = await invoke(
            model="openai/gpt-5.1",
            temperature=0.2,
            fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4.1"],
            messages=[
                {
                    "role": "system",
                    "content": PREDICTIVE_REACHOUT_VALIDATION_SYSTEM_PROMPT,
                },
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1600,
        )
        parsed = _safe_json_loads_from_text(str(response or ""))
    except Exception as exc:
        logger.warning("Reachout validation LLM failed: %s", exc)
        return []

    return _validate_people_to_reach_out_payload(parsed, candidates_by_id)


async def enrich_triggers_with_people_to_reach_out(
    *,
    triggers: List[Dict[str, Any]],
    profile: Dict[str, Any],
    profile_id: str,
    es_client: Any,
) -> List[Dict[str, Any]]:
    current = _current_operating_experience(profile)
    company_universal_name = _clean_text(
        current.get("company_universal_name"),
        240,
    )
    current_title = _clean_text(current.get("title"), 240)
    logger.info(
        "Predictive reachout enrichment start profile_id=%s triggers=%d current_title=%s company_universal_name=%s",
        profile_id,
        len(triggers),
        current_title,
        company_universal_name,
    )
    if not company_universal_name or es_client is None:
        logger.info(
            "Predictive reachout enrichment skipped profile_id=%s company_universal_name=%s es_client=%s",
            profile_id,
            company_universal_name,
            bool(es_client),
        )
        for trigger in triggers:
            trigger["people_to_reach_out"] = _people_to_reach_out_with_original(
                profile_id=profile_id,
                trigger=trigger,
            )
        return triggers

    async def enrich_one_trigger(trigger: Dict[str, Any]) -> Dict[str, Any]:
        existing_people = trigger.get("people_to_reach_out") or []
        existing_stakeholders = [
            person
            for person in existing_people
            if isinstance(person, dict) and not person.get("is_original_profile")
        ]
        if existing_stakeholders:
            logger.info(
                "Predictive reachout enrichment reused existing people profile_id=%s trigger_type=%s people_count=%d",
                profile_id,
                trigger.get("trigger_type"),
                len(existing_people),
            )
            trigger["people_to_reach_out"] = _people_to_reach_out_with_original(
                profile_id=profile_id,
                trigger=trigger,
            )
            return trigger
        title_groups = await _stakeholder_title_groups(trigger, current_title)
        logger.info(
            "Predictive reachout title groups profile_id=%s trigger_type=%s audience=%s current_title=%s groups=%s",
            profile_id,
            trigger.get("trigger_type"),
            trigger.get("outreach_audience"),
            current_title,
            title_groups,
        )
        if not title_groups:
            trigger["people_to_reach_out"] = _people_to_reach_out_with_original(
                profile_id=profile_id,
                trigger=trigger,
            )
            return trigger

        mapped_results = await asyncio.gather(
            *[
                _profile_ids_by_title_and_company(
                    titles=title_group,
                    company_universal_name=company_universal_name,
                )
                for title_group in title_groups
            ],
            return_exceptions=True,
        )

        filtered_id_groups: List[Tuple[List[str], List[str]]] = []
        all_candidate_ids: List[str] = []
        for title_group, candidate_ids in zip(title_groups, mapped_results):
            if isinstance(candidate_ids, Exception):
                logger.warning(
                    "Skipping stakeholder title group after mapping failure titles=%s trigger_type=%s error=%s",
                    title_group,
                    trigger.get("trigger_type"),
                    candidate_ids,
                )
                continue
            if not isinstance(candidate_ids, list):
                logger.warning(
                    "Skipping stakeholder title group after invalid mapping response titles=%s trigger_type=%s",
                    title_group,
                    trigger.get("trigger_type"),
                )
                continue
            group_ids: List[str] = []
            for candidate_id in candidate_ids:
                if candidate_id == str(profile_id) or candidate_id in group_ids:
                    continue
                group_ids.append(candidate_id)
                if candidate_id not in all_candidate_ids:
                    all_candidate_ids.append(candidate_id)
                if len(group_ids) >= 10:
                    break
            if group_ids:
                filtered_id_groups.append((title_group, group_ids))

        logger.info(
            "Predictive reachout mapped candidates profile_id=%s trigger_type=%s groups=%d candidate_ids_count=%d candidate_ids=%s",
            profile_id,
            trigger.get("trigger_type"),
            len(filtered_id_groups),
            len(all_candidate_ids),
            all_candidate_ids[:20],
        )
        if not all_candidate_ids:
            trigger["people_to_reach_out"] = _people_to_reach_out_with_original(
                profile_id=profile_id,
                trigger=trigger,
            )
            return trigger

        async def fetch_candidate(
            candidate_id: str,
        ) -> Tuple[str, Optional[Dict[str, Any]]]:
            fetched_id, candidate_profile = await fetch_profile_by_es_id(
                candidate_id,
                es_client=es_client,
                source_fields=[
                    "public_identifier",
                    "full_name",
                    "name",
                    "headline",
                    "summary",
                    "experience",
                ],
                only_top_experience=False,
            )
            if not candidate_profile:
                return candidate_id, None
            return candidate_id, _compact_reachout_candidate(
                fetched_id or candidate_id,
                candidate_profile,
            )

        fetched_results = await asyncio.gather(
            *[fetch_candidate(candidate_id) for candidate_id in all_candidate_ids],
            return_exceptions=True,
        )
        candidate_profile_by_id = {}
        for candidate_id, result in zip(all_candidate_ids, fetched_results):
            if isinstance(result, Exception):
                logger.warning(
                    "Skipping stakeholder candidate after profile fetch failure profile_id=%s trigger_type=%s error=%s",
                    candidate_id,
                    trigger.get("trigger_type"),
                    result,
                )
                continue
            fetched_candidate_id, candidate_profile = result
            if candidate_profile:
                candidate_profile_by_id[fetched_candidate_id] = candidate_profile

        logger.info(
            "Predictive reachout fetched candidates profile_id=%s trigger_type=%s fetched_count=%d missing_count=%d",
            profile_id,
            trigger.get("trigger_type"),
            len(candidate_profile_by_id),
            len(all_candidate_ids) - len(candidate_profile_by_id),
        )

        async def validate_group(
            title_group: List[str],
            candidate_ids: List[str],
        ) -> List[Dict[str, Any]]:
            candidate_profiles = [
                candidate_profile_by_id[candidate_id]
                for candidate_id in candidate_ids
                if candidate_id in candidate_profile_by_id
            ]
            if not candidate_profiles:
                logger.info(
                    "Predictive reachout validation skipped empty candidates profile_id=%s trigger_type=%s titles=%s candidate_ids=%s",
                    profile_id,
                    trigger.get("trigger_type"),
                    title_group,
                    candidate_ids,
                )
                return []
            try:
                validated = await _validate_people_to_reach_out(
                    reference_profile=profile,
                    trigger=trigger,
                    candidate_profiles=candidate_profiles,
                )
                logger.info(
                    "Predictive reachout validation result profile_id=%s trigger_type=%s titles=%s candidates=%d validated=%d",
                    profile_id,
                    trigger.get("trigger_type"),
                    title_group,
                    len(candidate_profiles),
                    len(validated),
                )
                return validated
            except Exception as exc:
                logger.warning(
                    "Skipping stakeholder title group after validation failure titles=%s trigger_type=%s error=%s",
                    title_group,
                    trigger.get("trigger_type"),
                    exc,
                )
                return []

        validated_groups = await asyncio.gather(
            *[
                validate_group(title_group, candidate_ids)
                for title_group, candidate_ids in filtered_id_groups
            ],
            return_exceptions=True,
        )

        people_by_id: Dict[str, Dict[str, Any]] = {}
        for validated_people in validated_groups:
            if isinstance(validated_people, Exception):
                logger.warning(
                    "Skipping stakeholder title group after unexpected validation failure trigger_type=%s error=%s",
                    trigger.get("trigger_type"),
                    validated_people,
                )
                continue
            for person in validated_people:
                person_id = person.get("profile_id")
                if not person_id:
                    continue
                current_person = people_by_id.get(person_id)
                if not current_person or float(person.get("confidence") or 0.0) > float(
                    current_person.get("confidence") or 0.0
                ):
                    people_by_id[person_id] = person

        logger.info(
            "Predictive reachout merged stakeholders profile_id=%s trigger_type=%s stakeholder_count=%d stakeholder_ids=%s",
            profile_id,
            trigger.get("trigger_type"),
            len(people_by_id),
            list(people_by_id.keys())[:10],
        )
        trigger["people_to_reach_out"] = sorted(
            people_by_id.values(),
            key=lambda item: float(item.get("confidence") or 0.0),
            reverse=True,
        )[:4]
        trigger["people_to_reach_out"] = _people_to_reach_out_with_original(
            profile_id=profile_id,
            trigger=trigger,
        )
        return trigger

    enriched_results = await asyncio.gather(
        *[enrich_one_trigger(trigger) for trigger in triggers],
        return_exceptions=True,
    )
    enriched_triggers: List[Dict[str, Any]] = []
    for trigger, result in zip(triggers, enriched_results):
        if isinstance(result, Exception):
            logger.warning(
                "Predictive trigger reachout enrichment failed trigger_type=%s error=%s",
                trigger.get("trigger_type"),
                result,
            )
            trigger["people_to_reach_out"] = _people_to_reach_out_with_original(
                profile_id=profile_id,
                trigger=trigger,
            )
            enriched_triggers.append(trigger)
            continue
        enriched_triggers.append(result)
    return enriched_triggers


def _build_predictive_messages(
    profile_packet: Dict[str, Any],
) -> List[Dict[str, str]]:
    profile_packet_json = json.dumps(profile_packet, ensure_ascii=False, default=str)
    user_prompt = PREDICTIVE_ANALYSIS_USER_PROMPT_TEMPLATE.format(
        profile_packet_json=profile_packet_json
    )
    return [
        {"role": "system", "content": PREDICTIVE_ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _normalize_trigger_type(value: Any) -> str:
    text = _clean_text(value, 120).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:80] if text else ""


def _canonical_trigger_type(value: Any) -> str:
    normalized = _normalize_trigger_type(value)
    if not normalized:
        return ""
    normalized = TRIGGER_TYPE_ALIASES.get(normalized, normalized)
    if normalized in CANONICAL_TRIGGER_TYPES:
        return normalized
    return "other_high_conviction_trigger"


def _normalize_outreach_audience(value: Any) -> str:
    text = _clean_text(value, 80).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if text in {"person", "candidate", "executive", "individual"}:
        return "person"
    if text in {
        "manager",
        "stakeholder",
        "manager_stakeholder",
        "leadership_stakeholder",
        "board",
        "investor",
        "company",
    }:
        return "manager_stakeholder"
    if text in {"both", "person_and_manager", "person_and_stakeholder"}:
        return "both"
    return ""


def _validate_llm_output(
    parsed: Any,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")

    profile_summary = _clean_text(parsed.get("profile_summary"), 700)
    raw_triggers = parsed.get("triggers")
    if raw_triggers is None:
        raw_triggers = []
    if not isinstance(raw_triggers, list):
        raise ValueError("LLM response field 'triggers' must be a list")

    valid: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []

    for index, item in enumerate(raw_triggers):
        if not isinstance(item, dict):
            dropped.append({"index": index, "reason": "trigger_not_object"})
            continue

        confidence = _safe_number(item.get("confidence"))
        if confidence is None or confidence < MIN_TRIGGER_CONFIDENCE:
            dropped.append(
                {
                    "index": index,
                    "reason": "low_confidence",
                    "confidence": confidence,
                }
            )
            continue

        trigger_type = _canonical_trigger_type(item.get("trigger_type"))
        if not trigger_type:
            dropped.append({"index": index, "reason": "missing_trigger_type"})
            continue

        title = _clean_text(item.get("trigger_title") or item.get("news_title"), 220)
        description = _clean_text(
            item.get("trigger_description") or item.get("description"),
            1600,
        )
        reasoning = _clean_text(item.get("reasoning"), 1200)
        search_relevance = _clean_text(item.get("search_relevance"), 1200)
        evidence = _clean_list(item.get("evidence"), max_items=8, item_limit=360)
        outreach_audience = _normalize_outreach_audience(item.get("outreach_audience"))
        person_outreach_angle = _clean_text(item.get("person_outreach_angle"), 900)
        person_ctas = _clean_list(item.get("person_ctas"), max_items=4, item_limit=240)
        manager_outreach_angle = _clean_text(item.get("manager_outreach_angle"), 900)
        suggested_sub_category = _canonical_trigger_type(
            item.get("suggested_sub_category") or trigger_type
        )

        missing = []
        if not title:
            missing.append("trigger_title")
        if not description:
            missing.append("trigger_description")
        if not reasoning:
            missing.append("reasoning")
        if not search_relevance:
            missing.append("search_relevance")
        if not evidence:
            missing.append("evidence")
        if not outreach_audience:
            missing.append("outreach_audience")
        if outreach_audience in {"person", "both"} and not person_outreach_angle:
            missing.append("person_outreach_angle")
        if (
            outreach_audience in {"manager_stakeholder", "both"}
            and not manager_outreach_angle
        ):
            missing.append("manager_outreach_angle")
        if missing:
            dropped.append(
                {
                    "index": index,
                    "reason": "missing_required_fields",
                    "fields": missing,
                }
            )
            continue

        valid.append(
            {
                "trigger_type": trigger_type,
                "confidence": round(max(0.0, min(float(confidence), 1.0)), 4),
                "trigger_title": title,
                "trigger_description": description,
                "reasoning": reasoning,
                "evidence": evidence,
                "outreach_audience": outreach_audience,
                "person_outreach_angle": person_outreach_angle,
                "person_ctas": person_ctas,
                "manager_outreach_angle": manager_outreach_angle,
                "search_relevance": search_relevance,
                "suggested_sub_category": suggested_sub_category or trigger_type,
            }
        )

    best_by_type: Dict[str, Dict[str, Any]] = {}
    ordered_types: List[str] = []
    for item in valid:
        trigger_type = item["trigger_type"]
        if trigger_type not in best_by_type:
            best_by_type[trigger_type] = item
            ordered_types.append(trigger_type)
            continue
        if float(item.get("confidence") or 0.0) > float(
            best_by_type[trigger_type].get("confidence") or 0.0
        ):
            dropped.append(
                {
                    "trigger_type": trigger_type,
                    "reason": "duplicate_trigger_type_replaced_by_higher_confidence",
                }
            )
            best_by_type[trigger_type] = item
        else:
            dropped.append(
                {
                    "trigger_type": trigger_type,
                    "reason": "duplicate_trigger_type_lower_confidence",
                }
            )

    return profile_summary, [best_by_type[t] for t in ordered_types], dropped


def _profile_fingerprint(profile_packet: Dict[str, Any]) -> str:
    fingerprint_packet = dict(profile_packet)
    fingerprint_packet.pop("current_date", None)
    raw = json.dumps(
        fingerprint_packet,
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_experience_fingerprint(profile_packet: Dict[str, Any]) -> str:
    identity = profile_packet.get("identity") or {}
    experience_items = profile_packet.get("experience")
    if not isinstance(experience_items, list):
        experience_items = []

    compact_experience = []
    for exp in experience_items:
        if not isinstance(exp, dict):
            continue
        compact_experience.append(
            {
                "index": exp.get("index"),
                "title": exp.get("title"),
                "company": exp.get("company"),
                "company_universal_name": exp.get("company_universal_name"),
                "company_industry": exp.get("company_industry"),
                "company_type": exp.get("company_type"),
                "company_ownership": exp.get("company_ownership"),
                "company_size": exp.get("company_size"),
                "staff_count_range": exp.get("staff_count_range"),
                "start": exp.get("start"),
                "end": (
                    _clean_text(exp.get("end"), 80)
                    if exp.get("end", None) is not None
                    else "Present"
                ),
                "management_level": exp.get("management_level"),
            }
        )

    payload = {
        "profile_id": profile_packet.get("profile_id"),
        "headline": identity.get("headline"),
        "experience": compact_experience,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _trigger_doc_id(*, profile_id: str, trigger_type: str) -> str:
    seed = f"{profile_id}:{trigger_type}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"predictive_profile_trigger_{digest}"


def _normalize_existing_doc(hit: Dict[str, Any]) -> Dict[str, Any]:
    src = hit.get("_source") if isinstance(hit, dict) else None
    if not isinstance(src, dict):
        return {}
    out = dict(src)
    if hit.get("_id"):
        out["_id"] = hit.get("_id")
    return out


async def fetch_existing_predictive_trigger_docs(
    *,
    es_client_bd: Any,
    profile_id: str,
    index_name: str,
) -> List[Dict[str, Any]]:
    if es_client_bd is None:
        return []

    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return []

    body = {
        "query": {
            "bool": {
                "filter": [
                    {
                        "bool": {
                            "should": [
                                {"term": {"news_type.keyword": NEWS_TYPE}},
                                {"term": {"news_type": NEWS_TYPE}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"term": {"entity_id.keyword": profile_id}},
                                {"term": {"entity_id": profile_id}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                ]
            }
        },
        "sort": [{"cache_date": {"order": "desc", "unmapped_type": "long"}}],
        "size": 100,
        "_source": [
            "id",
            "news_id",
            "news_title",
            "content",
            "news_type",
            "news_sub_type",
            "published_date",
            "cache_date",
            "profile_id",
            "entity_id",
            "linkedin_identifier",
            "predictive_trigger_type",
            "predictive_suggested_sub_category",
            "predictive_trigger_confidence",
            "predictive_trigger_reasoning",
            "predictive_trigger_evidence",
            "predictive_trigger_outreach_audience",
            "predictive_trigger_person_outreach_angle",
            "predictive_trigger_person_ctas",
            "predictive_trigger_manager_outreach_angle",
            "predictive_trigger_search_relevance",
            "people_to_reach_out",
            "predictive_profile_summary",
            "predictive_profile_fingerprint",
            "predictive_experience_fingerprint",
            "predictive_first_generated_at",
            "predictive_last_generated_at",
            "predictive_generation_count",
        ],
    }

    res = await es_client_bd.search(index=index_name, body=body)
    hits = res.get("hits", {}).get("hits", []) if res is not None else []
    return [normalized for hit in hits if (normalized := _normalize_existing_doc(hit))]


def cached_docs_for_experience(
    existing_docs: List[Dict[str, Any]],
    experience_fingerprint: str,
) -> List[Dict[str, Any]]:
    if not experience_fingerprint:
        return []

    by_type: Dict[str, Dict[str, Any]] = {}
    for doc in existing_docs:
        if doc.get("predictive_experience_fingerprint") != experience_fingerprint:
            continue
        trigger_type = _canonical_trigger_type(
            doc.get("predictive_trigger_type") or doc.get("news_sub_type")
        )
        if not trigger_type:
            continue
        current = by_type.get(trigger_type)
        if not current or int(doc.get("cache_date") or 0) > int(
            current.get("cache_date") or 0
        ):
            by_type[trigger_type] = doc
    return list(by_type.values())


def existing_docs_by_trigger_type(
    existing_docs: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    by_type: Dict[str, Dict[str, Any]] = {}
    for doc in existing_docs:
        trigger_type = _canonical_trigger_type(
            doc.get("predictive_trigger_type") or doc.get("news_sub_type")
        )
        if not trigger_type:
            continue
        current = by_type.get(trigger_type)
        if not current or int(doc.get("cache_date") or 0) > int(
            current.get("cache_date") or 0
        ):
            by_type[trigger_type] = doc
    return by_type


def trigger_payload_from_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    trigger_type = _canonical_trigger_type(
        doc.get("predictive_trigger_type") or doc.get("news_sub_type")
    )
    evidence = doc.get("predictive_trigger_evidence")
    if isinstance(evidence, str):
        try:
            loaded = json.loads(evidence)
            evidence = loaded if isinstance(loaded, list) else [evidence]
        except Exception:
            evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []

    payload = {
        "trigger_type": trigger_type,
        "confidence": _safe_number(doc.get("predictive_trigger_confidence")) or 0.0,
        "trigger_title": _clean_text(doc.get("news_title"), 220),
        "trigger_description": _clean_text(doc.get("content"), 1600),
        "reasoning": _clean_text(doc.get("predictive_trigger_reasoning"), 1200),
        "evidence": [
            cleaned for item in evidence if (cleaned := _clean_text(item, 360))
        ],
        "outreach_audience": _normalize_outreach_audience(
            doc.get("predictive_trigger_outreach_audience")
        ),
        "person_outreach_angle": _clean_text(
            doc.get("predictive_trigger_person_outreach_angle"), 900
        ),
        "person_ctas": _clean_list(
            doc.get("predictive_trigger_person_ctas"),
            max_items=4,
            item_limit=240,
        ),
        "manager_outreach_angle": _clean_text(
            doc.get("predictive_trigger_manager_outreach_angle"), 900
        ),
        "search_relevance": _clean_text(
            doc.get("predictive_trigger_search_relevance"), 1200
        ),
        "people_to_reach_out": _minimal_people_to_reach_out(
            doc.get("people_to_reach_out")
        ),
        "suggested_sub_category": _canonical_trigger_type(
            doc.get("predictive_suggested_sub_category") or trigger_type
        ),
        "news_id": doc.get("news_id") or doc.get("id") or doc.get("_id"),
    }
    payload["people_to_reach_out"] = _people_to_reach_out_with_original(
        profile_id=str(doc.get("profile_id") or doc.get("entity_id") or ""),
        trigger=payload,
    )
    return payload


def trigger_payloads_from_docs(
    docs: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    profile_summary = ""
    triggers: List[Dict[str, Any]] = []
    for doc in docs:
        if not profile_summary:
            profile_summary = _clean_text(doc.get("predictive_profile_summary"), 700)
        payload = trigger_payload_from_doc(doc)
        if payload.get("trigger_type"):
            triggers.append(payload)
    return profile_summary, triggers


def build_predictive_trigger_docs(
    *,
    profile_id: str,
    profile: Dict[str, Any],
    profile_packet: Dict[str, Any],
    profile_summary: str,
    triggers: List[Dict[str, Any]],
    existing_docs_by_type: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    fingerprint = _profile_fingerprint(profile_packet)
    experience_fingerprint = build_experience_fingerprint(profile_packet)
    public_identifier = _clean_text(profile.get("public_identifier"), 240)
    full_name = _clean_text(profile.get("full_name") or profile.get("name"), 240)
    headline = _clean_text(profile.get("headline"), 400)
    linkedin_identifier = public_identifier or str(profile_id)
    existing_docs_by_type = existing_docs_by_type or {}

    docs: List[Dict[str, Any]] = []
    for trigger in triggers:
        trigger_type = trigger["trigger_type"]
        existing_doc = existing_docs_by_type.get(trigger_type) or {}
        doc_id = (
            existing_doc.get("_id")
            or existing_doc.get("id")
            or _trigger_doc_id(
                profile_id=profile_id,
                trigger_type=trigger_type,
            )
        )
        previous_title = _clean_text(existing_doc.get("news_title"), 220)
        try:
            generation_count = int(existing_doc.get("predictive_generation_count") or 0)
        except (TypeError, ValueError):
            generation_count = 0
        generation_count += 1
        first_generated_at = int(
            existing_doc.get("predictive_first_generated_at")
            or existing_doc.get("published_date")
            or now_ts
        )
        docs.append(
            {
                "id": doc_id,
                "news_id": doc_id,
                "linkedin_identifier": linkedin_identifier,
                "entity_urn": profile.get("entity_urn"),
                "entity_id": str(profile_id),
                "profile_id": str(profile_id),
                "entity_type": ENTITY_TYPE,
                "author": full_name,
                "author_description": headline,
                "last_author_name": full_name,
                "last_author_description": headline,
                "news_title": trigger["trigger_title"],
                "content": trigger["trigger_description"],
                "link": "",
                "source": SOURCE,
                "published_date": now_ts,
                "published_date_relative": "just now",
                "post_type": "SYNTHETIC",
                "news_type": NEWS_TYPE,
                "news_sub_type": trigger_type,
                "cache_date": now_ts,
                "expiration_days": 60,
                "predictive_trigger_type": trigger_type,
                "predictive_suggested_sub_category": trigger.get(
                    "suggested_sub_category"
                ),
                "predictive_trigger_confidence": trigger["confidence"],
                "predictive_trigger_reasoning": trigger["reasoning"],
                "predictive_trigger_evidence": trigger["evidence"],
                "predictive_trigger_outreach_audience": trigger["outreach_audience"],
                "predictive_trigger_person_outreach_angle": trigger[
                    "person_outreach_angle"
                ],
                "predictive_trigger_person_ctas": _clean_list(
                    trigger.get("person_ctas"),
                    max_items=4,
                    item_limit=240,
                ),
                "predictive_trigger_manager_outreach_angle": trigger[
                    "manager_outreach_angle"
                ],
                "predictive_trigger_search_relevance": trigger["search_relevance"],
                "people_to_reach_out": _people_to_reach_out_with_original(
                    profile_id=profile_id,
                    trigger=trigger,
                ),
                "predictive_profile_summary": profile_summary,
                "predictive_profile_fingerprint": fingerprint,
                "predictive_experience_fingerprint": experience_fingerprint,
                "predictive_first_generated_at": first_generated_at,
                "predictive_last_generated_at": now_ts,
                "predictive_generation_count": generation_count,
                "predictive_previous_trigger_title": (
                    previous_title
                    if previous_title and previous_title != trigger["trigger_title"]
                    else ""
                ),
                "imagecount": 0,
                "videocount": 0,
            }
        )
    return docs


async def generate_predictive_triggers(
    profile_packet: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], str]:
    response = await invoke(
        model="openai/gpt-5.1",
        temperature=0.35,
        fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4.1"],
        messages=_build_predictive_messages(profile_packet),
        max_tokens=2500,
    )
    response_text = str(response or "")
    parsed = _safe_json_loads_from_text(response_text)
    profile_summary, triggers, dropped = _validate_llm_output(parsed)
    return profile_summary, triggers, dropped, response_text
