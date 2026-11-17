import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from openai import AsyncClient
from qutils.llm.asynchronous import invoke

logger = logging.getLogger(__name__)

NEWS_SEMANTIC_MODEL = os.getenv("NEWS_SEMANTIC_MODEL", "text-embedding-3-large")
NEWS_SEMANTIC_DIMENSIONS = int(os.getenv("NEWS_SEMANTIC_DIMENSIONS", "3072"))
PROFILE_RUBRIC_VERSION = os.getenv("PROFILE_NEWS_RUBRIC_VERSION", "v1")

BD_TRIGGER_KEYWORDS = [
    "funding",
    "IPO",
    "hiring",
    "executive",
    "recruitment",
    "acquisition",
    "merger",
    "expansion",
    "partnership",
    "CEO",
    "CFO",
    "earnings",
    "quarterly results",
    "departure",
    "vacancies",
    "layoffs",
    "restructuring",
    "strategy",
    "product launch",
    "board appointment",
    "new product",
]

SECONDARY_TRIGGER_KEYWORDS = [
    "financial results",
    "regulation",
    "litigation",
    "settlement",
    "closure",
    "opening",
    "venture",
    "alliance",
    "competition",
    "disruption",
    "contract",
    "loss",
    "technology",
    "crisis",
    "instability",
    "transition",
    "uncertainty",
]

DEFAULT_EVENT_TYPE_WEIGHTS = {
    "leadership_change": 1.25,
    "layoffs_downsizing": 1.20,
    "restructuring_hq_relocation": 1.15,
    "acquisition_merger": 1.20,
    "ipo_market_listing": 1.15,
    "funding_investment": 1.12,
    "hiring_talent_acquisition": 1.10,
    "market_expansion": 1.00,
    "partnership_joint_venture": 0.95,
    "earnings_revenue_report": 0.92,
    "new_product_launch": 0.85,
    "new_service_feature": 0.78,
    "beta_pilot_release": 0.72,
    "company_linkedin_repost": 0.75,
    "job_change": 0.82,
    "company_news": 0.80,
    "achievements": 0.55,
    "publication": 0.50,
    "social_impact": 0.48,
    "engagement": 0.35,
    "personal_reflection": 0.30,
    "others": 0.28,
}

EVENT_PRIORITY_BY_LABEL = {
    # Highest BD / mandate creation potential
    "leadership_change": 1.00,
    "acquisition_merger": 0.98,
    "restructuring_hq_relocation": 0.96,
    "layoffs_downsizing": 0.94,
    "ipo_market_listing": 0.93,
    # Strong commercial / growth / ownership-change signals
    "funding_investment": 0.90,
    "hiring_talent_acquisition": 0.88,
    "market_expansion": 0.84,
    "earnings_revenue_report": 0.82,
    "partnership_joint_venture": 0.76,
    # Useful but usually needs stronger relationship/context match
    "job_change": 0.74,
    "company_news": 0.66,
    "new_product_launch": 0.62,
    "new_service_feature": 0.56,
    "beta_pilot_release": 0.48,
    # Content / relationship-warming signals
    "publication": 0.46,
    "achievements": 0.44,
    "company_linkedin_repost": 0.38,
    "social_impact": 0.34,
    "personal_reflection": 0.30,
    "engagement": 0.26,
    # Fallback
    "others": 0.20,
}


def _clean_text(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit]


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


def _top_experience(profile: Dict[str, Any]) -> Dict[str, Any]:
    exp = profile.get("experience")
    if isinstance(exp, list) and exp and isinstance(exp[0], dict):
        return exp[0]
    return {}


def profile_to_compact_text(profile: Dict[str, Any]) -> str:
    if not isinstance(profile, dict):
        return ""

    fields: List[str] = []
    for key in [
        "full_name",
        "name",
        "headline",
        "title",
        "industry",
        "location",
        "summary",
        "about",
        "description",
    ]:
        value = _clean_text(profile.get(key), 1000)
        if value:
            fields.append(f"{key}: {value}")

    exp = _top_experience(profile)
    if exp:
        title = _clean_text(exp.get("title"), 300)
        company = _clean_text(
            exp.get("company")
            or exp.get("company_name")
            or exp.get("companyName")
            or exp.get("name"),
            300,
        )
        industry = _clean_text(exp.get("industry"), 300)
        description = _clean_text(exp.get("description"), 800)
        company_universal = _clean_text(
            exp.get("company_universal_name")
            or exp.get("company_universalname")
            or exp.get("companyUniversalName")
            or exp.get("companyUniversalname"),
            300,
        )
        fields.append(
            "current_experience: "
            f"title={title}; company={company}; company_universal={company_universal}; "
            f"industry={industry}; description={description}"
        )

    return "\n".join(fields)[:5000]


