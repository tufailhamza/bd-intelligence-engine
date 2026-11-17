import copy
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional

from elasticsearch import AsyncElasticsearch
from qutils.llm.asynchronous import invoke
from qutils.llm.utilities import asynchronous_llm

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


async def get_company_ownership_data(
    universal_names: list[str], es_client
) -> dict[str, list[str]]:
    ownership_types = ["private", "public", "vc_backed", "pe_backed"]

    query_body = {
        "query": {"terms": {"_id": ownership_types}},
        "script_fields": {
            "matched_companies": {
                "script": {
                    "lang": "painless",
                    "source": """
                        def result = [];
                        for (c in params.targets) {
                            if (doc['company_universal_name'].contains(c)) {
                                result.add(c);
                            }
                        }
                        return result;
                    """,
                    "params": {"targets": universal_names},
                }
            }
        },
        "_source": False,
    }

    company_ownership_data = {}

    try:
        response = await es_client.search(index="company_ownership", body=query_body)
    except Exception as e:
        logging.error(
            "Elasticsearch query failed in get_company_ownership_data: %s", str(e)
        )
        return {}

    for hit in response.get("hits", {}).get("hits", []):
        ownership = hit.get("_id")
        matched_companies = hit.get("fields", {}).get("matched_companies", [])

        for company in matched_companies:
            company_ownership_data.setdefault(company, []).append(ownership)

    return company_ownership_data


