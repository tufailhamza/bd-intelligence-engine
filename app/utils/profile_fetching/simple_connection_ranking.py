from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Handle trailing "Z" as UTC offset for fromisoformat
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Normalize to aware UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _event_start_dt(event: Dict[str, Any]) -> Optional[datetime]:
    start = event.get("start")
    if isinstance(start, dict):
        # Google Calendar style: {"dateTime": "..."} or {"date": "..."}
        dt = _parse_iso_datetime(start.get("dateTime"))
        if dt is not None:
            return dt
        d = start.get("date")
        if isinstance(d, str) and d:
            try:
                # Treat date-only as start of day UTC
                return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
            except Exception:
                return None
    return None


def _event_end_dt(event: Dict[str, Any]) -> Optional[datetime]:
    end = event.get("end")
    if isinstance(end, dict):
        dt = _parse_iso_datetime(end.get("dateTime"))
        if dt is not None:
            return dt
        d = end.get("date")
        if isinstance(d, str) and d:
            try:
                return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
            except Exception:
                return None
    return None


def _event_duration_minutes(event: Dict[str, Any]) -> Optional[float]:
    s = _event_start_dt(event)
    e = _event_end_dt(event)
    if s is None or e is None:
        return None
    try:
        mins = (e - s).total_seconds() / 60.0
        return mins if mins >= 0 else None
    except Exception:
        return None


def _normalize_company(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        c = value.strip()
    elif isinstance(value, dict):
        # Common shapes: {"name": "..."} or {"company": "..."}
        c = (
            value.get("name")
            or value.get("company")
            or value.get("company_name")
            or value.get("title")
        )
        c = c.strip() if isinstance(c, str) else ""
    else:
        c = str(value).strip()

    if not c:
        return None

    # collapse whitespace, lowercase for stable grouping
    c = re.sub(r"\s+", " ", c).strip().lower()
    return c or None


def _extract_current_company(profile: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(profile, dict):
        return None
    exp = profile.get("experience")
    if not isinstance(exp, list) or not exp:
        return None
    first = exp[0]
    if not isinstance(first, dict):
        return None
    # In this dataset, util_functions uses `company`
    return _normalize_company(
        first.get("company")
        or first.get("company_name")
        or first.get("companyTitle")
        or first.get("organization")
    )


_IMPORTANT_KEYWORDS = [
    "demo",
    "product demo",
    "proposal",
    "partnership",
    "pricing",
    "contract",
    "negotiation",
    "kickoff",
    "sales",
    "pipeline",
    "procurement",
]


def _importance_multiplier(events: List[Dict[str, Any]]) -> float:
    """
    Importance heuristic:
    - Keyword boost if summary/description implies high-value conversation
    - Duration boost if long meeting
    - Attendee-count boost if larger meeting

    Returns a multiplier in [1.0, 1.5].
    """
    best = 1.0
    for ev in events:
        if not isinstance(ev, dict):
            continue

        text = " ".join(
            [
                str(ev.get("summary") or ""),
                str(ev.get("description") or ""),
            ]
        ).lower()
        mult = 1.0

        if any(k in text for k in _IMPORTANT_KEYWORDS):
            mult += 0.2

        dur = _event_duration_minutes(ev)
        if isinstance(dur, (int, float)):
            if dur >= 90:
                mult += 0.15
            elif dur >= 60:
                mult += 0.1

        attendees = ev.get("attendees")
        if isinstance(attendees, list):
            # count only dict attendees with email-ish content
            count = sum(1 for a in attendees if isinstance(a, dict) and a.get("email"))
            if count >= 3:
                mult += 0.1

        if mult > best:
            best = mult

    return min(1.5, max(1.0, best))


def _recency_multiplier(events: List[Dict[str, Any]]) -> float:
    """
    Recency heuristic based on the most recent meeting:
    - <= 30 days: 1.00
    - <= 90 days: 0.85
    - <= 180 days: 0.70
    - <= 365 days: 0.50
    - > 365 days: 0.30
    """
    latest: Optional[datetime] = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        dt = _event_start_dt(ev)
        if dt is None:
            continue
        latest = dt if latest is None or dt > latest else latest

    if latest is None:
        return 1.0

    now = datetime.now(timezone.utc)
    days = (now - latest).total_seconds() / (60 * 60 * 24)
    if days <= 30:
        return 1.0
    if days <= 90:
        return 0.85
    if days <= 180:
        return 0.70
    if days <= 365:
        return 0.50
    return 0.30


def _meeting_points(num_meetings: int) -> float:
    """
    Your rule:
    - +30 for the first meeting
    - +15 for each subsequent meeting
    """
    if num_meetings <= 0:
        return 0.0
    return 30.0 + 15.0 * float(max(0, num_meetings - 1))


@dataclass(frozen=True)
class SimpleScoreResult:
    total_score: float
    metric_scores: Dict[str, float]
    reasoning: str


def score_connection_simple(
    *,
    connection_public_identifier: str,
    connection_profile: Optional[Dict[str, Any]],
    connection_events: Optional[List[Dict[str, Any]]],
    company_bonus_points: float,
) -> SimpleScoreResult:
    events: List[Dict[str, Any]] = (
        [e for e in connection_events if isinstance(e, dict)]
        if connection_events
        else []
    )

    num_meetings = len(events)
    base_meeting_points = _meeting_points(num_meetings)
    # Meeting scoring is deliberately simple and deterministic:
    # +30 for first meeting, +15 for each additional meeting
    meeting_points = base_meeting_points

    # Company context: if there was *any* meeting with someone at this organization,
    # then everyone in that organization gets +30 (once).
    company_context_points = float(company_bonus_points or 0.0)

    metric_scores = {
        "meeting_points": round(meeting_points, 4),
        "company_context_points": round(company_context_points, 4),
    }
    total = float(
        metric_scores["meeting_points"] + metric_scores["company_context_points"]
    )

    company = _extract_current_company(connection_profile) or "unknown"
    reasoning = (
        f"simple_v2: meetings={num_meetings} => meeting_points={meeting_points:.1f}; "
        f"company='{company}' => company_context={company_context_points:.1f}; "
        f"total={total:.2f}"
    )

    return SimpleScoreResult(
        total_score=total, metric_scores=metric_scores, reasoning=reasoning
    )


def compute_company_peer_counts(
    connection_profiles: Dict[str, Dict[str, Any]],
) -> Dict[str, int]:
    """
    Returns mapping: public_identifier -> peer_count_at_same_company (within the provided list).
    """
    companies_by_pid: Dict[str, Optional[str]] = {}
    for pid, profile in connection_profiles.items():
        companies_by_pid[pid] = _extract_current_company(profile)

    counts: Dict[str, int] = {}
    for c in companies_by_pid.values():
        if c:
            counts[c] = counts.get(c, 0) + 1

    peer_counts: Dict[str, int] = {}
    for pid, c in companies_by_pid.items():
        peer_counts[pid] = counts.get(c, 0) if c else 0

    return peer_counts