def profile_fingerprint(profile: Dict[str, Any]) -> str:
    compact = profile_to_compact_text(profile)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def event_priority_for_news(doc: Dict[str, Any]) -> Dict[str, Any]:
    news_sub_type = _clean_text(doc.get("news_sub_type"), 120).lower()
    news_type = _clean_text(doc.get("news_type"), 120).lower()
    post_type = _clean_text(doc.get("post_type"), 60).lower()

    label = news_sub_type or news_type or "others"
    priority = EVENT_PRIORITY_BY_LABEL.get(label)
    if priority is None:
        priority = EVENT_PRIORITY_BY_LABEL.get(
            news_type, EVENT_PRIORITY_BY_LABEL["others"]
        )

    if post_type == "repost" and label != "company_linkedin_repost":
        priority = max(0.0, priority - 0.08)

    return {
        "event_priority_label": label,
        "event_priority_score": round(float(priority), 4),
    }


def build_news_semantic_text(doc: Dict[str, Any]) -> str:
    priority = event_priority_for_news(doc)
    parts = [
        f"Title: {_clean_text(doc.get('news_title'), 500)}",
        f"Content: {_clean_text(doc.get('content'), 1800)}",
        f"Entity type: {_clean_text(doc.get('entity_type'), 120)}",
        f"News type: {_clean_text(doc.get('news_type'), 120)}",
        f"News subtype: {_clean_text(doc.get('news_sub_type'), 120)}",
        f"Event priority label: {priority['event_priority_label']}",
        f"Company: {_clean_text(doc.get('company_name'), 300)}",
        f"Author: {_clean_text(doc.get('author'), 300)}",
        f"Author description: {_clean_text(doc.get('author_description'), 500)}",
        f"Profile owner: {_clean_text(doc.get('last_author_name'), 300)}",
        f"Profile owner description: {_clean_text(doc.get('last_author_description'), 500)}",
        "Primary BD trigger keywords: " + ", ".join(BD_TRIGGER_KEYWORDS),
        "Secondary BD trigger keywords: " + ", ".join(SECONDARY_TRIGGER_KEYWORDS),
    ]
    return "\n".join(part for part in parts if part and not part.endswith(": "))