def xml_output_to_json(xml_output: str) -> Dict[str, Any]:
    """
    Convert the LLM's <OUTPUT> XML string into a JSON-serializable dict:
    {
      "metrics": [ ... ]
    }
    """
    if not xml_output or not str(xml_output).strip():
        return {"metrics": []}

    # If response contains extra text, extract the OUTPUT block
    s = str(xml_output).strip()
    m = re.search(r"(<OUTPUT>[\s\S]*?</OUTPUT>)", s, re.IGNORECASE)
    if not m:
        m = re.search(r"(<output>[\s\S]*?</output>)", s)
    if m:
        s = m.group(1)

    # Sanitize: escape stray '&' that breaks XML, and strip code fences if any.
    s = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", s).replace("```", "").strip()
    s = re.sub(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", s)

    def _repair_truncated_xml(x: str) -> str:
        # If truncated, keep only complete METRIC blocks and close the document.
        if "</OUTPUT>" in x or "</output>" in x:
            end_tag = "</OUTPUT>" if "</OUTPUT>" in x else "</output>"
            return x[: x.rfind(end_tag) + len(end_tag)]

        if "<OUTPUT" not in x and "<output" not in x:
            return x

        # Preserve header up to <METRICS> if present
        metrics_open_idx = max(x.find("<METRICS"), x.find("<metrics"))
        if metrics_open_idx == -1:
            # Just close root
            return x + "\n</OUTPUT>"

        header = x[:metrics_open_idx]
        metrics_tail = x[metrics_open_idx:]

        last_metric_close = max(
            metrics_tail.rfind("</METRIC>"), metrics_tail.rfind("</metric>")
        )
        if last_metric_close == -1:
            # No complete metric; close what we can
            return header + metrics_tail + "\n</METRICS>\n</OUTPUT>"

        metrics_body = metrics_tail[: last_metric_close + len("</METRIC>")]
        return header + metrics_body + "\n</METRICS>\n</OUTPUT>"

    try:
        root = ET.fromstring(s)
    except ET.ParseError as e:
        try:
            s2 = _repair_truncated_xml(s)
            root = ET.fromstring(s2)
        except ET.ParseError:
            raise ValueError(f"Could not parse XML: {str(e)}")

    def find(node: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
        if node is None:
            return None
        for n in names:
            child = node.find(n)
            if child is not None:
                return child
        return None

    def text(node: Optional[ET.Element]) -> str:
        return (node.text or "").strip() if node is not None else ""

    metrics_parent = find(root, "METRICS", "metrics")

    metrics: List[Dict[str, Any]] = []
    if metrics_parent is not None:
        metric_nodes = list(metrics_parent.findall("METRIC")) + list(
            metrics_parent.findall("metric")
        )
        for mn in metric_nodes:
            metrics.append(
                {
                    "name": text(find(mn, "NAME", "name")),
                    "weight": int(text(find(mn, "WEIGHT", "weight")) or "0"),
                    "what_to_check": text(find(mn, "WHAT_TO_CHECK", "what_to_check")),
                    "reasoning": text(find(mn, "REASONING", "reasoning")),
                    "tagline": text(find(mn, "TAGLINE", "tagline")),
                }
            )

    return {
        "metrics": metrics,
    }


async def _fetch_company_data_for_scoring(
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
        logging.error(f"Error fetching company data: {e}")
        return {}


def _extract_profile_for_scoring(
    profile: Dict[str, Any],
    company_data_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """
    Extract and format profile information as readable text for scoring.
    Includes relevant experience and professional details (excludes skills and education).

    Args:
        profile: Profile dictionary from Elasticsearch
        company_data_map: Optional dictionary mapping company_universal_name -> company data from Company index

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
    lines.append("PROFILE TO SCORE")
    lines.append("=" * 60)
    lines.append(f"\nName: {full_name}")
    if headline:
        lines.append(f"Headline: {headline}")
    if location:
        lines.append(f"Location: {location}")
    if summary:
        lines.append(f"\nSummary:\n{summary}")

    # Experience (only most recent experience)
    experience = profile.get("experience", [])
    if isinstance(experience, list) and experience:
        lines.append("\n" + "-" * 60)
        lines.append("EXPERIENCE")
        lines.append("-" * 60)

        for idx, exp in enumerate(experience[:1]):  # Only most recent experience
            if not isinstance(exp, dict):
                continue

            title = exp.get("title") or ""
            company = exp.get("company") or ""
            company_industry = exp.get("company_industry") or ""
            company_speciality = exp.get("company_speciality") or ""
            company_description = exp.get("company_description") or ""
            job_summary = exp.get("job_summary") or ""
            start = exp.get("start") or ""
            end = exp.get("end") or ""

            if idx > 0:
                lines.append("")
            lines.append(f"Experience {idx + 1}:")
            if title:
                lines.append(f"  Title: {title}")
            if company:
                lines.append(f"  Company: {company}")
            if company_industry:
                lines.append(f"  Industry: {company_industry}")
            if company_speciality:
                lines.append(f"  Company Specialty: {company_speciality}")
            if company_description:
                lines.append(f"  Company Description: {company_description}")

            # Add company data from Company index if available
            company_universal_name = exp.get("company_universal_name")
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
                            f"  Company Description (from Company Index): {li_description}"
                        )
                    if isinstance(li_industries, list) and li_industries:
                        lines.append(
                            f"  Company Industries (from Company Index): {', '.join(li_industries[:5])}"
                        )
                    if isinstance(li_specialties, list) and li_specialties:
                        lines.append(
                            f"  Company Specialties (from Company Index): {', '.join(li_specialties[:5])}"
                        )
                    if li_size:
                        lines.append(f"  Company Size: {li_size}")
                    if li_headquarter:
                        if isinstance(li_headquarter, dict):
                            hq_city = li_headquarter.get("city") or ""
                            hq_country = li_headquarter.get("country") or ""
                            if hq_city or hq_country:
                                lines.append(
                                    f"  Company Headquarters: {', '.join(filter(None, [hq_city, hq_country]))}"
                                )
                        elif isinstance(li_headquarter, str):
                            lines.append(f"  Company Headquarters: {li_headquarter}")
                    if cb_stock_symbol:
                        lines.append(f"  Stock Symbol: {cb_stock_symbol}")

            if start:
                period = f"{start}"
                if end:
                    period += f" to {end}"
                else:
                    period += " to Present"
                lines.append(f"  Period: {period}")
            if job_summary:
                lines.append(f"  Job Summary: {job_summary}")

    lines.append("\n" + "=" * 60)

    return "\n".join(lines)


async def score_profile_with_llm(
    profile: Dict[str, Any],
    metrics: List[Dict[str, Any]],
    model: str = "openai/gpt-4o",
    es_client: Optional[AsyncElasticsearch] = None,
    calendar_events: Optional[List[Dict[str, Any]]] = None,
    include_calendar_insights: bool = True,
    reference_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Score a profile using LLM based on the provided metrics.

    Args:
        profile: The connection profile to score
        metrics: List of scoring metrics with name, weight, what_to_check, reasoning
        model: LLM model to use
        es_client: Optional Elasticsearch client for company data
        calendar_events: Optional calendar events for this connection
        include_calendar_insights: Whether to include calendar insights in scoring
        reference_profile: Optional reference profile (the person we're scoring FOR)

    Returns: {
        "total_score": float,
        "metric_scores": {metric_name: score},
        "reasoning": str,
        "nullify": bool
    }
    """
    metrics_text = "\n".join(
        [
            f"{i+1}. {m.get('name', 'Unknown')} (Weight: {m.get('weight', 0)})\n"
            f"   What to Check: {m.get('what_to_check', '')}\n"
            f"   Reasoning: {m.get('reasoning', '')}"
            for i, m in enumerate(metrics)
        ]
    )

    # Extract company universal names from experience (only most recent)
    company_universal_names = []
    experience = profile.get("experience", [])
    if isinstance(experience, list):
        for exp in experience[:1]:  # Only most recent experience
            if isinstance(exp, dict):
                universal_name = exp.get("company_universal_name")
                if universal_name:
                    company_universal_names.append(universal_name)

    # Fetch company data if es_client is available
    company_data_map = {}
    if es_client and company_universal_names:
        company_data_map = await _fetch_company_data_for_scoring(
            company_universal_names, es_client
        )

    # Extract profile information as formatted text instead of JSON (with company data)
    profile_text = _extract_profile_for_scoring(profile, company_data_map)

    # Extract connection's current company for explicit inclusion in the prompt
    connection_company = ""
    exp_list = profile.get("experience", [])
    if isinstance(exp_list, list) and exp_list and isinstance(exp_list[0], dict):
        connection_company = (
            exp_list[0].get("company") or exp_list[0].get("company_name") or ""
        )
    connection_company_section = ""
    if connection_company:
        connection_company_section = f"""
CONNECTION'S CURRENT COMPANY: {connection_company}
"""

    # Extract reference profile information if provided
    reference_profile_section = ""
    if reference_profile:
        ref_name = (
            reference_profile.get("full_name")
            or reference_profile.get("name")
            or "Unknown"
        )
        ref_headline = reference_profile.get("headline") or ""
        ref_industry = ""
        ref_company = ""
        ref_title = ""
        ref_company_description = ""
        ref_exp = reference_profile.get("experience", [])
        if isinstance(ref_exp, list) and ref_exp:
            first_exp = ref_exp[0] if isinstance(ref_exp[0], dict) else {}
            ref_company = (
                first_exp.get("company") or first_exp.get("company_name") or ""
            )
            ref_title = first_exp.get("title") or ""
            ref_industry = first_exp.get("company_industry") or ""
            ref_company_description = first_exp.get("company_description") or ""

        # Build company description section only if available
        company_desc_section = ""
        if ref_company_description:
            company_desc_section = f"\nCompany Description: {ref_company_description}"

        reference_profile_section = f"""
            ================================
            REFERENCE PROFILE (Scoring connections FOR this person):
            ================================
            Name: {ref_name}
            Headline: {ref_headline}
            Current Title: {ref_title}
            Current Company: {ref_company}
            Industry:{company_desc_section}

            NOTE: The connection profile below should be scored based on how relevant/useful they would be to this reference person.
            ================================
            """

    # Extract calendar insights when explicitly enabled.
    calendar_insights_section = ""
    if include_calendar_insights:
        if calendar_events:
            try:
                # Import calendar processing functions
                from app.utils.profile_fetching.scoring_metrics_generation import (
                    _extract_calendar_insights,
                )

                calendar_insights = await _extract_calendar_insights(
                    calendar_events, model="openai/gpt-4o"
                )
                if calendar_insights:
                    calendar_insights_section = f"""

--------------------------------
CALENDAR-BASED INSIGHTS FOR THIS CONNECTION:
--------------------------------
{calendar_insights}

Use these calendar insights to better understand this connection's actual business relationships, meeting patterns, and professional focus areas when scoring.
"""
                else:
                    # Show message even if extraction returned empty
                    calendar_insights_section = f"""

--------------------------------
CALENDAR-BASED INSIGHTS FOR THIS CONNECTION:
--------------------------------
No calendar insights could be extracted from the {len(calendar_events)} calendar event(s) provided.
"""
            except Exception as e:
                logging.warning(f"Error extracting calendar insights: {e}")
                # Show message even if extraction failed
                calendar_insights_section = f"""

--------------------------------
CALENDAR-BASED INSIGHTS FOR THIS CONNECTION:
--------------------------------
Calendar events were provided but insights extraction failed: {str(e)}
"""
        else:
            # No calendar events provided
            calendar_insights_section = """

--------------------------------
CALENDAR-BASED INSIGHTS FOR THIS CONNECTION:
--------------------------------
No calendar events provided for this connection.
"""

    prompt = f"""
You are a  Executive Business Development scoring agent. Score the following CONNECTION profile based on how relevant/useful they would be to the REFERENCE person.

{reference_profile_section}
METRICS TO USE:
{metrics_text}

CONNECTION PROFILE TO SCORE:

{profile_text}

{connection_company_section}

-------------------------------------------------------
Calendar
{calendar_insights_section}

INSTRUCTIONS:
- GIVE FULL SCORE IN EVERY METRIC IF THE CONNECTION PERSON BELONGS TO A BIG COMPANY AND ALIGN GREALY WITH THE REFERENCE PROFILE SUCH AS CEO C-Suite SR VP CEO OF A COMPANY IN THE SAME INDUSTRY GIVE HIME FULL MARKS IN DECISION AUTHORITY METRICS
-Check the Title and the industry of the connection profile should specifically align with the reference profile
- IF the Connection person is from sales and MArketing and The reference profile id the CEO of a company obiously that particular person  he should be nulify 
- IF the connection is from QA and these type of worker role and the reference profile is the CEO of a company then that profile is not useful at all
- Please GIVE MORE WEIGGTAGE TO CEO AND HIGH Authority people on every metric if their company belong to relevant industry
- Please Consider the Company size before scoring the profile If individual belong to a big company and alings grealy with the reference profile  please give them full score on all the metrics
- Score each metric from 0 to its weight (based on how well the profile matches "what_to_check").
- Always make Sure the profile industry experience should specifically align with the metric industry experience is also necessary rather than position
- IF the relevant person is not in the Same industry then that profile is not useful at all 
- If calendar insights are provided, use them to better understand the connection's actual business activities and relationships

NULLIFY LOGIC (IMPORTANT):
- Make sure to score ragardless of nulfy is true or False
- A profile should be NULLIFIED (nullify: true) if it is fundamentally wrong for the referenced person, even if it scores well on some metrics.
- Examples of when to NULLIFY:
  * Person has decision-making power but is NOT AT ALL industry relevant → nullify: true
  * Person is in the exact relevant industry but has NO decision-making power → nullify: true
  * Profile scores very high on some metrics but critically fails on others that make them unsuitable
- When to NOT NULLIFY (nullify: false):
  * ALL metrics individually score above 75% of their maximum weight
  * The profile is a balanced fit across all scoring dimensions

Provide a JSON response with:
  - "total_score": sum of all metric scores (float)
  - "metric_scores": {{"metric_name": score}} for each metric (Score out of weight in the metrics section)
  - "reasoning": 1-2 concise sentences in plain language explaining WHY this connection is (or is not) commercially relevant to the reference person.
    - Focus on industry fit, seniority/decision authority, and concrete BD potential.
    - DO NOT mention internal metric names, weights, formulas, labels like "simple_v2", or raw numbers such as "meeting_points" or "company_context_points".
    - Avoid listing scores; summarize the overall judgment instead.
  - "nullify": boolean (true if profile should be nullified as explained above, false otherwise)

Return ONLY valid JSON, no markdown, no extra text.
"""

    try:
        response = await invoke(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model=model,
            temperature=0.2,
            fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4o"],
            verbose=False,
        )

        if not response:
            raise ValueError("Empty response from LLM")

        # Extract JSON from response
        response_text = response.strip()
        if response_text.startswith("```"):
            response_text = (
                re.sub(r"^```[a-zA-Z]*\n", "", response_text).replace("```", "").strip()
            )

        # Try to extract JSON object
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            result = json.loads(json_match.group(0))
        else:
            result = json.loads(response_text)

        # Post-process nullify logic: validate and apply 75% rule as fallback
        metric_scores = result.get("metric_scores", {})
        if metric_scores and metrics:
            # Check if all metrics are above 75% of their weight
            all_above_75_percent = True
            for m in metrics:
                metric_name = m.get("name", "")
                metric_weight = float(m.get("weight", 0))
                metric_score = float(metric_scores.get(metric_name, 0))
                threshold = metric_weight * 0.75
                if metric_score < threshold:
                    all_above_75_percent = False
                    break

            # If LLM didn't return nullify, apply the 75% rule
            if "nullify" not in result:
                result["nullify"] = not all_above_75_percent
            # If LLM says nullify: false but not all metrics are above 75%, override
            elif result.get("nullify") is False and not all_above_75_percent:
                # Keep LLM's decision if it explicitly said false
                # (LLM might have good reasons)
                pass
            # If LLM says nullify: true but all metrics are above 75%, trust LLM
            # (LLM might detect issues not captured by simple threshold)

        elif "nullify" not in result:
            # No metric scores to validate, default to nullify: true
            result["nullify"] = True

        return result

    except Exception as e:
        logging.error(f"Error scoring profile: {e}")
        return {
            "total_score": 0.0,
            "metric_scores": {
                m.get("name", f"metric_{i}"): 0.0 for i, m in enumerate(metrics)
            },
            "reasoning": f"Error during scoring: {str(e)}",
            "nullify": True,  # Nullify on error
        }


