"""
Daily email summary: one combined email (subject + body) for the reference person, covering all connections.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from qutils.llm.asynchronous import invoke

from app.utils.daily_email.prompts import (
    DAILY_EMAIL_SINGLE_SYSTEM,
    DAILY_EMAIL_SINGLE_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

DAILY_EMAIL_MODEL = "openai/gpt-4.1-mini"
DAILY_EMAIL_FALLBACKS = ["openai/gpt-4.1"]

SUBJECT_RE = re.compile(r"<subject>\s*(.*?)\s*</subject>", re.DOTALL | re.IGNORECASE)


def _parse_calendar_datetime_string(raw: str) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return date_parser.isoparse(raw.strip())
        except (ValueError, TypeError):
            return None


def _format_calendar_boundary(dt_field: Any, label: str, indent: str = "") -> List[str]:
    """
    Format start/end for the LLM: local wall time in calendar timeZone (when provided)
    plus UTC equivalent. Matches Google-style { dateTime, timeZone }.
    """
    lines: List[str] = []
    if isinstance(dt_field, str):
        lines.append(f"{indent}{label}: {dt_field}")
        return lines
    if not isinstance(dt_field, dict):
        return lines
    raw = dt_field.get("dateTime")
    tz_name = dt_field.get("timeZone")
    raw_s = str(raw).strip() if raw is not None else ""
    tz_name = tz_name.strip() if isinstance(tz_name, str) and tz_name.strip() else None
    if not raw_s:
        return lines

    dt = _parse_calendar_datetime_string(raw_s)
    if dt is None:
        suf = f" (timeZone: {tz_name})" if tz_name else ""
        lines.append(f"{indent}{label}: {raw_s}{suf}")
        return lines

    try:
        if dt.tzinfo is not None:
            utc_dt = dt.astimezone(ZoneInfo("UTC"))
            if tz_name:
                try:
                    z = ZoneInfo(tz_name)
                    local_dt = dt.astimezone(z)
                    lines.append(
                        f"{indent}{label} (local {tz_name}): "
                        f"{local_dt.strftime('%Y-%m-%d %H:%M')} ({local_dt.tzname() or tz_name})"
                    )
                except Exception:
                    lines.append(
                        f"{indent}{label} (instant): "
                        f"{dt.strftime('%Y-%m-%d %H:%M %Z')}"
                    )
            else:
                lines.append(
                    f"{indent}{label} (instant): " f"{dt.strftime('%Y-%m-%d %H:%M %Z')}"
                )
            lines.append(
                f"{indent}{label} (UTC): {utc_dt.strftime('%Y-%m-%d %H:%M UTC')}"
            )
        else:
            if tz_name:
                try:
                    z = ZoneInfo(tz_name)
                    local_dt = dt.replace(tzinfo=z)
                    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
                    lines.append(
                        f"{indent}{label} (local {tz_name}): "
                        f"{local_dt.strftime('%Y-%m-%d %H:%M')} ({local_dt.tzname() or tz_name})"
                    )
                    lines.append(
                        f"{indent}{label} (UTC): {utc_dt.strftime('%Y-%m-%d %H:%M UTC')}"
                    )
                except Exception:
                    lines.append(
                        f"{indent}{label}: {raw_s} (timeZone: {tz_name}, could not resolve)"
                    )
            else:
                lines.append(f"{indent}{label}: {raw_s}")
    except Exception as e:
        logger.debug("calendar boundary format failed: %s", e)
        lines.append(f"{indent}{label}: {raw_s}")
    return lines


def _wall_clock_12h(local_dt: datetime) -> str:
    """e.g. 7:30pm, 7pm on the hour."""
    h24 = local_dt.hour
    m = local_dt.minute
    h12 = h24 % 12
    if h12 == 0:
        h12 = 12
    ampm = "am" if h24 < 12 else "pm"
    if m == 0:
        return f"{h12}{ampm}"
    return f"{h12}:{m:02d}{ampm}"


def _event_start_local_datetime(
    ev: Dict[str, Any],
    reference_timezone: Optional[str] = None,
) -> Optional[datetime]:
    """
    Start as a timezone-aware datetime for display.

    If *reference_timezone* is supplied (IANA name, e.g. 'America/New_York') the
    UTC instant is converted to that zone regardless of what the event carries —
    this gives the reference person's local wall-clock time.
    Falls back to the per-event timeZone when reference_timezone is absent or invalid.
    """
    start = ev.get("start")
    if isinstance(start, dict):
        if start.get("date") and not str(start.get("dateTime") or "").strip():
            return None
        raw = start.get("dateTime")
        raw_s = str(raw).strip() if raw is not None else ""
        if not raw_s:
            return None
        event_tz_name = start.get("timeZone")
        event_tz_name = (
            event_tz_name.strip()
            if isinstance(event_tz_name, str) and event_tz_name.strip()
            else None
        )
        dt = _parse_calendar_datetime_string(raw_s)
        if dt is None:
            return None

        # Resolve to an absolute UTC instant first
        try:
            if dt.tzinfo is None and event_tz_name:
                try:
                    dt = dt.replace(tzinfo=ZoneInfo(event_tz_name))
                except Exception:
                    pass
        except Exception:
            pass

        # Convert to the reference person's timezone when provided
        display_tz_name = reference_timezone or event_tz_name
        if display_tz_name:
            try:
                return dt.astimezone(ZoneInfo(display_tz_name))
            except Exception:
                pass
        return dt

    if isinstance(start, str):
        dt = _parse_calendar_datetime_string(start)
        if dt is not None and reference_timezone:
            try:
                return dt.astimezone(ZoneInfo(reference_timezone))
            except Exception:
                pass
        return dt
    return None


def _event_all_day_start_date(ev: Dict[str, Any]) -> Optional[datetime]:
    start = ev.get("start")
    if not isinstance(start, dict):
        return None
    if str(start.get("dateTime") or "").strip():
        return None
    ds = start.get("date")
    if not ds:
        return None
    ds = str(ds).strip()
    if not ds:
        return None
    try:
        return datetime.fromisoformat(ds)
    except ValueError:
        return None


def format_event_start_friendly(
    ev: Dict[str, Any],
    reference_timezone: Optional[str] = None,
) -> Optional[str]:
    """
    Short phrase for event start: '7:30pm' in the reference person's timezone
    (or calendar local time when reference_timezone is not provided),
    or 'March 13, 2026' for all-day events.
    """
    ad = _event_all_day_start_date(ev)
    if ad is not None:
        return f"{ad.strftime('%B')} {ad.day}, {ad.year}"

    local_dt = _event_start_local_datetime(ev, reference_timezone=reference_timezone)
    if local_dt is not None:
        return _wall_clock_12h(local_dt)

    start = ev.get("start")
    if isinstance(start, dict) and start.get("dateTime"):
        raw = str(start["dateTime"]).strip()
        if raw:
            return raw
    if isinstance(start, str) and start.strip():
        return start.strip()

    return None


def _event_identity(ev: Dict[str, Any]) -> str:
    """
    Stable key for deduping the same event across calendar buckets.

    Always includes the start datetime so that two events sharing the same id
    but occurring at different times (e.g. back-to-back recurring occurrences
    with an identical base id) are treated as distinct.
    """
    eid = ev.get("id")
    start = ev.get("start")
    st = ""
    if isinstance(start, dict) and start.get("dateTime"):
        st = str(start["dateTime"])
    elif isinstance(start, str):
        st = start
    if eid:
        return f"{eid}|{st}" if st else str(eid)
    return f"{ev.get('summary')}|{st}"


def _people_tied_to_event(ev: Dict[str, Any]) -> set:
    """public_identifiers appearing on this event (organizer + attendees)."""
    ids: set = set()
    org = ev.get("organizer")
    if isinstance(org, dict):
        pid = org.get("public_identifier")
        if isinstance(pid, str) and pid.strip():
            ids.add(pid.strip())
    ev_pid = ev.get("public_identifier")
    if isinstance(ev_pid, str) and ev_pid.strip():
        ids.add(ev_pid.strip())
    for att in ev.get("attendees") or []:
        if not isinstance(att, dict):
            continue
        pid = att.get("public_identifier")
        if isinstance(pid, str) and pid.strip():
            ids.add(pid.strip())
    return ids


def _events_for_connection(
    conn_id: str,
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Events for this connection: same calendar bucket key, or they are organizer/attendee.
    Fixes payloads where calendar_events keys (e.g. reference user) differ from connection_public_identifiers.
    """
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for bucket_key, events in calendar_events_by_connection.items():
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            key = _event_identity(ev)
            if bucket_key == conn_id or conn_id in _people_tied_to_event(ev):
                if key not in seen:
                    seen.add(key)
                    out.append(ev)
    return out