async def embed_texts(
    texts: List[str],
    *,
    model: str = NEWS_SEMANTIC_MODEL,
    batch_size: int = 128,
) -> List[List[float]]:
    if not texts:
        return []

    client = AsyncClient()
    out: List[List[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        res = await client.embeddings.create(input=batch, model=model)
        out.extend([list(item.embedding) for item in res.data])
    return out


async def enrich_news_docs_with_semantics(
    docs: List[Dict[str, Any]],
    *,
    model: str = NEWS_SEMANTIC_MODEL,
) -> List[Dict[str, Any]]:
    if not docs:
        return docs

    semantic_texts: List[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for doc in docs:
        priority = event_priority_for_news(doc)
        doc.update(priority)
        doc["semantic_text"] = build_news_semantic_text(doc)
        doc["semantic_model"] = model
        doc["semantic_created_at"] = now_iso
        semantic_texts.append(doc["semantic_text"])

    try:
        embeddings = await embed_texts(semantic_texts, model=model)
    except Exception as exc:
        logger.error("Failed to embed %d news docs: %s", len(docs), exc)
        return docs

    for doc, embedding in zip(docs, embeddings):
        doc["semantic_embedding"] = embedding

    return docs


def default_profile_news_rubric(profile: Dict[str, Any]) -> Dict[str, Any]:
    compact = profile_to_compact_text(profile)
    return {
        "semantic_query_text": (
            "Executive search and business development trigger relevance for this profile. "
            f"Prioritize {', '.join(BD_TRIGGER_KEYWORDS)}. Profile context: {compact[:1800]}"
        ),
        "semantic_keywords": BD_TRIGGER_KEYWORDS + SECONDARY_TRIGGER_KEYWORDS[:8],
        "event_type_weights": DEFAULT_EVENT_TYPE_WEIGHTS,
        "industry_focus": [],
        "seniority_focus": [],
        "role_function_focus": [],
        "negative_or_low_value_signals": [
            "generic commentary",
            "personal reflection",
            "low context engagement",
        ],
    }


def normalize_profile_rubric(
    raw: Dict[str, Any], profile: Dict[str, Any]
) -> Dict[str, Any]:
    fallback = default_profile_news_rubric(profile)
    if not isinstance(raw, dict):
        raw = {}

    semantic_keywords = raw.get("semantic_keywords")
    if not isinstance(semantic_keywords, list):
        semantic_keywords = fallback["semantic_keywords"]
    semantic_keywords = [str(x).strip() for x in semantic_keywords if str(x).strip()][
        :80
    ]

    weights = dict(DEFAULT_EVENT_TYPE_WEIGHTS)
    raw_weights = raw.get("event_type_weights")
    if isinstance(raw_weights, dict):
        for key, value in raw_weights.items():
            try:
                weights[str(key).strip()] = max(0.0, min(1.5, float(value)))
            except Exception:
                continue

    semantic_query_text = _clean_text(raw.get("semantic_query_text"), 4000)
    if not semantic_query_text:
        semantic_query_text = fallback["semantic_query_text"]

    return {
        "semantic_query_text": semantic_query_text,
        "semantic_keywords": semantic_keywords,
        "event_type_weights": weights,
        "industry_focus": (
            raw.get("industry_focus")
            if isinstance(raw.get("industry_focus"), list)
            else []
        ),
        "seniority_focus": (
            raw.get("seniority_focus")
            if isinstance(raw.get("seniority_focus"), list)
            else []
        ),
        "role_function_focus": (
            raw.get("role_function_focus")
            if isinstance(raw.get("role_function_focus"), list)
            else []
        ),
        "negative_or_low_value_signals": (
            raw.get("negative_or_low_value_signals")
            if isinstance(raw.get("negative_or_low_value_signals"), list)
            else fallback["negative_or_low_value_signals"]
        ),
    }


async def generate_profile_news_rubric(profile: Dict[str, Any]) -> Dict[str, Any]:
    profile_text = profile_to_compact_text(profile)
    if not profile_text:
        return default_profile_news_rubric(profile)

    prompt = f"""
You are creating a personalized news-ranking rubric for executive-search business development.

Use the profile to decide which business trigger events matter most for outreach.
The rubric must emphasize these BD trigger keywords where relevant:
{", ".join(BD_TRIGGER_KEYWORDS)}

Secondary trigger context:
{", ".join(SECONDARY_TRIGGER_KEYWORDS)}

Return ONLY strict JSON with these keys:
{{
  "semantic_query_text": "compact personalized semantic search query",
  "semantic_keywords": ["keyword", "..."],
  "event_type_weights": {{
    "leadership_change": 1.0,
    "layoffs_downsizing": 1.0,
    "restructuring_hq_relocation": 1.0,
    "acquisition_merger": 1.0,
    "ipo_market_listing": 1.0,
    "funding_investment": 1.0,
    "hiring_talent_acquisition": 1.0,
    "market_expansion": 1.0,
    "partnership_joint_venture": 1.0,
    "earnings_revenue_report": 1.0,
    "new_product_launch": 1.0,
    "company_linkedin_repost": 1.0,
    "job_change": 1.0,
    "achievements": 1.0,
    "publication": 1.0,
    "engagement": 1.0,
    "personal_reflection": 1.0,
    "others": 1.0
  }},
  "industry_focus": ["..."],
  "seniority_focus": ["..."],
  "role_function_focus": ["..."],
  "negative_or_low_value_signals": ["..."]
}}

Weights must be numeric from 0.0 to 1.5. Higher means more relevant for this specific profile.

PROFILE:
{profile_text}
""".strip()

    try:
        response = await invoke(
            model="openai/gpt-4.1-mini",
            temperature=0,
            fallbacks=["openai/gpt-4o-mini", "openai/gpt-4o"],
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = _safe_json_loads_from_text(str(response))
        return normalize_profile_rubric(parsed, profile)
    except Exception as exc:
        logger.error("Failed to generate profile news rubric: %s", exc)
        return default_profile_news_rubric(profile)


async def build_profile_semantics(profile: Dict[str, Any]) -> Dict[str, Any]:
    rubric = await generate_profile_news_rubric(profile)
    embedding_text = (
        rubric["semantic_query_text"]
        + "\nImportant keywords: "
        + ", ".join(rubric.get("semantic_keywords") or [])
    )
    embeddings = await embed_texts([embedding_text])
    rubric["semantic_embedding"] = embeddings[0] if embeddings else []
    rubric["semantic_model"] = NEWS_SEMANTIC_MODEL
    rubric["rubric_version"] = PROFILE_RUBRIC_VERSION
    return rubric