async def generate_profile_description(
    profile: Dict[str, Any],
    model: str = "openai/gpt-4o",
) -> str:
    """
    Generate a markdown description of a LinkedIn profile with structured headings.
    Returns markdown with sections for network and industries.
    """
    # Use the same extracted-text builder as the scoring metrics flow (more robust than JSON dumps).
    # Note: the route already fetches with only_top_experience=True, so this will only include the most recent experience.
    try:
        from app.utils.profile_fetching.scoring_metrics_generation import (
            _extract_profile_information,
        )

        profile_context = _extract_profile_information(profile, company_data_map=None)
    except Exception:
        # Fallback to a minimal JSON-free summary if the import/extraction fails
        name = profile.get("full_name") or profile.get("name") or "Professional"
        headline = profile.get("headline") or ""
        profile_context = f"Name: {name}\nHeadline: {headline}".strip()

    prompt = f"""
You are a Business Development professional. Generate a professional markdown description of the following LinkedIn profile.

PROFILE DATA:
{profile_context}

INSTRUCTIONS:
- Generate a markdown-formatted description with the following structure:
- Use the exact heading "Your Network likely includes:" (with colon)
- Use the exact heading "Across multiple Industries like :" (with colon and space before colon)
- Under "Your Network likely includes:", list relevant professional connections, roles, or types of people this profile would connect with
- Under "Across multiple Industries like :", list the industries this profile spans or is involved in 
- Write in a professional tone
- Be specific and concrete, avoid generic statements
- Alssoo give 6-7 words tagline for the profile
- Tagline should be a single sentence that captures the essence of the profile
- Analyze the profile's experience, companies, and background to populate these sections accurately
- Return ONLY valid markdown with these headings, no code fences, no extra text before or after

EXAMPLE FORMAT:
Your Network likely includes:
- [relevant connections/roles]

Across multiple Industries like :
- [industry 1], [industry 2], [industry 3], [industry 4]

TAGLINE:
- [tagline]

EXAMPLE OUTPUT:
Your Network likely includes:
- descision-makers

Across multiple Industries like :
- computer software, Software, Information Technology, Artificial Intelligence
"""

    try:
        response = await invoke(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model=model,
            temperature=0.2,
            max_tokens=2500,
            verbose=False,
        )

        if not response:
            raise ValueError("Empty response from LLM")

        # Clean up the response - keep markdown but remove code fences if present
        description = response.strip()
        # Remove markdown code fences if present
        if description.startswith("```"):
            description = (
                re.sub(r"^```[a-zA-Z]*\n", "", description).replace("```", "").strip()
            )
        # Remove quotes if wrapped (but keep markdown formatting)
        if description.startswith('"') and description.endswith('"'):
            description = description[1:-1]
        if description.startswith("'") and description.endswith("'"):
            description = description[1:-1]

        return description

    except Exception as e:
        logging.error(f"Error generating profile description: {e}")
        # Extract basic info as fallback with markdown format
        name = profile.get("full_name") or profile.get("name") or "Professional"
        headline = profile.get("headline") or ""
        industries = profile.get("industries", []) or []

        fallback = f"Your Network likely includes:\n- {name}{(' specializing in ' + headline) if headline else ''}\n\n"
        if industries:
            fallback += "Across multiple Industries like :\n"
            for industry in industries[:5]:  # Limit to 5 industries
                fallback += f"- {industry}\n"
        else:
            fallback += "Across multiple Industries like :\n- Various industries\n"

        return fallback.strip()


