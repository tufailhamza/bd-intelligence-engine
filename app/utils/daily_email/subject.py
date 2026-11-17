"""
Subject-line generation for the daily email.

Provides a deterministic fallback and an LLM-powered version that calls the
configured model with a short company/meeting-count prompt.
"""

import logging
from typing import Any, Dict, List

from qutils.llm.asynchronous import invoke

from app.utils.daily_email.prompts import (
    DAILY_EMAIL_SUBJECT_SYSTEM,
    DAILY_EMAIL_SUBJECT_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

_SUBJECT_MODEL = "openai/gpt-4.1-mini"
_SUBJECT_FALLBACKS = ["openai/gpt-4.1"]
_SUBJECT_MAX_LEN = 120


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


def build_daily_email_subject(
    meeting_groups: List[Dict[str, Any]],
    *,
    unique_calendar_event_count: int,
) -> str:
    """Return a plain subject line without calling the LLM (matches LLM prompt pattern)."""
    if not meeting_groups:
        return "Your daily digest"
    companies: List[str] = []
    seen: set = set()
    for g in meeting_groups:
        cn = (g.get("company_name") or "").strip()
        if cn and cn not in seen:
            seen.add(cn)
            companies.append(cn)
    if not companies:
        n = max(0, int(unique_calendar_event_count))
        if n:
            return f"Your meetings today ({n} event{'s' if n != 1 else ''})"
        return "Your daily digest"
    if len(companies) == 1:
        return f"Your meetings with {companies[0]}"
    if len(companies) == 2:
        return f"Your meetings with {companies[0]} and {companies[1]}"
    additional = len(companies) - 2
    return (
        f"Your meetings with {companies[0]}, {companies[1]} "
        f"and {additional} {'other' if additional == 1 else 'others'}"
    )


# ---------------------------------------------------------------------------
# LLM-powered subject (falls back to deterministic on any failure)
# ---------------------------------------------------------------------------


async def generate_daily_email_subject(
    *,
    reference_full_name: str,
    meeting_groups: List[Dict[str, Any]],
    unique_calendar_event_count: int,
) -> str:
    """
    Generate a short, engaging subject line via LLM.

    Falls back to :func:`build_daily_email_subject` when:
    - there are no meeting groups (empty digest)
    - no company names could be resolved from the groups
    - the LLM call fails
    - the response is empty or not a string
    """
    if not meeting_groups:
        return build_daily_email_subject(
            meeting_groups,
            unique_calendar_event_count=unique_calendar_event_count,
        )

    companies: List[str] = []
    seen: set = set()
    for g in meeting_groups:
        cn = str(g.get("company_name") or "").strip()
        if cn and cn not in seen:
            seen.add(cn)
            companies.append(cn)

    if not companies:
        return build_daily_email_subject(
            meeting_groups,
            unique_calendar_event_count=unique_calendar_event_count,
        )

    total_companies = len(companies)
    company_first = companies[0] if companies else ""
    company_second = companies[1] if len(companies) > 1 else "(none)"
    additional_company_count = max(0, total_companies - 2)
    max_csv = 40
    if len(companies) <= max_csv:
        companies_csv = ", ".join(companies) if companies else "none"
    else:
        companies_csv = (
            ", ".join(companies[:max_csv])
            + f" … (+{len(companies) - max_csv} more distinct companies)"
        )

    user_content = DAILY_EMAIL_SUBJECT_USER_TEMPLATE.format(
        reference_full_name=reference_full_name,
        unique_calendar_event_count=max(0, int(unique_calendar_event_count)),
        total_companies=total_companies,
        company_first=company_first,
        company_second=company_second,
        additional_company_count=additional_company_count,
        companies_csv=companies_csv,
    )

    try:
        response = await invoke(
            messages=[
                {"role": "system", "content": DAILY_EMAIL_SUBJECT_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            model=_SUBJECT_MODEL,
            temperature=0.2,
            fallbacks=_SUBJECT_FALLBACKS,
        )
    except Exception as e:
        logger.warning("Daily email subject LLM call failed: %s", e)
        return build_daily_email_subject(
            meeting_groups,
            unique_calendar_event_count=unique_calendar_event_count,
        )

    if not isinstance(response, str):
        return build_daily_email_subject(
            meeting_groups,
            unique_calendar_event_count=unique_calendar_event_count,
        )

    subject = response.strip().replace("\n", " ")
    if len(subject) > _SUBJECT_MAX_LEN:
        subject = subject[:_SUBJECT_MAX_LEN].rstrip()
    if not subject:
        return build_daily_email_subject(
            meeting_groups,
            unique_calendar_event_count=unique_calendar_event_count,
        )
    return subject
