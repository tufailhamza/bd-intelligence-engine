"""
Lightweight profile and email helpers shared across the daily email pipeline.
"""

from typing import Any, Dict, List, Optional, Set


def normalize_company(value: Any) -> Optional[str]:
    """Return a lowercase stripped company name, or None if blank/non-string."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v or None


def experience_row_is_current(row: Any) -> bool:
    """True when an experience entry looks like a current role (no end date)."""
    if not isinstance(row, dict):
        return False
    end = row.get("end")
    if end in (None, "", "null"):
        return True
    if isinstance(end, str) and end.strip().lower() in ("present", "now", "current"):
        return True
    return False


def current_experience_rows(profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    All experience dicts marked as current (no end date).

    If none are marked current (odd data), fall back to the first row only—same
    spirit as the legacy single ``experience[0]`` behaviour.
    """
    if not isinstance(profile, dict):
        return []
    exp = profile.get("experience")
    if not isinstance(exp, list) or not exp:
        return []
    rows = [e for e in exp if isinstance(e, dict) and experience_row_is_current(e)]
    if rows:
        return rows
    if isinstance(exp[0], dict):
        return [exp[0]]
    return []


def all_current_normalized_company_names(profile: Optional[Dict[str, Any]]) -> Set[str]:
    """Normalized display company names from every current experience row."""
    names: Set[str] = set()
    for row in current_experience_rows(profile):
        co = row.get("company") or row.get("company_name")
        n = normalize_company(co)
        if n:
            names.add(n)
    return names


def get_profile_full_name(profile: Any) -> str:
    """
    Display name from a people-index style profile dict.
    Uses only canonical ``full_name``.
    """
    if hasattr(profile, "model_dump"):
        try:
            profile = profile.model_dump()
        except Exception:
            pass
    elif hasattr(profile, "dict"):
        try:
            profile = profile.dict()
        except Exception:
            pass
    if not isinstance(profile, dict):
        return ""
    v = profile.get("full_name")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return ""


def build_daily_email_greeting(recipient_full_name: str) -> str:
    """Standard morning greeting for the daily digest email."""
    return (
        f"Good morning {recipient_full_name},\n\n"
        f"While you were away, our AI completed the daily calendar refresh. "
        f"Here's a quick overview of your upcoming meetings."
    )