def extract_generic(start: str, end: str, text: str):
    """
    Extracts text between two specified delimiters using regular expressions.

    This function searches for text that appears between the start and end delimiters
    in the provided text string, handling multi-line content with the DOTALL flag.

    Args:
        start (str): The starting delimiter to search for
        end (str): The ending delimiter to search for
        text (str): The text to search within

    Returns:
        str or None: The extracted text if found, None otherwise
    """
    match = re.search(rf"{start}(.*?){end}", text, re.DOTALL)
    return match.group(1) if match else None


class Experts:
    L2_INDUSTRY_EXPERT = {
        "SYSTEM": """
You are a comprehensive expert at finding company and industry filter values in a multi-agent system for executive recruiting search, where you can work with other agents as well. You have access to the following information:

## Definitions and Instructions for Industry and Company Filters and IMPORTANT FILTER RELATIONS
1. INDUSTRY
- Backend mapping: Matched in 'experience.company_description', 'experience.company_industry', 'experience.company_speciality', 'experience.title', and 'experience.job_summary'
- Any Value that is selected in this filter will be matched as a sub string in all these fields, and all profiles with substring matches in these fields will be returned.
- Industry is preferable when there is no specificity in the company name and the industry is known.
- Since, this filter is mapped to a lot of fields, you can see that this yields very unpredictable results, because any substring that is matched to any of the field against a profile will be retrieved.
- Profiles that are retrieved are somewhat less accurate because it is a very broader filter.
- But it becomes important in cases, where user has not provided any specificity and is broadly looking for a profile in a particular industry.
- It becomes very important in times where going for specificity results in very low recall and low number of retrieved profiles, so in those cases, this filter becomes very important as it returns more profiles but they include mention of that particular substring in any of the fields mentioned above.
- Example: {"INDUSTRY": ["Pharmaceuticals", "Healthcare", "Consumer Electronics"]}


2. COMPANY
- Backend mapping: Matched in 'experience.company', this key holds mostly just the name of the company
- Mostly this filter is used when user is looking for specific classes of companies and not the industry.
- It is very relevant when used for queries with certain metrics or conditions, such as "FAANG Companies", or "companies with revenue > 2.3$ billion" or "companies with employee size > 10000".
- It is also used when user is looking for specific companies or similar companies by providing some example companies, such as "Companies with similar culture to Google".
- Profiles that are retrieved with this filter are more precise and more accurate as it is a very specific filter.
- One more thing to note is that this filter is linked to another "company generation service" that works seperately to generate a accurate list of companies given the company prompt. So in that case usage of that prompt yields to very good results.
- But it fails when the requirment becomes overwhelmingly specific, so in that case INDUSTRY filter yields good results.

3. Min/Max STAFF(Optional):
- This case does not always occur, but it is important
- This is just the value of min_staff and max_staff that you need to infer based on the company prompt that you are getting from the user.
- You don't need to include it in the output, but you should pass it to your expert brother in every message, whether it is zero or some inferred value for the company.

- Timeline options for both JOB_TITLE/FUNCTIONAL_AREA and MANAGEMENT_LEVEL:
    * Current: Profiles which have the Current entities in their Current experience will be shown
    * Past: Only profiles which have the Past entities in their Past Experience will be shown
    * Current or Past: Profiles that match any of the entities in either Current or Past experiences will be shown
    * Current and Past: Only profiles that match atleast one current entity in their current experience AND match atleast one past entity in their Past experience will be shown

NOTE: 
- Timeline is important and can change results drastically. So, it is important to consider the timeline while filtering profiles.
- Before writing the timeline, it is important to analyze the intent of the user query and the feedback suggestions and then decide the timeline. While improving the results, this is also an important factor to consider.
- When you are reasoning which timeline should be used, you should focus on the reasoning and while writing the values in the filters, keep that in mind as well. And also which relation i.e., CURRENT AND/OR PAST or just CURRENT, PAST etc. This is really important



## Thought Process:
- Your main focus should be understanding the intent of the user query and feedback and messages from your expert brothers.
- While analyzing the intent of the user, you must think which requirments are required to be catered by the company and which by the industry.
- Once you get correct understanding of the user query, your next step should be to to map that intent into the strategy of filters that you are going to use.
- Because, depending on the scenario, it could be that even if the user has not mentioned explicit companies, but because of the way the user has asked the query, it could be that the company filter is best used in the query, or it could be that the industry filter is best used in the query.
- So, it is important to understand the intent of the user query and then map that intent into the filters that you are going to use.
- Following nuances and examples should guide you through this process, so take inspirations from them.


## Some Nuances and Examples to help you through the process:
- As you are very well aware that industry filter is mapped to a lot of fields, and whatever value we add in that filter so it just does the dumb string matching and returns those results. 
- Values in the industry filter doesn't mean that only those industries will come, if you apply a generic keywords, or if you try to broader the domain using industry filter, it will bring in a lot of profiles that are not relevant, so it is important to keep this in mind, while writing values in this filter. SO THIS IS VERY IMPORTANT FOR YOU TO UNDERSTAND that while using the INDUSTRY filter, the industry values that you write must be very NARROW depending on the task and not very GENERIC industries as they will bring in profiles from irrelevant sectors because of very broad mapping of the industry filter.
- One General Rule of Thumb to use a company filter is when user has either provided some company names or want a very specific class of companies, such as "FAANG Companies", or "companies with revenue > 2.3$ billion" or "companies with employee size > 10000". This is because, values in company prompt are passed to a company generation service that yeilds almost correct companies as compared to jsut dumb string matching in industry filter.
- Another scenario is when user has not provided any company names, but has provided some industry names, in that case, it is better to use industry filter, because company filter would be too specific and would yield very low recall.
- As mentioned above, user can ask in prompts companies with certain restrictions. One such restriction is important in this case which is companies which a certain revenue or employee size or a certain class. One example of such scenario is "premium FMCG companies with revenue greater than 1 billion dollars", in this case since it is a specific condition, we can cater it using company prompt, but one additional thing that we can do is give an estimate of min_staff and max_staff values to your brother expert of jobtitles/managementlevel/ agent, that will apply these restrictions in that title. This way we can cater the requirment of the user in a very specific way, because adding constraint of certain employee sizes will bring those companies.
- Similarly if user query is very generic and is asking for "big companies in tech", then it is better to use industry filter, because company filter would be too specific and would yield very low recall. but since user has asked for big companies we can add additional constraint of min_staff and max_staff as well. You should write the estimated values in the message to that agent as well.
- IMPORTANT: RELATION BETWEEN COMPANY AND INDUSTRY FILTERS IS `OR`, so if you have values in both company and industry filter, then it would be looking for profiles that have either of the values in the company or the industry field. So it is important to keep this in mind while writing values in the filters.

## Messages for other EXPERTS
- You have a brother expert, whose Job is to deal with JobTitle, ManagementLevel and Skills.
- While executing the query, if you have any messages for EXPERTS you can write them here.
- But before writing you should ask yourself the following questions:
    - What is it in the query that I am unable to incorporate just by using Company and Industry?
    - What is it in the query that your brother expert would be able to solve it?
    - What should you write in the message.
- Then write the message for your brother expert of JobTitle, ManagementLevel and Skills
- You should include the min_staff and max_staff values in the message to your brother expert of JobTitle, ManagementLevel and Skills and their little reasoning as well.

    """,
        "USER": """   
Now following is the query that you need to solve:
`{{QUERY}}`
I want you to think deeply withiin the "reasoning" key and provide extensive reasoning for the filters that you have selected while also referencing the instructions. 
While thinking, you should be asking questions to yourself like:
- What is the base intent of the query sub-part related to Company and Industry?
- What are the possible valid approaches to solve the same problem? Don't be redundant while doing this, ensure that each possible approach is unique and meaningful.
- What would be the boolean expression for each approach that you are suggesting and what would be the most relevant to the current scenario?
- If I select a particular value in Industry, what should I be selecting in companies and vice versa?
- Should I be using Industry or Company at all? or use just one of them?
- What is the benefit of selecting a particular value in Industry and Company?
and other similar questions.

Provide answer to these questions in the "reasoning" key explanation and other thinking process in it as well.


Finally this should be your output format. Do not include anything else:
    <reasoning> 
    Provide your extensive reasoning here in detail in the following format:
    # Current Filters Analysis:
    - If current filters are present, then do current filters analysis first. Understand the intent of the query and then try to address the problem.
    # Feedback Analysis:
    - If feedback is present do your feedback analysis first. 
    - While doing feedback analysis, understand the intent of the feedback.
    - Then understand the shortcomings that are mentioned in the feedback and their reasons as to why they have occurred by referencing current filters with proper reasoning.
    - Clearly understand do's and don'ts and then try to address the problem.
    - CAREFULLY CONSIDER THE PROFILE COUNT IN THE FEEDBACK AS WELL. While precision is important, we need to have a good recall as well. You should try to address the problem in such a way that if profiles count is less than 100, then you should try to increase the recall.
    # Solutions to the problems:
    - Write the solutions to the problems that you have identified in the feedback analysis.
    - Write the reasoning for that solutions being effective that you have provided.
    - What will be the improvement that this solution will give according to the relations between filters and the user query.
    - While writing solutions, Your possible approaches don't necessarily have to be a combination of various filters, it can try different values in the same combination as well.
    # Filters Manipulation: 
    - Now based on all the previous reasoning, think through deeply and reason on what will be the values in the filters.
    - Think in terms of boolean expressions and how they will be impacting the results.
    - Carefully consider the timeline values and intent of the query as well.

    All reasoning should also be in detail and comprehensive and cohesive.
    </reasoning>
    Following is the filter output that you have to give, and it should be strictly based on the above reasoning, the selection of optimal values and boolean expressions. Make sure you output valid and parseable yaml. For now just give values for the industry filter, do not bother with companies.
    <filters_json>
    {
    "expert": "COMPANY_INDUSTRY_EXPERT",  // this should be same and not changed
    "other_filters_dependency": "other filters that are not related to Company and Industry but can contribute to the solution",
    "filters": {
        "company": {
        "current_prompt": "prompt for current timeline companies",
        "past_prompt": "prompt for past timeline companies",
        "event": ""  // Can only be CURRENT, PAST, CURRENT OR PAST, CURRENT AND PAST, and cannot be left blank
        },
        "industry": {
        "current": [],  // The entities that must be in current, and not past experiences
        "past": [],  // The entities that must be in past, and not current experiences
        "both": [],  // The entities that can be in current, past both
        "event": ""  // Can only be CURRENT, PAST, CURRENT OR PAST, CURRENT AND PAST, and cannot be left blank
        }
    },
    "message_for_other_expert": "write your detailed message for other agent here."
    }
    </filters_json>""",
    }