def _format_event_full(ev: Dict[str, Any], indent: str = "") -> str:
    """One calendar event with meeting details for the prompt."""
    lines: List[str] = []
    summary = ev.get("summary") or "(No title)"
    eid = ev.get("id") or ""
    status = ev.get("status") or ""
    lines.append(f"{indent}Title: {summary}")
    if eid:
        lines.append(f"{indent}Event id: {eid}")
    if status:
        lines.append(f"{indent}Status: {status}")

    start = ev.get("start")
    end = ev.get("end")
    for line in _format_calendar_boundary(start, "Start", indent):
        lines.append(line)
    for line in _format_calendar_boundary(end, "End", indent):
        lines.append(line)

    loc = ev.get("location")
    if loc:
        lines.append(f"{indent}Location: {loc}")

    desc = (ev.get("description") or "").strip()
    if desc:
        lines.append(
            f"{indent}Description: {desc[:600]}{'...' if len(desc) > 600 else ''}"
        )

    org = ev.get("organizer")
    if isinstance(org, dict) and (org.get("email") or org.get("public_identifier")):
        lines.append(
            f"{indent}Organizer: email={org.get('email', '')} public_identifier={org.get('public_identifier', '')}"
        )

    atts = ev.get("attendees") or []
    if atts:
        lines.append(f"{indent}Participants (attendees):")
        for j, att in enumerate(atts, 1):
            if not isinstance(att, dict):
                continue
            lines.append(
                f"{indent}  {j}. email={att.get('email', '')} "
                f"public_identifier={att.get('public_identifier', '')} "
                f"response={att.get('responseStatus', '')}"
            )
    return "\n".join(lines)


