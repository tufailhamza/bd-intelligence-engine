import json
import logging
import re
from typing import Any, Dict, List, Optional

from qutils.llm.asynchronous import invoke

logger = logging.getLogger(__name__)


def _clean_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact_profile(profile: Optional[Dict[str, Any]]) -> str:
    if not isinstance(profile, dict):
        return "N/A"

    lines: List[str] = []
    name = _clean_string(profile.get("full_name") or profile.get("name"))
    headline = _clean_string(profile.get("headline"))
    summary = _clean_string(profile.get("summary") or profile.get("about"))
    if name:
        lines.append(f"Name: {name}")
    if headline:
        lines.append(f"Headline: {headline}")
    if summary:
        lines.append(f"Summary: {summary}")

    experience = profile.get("experience")
    if isinstance(experience, list) and experience:
        lines.append("Recent Experience:")
        for item in experience[:2]:
            if not isinstance(item, dict):
                continue
            title = _clean_string(item.get("title"))
            company = _clean_string(item.get("company") or item.get("company_name"))
            if title or company:
                lines.append(f"- {title or ''} @ {company or ''}".strip())

    return "\n".join(lines)[:2500] if lines else "N/A"


def _first_name(profile: Optional[Dict[str, Any]], fallback_identifier: str) -> str:
    if isinstance(profile, dict):
        name = _clean_string(profile.get("full_name") or profile.get("name"))
        if name:
            return name.split()[0]
    return fallback_identifier


def _normalize_tones(tones: Optional[List[str]]) -> List[str]:
    raw = tones or ["catch_up", "support", "congratulate", "suggested"]
    normalized: List[str] = []
    seen = set()
    for tone in raw:
        text = _clean_string(tone)
        if not text:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized or ["catch_up", "support", "congratulate", "suggested"]


def _tone_instruction(tone: str) -> str:
    mapping = {
        "suggested": "Natural, concise, and directly aligned to the opportunity.",
        "formal": "More polished and formal while still sounding human.",
        "professional": "Professional, clear, and business-appropriate.",
        "casual": "Warm, conversational, and less formal.",
        "friendly": "Friendly, approachable, and relationship-led.",
        "short": "Very concise and direct.",
    }
    return mapping.get(tone, f"Use a {tone.replace('_', ' ')} tone.")


def _safe_json_from_text(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    raw = str(text).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", raw).replace("```", "").strip()
    match = re.search(r"\{[\s\S]*\}", raw)
    try:
        parsed = json.loads(match.group(0) if match else raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        logger.exception("Failed to parse opportunity message LLM JSON")
        return {}


async def generate_opportunity_messages(
    *,
    reference_public_identifier: str,
    reference_es_id: Optional[str],
    reference_profile: Optional[Dict[str, Any]],
    related_person_public_identifier: str,
    related_person_profile: Optional[Dict[str, Any]],
    opportunity: Dict[str, Any],
    channel: str = "linkedin_message",
    tones: Optional[List[str]] = None,
    max_length: Optional[int] = None,
) -> Dict[str, Any]:
    tone_keys = _normalize_tones(tones)
    recipient_first_name = _first_name(
        related_person_profile, related_person_public_identifier
    )
    is_email = channel == "email"

    max_length_rule = (
        f"Each message must be at most {int(max_length)} characters (including spaces and punctuation)."
        if isinstance(max_length, int) and max_length > 0
        else "Keep each message concise, ideally 300-600 characters."
    )
    tone_lines = "\n".join(f"- {tone}: {_tone_instruction(tone)}" for tone in tone_keys)

    channel_note = (
        "Channel: Email — write a proper email body. Also generate a short subject line for each tone."
        if is_email
        else "Channel: LinkedIn message — keep it conversational and concise, no formal subject line needed."
    )

    output_schema = (
        '{{"messages": {{"tone_key": "message_body"}}, "subject_lines": {{"tone_key": "subject"}}}}'
        if is_email
        else '{{"messages": {{"tone_key": "message"}}}}'
    )

    system_prompt = f"""
You are an expert BD outreach assistant.

Task:
- Generate personalized outreach messages for a relationship opportunity.
- Ground the message in the provided opportunity payload and profile context.
- Write to the related person, from the reference person.

Channel:
{channel_note}

Rules:
- Return valid JSON only: {output_schema}
- Generate exactly one message (and subject line if email) for each requested tone key.
- Start each message with "Hi {recipient_first_name},".
- Mention the opportunity naturally; do not copy the payload mechanically.
- Keep the ask specific, low-pressure, and relationship-oriented.
- Do not invent facts, relationships, employers, outcomes, or names not present in the input.
- Do not include placeholders like [Your Name].
- No markdown.
- Do not use horizontal rules, separators, or decorative lines such as "---", "***", or "___".
- Write plain, natural prose only — no AI-style formatting artifacts.
 - For email channel, end each message with two newline characters followed by "Best regards" on its own line, then a newline and the reference person's full name when available (otherwise "Best regards" alone).
""".strip()

    user_prompt = f"""
REFERENCE / SENDER:
public_identifier: {reference_public_identifier}
es_id: {reference_es_id or "N/A"}
profile:
{_compact_profile(reference_profile)}

RELATED PERSON / RECIPIENT:
public_identifier: {related_person_public_identifier}
recipient_first_name: {recipient_first_name}
profile:
{_compact_profile(related_person_profile)}

OPPORTUNITY PAYLOAD:
{json.dumps(opportunity or {}, ensure_ascii=False, indent=2, default=str)}

REQUESTED TONES:
{tone_lines}

LENGTH:
{max_length_rule}

Return JSON only.
""".strip()

    response = await invoke(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model="openai/gpt-4.1",
        temperature=0.2,
        fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4o"],
    )
    parsed = _safe_json_from_text(str(response))
    raw_messages = parsed.get("messages") if isinstance(parsed, dict) else None
    if not isinstance(raw_messages, dict):
        raw_messages = {}

    messages: Dict[str, str] = {}
    for tone in tone_keys:
        messages[tone] = _clean_string(raw_messages.get(tone)) or ""

    subject_line: Optional[Dict[str, str]] = None
    if is_email:
        raw_subjects = parsed.get("subject_lines") if isinstance(parsed, dict) else None
        if isinstance(raw_subjects, dict):
            subject_line = {
                tone: _clean_string(raw_subjects.get(tone)) or "" for tone in tone_keys
            }

    return {"messages": messages, "subject_line": subject_line}