def write_logs(*args, **kwargs):
    logging.info("write_logs called with:")

    if args:
        logging.info("Positional arguments:")
        for i, arg in enumerate(args):
            logging.info(f"  arg[{i}]: {arg}")

    if kwargs:
        logging.info("Keyword arguments:")
        for key, value in kwargs.items():
            # Safely log JSON-formatted content when possible
            try:
                formatted = json.dumps(value, indent=2)
                logging.info(f"  {key}: {formatted}")
            except (TypeError, ValueError):
                logging.info(f"  {key}: {value}")


MAP_TIMELINE = {
    "CURRENT": "Current",
    "PAST": "Past",
    "OR": "Current or Past",
    "AND": "Current and Past",
    "CURRENT OR PAST": "Current or Past",
    "CURRENT AND PAST": "Current and Past",
}


EXPERTS_MAPPING = {
    "L2_INDUSTRY_EXPERT": Experts.L2_INDUSTRY_EXPERT,
}


async def get_prompt_expert(
    query,
    expert_name,
):

    system_prompt = EXPERTS_MAPPING[expert_name]["SYSTEM"]
    user_prompt = EXPERTS_MAPPING[expert_name]["USER"]
    user_prompt = user_prompt.replace("{{QUERY}}", query)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


async def get_expert_response(prompt):
    return await asynchronous_llm(
        messages=copy.deepcopy(prompt),
        model="gpt-4.1-2025-04-14",
        fallback_openai_model="gpt-4o",
        provider="openai",
        temperature=0.01,
        max_tokens=8000,
    )


