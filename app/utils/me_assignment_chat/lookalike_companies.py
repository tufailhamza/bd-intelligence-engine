import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession, ClientTimeout
from elasticsearch import AsyncElasticsearch

logger = logging.getLogger(__name__)

LOOKALIKE_OPPORTUNITY_TYPE = "LOOKALIKE COMPANIES"
MAX_LOOKALIKE_COMPANIES = 8
MAX_LOOKALIKE_SUMMARY_SENTENCES = 3
MAX_LOOKALIKE_SUMMARY_CHARS = 400
FAST_COMP_GEN_TIMEOUT = 600

_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "\U00002600-\U000026ff"
    "]+",
    flags=re.UNICODE,
)


def _to_clean_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


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


def _debug_json_preview(value: Any, *, limit: int = 1500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _fast_comp_gen_service_headers() -> Dict[str, str]:
    raw_api_key = (os.getenv("FAST_COMP_API_KEY") or "").strip()
    api_key = raw_api_key
    if raw_api_key.lower().startswith("bearer "):
        api_key = raw_api_key.split(" ", 1)[1].strip()

    headers = {"Content-Type": "application/json", "accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _map_comp_gen_chunk_to_company_entry(
    chunk: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if chunk.get("excluded") is True:
        return None

    es_data = chunk.get("es_data")
    if not isinstance(es_data, dict):
        return None

    universal_name = es_data.get("universalName")
    if not universal_name:
        return None

    return {
        "id": es_data.get("es_id"),
        "name": es_data.get("name"),
        "industry": es_data.get("industry"),
        "universalName": universal_name,
        "urn": es_data.get("urn"),
        "logo": "",
    }


async def call_fast_comp_gen_endpoint(userquery: str) -> tuple[list, list, str]:
    url = (os.getenv("FAST_COMP_GEN_URL") or "").strip()
    if not url:
        logger.warning("[fast_comp_gen] FAST_COMP_GEN_URL is not configured")
        return [], [], "CURRENT"

    logger.info(
        "[fast_comp_gen] input userquery=%s",
        _debug_json_preview(userquery, limit=800),
    )
    service_headers = _fast_comp_gen_service_headers()
    if "x-api-key" not in service_headers:
        logger.warning("[fast_comp_gen] no API key configured (FAST_COMP_API_KEY)")

    current_comps: list = []
    past_comps: list = []
    raw_chunk_count = 0

    try:
        start = time.perf_counter()
        async with ClientSession() as session:
            async with session.post(
                url,
                json={"userquery": userquery},
                headers=service_headers,
                timeout=ClientTimeout(total=FAST_COMP_GEN_TIMEOUT),
            ) as response:
                logger.info(
                    "[fast_comp_gen] status=%s url=%s",
                    response.status,
                    url,
                )
                if response.status != 200:
                    response_body = await response.text()
                    logger.warning(
                        "[fast_comp_gen] non-200 body=%s",
                        _debug_json_preview(response_body, limit=1000),
                    )
                    return [], [], "CURRENT"

                async for chunk in response.content:
                    if not chunk:
                        continue
                    chunk = chunk.strip().decode()
                    if not chunk:
                        continue
                    chunk = chunk.replace("data: ", "")
                    raw_chunk_count += 1
                    try:
                        parsed = json.loads(chunk)
                    except Exception as decode_err:
                        logger.warning(
                            "[fast_comp_gen] skipping bad chunk #%s decode_error=%s raw=%s",
                            raw_chunk_count,
                            decode_err,
                            _debug_json_preview(chunk, limit=300),
                        )
                        continue

                    if not isinstance(parsed, dict):
                        continue

                    logger.info(
                        "[fast_comp_gen] chunk #%s preview=%s",
                        raw_chunk_count,
                        _debug_json_preview(parsed, limit=500),
                    )

                    company_entry = _map_comp_gen_chunk_to_company_entry(parsed)
                    if not company_entry:
                        continue

                    if parsed.get("type") == "CURRENT":
                        current_comps.append(company_entry)
                    else:
                        past_comps.append(company_entry)

        end = time.perf_counter()
        logger.info("Fast Comp Gen URL took %s seconds", end - start)
        logger.info(
            "[fast_comp_gen] result chunks=%s current_count=%s past_count=%s current_sample=%s past_sample=%s",
            raw_chunk_count,
            len(current_comps),
            len(past_comps),
            _debug_json_preview(current_comps[:3], limit=1000),
            _debug_json_preview(past_comps[:3], limit=1000),
        )
        return current_comps, past_comps, "CURRENT"
    except Exception as err:
        logger.error(f"Error in Fast Comp Gen URL service {err}")
        return [], [], "CURRENT"


def _is_lookalike_opportunity(opportunity: Dict[str, Any]) -> bool:
    if not isinstance(opportunity, dict):
        return False
    text = " ".join(
        str(opportunity.get(key) or "") for key in ("type", "title", "action")
    ).lower()
    return "lookalike" in text


def pick_lookalike_opportunity(
    opportunities: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for opportunity in opportunities:
        if _is_lookalike_opportunity(opportunity):
            return dict(opportunity)
    return None


def exclude_lookalike_opportunities(
    opportunities: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [
        opportunity
        for opportunity in opportunities
        if isinstance(opportunity, dict) and not _is_lookalike_opportunity(opportunity)
    ]


def _normalize_review_lookalike_opportunity(
    opportunity: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "type": LOOKALIKE_OPPORTUNITY_TYPE,
        "title": _to_clean_string(opportunity.get("title")) or "",
        "action": _to_clean_string(opportunity.get("action")) or "",
        "why_now": _to_clean_string(opportunity.get("why_now")) or "",
        "related_person_name": None,
        "related_person_public_identifier": None,
        "cta_label": None,
    }


def prepare_review_opportunities(
    opportunities: List[Dict[str, Any]],
    *,
    assignment_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Review keeps a single LOOKALIKE COMPANIES text card (no companies[]).
    Company lists are returned only from /lookalike_companies.
    """
    non_lookalike: List[Dict[str, Any]] = []
    lookalike: Optional[Dict[str, Any]] = None

    for opportunity in opportunities:
        if not isinstance(opportunity, dict):
            continue
        if _is_lookalike_opportunity(opportunity):
            if lookalike is None:
                lookalike = _normalize_review_lookalike_opportunity(opportunity)
            continue
        non_lookalike.append(dict(opportunity))

    if lookalike is None:
        lookalike = build_default_lookalike_opportunity(assignment_data or {})

    return [*non_lookalike, lookalike]


def build_review_summary_chips(
    *,
    opportunities: List[Dict[str, Any]],
    feed_impact: Dict[str, Any],
) -> List[Dict[str, Any]]:
    non_lookalike_count = sum(
        1
        for item in opportunities
        if isinstance(item, dict) and not _is_lookalike_opportunity(item)
    )
    has_lookalike = any(
        isinstance(item, dict) and _is_lookalike_opportunity(item)
        for item in opportunities
    )

    chips: List[Dict[str, Any]] = [
        {
            "key": "suggested_triggers",
            "label": "Triggers Created",
            "count": len(feed_impact.get("will_increase_priority_for") or [])
            + len(feed_impact.get("trigger_rules_created") or []),
        },
        {
            "key": "relationship_actions",
            "label": "Relationship Actions",
            "count": non_lookalike_count,
        },
    ]
    if has_lookalike:
        chips.append(
            {
                "key": "lookalike_companies",
                "label": "Lookalike Companies",
                "count": 1,
            }
        )
    return chips


def build_default_lookalike_opportunity(
    assignment_data: Dict[str, Any],
) -> Dict[str, Any]:
    client_entries = _normalize_client_entries(assignment_data.get("client"))
    client_names = [
        _to_clean_string(entry.get("display_name"))
        for entry in client_entries
        if _to_clean_string(entry.get("display_name"))
    ]
    industry = _to_clean_string(assignment_data.get("industry")) or "similar"
    mandate = _to_clean_string(assignment_data.get("mandate"))
    primary_client = client_names[0] if client_names else None

    if primary_client:
        title = f"Review lookalike {industry} accounts"
        action = (
            f"Identify {industry.lower()} companies similar to {primary_client} "
            "that may benefit from this type of assignment."
        )
    else:
        title = f"Review lookalike {industry} accounts"
        action = (
            f"Identify similar {industry.lower()} companies that may benefit "
            "from this type of assignment."
        )

    why_now = mandate or (
        "Use this completed assignment as proof for outreach to similar accounts."
    )

    return {
        "type": LOOKALIKE_OPPORTUNITY_TYPE,
        "title": title,
        "action": action,
        "why_now": why_now,
        "related_person_name": None,
        "related_person_public_identifier": None,
        "cta_label": None,
    }


def _normalize_company_urn_value(urn: Any) -> Optional[str]:
    text = _to_clean_string(urn)
    if not text:
        return None
    if ":" in text:
        return text.rsplit(":", 1)[-1].strip() or None
    return text


def _lookalike_employee_size_range(
    client_entries: List[Dict[str, Any]],
) -> Optional[tuple[int, int]]:
    employee_counts: List[float] = []
    for entry in client_entries:
        raw_count = entry.get("employee_count")
        if raw_count is None:
            continue
        try:
            employee_counts.append(float(raw_count))
        except (TypeError, ValueError):
            continue

    if not employee_counts:
        return None

    avg_size = sum(employee_counts) / len(employee_counts)
    min_size = max(1, int(avg_size * 0.5))
    max_size = max(min_size + 1, int(avg_size * 1.5))
    return min_size, max_size


def _build_lookalike_comp_gen_prompts(assignment_data: Dict[str, Any]) -> List[str]:
    client_entries = _normalize_client_entries(assignment_data.get("client"))
    client_names = [
        _to_clean_string(entry.get("display_name"))
        for entry in client_entries
        if _to_clean_string(entry.get("display_name"))
    ]

    industry = _to_clean_string(assignment_data.get("industry"))
    mandate = _to_clean_string(assignment_data.get("mandate"))
    size_range = _lookalike_employee_size_range(client_entries)

    prompts: List[str] = []
    seen_prompts = set()

    def _add_prompt(*parts: str) -> None:
        prompt = " ".join(part for part in parts if part).strip()
        if not prompt:
            return
        key = prompt.lower()
        if key in seen_prompts:
            return
        seen_prompts.add(key)
        prompts.append(prompt)

    primary_client = client_names[0] if client_names else ""
    industry_label = (industry or "similar").lower()
    if primary_client:
        similar_parts = [f"{industry_label} companies similar to {primary_client}"]
        if size_range:
            similar_parts.append(f"with {size_range[0]}-{size_range[1]} employees")
        if mandate:
            similar_parts.append(f"that may need {mandate} leadership")
        _add_prompt(*similar_parts)

    if industry and size_range:
        _add_prompt(
            f"{industry.lower()} companies with {size_range[0]}-{size_range[1]} employees"
        )
    elif industry:
        _add_prompt(f"{industry.lower()} companies")

    if industry and mandate and not primary_client:
        _add_prompt(
            f"{industry.lower()} companies that may need {mandate} leadership",
            (f"with {size_range[0]}-{size_range[1]} employees" if size_range else ""),
        )

    return prompts


async def _fetch_company_summaries_from_es(
    *,
    universal_names: List[str],
    es_client: Optional[AsyncElasticsearch],
) -> Dict[str, str]:
    if not es_client or not universal_names:
        return {}

    companies_index = (os.getenv("ES_COMPANIES_INDEX") or "").strip()
    if not companies_index:
        return {}

    ids = [
        name
        for name in dict.fromkeys(
            _to_clean_string(value) or ""
            for value in universal_names
            if _to_clean_string(value)
        )
    ]
    if not ids:
        return {}

    try:
        response = await es_client.search(
            index=companies_index,
            body={
                "query": {"terms": {"li_universalname": ids}},
                "_source": ["li_universalname", "li_description"],
                "size": len(ids),
            },
        )
    except Exception:
        logger.exception("Failed to fetch company summaries for lookalike companies")
        return {}

    summaries: Dict[str, str] = {}
    for hit in response.get("hits", {}).get("hits", []) or []:
        source = hit.get("_source")
        if not isinstance(source, dict):
            continue
        universal_name = _to_clean_string(source.get("li_universalname"))
        description = _to_clean_string(source.get("li_description"))
        if universal_name and description:
            summaries[universal_name.lower()] = description
    return summaries


def _normalize_company_summary(
    text: str,
    *,
    max_sentences: int = MAX_LOOKALIKE_SUMMARY_SENTENCES,
) -> str:
    cleaned = _EMOJI_RE.sub("", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""

    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    sentences = [part.strip() for part in parts if part.strip()]
    if not sentences:
        return cleaned

    truncated = " ".join(sentences[:max_sentences]).strip()
    if len(truncated) > MAX_LOOKALIKE_SUMMARY_CHARS:
        truncated = truncated[:MAX_LOOKALIKE_SUMMARY_CHARS].rsplit(" ", 1)[0].strip()
    if truncated and truncated[-1] not in ".!?":
        truncated += "."
    return truncated


def _map_comp_gen_company_to_lookalike_entry(
    company: Dict[str, Any],
    *,
    description_map: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    universal_name = _to_clean_string(company.get("universalName"))
    if not universal_name:
        return None

    display_name = _to_clean_string(company.get("name")) or universal_name
    company_urn = _normalize_company_urn_value(company.get("urn"))
    summary = description_map.get(universal_name.lower()) or ""
    if not summary:
        industry = _to_clean_string(company.get("industry"))
        if industry:
            summary = f"{display_name} operates in {industry}."
    summary = _normalize_company_summary(summary)

    return {
        "universal_name": universal_name,
        "display_name": display_name,
        "company_urn": company_urn,
        "summary": summary,
    }


async def fetch_lookalike_companies_via_comp_gen(
    *,
    assignment_data: Dict[str, Any],
    es_client: Optional[AsyncElasticsearch],
) -> List[Dict[str, Any]]:
    prompts = _build_lookalike_comp_gen_prompts(assignment_data)
    logger.info(
        "[lookalike] built comp_gen prompts=%s",
        _debug_json_preview(prompts, limit=1200),
    )
    if not prompts:
        logger.warning("[lookalike] empty comp_gen prompts; skipping company fetch")
        return []

    current_comps: List[Dict[str, Any]] = []
    past_comps: List[Dict[str, Any]] = []
    event = "CURRENT"
    for index, prompt in enumerate(prompts, start=1):
        logger.info(
            "[lookalike] comp_gen attempt=%s prompt=%s",
            index,
            _debug_json_preview(prompt, limit=800),
        )
        try:
            current_comps, past_comps, event = await call_fast_comp_gen_endpoint(
                prompt,
            )
        except Exception:
            logger.exception(
                "Failed to fetch lookalike companies via fast comp gen on attempt=%s",
                index,
            )
            continue

        logger.info(
            "[lookalike] comp_gen attempt=%s event=%s current_count=%s past_count=%s current=%s past=%s",
            index,
            event,
            len(current_comps or []),
            len(past_comps or []),
            _debug_json_preview(current_comps, limit=1200),
            _debug_json_preview(past_comps, limit=1200),
        )
        if current_comps or past_comps:
            break
        logger.warning(
            "[lookalike] comp_gen attempt=%s returned no companies; trying next prompt",
            index,
        )

    seed_universals = {
        (_to_clean_string(entry.get("universal_name")) or "").lower()
        for entry in _normalize_client_entries(assignment_data.get("client"))
        if _to_clean_string(entry.get("universal_name"))
    }
    logger.info("[lookalike] seed_universals=%s", sorted(seed_universals))

    merged_companies: List[Dict[str, Any]] = []
    seen_universals = set()
    skipped_seed: List[str] = []
    skipped_duplicate: List[str] = []
    skipped_invalid: int = 0
    for company in (current_comps or []) + (past_comps or []):
        if not isinstance(company, dict):
            skipped_invalid += 1
            continue
        universal_name = (_to_clean_string(company.get("universalName")) or "").lower()
        if not universal_name:
            skipped_invalid += 1
            continue
        if universal_name in seen_universals:
            skipped_duplicate.append(universal_name)
            continue
        if universal_name in seed_universals:
            skipped_seed.append(universal_name)
            continue
        seen_universals.add(universal_name)
        merged_companies.append(company)
        if len(merged_companies) >= MAX_LOOKALIKE_COMPANIES:
            break

    logger.info(
        "[lookalike] merge stats merged_count=%s skipped_seed=%s skipped_duplicate=%s skipped_invalid=%s merged_sample=%s",
        len(merged_companies),
        skipped_seed,
        skipped_duplicate,
        skipped_invalid,
        _debug_json_preview(merged_companies, limit=1200),
    )

    if not merged_companies:
        logger.warning("[lookalike] no companies left after filtering comp_gen results")
        return []

    universal_names = [
        _to_clean_string(company.get("universalName"))
        for company in merged_companies
        if _to_clean_string(company.get("universalName"))
    ]
    description_map = await _fetch_company_summaries_from_es(
        universal_names=universal_names,
        es_client=es_client,
    )
    logger.info(
        "[lookalike] es summary hits=%s/%s keys=%s",
        len(description_map),
        len(universal_names),
        sorted(description_map.keys()),
    )

    companies: List[Dict[str, Any]] = []
    for company in merged_companies:
        entry = _map_comp_gen_company_to_lookalike_entry(
            company,
            description_map=description_map,
        )
        if entry:
            companies.append(entry)

    logger.info(
        "[lookalike] final companies count=%s payload=%s",
        len(companies),
        _debug_json_preview(companies, limit=2000),
    )
    return companies


async def _cancel_lookalike_companies_task(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def enrich_opportunities_with_lookalike_companies(
    *,
    opportunities: List[Dict[str, Any]],
    lookalike_companies_task: asyncio.Task,
) -> List[Dict[str, Any]]:
    if not opportunities:
        await _cancel_lookalike_companies_task(lookalike_companies_task)
        return opportunities

    lookalike_index = next(
        (
            index
            for index, opportunity in enumerate(opportunities)
            if _is_lookalike_opportunity(opportunity)
        ),
        None,
    )
    if lookalike_index is None:
        logger.info(
            "[lookalike] no lookalike opportunity card found; skipping enrichment"
        )
        await _cancel_lookalike_companies_task(lookalike_companies_task)
        return opportunities

    logger.info(
        "[lookalike] enriching opportunity index=%s type=%s title=%s",
        lookalike_index,
        opportunities[lookalike_index].get("type"),
        opportunities[lookalike_index].get("title"),
    )

    companies = await lookalike_companies_task

    enriched_opportunities = [dict(opportunity) for opportunity in opportunities]
    lookalike_opportunity = dict(enriched_opportunities[lookalike_index])
    lookalike_opportunity["type"] = LOOKALIKE_OPPORTUNITY_TYPE
    lookalike_opportunity["companies"] = companies
    enriched_opportunities[lookalike_index] = lookalike_opportunity
    logger.info(
        "[lookalike] attached companies count=%s to opportunity type=%s",
        len(companies),
        lookalike_opportunity.get("type"),
    )
    return enriched_opportunities


def count_lookalike_companies(opportunities: List[Dict[str, Any]]) -> int:
    total = 0
    for opportunity in opportunities:
        if not isinstance(opportunity, dict) or not _is_lookalike_opportunity(
            opportunity
        ):
            continue
        companies = opportunity.get("companies")
        if isinstance(companies, list):
            total += len(companies)
    return total