def build_meeting_details_block(
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
) -> str:
    """
    Full meeting/calendar section: every event in the payload, grouped by calendar_events map key.
    """
    if not calendar_events_by_connection:
        return "(No calendar / meeting events in payload.)"
    parts: List[str] = []
    for bucket_key in sorted(calendar_events_by_connection.keys()):
        events = calendar_events_by_connection.get(bucket_key) or []
        parts.append(f"=== Calendar bucket: {bucket_key} ({len(events)} event(s)) ===")
        for i, ev in enumerate(events, 1):
            if not isinstance(ev, dict):
                continue
            parts.append(f"--- Meeting {i} ---")
            parts.append(_format_event_full(ev, indent=""))
        parts.append("")
    return "\n".join(parts).strip()


def _format_events_for_prompt(events: List[Dict[str, Any]]) -> str:
    """Turn a list of calendar event dicts into a readable string for the prompt."""
    if not events:
        return "No events linked to this connection (see full Meeting details section for all meetings)."
    lines = []
    for i, ev in enumerate(events, 1):
        lines.append(f"--- Linked event {i} ---")
        lines.append(_format_event_full(ev, indent=""))
    return "\n".join(lines)


def _build_connections_block(
    connection_public_identifiers: List[str],
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
    profiles_by_connection: Dict[str, Optional[Dict[str, Any]]],
) -> str:
    """Build one text block listing each connection with their profile and events."""
    sections = []
    for conn_id in connection_public_identifiers:
        profile = profiles_by_connection.get(conn_id)
        events = _events_for_connection(conn_id, calendar_events_by_connection)
        profile_json = (
            json.dumps(profile, default=str, indent=2)
            if profile
            else "(Profile not found)"
        )
        events_text = _format_events_for_prompt(events)
        sections.append(
            f"[Connection: {conn_id}]\n"
            f"Profile:\n{profile_json}\n\n"
            f"Calendar events tied to this connection (same bucket key or organizer/attendee):\n{events_text}\n"
        )
    return "\n---\n".join(sections)


def _parse_subject_and_body(response: str) -> Tuple[str, str]:
    """Extract <subject>...</subject> and the rest as body. Returns (subject, body)."""
    if not response or not isinstance(response, str):
        return "Your daily digest", ""
    text = response.strip()
    subject = "Your daily digest"
    body = text
    match = SUBJECT_RE.search(text)
    if match:
        subject = (match.group(1) or "").strip() or subject
        body = (text[: match.start()] + text[match.end() :]).strip()
    return subject, body


async def generate_daily_email_single_summary(
    reference_public_identifier: str,
    connection_public_identifiers: List[str],
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
    profiles_by_connection: Dict[str, Optional[Dict[str, Any]]],
    news_insights: Optional[str] = None,
    per_meeting_digest_block: Optional[str] = None,
    reference_full_name: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Generate one combined daily email (subject + body) for the reference person.
    Optionally includes extra news_insights and per_meeting_digest_block. Returns (subject, body).
    """
    if not connection_public_identifiers:
        return "Your daily digest", "No connections provided."

    meeting_details_block = build_meeting_details_block(calendar_events_by_connection)

    connections_block = _build_connections_block(
        connection_public_identifiers=connection_public_identifiers,
        calendar_events_by_connection=calendar_events_by_connection,
        profiles_by_connection=profiles_by_connection,
    )
    news_block = ""
    if news_insights and news_insights.strip():
        news_block = "\n\n---\n" + news_insights.strip() + "\n\n---\n"

    digest = (
        per_meeting_digest_block.strip()
        if per_meeting_digest_block and per_meeting_digest_block.strip()
        else "(No per-meeting company/news digest available.)"
    )

    greeting_name = (reference_full_name or "").strip() or reference_public_identifier

    user_content = DAILY_EMAIL_SINGLE_USER_TEMPLATE.format(
        reference_public_identifier=reference_public_identifier,
        reference_full_name=greeting_name,
        meeting_details_block=meeting_details_block,
        per_meeting_digest_block=digest,
        connections_block=connections_block,
        news_block=news_block,
    )

    try:
        response = await invoke(
            messages=[
                {"role": "system", "content": DAILY_EMAIL_SINGLE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            model=DAILY_EMAIL_MODEL,
            temperature=0.2,
            fallbacks=DAILY_EMAIL_FALLBACKS,
        )
        if response and isinstance(response, str):
            return _parse_subject_and_body(response)
        return "Your daily digest", ""
    except Exception as e:
        logger.warning("Daily email LLM call failed: %s", str(e))
        return "Your daily digest", f"(Daily email summary unavailable: {str(e)})"
