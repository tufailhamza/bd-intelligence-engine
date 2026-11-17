import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional, List

from elasticsearch import AsyncElasticsearch
from qutils.llm.asynchronous import invoke

logger = logging.getLogger(__name__)


def _redact_sensitive_info(text: str) -> str:
    """
    Redact sensitive information from text before logging.
    Redacts email addresses, phone numbers, and URLs.

    Args:
        text: Text string that may contain sensitive information

    Returns:
        Text with sensitive information redacted
    """
    if not isinstance(text, str):
        return str(text)

    # Redact email addresses
    text = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL_REDACTED]", text
    )

    # Redact phone numbers (various formats)
    text = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[PHONE_REDACTED]", text)
    text = re.sub(
        r"\b\+?\d{1,3}[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
        "[PHONE_REDACTED]",
        text,
    )

    # Redact URLs that might contain sensitive info (keep domain names visible)
    text = re.sub(r"https?://[^\s]+", "[URL_REDACTED]", text)

    return text


def _sanitize_xml(text: str) -> str:
    """
    Best-effort sanitization:
    - Strip markdown fences
    - Escape stray ampersands that would break XML parsing
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", t)
        t = t.replace("```", "").strip()

    # Escape '&' that isn't already part of an entity
    t = re.sub(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", t)
    return t


def _extract_scoring_rubric_xml(text: str) -> Optional[str]:
    """
    Extract the <OUTPUT>...</OUTPUT> block.
    """
    if not text:
        return None
    t = _sanitize_xml(text)
    m = re.search(r"(<OUTPUT>[\s\S]*?</OUTPUT>)", t, re.IGNORECASE)
    if m:
        return m.group(1)
    # Sometimes the model might use lowercase tag names.
    m2 = re.search(r"(<output>[\s\S]*?</output>)", t)
    return m2.group(1) if m2 else None


async def _fetch_company_data(
    universal_names: List[str], es_client: AsyncElasticsearch
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch company data from Elasticsearch Company index using universal names.

    Args:
        universal_names: List of company universal names
        es_client: AsyncElasticsearch client

    Returns:
        Dictionary mapping universal_name -> company data
    """
    if not universal_names:
        return {}

    companies_index = os.getenv("ES_COMPANIES_INDEX")
    if not companies_index:
        return {}

    try:
        body = {
            "query": {"terms": {"li_universalname": universal_names}},
            "_source": [
                "li_universalname",
                "li_name",
                "li_description",
                "li_industries",
                "li_specialties",
                "li_size",
                "li_staffcount",
                "li_headquarter",
                "cb_stock_symbol",
            ],
            "size": len(universal_names),
        }

        res = await es_client.search(index=companies_index, body=body)
        hits = res.get("hits", {}).get("hits", []) or []

        company_map = {}
        for hit in hits:
            source = hit.get("_source", {})
            universal_name = source.get("li_universalname")
            if universal_name:
                company_map[universal_name] = source

        return company_map
    except Exception as e:
        # Log error but don't fail - return empty dict
        return {}


def _is_current_experience(exp: Dict[str, Any]) -> bool:
    """
    Determine if an experience entry represents a current/ongoing role.

    Args:
        exp: Experience dictionary

    Returns:
        True if the experience is current (no end date or end date is in the future)
    """
    if not isinstance(exp, dict):
        return False

    end = exp.get("end")
    # If end is None, empty string, or "null", it's a current role
    if end in (None, "", "null"):
        return True

    # If end is a string, try to parse it
    # For now, if it exists and is not empty/null, consider it past
    # You could add date parsing here if needed
    return False


