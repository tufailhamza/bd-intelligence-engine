import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from elasticsearch import AsyncElasticsearch
from qutils.llm.asynchronous import invoke

from app.utils.me_summary.prompts import (
    ME_SUMMARY_SYSTEM_PROMPT,
    ME_SUMMARY_USER_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)


def _safe_json_loads_from_text(text: str) -> Any:
    if not text:
        raise ValueError("Empty response")

    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", t).replace("```", "").strip()

    # Prefer object extraction if extra text surrounds JSON.
    obj = re.search(r"\{[\s\S]*\}", t)
    return json.loads(obj.group(0) if obj else t)


def _select_profile_context(profile: Dict[str, Any]) -> str:
    lines: List[str] = []

    full_name = profile.get("full_name") or profile.get("name") or ""
    headline = profile.get("headline") or ""
    summary = profile.get("summary") or ""
    location = profile.get("location") or ""

    if full_name:
        lines.append(f"Name: {full_name}")
    if headline:
        lines.append(f"Headline: {headline}")
    if location:
        lines.append(f"Location: {location}")
    if summary:
        lines.append(f"Summary: {summary}")

    exp = profile.get("experience")
    if isinstance(exp, list) and exp:
        lines.append("Current/Recent Experience:")
        for e in exp[:3]:
            if not isinstance(e, dict):
                continue
            title = e.get("title") or ""
            company = e.get("company") or e.get("company_name") or ""
            industry = e.get("company_industry") or ""
            role_line = " - ".join(x for x in [title, company] if x)
            if role_line:
                if industry:
                    lines.append(f"- {role_line} ({industry})")
                else:
                    lines.append(f"- {role_line}")

    skills = profile.get("skills")
    if isinstance(skills, list) and skills:
        top_skills = [
            str(s).strip() for s in skills[:12] if isinstance(s, str) and s.strip()
        ]
        if top_skills:
            lines.append(f"Skills: {', '.join(top_skills)}")

    return "\n".join(lines)[:7000]


async def fetch_profile_by_public_identifier(
    *,
    public_identifier: str,
    es_client: AsyncElasticsearch,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Fetch one profile from ES using common universal/public identifier field variants.
    """
    index = (os.getenv("ES_PROFILES_INDEX") or "").strip()
    if not index:
        raise RuntimeError("ES_PROFILES_INDEX env var is not set")

    identifier = (public_identifier or "").strip()
    if not identifier:
        return None, None

    body = {
        "query": {
            "bool": {
                "should": [
                    {"term": {"public_identifier.keyword": identifier}},
                    {"term": {"public_identifier": identifier}},
                    {"term": {"publicIdentifier.keyword": identifier}},
                    {"term": {"publicIdentifier": identifier}},
                    {"term": {"universal_name.keyword": identifier}},
                    {"term": {"universal_name": identifier}},
                ],
                "minimum_should_match": 1,
            }
        },
        "size": 1,
    }

    res = await es_client.search(index=index, body=body)
    hits = res.get("hits", {}).get("hits", []) or []
    if not hits:
        return None, None

    hit = hits[0]
    es_id = hit.get("_id")
    src = hit.get("_source")
    if not isinstance(src, dict):
        return es_id, None
    return es_id, src


async def generate_ai_summary_and_specializations(
    *,
    profile: Dict[str, Any],
    model: str = "openai/gpt-4o",
) -> Dict[str, Any]:
    profile_context = _select_profile_context(profile)
    user_prompt = ME_SUMMARY_USER_PROMPT_TEMPLATE.format(
        profile_context=profile_context
    )

    response = await invoke(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": ME_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        fallbacks=["openai/gpt-4.1"],
    )

    parsed = _safe_json_loads_from_text(str(response))
    if not isinstance(parsed, dict):
        raise ValueError("LLM output is not a JSON object")

    ai_summary = str(parsed.get("ai_summary") or "").strip()
    specialization_areas_raw = parsed.get("specialization_areas")

    specialization_areas: List[str] = []
    if isinstance(specialization_areas_raw, list):
        for item in specialization_areas_raw:
            if isinstance(item, str) and item.strip():
                specialization_areas.append(item.strip())

    # Defensive fallback to keep response schema stable.
    if not ai_summary:
        ai_summary = "No summary could be generated from the current profile context."
    else:
        # Enforce second-person style for frontend consistency.
        if not ai_summary.lower().startswith("you are"):
            full_name = str(
                profile.get("full_name") or profile.get("name") or ""
            ).strip()
            if full_name and ai_summary.lower().startswith(full_name.lower()):
                remainder = ai_summary[len(full_name) :].lstrip(" ,.-")
                if remainder.lower().startswith("is "):
                    remainder = remainder[3:]
                ai_summary = f"You are {remainder}".strip()
            else:
                ai_summary = f"You are {ai_summary}".strip()

    if not specialization_areas:
        specialization_areas = ["General leadership", "Business development"]

    return {
        "ai_summary": ai_summary,
        "specialization_areas": specialization_areas[:8],
    }