def clean_results(data):
    key_mapping = {
        "total_working_years": "experience",
        "job_title": "title",
        "job_title/business_function": "title",
        "management_levels": "management_level",
        "company_keywords": "industry",
        "profile_keywords": "skill",
        "skills": "skill",
        "locations": "location",
        "names": "name",
        "isExperienceSelected": "isExperienceSelected",  # Key added as requested
    }

    for key in ["job_title", "management_level", "location", "industry"]:
        if key in data and all(
            not data[key][k] for k in ["current", "past", "both"] if k in data[key]
        ):
            del data[key]

    if "company" in data and not data["company"]:
        del data["company"]
    elif "company" in data and (
        not data["company"].get("current_prompt", None)
        and not data["company"].get("past_prompt", None)
    ):
        del data["company"]

    if "skill" in data and not data["skill"]:
        del data["skill"]

    for key in ["school", "education", "name"]:
        try:
            if key in data and not data[key]:
                del data[key]
            elif key in data and key == "education":
                if isinstance(data[key], dict):
                    data[key] = [data[key]]
                for index in range(len(data[key])):
                    if (
                        "degree" in data[key][index]
                        and data[key][index].get("degree")
                        and data[key][index].get("degree")
                        in [
                            "Any",
                            "Associate",
                            "Bachelor's",
                            "Bachelor’s",
                            "Bachelor\u2019s",
                            "Master's",
                            "Master’s",
                            "Master\u2019s",
                            "Doctorate",
                            "Diploma",
                            "Certificate",
                        ]
                    ):
                        data[key][index]["degree"] = (
                            data[key][index]["degree"]
                            .replace("Bachelor's", "Bachelor’s")
                            .replace("Master's", "Master’s")
                            .replace("\u2019", "’")
                            .capitalize()
                            .strip()
                        )
                    elif "degree" in data[key][index]:
                        del data[key][index]["degree"]

                    if "major" in data[key][index] and data[key][index].get("major"):
                        data[key][index]["major"] = (
                            data[key][index]["major"].replace("'", "’").title()
                        )
                    elif "major" in data[key][index]:
                        del data[key][index]["major"]

                    if not data[key][index]:
                        del data[key][index]
        except:
            del data[key]

    if "total_working_years" in data and (
        data["total_working_years"] == {"min": None, "max": None}
        or data["total_working_years"] == {"min": -1, "max": 60}
        or data["total_working_years"] == [0, 60]
    ):
        del data["total_working_years"]

    if "role_tenure" in data and (
        data["role_tenure"] == {"min": None, "max": None}
        or data["role_tenure"] == {"min": 0, "max": 60}
        or data["role_tenure"] == [0, 60]
    ):
        del data["role_tenure"]
    elif (
        "role_tenure" in data
        and ("min" in data["role_tenure"] and "max" in data["role_tenure"])
        and (data["role_tenure"]["min"] == data["role_tenure"]["max"])
    ):
        data["role_tenure"]["min"] -= 1
        data["role_tenure"]["max"] += 1

    if "company_tenure" in data and (
        data["company_tenure"] == {"min": None, "max": None}
        or data["company_tenure"] == {"min": 0, "max": 60}
        or data["company_tenure"] == [0, 60]
    ):
        del data["company_tenure"]
    elif (
        "company_tenure" in data
        and ("min" in data["company_tenure"] and "max" in data["company_tenure"])
        and (data["company_tenure"]["min"] == data["company_tenure"]["max"])
    ):
        data["company_tenure"]["min"] -= 1
        data["company_tenure"]["max"] += 1

    data = {
        key_mapping[key] if key in key_mapping else key: value
        for key, value in data.items()
    }
    return data