def _extract_profile_information(
    profile: Dict[str, Any],
    company_data_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """
    Extract and format relevant information from profile dictionary.
    Focuses on current experience and key professional details.
    Handles multiple simultaneous current experiences.

    Args:
        profile: Profile dictionary from Elasticsearch
        company_data_map: Optional dictionary mapping company universal names to company data

    Returns:
        Formatted string with extracted profile information
    """
    lines = []

    # Basic Information
    full_name = profile.get("full_name") or profile.get("name") or "N/A"
    headline = profile.get("headline") or ""
    summary = profile.get("summary") or ""
    location = profile.get("location") or ""

    lines.append("=" * 60)
    lines.append("REFERENCE PROFILE INFORMATION")
    lines.append("=" * 60)
    lines.append(f"\nName: {full_name}")
    if headline:
        lines.append(f"Headline: {headline}")
    if location:
        lines.append(f"Location: {location}")
    if summary:
        lines.append(f"\nSummary:\n{summary}")

    # Current Experience (MOST IMPORTANT) - Handle multiple simultaneous roles
    experience = profile.get("experience", [])
    if isinstance(experience, list) and experience:
        # Filter for all current experiences
        current_exps = [
            e for e in experience if isinstance(e, dict) and _is_current_experience(e)
        ]
        if not current_exps:
            # Fallback: include the most recent experience if current can't be detected
            current_exps = [experience[0]] if isinstance(experience[0], dict) else []

        lines.append("\n" + "-" * 60)
        if len(current_exps) > 1:
            lines.append("CURRENT EXPERIENCES (PRIMARY FOCUS - MULTIPLE ROLES)")
        else:
            lines.append("CURRENT EXPERIENCE (PRIMARY FOCUS)")
        lines.append("-" * 60)

        # Process each current experience
        for idx, current_exp in enumerate(current_exps, start=1):
            if not isinstance(current_exp, dict):
                continue

            if len(current_exps) > 1:
                lines.append(f"\n--- Role #{idx} ---")

            title = current_exp.get("title") or ""
            company = current_exp.get("company") or ""
            company_industry = current_exp.get("company_industry") or ""
            company_speciality = current_exp.get("company_speciality") or ""
            company_description = current_exp.get("company_description") or ""
            job_summary = current_exp.get("job_summary") or ""
            start = current_exp.get("start") or ""
            end = current_exp.get("end") or ""

            if title:
                lines.append(f"Title: {title}")
            if company:
                lines.append(f"Company: {company}")
            if company_industry:
                lines.append(f"Industry: {company_industry}")
            if company_speciality:
                lines.append(f"Company Specialty: {company_speciality}")
            if company_description:
                lines.append(f"Company Description: {company_description}")

            # Add company data from Company index if available
            company_universal_name = current_exp.get("company_universal_name")
            if company_universal_name and company_data_map:
                company_data = company_data_map.get(company_universal_name)
                if company_data:
                    li_description = company_data.get("li_description")
                    li_industries = company_data.get("li_industries", [])
                    li_specialties = company_data.get("li_specialties", [])
                    li_size = company_data.get("li_size") or company_data.get(
                        "li_staffcount"
                    )
                    li_headquarter = company_data.get("li_headquarter")
                    cb_stock_symbol = company_data.get("cb_stock_symbol")

                    if li_description:
                        lines.append(
                            f"Company Description (from Company Index): {li_description}"
                        )
                    if isinstance(li_industries, list) and li_industries:
                        lines.append(
                            f"Company Industries (from Company Index): {', '.join(li_industries[:5])}"
                        )
                    if isinstance(li_specialties, list) and li_specialties:
                        lines.append(
                            f"Company Specialties (from Company Index): {', '.join(li_specialties[:5])}"
                        )
                    if li_size:
                        lines.append(f"Company Size: {li_size}")
                    if li_headquarter:
                        if isinstance(li_headquarter, dict):
                            hq_city = li_headquarter.get("city") or ""
                            hq_country = li_headquarter.get("country") or ""
                            if hq_city or hq_country:
                                lines.append(
                                    f"Company Headquarters: {', '.join(filter(None, [hq_city, hq_country]))}"
                                )
                        elif isinstance(li_headquarter, str):
                            lines.append(f"Company Headquarters: {li_headquarter}")
                    if cb_stock_symbol:
                        lines.append(f"Stock Symbol: {cb_stock_symbol}")

            if start:
                period = f"{start}"
                if end:
                    period += f" to {end}"
                else:
                    period += " to Present"
                lines.append(f"Period: {period}")
            if job_summary:
                lines.append(f"\nJob Summary:\n{job_summary}")

            # Add spacing between multiple roles
            if len(current_exps) > 1 and idx < len(current_exps):
                lines.append("")

    # Skills (top 10)
    skills = profile.get("skills", [])
    if isinstance(skills, list) and skills:
        top_skills = skills[:10]  # Limit to top 10
        lines.append("\n" + "-" * 60)
        lines.append("KEY SKILLS")
        lines.append("-" * 60)
        lines.append(", ".join(top_skills))

    # Education (if available, just highest degree)
    education = profile.get("education", [])
    if isinstance(education, list) and education:
        highest_edu = education[0] if education else {}
        if highest_edu:
            school = highest_edu.get("school") or ""
            degree = highest_edu.get("degree") or ""
            major = highest_edu.get("major") or ""
            if school or degree:
                lines.append("\n" + "-" * 60)
                lines.append("EDUCATION")
                lines.append("-" * 60)
                edu_parts = []
                if degree:
                    edu_parts.append(degree)
                if major:
                    edu_parts.append(f"in {major}")
                if school:
                    edu_parts.append(f"from {school}")
                if edu_parts:
                    lines.append(" ".join(edu_parts))

    lines.append("\n" + "=" * 60)

    return "\n".join(lines)


def _generate_calendar_stats(calendar_events: Optional[List[Dict[str, Any]]]) -> str:
    """
    Generate statistics from calendar events including members and meeting descriptions.

    Args:
        calendar_events: List of calendar event dictionaries

    Returns:
        Formatted string with calendar statistics
    """
    if not calendar_events:
        return ""

    lines = []
    lines.append("\n" + "=" * 60)
    lines.append("CALENDAR EVENTS STATISTICS")
    lines.append("=" * 60)

    # Collect unique attendees/emails
    unique_attendees = set()
    meeting_descriptions = []
    meeting_summaries = []

    for event in calendar_events:
        # Collect attendees
        attendees = event.get("attendees", [])
        if isinstance(attendees, list):
            for attendee in attendees:
                if isinstance(attendee, dict):
                    email = attendee.get("email", "")
                    if email:
                        unique_attendees.add(email)

        # Collect summaries
        summary = event.get("summary", "")
        if summary:
            meeting_summaries.append(summary)

        # Collect descriptions
        description = event.get("description", "")
        if description:
            meeting_descriptions.append(description)

    # Generate stats
    lines.append(f"\nTotal Events: {len(calendar_events)}")
    lines.append(f"Unique Attendees/Members: {len(unique_attendees)}")
    lines.append(f"Events with Descriptions: {len(meeting_descriptions)}")

    # List unique attendees (limit to top 50)
    if unique_attendees:
        lines.append(f"\nAttendees/Members ({min(len(unique_attendees), 50)} shown):")
        for email in sorted(list(unique_attendees))[:50]:
            lines.append(f"  - {email}")
        if len(unique_attendees) > 50:
            lines.append(f"  ... and {len(unique_attendees) - 50} more")

    # Include meeting summaries (limit to recent 20)
    if meeting_summaries:
        lines.append(f"\nMeeting Summaries (recent {min(len(meeting_summaries), 20)}):")
        for summary in meeting_summaries[-20:]:
            lines.append(f"  - {summary}")

    # Include meeting descriptions (limit to recent 10)
    if meeting_descriptions:
        lines.append(
            f"\nMeeting Descriptions (recent {min(len(meeting_descriptions), 10)}):"
        )
        for desc in meeting_descriptions[-10:]:
            # Truncate long descriptions
            desc_short = desc[:500] + "..." if len(desc) > 500 else desc
            lines.append(f"  {desc_short}")
            lines.append("")

    lines.append("=" * 60)

    return "\n".join(lines)


async def _extract_calendar_insights(
    calendar_events: Optional[List[Dict[str, Any]]],
    model: str = "openai/gpt-4o",
) -> str:
    """
    Extract meaningful insights from calendar events using LLM.

    Args:
        calendar_events: List of calendar event dictionaries
        model: Model to use for extraction (defaults to openai/gpt-4o)

    Returns:
        Summary of meaningful insights from calendar data
    """
    if not calendar_events:
        return ""

    # Generate calendar stats
    calendar_stats = _generate_calendar_stats(calendar_events)

    prompt = f"""
You are analyzing calendar events to extract meaningful professional insights that would help understand:
- Who this person meets with (industries, roles, companies)
- What types of meetings they have (sales, partnerships, hiring, etc.)
- Key business relationships and patterns
- Professional focus areas and priorities

CALENDAR EVENTS DATA:
{calendar_stats}

INSTRUCTIONS:
- Extract meaningful professional insights from the calendar events
- Focus on business relationships, meeting types, industries, and professional patterns
- Identify key contacts, companies, and relationship types
- Summarize in 2-3 concise paragraphs
- Be specific about industries, roles, and meeting purposes
- Ignore personal events (vacations, home, family time, etc.)

Return ONLY the insights summary, no markdown formatting, no code fences.
"""

    try:
        logger.info(
            f"Extracting calendar insights from {len(calendar_events)} events using model={model}"
        )
        response = await invoke(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model="openai/gpt-4o",
            fallbacks=["openai/gpt-4.1"],
        )
        # invoke returns a string directly, not a dict
        insights = response.strip() if response else ""
        logger.info(f"Calendar insights extracted: {len(insights)} characters")
        return insights
    except Exception as e:
        # If extraction fails, log error but don't fail the whole process
        logger.error(f"Error extracting calendar insights: {str(e)}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        return ""


async def _build_metrics_prompt_xml(
    profile: Dict[str, Any],
    es_client: Optional[AsyncElasticsearch] = None,
    calendar_insights: Optional[str] = None,
) -> str:
    """
    Builds a prompt that instructs the model to return STRICT XML.
    Extracts relevant information from profile instead of sending raw JSON.
    Also fetches company data from Company index if es_client is provided.
    Fetches company data for all current experiences (handles multiple simultaneous roles).
    """
    # Extract company universal names from all current experiences
    company_universal_names = []
    experience = profile.get("experience", [])
    if isinstance(experience, list):
        # Get all current experiences (not just the first one)
        current_exps = [
            e for e in experience if isinstance(e, dict) and _is_current_experience(e)
        ]
        if not current_exps:
            # Fallback: use the most recent experience if current can't be detected
            current_exps = (
                [experience[0]]
                if experience and isinstance(experience[0], dict)
                else []
            )

        # Extract company universal names from all current experiences
        for exp in current_exps:
            if isinstance(exp, dict):
                universal_name = exp.get("company_universal_name")
                if universal_name and universal_name not in company_universal_names:
                    company_universal_names.append(universal_name)

    # Fetch company data if es_client is available
    company_data_map = {}
    if es_client and company_universal_names:
        company_data_map = await _fetch_company_data(company_universal_names, es_client)

    profile_info = _extract_profile_information(profile, company_data_map)
    logger.debug(
        "Profile information extracted for scoring metrics generation",
        extra={
            "profile_info_length": len(profile_info) if profile_info else 0,
            "profile_info_preview": (
                _redact_sensitive_info(profile_info[:500]) if profile_info else None
            ),
        },
    )
    logger.debug(
        "Calendar insights for scoring metrics generation",
        extra={
            "calendar_insights_length": (
                len(calendar_insights) if calendar_insights else 0
            ),
            "calendar_insights_preview": (
                _redact_sensitive_info(calendar_insights[:500])
                if calendar_insights
                else None
            ),
        },
    )
    # Include calendar insights if available
    calendar_section = ""
    if calendar_insights:
        calendar_section = f"""

--------------------------------
CALENDAR-BASED INSIGHTS
--------------------------------
{calendar_insights}

Use these calendar insights to better understand the reference profile's actual business relationships, meeting patterns, and professional focus areas when designing the scoring rubric.
"""

    return f"""
        You are a senior Business Development strategist, revenue intelligence expert, executive-search domain specialist, and scoring-rubric designer.
        You will be given ONE reference LinkedIn-like profile with key professional information.
        Your task is to design a dynamic scoring rubric that will be used to evaluate OTHER profiles, based on Who represents the shortest path to revenue about from a business, revenue, hiring, partnership, or sales perspective.
        This is interest-driven and opportunity-driven scoring, NOT similarity matching.
        The rubric must adapt based on the reference profile's current role, responsibilities, and monetization opportunities.
        

        {profile_info}{calendar_section}
""".strip()


async def _call_qutils_llm(*, model: str, prompt: str) -> str:
    """
    Call LLM via qutils to generate text response.
    Uses QLU_API_KEY from environment.
    """
    system_message = """
        You are a senior Business Development strategist, revenue intelligence expert, executive-search domain specialist, and scoring-rubric designer.
        You will be given ONE reference LinkedIn-like profile with key professional information.
        Your task is to design a dynamic scoring rubric that will be used to evaluate OTHER profiles, based on who the reference profile should care about from a business, revenue, hiring, partnership, or sales perspective.
        This is interest-driven and opportunity-driven scoring, NOT similarity matching.
        The rubric must adapt based on the reference profile’s current role, responsibilities, and monetization opportunities.

        ──────────────────
        INTERNAL REASONING
        ──────────────────
        Before producing the final XML, internally reason through these steps:
        NOTE: ALWAYS give the highest importance to the CURRENT EXPERIENCE of the reference profile.
        Infer the reference profile’s primary persona
        (e.g., BD leader, founder, seller, recruiter, advisor, operator)
        Infer the reference profile’s current business objectives
        (e.g., selling services, lead generation, executive hiring, partnerships, GTM, deal flow).
        Identify who the reference profile should sell to, partner with, or extract value from:
        Buyers
        Decision-makers
        Budget owners
        Influencers connected to revenue or hiring authority
        Identify the strongest commercial, hiring, or You are a careful BD scoring-rubric designer. ASSIGN WEIGHTS TO THE METRICS BASED ON THE REFERENCE PROFILE AND THE CONTEXT. IMAGINE THIS PROFILE IS LOOKING FOR NEWS RELEVANT TO HIMSELF SO THAT HE CAN SELL OR BENEFIT FROM THE OTHER PROFILE AND INTREST WHAT METRICS OTHER PROFILE SHOULD HAVE SO THAT HE IS INTRESTED IN
        Revenue potential
        Decision-making authority
        Budget ownership
        Strategic leverage
        Hiring or partnership relevance
        Convert these priorities into a DYNAMIC number of measurable scoring metrics(the number of metrics must be decided by you based on the reference profile — NOT fixed).
        Assign weights based on decision impact, allowing majority weight to metrics tied directly to:
        Selling opportunities
        Lead quality
        Current role alignment
        Do NOT reveal your internal reasoning.
        ────────────────────────────
        SPECIFIC INSTRUCTIONS
        ────────────────────────────
        "Relative Weighting" logic so that the most important metric is clearly distinguishable.
        SCORES DOESNT NEED TO ADD UP TO 100. THEY CAN BE DYNAMIC BASED ON INDIVIDUAL PROFILE.
        THEY SHOULD BE DYNAMIC BASED ON THE INDIVIDUAL PROFILE AND THE CONTEXT.
        THERE IS NO RESTRICTION ON TOTAL SCORE OF ALL THE METRICS THEY CAN BE ANY NUMBER.
        Treat the reference profile as the decision-maker and seller.
        Design metrics that answer:
        “Why should THIS person sell to, recruit, advise, or partner with THIS other profile?”
        Metrics MUST reflect:
        Current experience (highest priority)
        Seniority and authority
        Industry and domain relevance
        Revenue, hiring, or partnership potential
        Practical business outcomes
        DO NOT score similarity.
        DO NOT score personality traits.
        DO NOT optimize for prestige alone.
        Focus on:
        MAKE SURE YOUR TARGETING THE prospect relevant to the buissness or targeting niche make sure to analyze the Experience company description
        Buying power
        Decision authority of the person
        Commercial relevance
        Strategic alignment
        Metric names must be short (1–2 words).
        Each metric must represent one clear business signal.
        IGNORE THE OLD EXPERIENCE OF THE REFERENCE PROFILE.
        ONLY CONSIDER THE CURRENT EXPERIENCE OF THE REFERENCE PROFILE.
        Metrics SHOULD cover all meaningful interest vectors of the reference profile, including sales and lead generation when applicable.
        You should give weightage according o the industry.
        IF you think A metric has a high priority then it may be possibleou can assign that metrics majority of the weightage
        choose the best number between 3 and 5 based on the complexity of the profile.

        ────────────────────────────
        TAGLINE REQUIREMENTS
        ────────────────────────────
        Each metric MUST include a TAGLINE field.
        The tagline should be a short, natural-sounding sentence (max 14 words) that explains why this metric matters for the reference profile.
        The tagline should guide users on what connections are relevant based on this metric.
        Format: "Connections relevant to [metric context] are valuable for [reference profile's business objective]"
        Examples:
        - For "Industry Match": "Connections in your target industry align with your business development goals"
        - For "Seniority": "Senior executives bring decision-making authority and budget control"
        - For "Revenue Potential": "High-value prospects offer significant commercial opportunities"
        Keep taglines concise, specific, and focused on business value.

        ────────────────────────────
        INDUSTRY RELEVANCE
        ────────────────────────────
        -Make Sure you Identify the Industry precisely and give weightage according to the industry.
        -NEVER GENERALIZE POSITIONS OR INDUSTRY OR COMPANY OR INDUSTRY EXPERIENCE OF THE REFERENCE PROFILE.
        -FOR EXAMPLE IF THE REFERENCE PROFILE HAS EXPERIENCE ON WORKING ON EXECUTIVE SEARCH THEN YOU SHOULD CONSIDER EXECUTIVE SEARCH WHICH IS VERY DIFFERENT FROM ANY OTHER RECRUITING AGENCY.
        -FOR TOOLS IF A COMPANY iselling AI BAsed product not necessary the what to check should include AI BAsed product since people can adopt it too
        ────────────────────────────
        OUTPUT REQUIREMENTS (STRICT)
        ────────────────────────────
        Output MUST be valid XML only.
        No markdown.
        No commentary.
        No explanations outside XML.
        Output exactly ONE <OUTPUT> root element.
        Output a variable number of <METRIC> elements (as determined by you).
        You can Give as Much Weightage to a metric as you want if You think it is important for the reference profile to care about it.
        XML-escape all special characters (&, <, >, ", ').
       
        ────────────────────────────
        REQUIRED XML STRUCTURE
        ────────────────────────────
        <REASONING>
        (High-level explanation of how the rubric was derived, without exposing internal step-by-step logic)
        </REASONING>
        <OUTPUT>
        <METRICS>
        <METRIC>
        <NAME>...</NAME>
        <WEIGHT>...</WEIGHT>
        <WHAT_TO_CHECK>...</WHAT_TO_CHECK>
        <REASONING>...</REASONING>
        <TAGLINE>...</TAGLINE>
        </METRIC> ... </METRICS>
        </OUTPUT> 
    
    """

    try:
        response = await invoke(
            messages=[
                {
                    "role": "system",
                    "content": system_message,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            model="anthropic/claude-sonnet-4-5-20250929",
            temperature=0.2,
            fallbacks=["openai/gpt-4o"],
            verbose=False,
        )

        if not response:
            raise RuntimeError("LLM returned empty response")

        return response.strip()
    except Exception as e:
        raise RuntimeError(f"LLM API error: {str(e)}")


async def generate_scoring_metrics_from_profile(
    *,
    profile: Dict[str, Any],
    model: str = "anthropic/claude-sonnet-4-5-20250929",  # Changed from anthropic/claude-sonnet-4-5 to use OpenAI
    es_client: Optional[AsyncElasticsearch] = None,
    calendar_events: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Generate scoring metrics from a profile using qutils LLM (via QLU_API_KEY).

    Args:
        profile: Profile dictionary
        model: Model name
        es_client: Optional Elasticsearch client to fetch company data from Company index
        calendar_events: Optional list of calendar events to extract insights from

    Returns:
        XML string containing the scoring rubric
    """
    # Extract calendar insights if calendar events are provided
    calendar_insights = ""
    if calendar_events:
        logger.info(f"Processing {len(calendar_events)} calendar events for insights")
        calendar_insights = await _extract_calendar_insights(
            calendar_events, model=model
        )
        if not calendar_insights:
            logger.warning("Calendar insights extraction returned empty string")

    # Extract relevant information from profile and build prompt (with company data and calendar insights if available)
    prompt = await _build_metrics_prompt_xml(
        profile, es_client, calendar_insights=calendar_insights
    )

    raw = await _call_qutils_llm(model=model, prompt=prompt)
    # If the model returns extra text, try to extract the rubric block.
    xml_block = _extract_scoring_rubric_xml(raw) or (raw or "").strip()
    return xml_block
