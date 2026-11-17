import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from qutils.llm.utilities import asynchronous_llm

from app.utils.icp.prompts import PROFILE_PERSONA_PROMPT, SUGGESTED_ICPS_PROMPT
from app.utils.network_clustering.clustering_utils import clean_json_from_llm_output

logger = logging.getLogger(__name__)


async def fetch_user_profile_data(public_identifier, es_client):
    es_query = {
        "_source": [
            "headline",
            "summary",
            "experience.job_summary",
            "experience.company_industry",
            "experience.company_speciality",
            "experience.title",
        ],
        "query": {"terms": {"public_identifier": [public_identifier]}},
        "size": 1,
    }

    try:
        es_response = await es_client.search(
            body=es_query, index=os.getenv("ES_PEOPLE_INDEX")
        )
    except Exception as err:
        logger.error(err)
        return None

    hit = es_response.get("hits", {}).get("hits", [])

    profile_info = None

    if hit:
        profile_info = hit[0].get("_source", None)

    return profile_info


async def clean_text(text: Optional[str]) -> str:
    """Clean and normalize a text field."""
    if not text:
        return ""
    return " ".join(text.strip().lower().split())


async def preprocess_profile_for_prompt(
    exp: List[Dict[str, Any]], headline: Optional[str], summary: Optional[str]
) -> str:
    """
    Prepare structured recruiter background as LLM prompt string.
    """
    headline_clean = await clean_text(headline)
    summary_clean = await clean_text(summary)

    prompt_lines = []

    # Section 1: Headline and summary
    if headline_clean or summary_clean:
        intro = f"Headline: {headline_clean}." if headline_clean else ""
        if summary_clean:
            intro += f" Summary: {summary_clean}."
        prompt_lines.append(intro.strip())

    # Section 2: Recruiter's past experience summaries
    if exp:
        prompt_lines.append("\nThe recruiter's past experiences are:")
        for idx, item in enumerate(exp):
            title = await clean_text(item.get("title")) if item else ""
            job_summary = await clean_text(item.get("job_summary")) if item else ""
            if job_summary:
                job_summary = job_summary + ("..." if len(job_summary) > 300 else "")
            summary_line = f"{idx + 1}. Worked as {title}"
            if job_summary:
                summary_line += f" with the experience summary: {job_summary}"
            prompt_lines.append(summary_line.strip())

    # Section 3: Company specialties (flattened)
    all_specialities = set()
    for item in exp or []:
        raw_specialities = item.get("company_speciality") if item else []
        if raw_specialities is None:
            raw_specialities = []
        cleaned = await asyncio.gather(*[clean_text(s) for s in raw_specialities if s])
        all_specialities.update(cleaned)
    if all_specialities:
        prompt_lines.append(
            "\nThe recruiter has worked with companies having specialties like:"
        )
        prompt_lines.append(", ".join(sorted(all_specialities)))

    # Section 4: Industries
    all_industries = set()
    for item in exp or []:
        raw_industries = item.get("company_industry") if item else []
        if raw_industries is None:
            raw_industries = []
        cleaned = await asyncio.gather(*[clean_text(i) for i in raw_industries if i])
        all_industries.update(cleaned)
    if all_industries:
        prompt_lines.append("\nThe recruiter has worked in the following industries:")
        prompt_lines.append(", ".join(sorted(all_industries)))

    return "\n".join([line for line in prompt_lines if line.strip()])


async def summarize_exec_recruiter_scope(structured_input: str) -> str:
    if not structured_input:
        return None

    prompt = PROFILE_PERSONA_PROMPT.format(structured_input=structured_input)

    try:
        response = await asynchronous_llm(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4.1-mini",
            temperature=0,
            max_tokens=2048,
            fallback_openai_model="gpt-4o",
            fallback_anthropic_model="claude-3-5-sonnet-latest",
            verbose=False,
        )
    except Exception as e:
        logger.error(f"Error calling asynchronous_llm: {e}")
        return None

    return response


async def recommend_icps(
    recruiter_specialities: str, existing_cluster_info: str, existing_icps: str
) -> str:

    if (not recruiter_specialities) or (not existing_cluster_info):
        return None

    prompt = SUGGESTED_ICPS_PROMPT.format(
        recruiter_specialities=recruiter_specialities,
        existing_cluster_info=existing_cluster_info,
        existing_icps=existing_icps,
    )

    try:
        response = await asynchronous_llm(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4.1",
            temperature=0.1,
            max_tokens=4000,
            fallback_openai_model="gpt-4o",
            fallback_anthropic_model="claude-3-5-sonnet-latest",
            verbose=False,
        )
    except Exception as e:
        logger.error(f"Error calling asynchronous_llm: {e}")
        return None

    return response


async def generate_profile_persona(public_identifier, es_client):

    try:
        raw_profile_data = await fetch_user_profile_data(public_identifier, es_client)
        if not raw_profile_data:
            logger.error(f"No profile data found for: {public_identifier}")
            return None

        structured_profile_input = await preprocess_profile_for_prompt(
            raw_profile_data.get("experience"),
            raw_profile_data.get("headline"),
            raw_profile_data.get("summary"),
        )

        persona_llm_output = await summarize_exec_recruiter_scope(
            structured_profile_input
        )
        profile_persona = clean_json_from_llm_output(persona_llm_output)
        return profile_persona

    except Exception as err:
        logger.error(
            f"Failed to generate profile persona for {public_identifier}: {err}"
        )
        return None


async def suggest_icps(profile_persona, summarized_cluster_info, existing_icps):

    if not profile_persona:
        logger.error("Empty profile persona provided to suggest_icps.")
        return []

    try:
        recommended_clusters_output = await recommend_icps(
            profile_persona, summarized_cluster_info, existing_icps
        )

        recommended_clusters = clean_json_from_llm_output(recommended_clusters_output)
        return recommended_clusters or []

    except Exception as err:
        logger.error(f"Failed to recommend ICPs: {err}")
        return []
