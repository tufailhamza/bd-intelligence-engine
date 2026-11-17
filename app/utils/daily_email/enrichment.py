"""
Format meeting payloads, participant profiles, company docs, and BD news for daily-email prompts.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.utils.profile_fetching.public_identifier_fetch import _select_top_experience

logger = logging.getLogger(__name__)


def format_meetings_for_prompt(meetings: Optional[List[Any]]) -> str:
    """Serialize meeting list (Pydantic models or dicts) for the LLM."""
    if not meetings:
        return "(No meeting details provided in payload.)"
    out = []
    for i, m in enumerate(meetings, 1):
        if hasattr(m, "model_dump"):
            d = m.model_dump(mode="json", exclude_none=True)
        elif isinstance(m, dict):
            d = dict(m)
        else:
            d = {"raw": str(m)}
        out.append(f"Meeting {i}:\n{json.dumps(d, default=str, indent=2)}")
    return "\n\n".join(out)


def _profile_compact(profile: Optional[Dict[str, Any]]) -> str:
    if not isinstance(profile, dict):
        return "(not found)"
    lines = []
    name = profile.get("full_name") or profile.get("name") or ""
    if name:
        lines.append(f"name: {name}")
    for k in ("headline", "title", "industry", "location"):
        v = profile.get(k)
        if isinstance(v, str) and v.strip():
            lines.append(f"{k}: {v.strip()}")
    exp = profile.get("experience")
    top = _select_top_experience(exp) if isinstance(exp, list) else None
    if isinstance(top, dict):
        e0 = top
        t = e0.get("title") or ""
        c = e0.get("company") or e0.get("company_name") or ""
        u = e0.get("company_universal_name") or e0.get("company_universalname") or ""
        if t or c:
            lines.append(f"current_role: {t} @ {c}")
        if u:
            lines.append(f"company_universal_name: {u}")
    return "\n".join(lines) if lines else json.dumps(profile, default=str)[:2000]


def format_participants_block(
    participant_ids: List[str],
    profiles_map: Dict[str, Tuple[Optional[str], Optional[Dict[str, Any]]]],
) -> str:
    """
    profiles_map: public_identifier -> (es_id, profile) as from fetch_profiles_by_public_identifiers.
    """
    if not participant_ids:
        return "(No meeting participants to enrich from ES.)"
    sections = []
    for pid in participant_ids:
        pair = profiles_map.get(pid, (None, None))
        prof = pair[1] if isinstance(pair, (list, tuple)) and len(pair) > 1 else None
        sections.append(f"[Participant: {pid}]\n{_profile_compact(prof)}")
    return "\n---\n".join(sections)


def format_companies_block(company_map: Dict[str, Dict[str, Any]]) -> str:
    if not company_map:
        return "(No company details from ES.)"
    parts = []
    for universal, src in company_map.items():
        parts.append(
            f"[Company universal: {universal}]\n"
            f"{json.dumps(src, default=str, indent=2)}"
        )
    return "\n---\n".join(parts)


def format_company_news_lines(items: List[Dict[str, Any]], max_lines: int = 60) -> str:
    if not items:
        return ""
    lines = []
    seen = set()
    for doc in items[:max_lines]:
        lid = str(doc.get("linkedin_identifier") or "").strip() or "unknown"
        nid = str(doc.get("news_id") or "").strip()
        title = (doc.get("news_title") or "").strip()
        content = (doc.get("content") or "").strip()[:280]
        key = (lid, nid, title[:80])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- [{lid}] {title}. {content}")
    return "Recent company-related news from BD index (bd_news):\n" + "\n".join(lines)