async def reverse_output_to_input(output_data):
    def print_logs(message):
        payload = {"message": message}
        # print(payload)

    main_output = {}

    levels_mapping = {
        "C-Suite/Chiefs": "C Suite",
        "President": "President",
        "Executive VP or Sr. VP": "Executive and Sr. VP",
        "Founder or Co-founder": "Founder/Co-Founder",
        "Senior (All Senior-Level Individual Contributors)": "Senior (Individual Contributor)",
        "Mid (All Mid-Level Individual Contributors)": "Mid (Individual Contributor)",
        "Junior (All Junior-Level Individual Contributors)": "Junior",
        "Senior": "Senior (Individual Contributor)",
        "Mid": "Mid (Individual Contributor)",
        "Junior": "Junior",
    }

    input_data = {}
    try:
        if "school" in output_data:
            input_data["school"] = output_data.get("school", [])
            main_output["school"] = input_data["school"]
    except:
        print_logs("school error")
        pass

    try:
        if "company" in output_data:
            input_data["company"] = output_data.get("company", {})
            main_output["company"] = input_data["company"]
    except:
        print_logs("company error")
        pass

    try:
        if "company_timeline" in output_data:
            input_data["company_timeline"] = output_data.get("company_timeline", {})
            main_output["company_timeline"] = input_data["company_timeline"]
    except:
        print_logs("company timeline error")
        pass

    # Reverse education
    try:
        if "education" in output_data:
            input_data["education"] = output_data.get("education", [])
        main_output["education"] = input_data.get("education", [])
    except:
        print_logs("education error")
        pass

    # Reverse experience
    try:
        if "experience" in output_data:
            input_data["experience"] = (
                {
                    "min": output_data["experience"]["min"],
                    "max": output_data["experience"]["max"],
                }
                if isinstance(output_data["experience"], dict)
                and len(output_data["experience"]) == 2
                else None
            )
        main_output["experience"] = input_data.get("experience", {})
    except:
        print_logs("experience error")
        pass

    try:
        if "company_tenure" in output_data:
            input_data["company_tenure"] = (
                {
                    "min": output_data["company_tenure"]["min"],
                    "max": output_data["company_tenure"]["max"],
                }
                if isinstance(output_data["company_tenure"], dict)
                and len(output_data["company_tenure"]) == 2
                else None
            )
        main_output["company_tenure"] = input_data.get("company_tenure", {})
    except Exception as e:
        print_logs(e)
        print_logs("company tenure error")
        pass

    try:
        if "role_tenure" in output_data:
            input_data["role_tenure"] = (
                {
                    "min": output_data["role_tenure"]["min"],
                    "max": output_data["role_tenure"]["max"],
                }
                if isinstance(output_data["role_tenure"], dict)
                and len(output_data["role_tenure"]) == 2
                else None
            )
        main_output["role_tenure"] = input_data.get("role_tenure", {})
    except:
        print_logs("role tenure error")
        pass

    try:
        if "industry" in output_data:
            common_elements = set(output_data["industry"].get("current", [])) & set(
                output_data["industry"].get("past", [])
            )
            both = output_data["industry"].get("both", [])
            both.extend(
                item
                for item in common_elements
                if item not in output_data["industry"].get("both", [])
            )
            output_data["industry"]["both"] = both

            output_data["industry"]["current"] = [
                item
                for item in output_data["industry"].get("current", [])
                if item not in common_elements
            ]
            output_data["industry"]["past"] = [
                item
                for item in output_data["industry"].get("past", [])
                if item not in common_elements
            ]

            input_data["industry"] = {"event": None, "filter": {}}
            current = output_data["industry"].get("current", [])
            past = output_data["industry"].get("past", [])

            if current and not past and not both:
                input_data["industry"]["event"] = "CURRENT"
            elif past and not current and not both:
                input_data["industry"]["event"] = "PAST"
            else:
                input_data["industry"]["event"] = next(
                    (
                        item
                        for item in ["OR", "AND"]
                        if item in output_data["industry"].get("event", "").upper()
                    ),
                    "OR",
                )

            input_data["industry"]["filter"].update(
                {
                    location: {"type": "CURRENT"}
                    for location in output_data["industry"].get("current", [])
                }
            )
            input_data["industry"]["filter"].update(
                {
                    location: {"type": "PAST"}
                    for location in output_data["industry"].get("past", [])
                }
            )
            input_data["industry"]["filter"].update(
                {
                    location: {"type": "BOTH"}
                    for location in output_data["industry"].get("both", [])
                }
            )
            main_output["industry"] = input_data.get("industry", {})
    except:
        print_logs("industry error")
        pass
    # Reverse job_titles
    try:
        if "title" in output_data:
            common_elements = {}
            current_titles = output_data["title"].get("current", [])
            past_titles = output_data["title"].get("past", [])
            both_titles = output_data["title"].get("both", [])

            for current in current_titles:
                current_name = current.get("title_name")
                if current_name:
                    for past in past_titles:
                        if current_name == past.get("title_name"):
                            common_elements[current_name] = {
                                "title_name": current_name,
                                "min_staff": min(
                                    current.get("min_staff", float("inf")),
                                    past.get("min_staff", float("inf")),
                                ),
                                "max_staff": max(
                                    current.get("max_staff", float("-inf")),
                                    past.get("max_staff", float("-inf")),
                                ),
                            }

            both_names = {item.get("title_name") for item in both_titles}
            for common in common_elements.values():
                if common["title_name"] not in both_names:
                    both_titles.append(common)

            output_data["title"]["current"] = [
                item
                for item in current_titles
                if item.get("title_name") not in common_elements
            ]

            output_data["title"]["past"] = [
                item
                for item in past_titles
                if item.get("title_name") not in common_elements
            ]

            output_data["title"]["both"] = both_titles

            current = output_data["title"].get("current", [])
            past = output_data["title"].get("past", [])
            both = both_titles

            input_data["title"] = {"event": None, "filter": {}}
            if current and not past and not both:
                input_data["title"]["event"] = "CURRENT"
            elif past and not current and not both:
                input_data["title"]["event"] = "PAST"
            else:
                input_data["title"]["event"] = next(
                    (
                        item
                        for item in ["OR", "AND"]
                        if item in output_data["title"].get("event", "").upper()
                    ),
                    "OR",
                )

            input_data["title"]["filter"].update(
                {
                    title["title_name"]: {
                        "min": title["min_staff"],
                        "max": title["max_staff"],
                        "type": "CURRENT",
                    }
                    for title in output_data["title"].get("current", [])
                }
            )
            input_data["title"]["filter"].update(
                {
                    title["title_name"]: {
                        "min": title["min_staff"],
                        "max": title["max_staff"],
                        "type": "PAST",
                    }
                    for title in output_data["title"].get("past", [])
                }
            )
            input_data["title"]["filter"].update(
                {
                    title["title_name"]: {
                        "min": title["min_staff"],
                        "max": title["max_staff"],
                        "type": "BOTH",
                    }
                    for title in output_data["title"].get("both", [])
                }
            )
        main_output["title"] = input_data.get("title", {})
    except Exception as e:
        print_logs("title error")
        print_logs(output_data["title"])
        raise e
    # Reverse management_levels
    try:
        if "management_level" in output_data:
            possible_levels = [
                "Partners",
                "Founder/Co-Founder",
                "Board of Directors",
                "C Suite",
                "President",
                "Executive and Sr. VP",
                "VP",
                "Director",
                "Manager",
                "Senior (Individual Contributor)",
                "Mid (Individual Contributor)",
                "Junior",
                "Head",
            ]

            common_elements = set(
                output_data["management_level"].get("current", [])
            ) & set(output_data["management_level"].get("past", []))
            both = output_data["management_level"].get("both", [])
            both.extend(
                item
                for item in common_elements
                if item not in output_data["management_level"].get("both", [])
            )
            output_data["management_level"]["both"] = both

            output_data["management_level"]["current"] = [
                item
                for item in output_data["management_level"].get("current", [])
                if item not in common_elements
            ]
            output_data["management_level"]["past"] = [
                item
                for item in output_data["management_level"].get("past", [])
                if item not in common_elements
            ]

            current = [
                item for item in output_data["management_level"].get("current", [])
            ]
            past = [item for item in output_data["management_level"].get("past", [])]
            both = [item for item in output_data["management_level"].get("both", [])]

            input_data["management_level"] = {"event": None}

            if current and not past and not both:
                input_data["management_level"]["event"] = "CURRENT"  # type: ignore
            elif past and not current and not both:
                input_data["management_level"]["event"] = "PAST"  # type: ignore
            else:
                input_data["management_level"]["event"] = next(  # type: ignore
                    (
                        item
                        for item in ["OR", "AND"]
                        if item
                        in output_data["management_level"].get("event", "").upper()
                    ),
                    "OR",
                )

            input_data["management_level"]["filter"] = {  # type: ignore
                levels_mapping.get(level, level): {"type": "CURRENT"}
                for level in output_data["management_level"].get("current", [])
                if levels_mapping.get(level, level) in possible_levels
            }

            input_data["management_level"]["filter"].update(
                {
                    levels_mapping.get(level, level): {"type": "PAST"}
                    for level in output_data["management_level"].get("past", [])
                    if levels_mapping.get(level, level) in possible_levels
                }
            )
            input_data["management_level"]["filter"].update(
                {
                    levels_mapping.get(level, level): {"type": "BOTH"}
                    for level in output_data["management_level"].get("both", [])
                    if levels_mapping.get(level, level) in possible_levels
                }
            )
        main_output["management_level"] = input_data.get("management_level", {})
    except Exception as e:
        print_logs("level error")
        pass
    # Reverse skills
    try:
        if "skill" in output_data:
            if output_data["skill"] and (
                output_data["skill"]["good_to_have"]
                or output_data["skill"]["must_have"]
            ):
                temp = {}
                for skill in output_data["skill"]["must_have"]:
                    temp[skill] = {
                        "exclusion": False,
                        "type": "CURRENT",
                        "logic": "AND",
                    }

                for skill in output_data["skill"]["good_to_have"]:
                    temp[skill] = {"exclusion": False, "type": "CURRENT", "logic": "OR"}

                input_data["skill"] = {
                    "event": "OR",
                    "relation": "OR",
                    "filter": temp,
                }
            else:
                input_data["skill"] = {}
        main_output["skill"] = input_data.get("skill", {})
    except:
        print_logs("skill error")
        pass

    try:
        if "location" in output_data:
            common_elements = set(output_data["location"].get("current", [])) & set(
                output_data["location"].get("past", [])
            )
            both = output_data["location"].get("both", [])
            both.extend(
                item
                for item in common_elements
                if item not in output_data["location"].get("both", [])
            )
            output_data["location"]["both"] = both

            output_data["location"]["current"] = [
                item
                for item in output_data["location"].get("current", [])
                if item not in common_elements
            ]
            output_data["location"]["past"] = [
                item
                for item in output_data["location"].get("past", [])
                if item not in common_elements
            ]

            for timeline in ["current", "both", "past"]:
                if "South America" in output_data["location"].get(timeline, []):
                    output_data["location"].get(timeline, []).extend(
                        [
                            "Argentina",
                            "Bolivia",
                            "Brazil",
                            "Chile",
                            "Colombia",
                            "Ecuador",
                            "Guyana",
                            "Paraguay",
                            "Peru",
                            "Suriname",
                            "Uruguay",
                            "Venezuela",
                        ]
                    )
                if "North America" in output_data["location"].get(timeline, []):
                    output_data["location"].get(timeline, []).extend(
                        [
                            "Antigua and Barbuda",
                            "Bahamas",
                            "Barbados",
                            "Belize",
                            "Canada",
                            "Costa Rica",
                            "Cuba",
                            "Dominica",
                            "Dominican Republic",
                            "El Salvador",
                            "Grenada",
                            "Guatemala",
                            "Haiti",
                            "Honduras",
                            "Jamaica",
                            "Mexico",
                            "Nicaragua",
                            "Panama",
                            "Saint Kitts and Nevis",
                            "Saint Lucia",
                            "Saint Vincent and the Grenadines",
                            "Trinidad and Tobago",
                            "United States",
                        ]
                    )
                if "Oceania" in output_data["location"].get(timeline, []):
                    output_data["location"].get(timeline, []).extend(
                        [
                            "Australia",
                            "Fiji",
                            "Kiribati",
                            "Marshall Islands",
                            "Micronesia",
                            "Nauru",
                            "New Zealand",
                            "Palau",
                            "Papua New Guinea",
                            "Samoa",
                            "Solomon Islands",
                            "Tonga",
                            "Tuvalu",
                            "Vanuatu",
                        ]
                    )

            input_data["location"] = {"event": None, "filter": {}}
            current = output_data["location"].get("current", [])
            past = output_data["location"].get("past", [])

            if current and not past and not both:
                input_data["location"]["event"] = "CURRENT"
            elif past and not current and not both:
                input_data["location"]["event"] = "PAST"
            else:
                input_data["location"]["event"] = next(
                    (
                        item
                        for item in ["OR", "AND"]
                        if item in output_data["location"].get("event", "").upper()
                    ),
                    "OR",
                )

            input_data["location"]["filter"].update(
                {
                    location: {"type": "CURRENT"}
                    for location in output_data["location"].get("current", [])
                }
            )
            input_data["location"]["filter"].update(
                {
                    location: {"type": "PAST"}
                    for location in output_data["location"].get("past", [])
                }
            )
            input_data["location"]["filter"].update(
                {
                    location: {"type": "BOTH"}
                    for location in output_data["location"].get("both", [])
                }
            )
        main_output["location"] = input_data.get("location", {})
    except:
        print_logs("location error")
        pass
    # Reverse names

    try:
        if "name" in output_data:
            input_data["name"] = output_data.get("name", [])
        main_output["name"] = input_data.get("name", [])
    except:
        print_logs("name error")
        pass

    try:
        if "age" in output_data:
            input_data["age"] = [
                item
                for item in output_data.get("age", [])
                if item in ["Under 25", "Over 50", "Over 65"]
            ]
        main_output["age"] = input_data.get("age", [])
    except:
        print_logs("age error")
        pass

    try:
        if "gender" in output_data:
            if isinstance(output_data["gender"], list):
                if (
                    "female" in output_data.get("gender", [])
                    or "Female" in output_data.get("gender", [])
                ) and len(output_data.get("gender", [])) == 1:
                    input_data["gender"] = "Female"
            elif isinstance(output_data["gender"], str):
                if "female" in output_data["gender"].lower():
                    input_data["gender"] = "Female"
        main_output["gender"] = input_data.get("gender", [])
    except:
        print_logs("gender error")
        pass

    try:
        if "ethnicity" in output_data:
            input_data["ethnicity"] = [
                item
                for item in output_data.get("ethnicity", [])
                if item
                in [
                    "Asian",
                    "African",
                    "Hispanic",
                    "Middle Eastern",
                    "South East Asian",
                    "South Asian",
                ]
            ]
            if "Black" in input_data["ethnicity"]:
                input_data["ethnicity"].remove("Black")
                input_data["ethnicity"].append("African")
            if "South East Asian" in input_data["ethnicity"]:
                input_data["ethnicity"].remove("South East Asian")
                if "South Asian" not in input_data["ethnicity"]:
                    input_data["ethnicity"].append("South Asian")
        main_output["ethnicity"] = input_data.get("ethnicity", [])
    except:
        print_logs("ethnicity error")
        pass

    try:
        input_data["ownership"] = [
            item
            for item in output_data.get("current_ownership", [])
            if item in ["Public", "Private", "VC Funded", "Private Equity Backed"]
        ]
        main_output["ownership"] = input_data.get("ownership", [])
    except:
        print_logs("ownership error")
        pass

    main_output = {key: value for key, value in main_output.items() if value}
    return main_output
