"""
Per-meeting digest for daily email: company from connection profile, times,
and news (bd_news by company universal, else bd_news by connection public_identifier).
"""

from collections import OrderedDict
from typing import AbstractSet, Any, Dict, List, Optional, Set, Tuple

from app.utils.daily_email.company_fetch import pick_company_website
from app.utils.daily_email.domain_mapping import universal_name_from_linkedin_url
from app.utils.daily_email.helpers import current_experience_rows, get_profile_full_name
from app.utils.daily_email.participant_news import make_meeting_event_key
from app.utils.profile_fetching.public_identifier_fetch import _select_top_experience
from app.utils.daily_email.summary import (
    _event_identity,
    _events_for_connection,
    format_event_start_friendly,
)

_NEWS_CONTENT_MAX = 500


def _global_unique_event_key_for_count(
    connection_public_identifier: str, ev: Dict[str, Any]
) -> str:
    """
    Distinct-meeting key for subject-line counts across the whole digest.

    - If the calendar payload has a provider event ``id``, dedupe globally (same id
      across connections = one meeting).
    - If ``id`` is missing, ``_event_identity`` falls back to ``summary|start`` which
      can collide across unrelated meetings; scope by ``connection_public_identifier``
      so we do not under-count globally.
    """
    if not isinstance(ev, dict):
        return f"{connection_public_identifier}|invalid"
    eid = ev.get("id")
    if eid is not None and str(eid).strip():
        return str(eid).strip()
    return f"{connection_public_identifier}|{_event_identity(ev)}"


def _truncate_text(val: Any, max_len: int = _NEWS_CONTENT_MAX) -> Optional[str]:
    if not isinstance(val, str) or not val.strip():
        return None
    s = val.strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3].rstrip() + "..."


def _safe_int_count(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _published_date_epoch(value: Any) -> Optional[int]:
    """Normalize bd_news ``published_date`` to Unix seconds (int), or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def profile_company_display_and_universal(
    profile: Optional[Dict[str, Any]],
) -> Tuple[str, Optional[str]]:
    """Company display name and universal from the profile's primary experience row."""
    if not isinstance(profile, dict):
        return "Unknown", None
    exp = profile.get("experience")
    if not isinstance(exp, list) or not exp:
        return "Unknown", None
    top = _select_top_experience(exp)
    if not top or not isinstance(top, dict):
        return "Unknown", None
    e0 = top
    display = e0.get("company") or e0.get("company_name") or e0.get("name") or "Unknown"
    if not isinstance(display, str):
        display = str(display)
    universal = e0.get("company_universal_name") or e0.get("company_universalname")
    if isinstance(universal, str) and universal.strip():
        return display.strip(), universal.strip()
    return display.strip() if display else "Unknown", None


def profile_top_experience_title(profile: Optional[Dict[str, Any]]) -> Optional[str]:
    """Title from the same experience row used for company display/universal, if present."""
    if not isinstance(profile, dict):
        return None
    exp = profile.get("experience")
    if not isinstance(exp, list) or not exp:
        return None
    top = _select_top_experience(exp)
    if not top or not isinstance(top, dict):
        return None
    t = top.get("title") or top.get("positionTitle") or top.get("jobTitle")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return None


def resolve_experience_title_for_connection(
    connection_public_identifier: str,
    profile: Optional[Dict[str, Any]],
    domain_mapped_connection_ids: Optional[AbstractSet[str]],
) -> str:
    """
    Experience job title for API output: real title from profile when employer is
    profile-derived; empty string when company came from domain mapping or title
    is missing.
    """
    if domain_mapped_connection_ids and connection_public_identifier in (
        domain_mapped_connection_ids
    ):
        return ""
    title = profile_top_experience_title(profile)
    return title if title else ""


def connection_full_name_from_profile(profile: Optional[Dict[str, Any]]) -> str:
    """Resolved display name for API, or N/A when the profile has no usable name fields."""
    fn = (get_profile_full_name(profile) or "").strip()
    return fn if fn else "N/A"


def _universals_from_experience_row(row: Dict[str, Any]) -> Set[str]:
    """Lowercased company universal + /company/{slug} from one experience dict."""
    out: Set[str] = set()
    uni = row.get("company_universal_name") or row.get("company_universalname")
    if isinstance(uni, str) and uni.strip():
        out.add(uni.strip().lower())
    for key in (
        "linkedin_url",
        "company_linkedin_url",
        "companyLinkedinUrl",
        "company_linkedin",
        "url",
    ):
        raw = row.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        parsed = universal_name_from_linkedin_url(raw)
        if parsed:
            out.add(parsed.lower())
    return out


def employer_company_universals_from_profile(
    profile: Optional[Dict[str, Any]],
) -> Set[str]:
    """
    Union of LinkedIn company universal names for **every current** experience row.

    Same slug rules as legacy single-row logic, applied per current role. Used to
    exclude same-organization connections for reference and (after domain mapping)
    for each connection when they hold multiple concurrent jobs.
    """
    out: Set[str] = set()
    if not isinstance(profile, dict):
        return out
    for row in current_experience_rows(profile):
        out |= _universals_from_experience_row(row)
    return out


def reference_employer_company_universals(
    profile: Optional[Dict[str, Any]],
) -> Set[str]:
    """
    Normalized LinkedIn company universal names for the reference's employer(s).

    Includes **all** concurrent current roles (see ``current_experience_rows``).
    """
    return employer_company_universals_from_profile(profile)


def format_meeting_time_sentence(
    company_name: str,
    ev: Dict[str, Any],
    reference_timezone: Optional[str] = None,
) -> str:
    """
    One line for API / digest, e.g. 'You are meeting Acme at 7:30pm'.
    Time is expressed in *reference_timezone* when provided, else the event's own timezone.
    All-day: 'You are meeting Acme on March 13, 2026'.
    """
    name = (company_name or "").strip() or "Unknown"
    phrase = format_event_start_friendly(ev, reference_timezone=reference_timezone)
    if not phrase:
        return f"You are meeting {name} (time not available)"
    pl = phrase.lower()
    # Month-day-year all-day phrases have no am/pm
    if "am" not in pl and "pm" not in pl and phrase[0].isalpha() and "," in phrase:
        return f"You are meeting {name} on {phrase}"
    return f"You are meeting {name} at {phrase}"


def _extract_time_part_from_sentence(sentence: Any) -> Optional[str]:
    if not isinstance(sentence, str):
        return None
    s = sentence.strip()
    if not s:
        return None
    if " at " in s:
        return s.split(" at ", 1)[1].strip() or None
    if " on " in s:
        return s.split(" on ", 1)[1].strip() or None
    return s


def _join_phrases(parts: List[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _build_combined_meeting_sentence(company_name: str, times: List[str]) -> str:
    name = (company_name or "").strip() or "Unknown"
    clean_times: List[str] = []
    seen: set = set()
    for t in times:
        ts = str(t).strip()
        if ts and ts not in seen:
            seen.add(ts)
            clean_times.append(ts)
    if not clean_times:
        return f"You are meeting {name} (time not available)"
    all_day = all(
        ("am" not in t.lower() and "pm" not in t.lower()) for t in clean_times
    )
    joined = _join_phrases(clean_times)
    if all_day:
        return f"You are meeting {name} on {joined}"
    return f"You are meeting {name} at {joined}"


def _snippet_from_bd_doc(
    doc: Optional[Dict[str, Any]], source_label: str
) -> Optional[Dict[str, Any]]:
    """Build a news dict from a bd_news _source doc, or None if missing news_id."""
    if not isinstance(doc, dict):
        return None
    nid = doc.get("news_id")
    if nid is None or not str(nid).strip():
        return None
    nid_s = str(nid).strip()
    return {
        "source": source_label,
        "news_id": nid_s,
        "news_title": (doc.get("news_title") or "").strip() or None,
        "content": _truncate_text(doc.get("content")),
        "link": (doc.get("link") or "").strip() or None,
        "imagecount": _safe_int_count(doc.get("imagecount")),
        "videocount": _safe_int_count(doc.get("videocount")),
        "published_date": _published_date_epoch(doc.get("published_date")),
    }


def resolve_news_for_meeting(
    company_universal: Optional[str],
    latest_bd_by_company: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Returns keys: source (bd_news_company | none), news_id,
    news_title, content, link. Text fields suitable for email; content truncated for prompt size.

    Order: company bd_news (by company universal) only.
    """
    empty: Dict[str, Any] = {
        "source": "none",
        "news_id": None,
        "news_title": None,
        "content": None,
        "link": None,
        "imagecount": None,
        "videocount": None,
        "published_date": None,
    }
    if company_universal:
        cu = str(company_universal).strip()
        sn = _snippet_from_bd_doc(latest_bd_by_company.get(cu), "bd_news_company")
        if sn:
            return sn
    return dict(empty)


def _group_has_news_payload(news: Optional[Dict[str, Any]]) -> bool:
    """True when the group has company bd_news or a participant summary (not source=none)."""
    if not isinstance(news, dict):
        return False
    return (news.get("source") or "none").strip() != "none"


def _news_priority(source: Optional[str]) -> int:
    """Higher = preferred for a single company-level snippet."""
    s = (source or "none").strip()
    if s == "bd_news_company":
        return 3
    if s == "bd_news_participants_summary":
        return 2
    return 0


def pick_news_for_company_group(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    One news payload per company: prefer company bd_news, then participant summary;
    tie-breaker first meeting in group order.
    """
    empty: Dict[str, Any] = {
        "source": "none",
        "news_id": None,
        "news_title": None,
        "content": None,
        "link": None,
        "imagecount": None,
        "videocount": None,
        "published_date": None,
    }
    if not items:
        return dict(empty)
    best = empty
    best_p = -1
    for row in items:
        n = row.get("news")
        if not isinstance(n, dict):
            continue
        p = _news_priority(n.get("source"))
        if p > best_p:
            best_p = p
            best = {
                "source": n.get("source") or "none",
                "news_id": n.get("news_id"),
                "news_title": n.get("news_title"),
                "content": n.get("content"),
                "link": n.get("link"),
                "imagecount": n.get("imagecount"),
                "videocount": n.get("videocount"),
                "published_date": n.get("published_date"),
            }
        if best_p >= 3:
            break
    return best


def build_structured_daily_meetings(
    *,
    connection_public_identifiers: List[str],
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
    profiles_by_connection: Dict[str, Optional[Dict[str, Any]]],
    latest_bd_by_company: Dict[str, Dict[str, Any]],
    participant_news_by_event: Optional[Dict[str, Dict[str, Any]]] = None,
    company_es_by_universal: Optional[Dict[str, Dict[str, Any]]] = None,
    reference_timezone: Optional[str] = None,
    domain_mapped_connection_ids: Optional[AbstractSet[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Frontend-friendly list of meetings (same logic as text digest, no HTML).
    Meeting times are expressed in *reference_timezone* when provided.
    """
    out: List[Dict[str, Any]] = []
    for conn_id in connection_public_identifiers:
        events = _events_for_connection(conn_id, calendar_events_by_connection)
        profile = profiles_by_connection.get(conn_id)
        company_display, company_universal = profile_company_display_and_universal(
            profile
        )
        for ev in events:
            if not isinstance(ev, dict):
                continue
            news = resolve_news_for_meeting(
                company_universal,
                latest_bd_by_company,
            )
            if (news.get("source") or "none") == "none":
                ev_key = make_meeting_event_key(conn_id, ev)
                participant_news = (participant_news_by_event or {}).get(ev_key)
                if isinstance(participant_news, dict):
                    news = participant_news
            web: Optional[str] = None
            if company_universal:
                co_src = (company_es_by_universal or {}).get(
                    str(company_universal).strip()
                )
                web = pick_company_website(co_src)
            ev_title = ev.get("summary") or ev.get("title")
            et = (
                ev_title.strip()
                if isinstance(ev_title, str) and ev_title.strip()
                else None
            )
            experience_title = resolve_experience_title_for_connection(
                conn_id,
                profile,
                domain_mapped_connection_ids,
            )
            full_name = connection_full_name_from_profile(profile)
            out.append(
                {
                    "connection_public_identifier": conn_id,
                    "_calendar_event_key": _event_identity(ev),
                    "_global_unique_event_key": _global_unique_event_key_for_count(
                        conn_id, ev
                    ),
                    "event_title": et,
                    "company_name": company_display,
                    "company_universal_name": company_universal,
                    "company_website_url": web,
                    "experience_title": experience_title,
                    "full_name": full_name,
                    "meeting_time": format_meeting_time_sentence(
                        company_display, ev, reference_timezone=reference_timezone
                    ),
                    "news": {
                        "source": news.get("source") or "none",
                        "news_id": news.get("news_id"),
                        "news_title": news.get("news_title"),
                        "content": news.get("content"),
                        "link": news.get("link"),
                        "imagecount": news.get("imagecount"),
                        "videocount": news.get("videocount"),
                        "published_date": news.get("published_date"),
                    },
                }
            )
    return out


def build_grouped_daily_meetings(
    *,
    connection_public_identifiers: List[str],
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
    profiles_by_connection: Dict[str, Optional[Dict[str, Any]]],
    latest_bd_by_company: Dict[str, Dict[str, Any]],
    participant_news_by_event: Optional[Dict[str, Dict[str, Any]]] = None,
    company_es_by_universal: Optional[Dict[str, Dict[str, Any]]] = None,
    company_participant_news_by_universal: Optional[Dict[str, Dict[str, Any]]] = None,
    reference_timezone: Optional[str] = None,
    domain_mapped_connection_ids: Optional[AbstractSet[str]] = None,
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """
    Like build_structured_daily_meetings, but:
    - Drops meetings whose company has no document in the ES companies index
      (no li_universalname hit in company_es_by_universal).
    - Drops company groups with no news (no bd_news for the company and no participant
      summary fallback).
    - Groups remaining rows by company_universal_name (stable order: first seen).
    - Within each company, dedupes rows that are the same calendar event (multiple
      connections can each surface the same event from their calendar buckets).

    Returns:
        (grouped_payload, flat_meeting_count, unique_calendar_event_count, es_matched_row_count)
        - flat_meeting_count: per-connection calendar rows before ES filtering
        - unique_calendar_event_count: distinct calendar events across **included** groups only
        - es_matched_row_count: rows after ES company filter (before news filter)
    """
    flat = build_structured_daily_meetings(
        connection_public_identifiers=connection_public_identifiers,
        calendar_events_by_connection=calendar_events_by_connection,
        profiles_by_connection=profiles_by_connection,
        latest_bd_by_company=latest_bd_by_company,
        participant_news_by_event=participant_news_by_event,
        company_es_by_universal=company_es_by_universal,
        reference_timezone=reference_timezone,
        domain_mapped_connection_ids=domain_mapped_connection_ids,
    )
    flat_n = len(flat)
    es_map = company_es_by_universal or {}
    filtered: List[Dict[str, Any]] = []
    for m in flat:
        u = m.get("company_universal_name")
        if not isinstance(u, str) or not u.strip():
            continue
        u_key = u.strip()
        co = es_map.get(u_key)
        if not isinstance(co, dict) or not co:
            continue
        filtered.append(m)

    es_matched_row_count = len(filtered)

    groups: OrderedDict[str, List[Dict[str, Any]]] = OrderedDict()
    for m in filtered:
        u_key = str(m["company_universal_name"]).strip()
        groups.setdefault(u_key, []).append(m)

    out_groups: List[Dict[str, Any]] = []
    unique_calendar_keys: set = set()
    for u_key, items in groups.items():
        first = items[0]
        # Prefer a pre-built combined company-level summary (covers all participants)
        # over picking one individual summary from the group rows.
        co_level_news = (company_participant_news_by_universal or {}).get(u_key)
        group_news = (
            co_level_news if co_level_news else pick_news_for_company_group(items)
        )
        if not _group_has_news_payload(group_news):
            continue
        # Collapse all participants across the company group into ONE entry.
        # connection_public_identifier becomes a list of per-connection summaries;
        # event_title becomes a list; meeting_time is the single combined sentence
        # ("You are meeting X at 9am and 10am").
        seen_conn_event_keys: set = set()
        all_pids: List[str] = []
        seen_pids: set = set()
        all_titles: List[str] = []
        seen_title_strs: set = set()
        all_times: List[str] = []

        for row in items:
            gk = row.get("_global_unique_event_key")
            if isinstance(gk, str) and gk.strip():
                unique_calendar_keys.add(gk.strip())
            else:
                unique_calendar_keys.add(
                    "row:"
                    f"{row.get('connection_public_identifier')}|"
                    f"{row.get('meeting_time')}|"
                    f"{row.get('event_title')}"
                )

            conn_id = row["connection_public_identifier"]
            cal_key = row.get("_calendar_event_key")
            if isinstance(cal_key, str) and cal_key.strip():
                conn_cal_key = f"{conn_id}::{cal_key}"
                if conn_cal_key in seen_conn_event_keys:
                    continue
                seen_conn_event_keys.add(conn_cal_key)

            if conn_id not in seen_pids:
                seen_pids.add(conn_id)
                all_pids.append(conn_id)

            t = row.get("event_title")
            if isinstance(t, str) and t.strip() and t.strip() not in seen_title_strs:
                seen_title_strs.add(t.strip())
                all_titles.append(t.strip())

            time_part = _extract_time_part_from_sentence(row.get("meeting_time"))
            if isinstance(time_part, str) and time_part.strip():
                all_times.append(time_part)

        connection_rows: List[Dict[str, Any]] = []
        for cid in all_pids:
            row_match = next(
                (
                    r
                    for r in items
                    if isinstance(r, dict)
                    and r.get("connection_public_identifier") == cid
                ),
                None,
            )
            if isinstance(row_match, dict):
                cn = row_match.get("company_name")
                company_name = (
                    cn.strip() if isinstance(cn, str) and cn.strip() else "Unknown"
                )
                u = row_match.get("company_universal_name")
                company_universal = (
                    u.strip() if isinstance(u, str) and u.strip() else None
                )
                web = row_match.get("company_website_url")
                company_web = (
                    web.strip() if isinstance(web, str) and web.strip() else None
                )
                etitle = row_match.get("experience_title")
                experience_title = (
                    etitle.strip()
                    if isinstance(etitle, str) and etitle.strip()
                    else resolve_experience_title_for_connection(
                        cid,
                        profiles_by_connection.get(cid),
                        domain_mapped_connection_ids,
                    )
                )
                fn_raw = row_match.get("full_name")
                full_name = (
                    fn_raw.strip()
                    if isinstance(fn_raw, str) and fn_raw.strip()
                    else connection_full_name_from_profile(
                        profiles_by_connection.get(cid)
                    )
                )
            else:
                prof = profiles_by_connection.get(cid)
                d, uni = profile_company_display_and_universal(prof)
                company_name = (
                    d.strip() if isinstance(d, str) and d.strip() else "Unknown"
                )
                company_universal = (
                    uni.strip() if isinstance(uni, str) and uni.strip() else None
                )
                company_web = None
                if company_universal:
                    co_src = es_map.get(str(company_universal).strip())
                    company_web = (
                        pick_company_website(co_src)
                        if isinstance(co_src, dict)
                        else None
                    )
                experience_title = resolve_experience_title_for_connection(
                    cid,
                    profiles_by_connection.get(cid),
                    domain_mapped_connection_ids,
                )
                full_name = connection_full_name_from_profile(
                    profiles_by_connection.get(cid)
                )

            # If profile/name is missing in ES, omit this participant from output.
            if full_name == "N/A":
                continue

            connection_rows.append(
                {
                    "connection_public_identifier": cid,
                    "full_name": full_name,
                    "company_name": company_name or "Unknown",
                    "company_universal_name": company_universal,
                    "company_website_url": company_web,
                    "experience_title": experience_title,
                }
            )

        meetings_out: List[Dict[str, Any]] = [
            {
                "connection_public_identifier": connection_rows,
                "event_title": all_titles,
                "meeting_time": _build_combined_meeting_sentence(
                    first["company_name"], all_times
                ),
            }
        ]
        out_groups.append(
            {
                "company_name": first["company_name"],
                "company_universal_name": u_key,
                "company_website_url": first.get("company_website_url"),
                "news": group_news,
                "meetings": meetings_out,
            }
        )
    unique_calendar_event_count = len(unique_calendar_keys)
    return out_groups, flat_n, unique_calendar_event_count, es_matched_row_count


def build_per_meeting_news_digest_block(
    *,
    connection_public_identifiers: List[str],
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
    profiles_by_connection: Dict[str, Optional[Dict[str, Any]]],
    latest_bd_by_company: Dict[str, Dict[str, Any]],
    participant_news_by_event: Optional[Dict[str, Dict[str, Any]]] = None,
    company_es_by_universal: Optional[Dict[str, Dict[str, Any]]] = None,
    reference_timezone: Optional[str] = None,
) -> str:
    """
    Structured text for the LLM: per meeting → company name, times, news_title/content/link.
    Meeting times are expressed in *reference_timezone* when provided.
    """
    parts: List[str] = []
    parts.append(
        "PER-MEETING DIGEST — for the email, each event (no bios, no organizers, no bullets):\n"
        "1) Company: one line only — put the URL inside the same headline as <strong> (see Headline link URL). "
        'Do NOT add a separate paragraph or label "Company website".\n'
        "2) Meeting time (one line)\n"
        "3) News: news_title, content excerpt, and link (bd_news by company, else participant summary from bd_news). "
        "Do not show news_id in the email—use title, content, link only.\n"
    )
    n = 0
    for conn_id in connection_public_identifiers:
        events = _events_for_connection(conn_id, calendar_events_by_connection)
        profile = profiles_by_connection.get(conn_id)
        company_display, company_universal = profile_company_display_and_universal(
            profile
        )
        for ev in events:
            if not isinstance(ev, dict):
                continue
            n += 1
            news = resolve_news_for_meeting(
                company_universal,
                latest_bd_by_company,
            )
            if (news.get("source") or "none") == "none":
                ev_key = make_meeting_event_key(conn_id, ev)
                participant_news = (participant_news_by_event or {}).get(ev_key)
                if isinstance(participant_news, dict):
                    news = participant_news
            parts.append(f"--- Event {n} ---")
            parts.append(
                f"Company name (use inside <strong>…</strong> headline): {company_display}"
            )
            headline_url = "NONE"
            if company_universal:
                co_src = (company_es_by_universal or {}).get(company_universal)
                web = pick_company_website(co_src)
                if web:
                    headline_url = web
                else:
                    headline_url = (
                        "NONE (not listed in company ES for this universal name)"
                    )
            else:
                headline_url = "NONE (no company_universal_name on profile — not looked up in company ES)"
            parts.append(
                f"Headline link URL (if a real http(s) URL: use "
                f'<p><strong><a href="URL">{company_display}</a></strong></p> — '
                f"same line as the name, no separate Company website paragraph): {headline_url}"
            )
            parts.append(
                f"Meeting time (one line): {format_meeting_time_sentence(company_display, ev, reference_timezone=reference_timezone)}"
            )
            src = news.get("source") or "none"
            title = news.get("news_title")
            content = news.get("content")
            link = news.get("link")
            if src != "none":
                parts.append(
                    f"News source: {src} (bd_news_company or participant summary from bd_news)"
                )
                if title:
                    parts.append(f"news_title: {title}")
                if content:
                    parts.append(f"content: {content}")
                if link:
                    parts.append(f"link: {link}")
            else:
                parts.append(
                    "News: none (no bd_news for company universal and no participant news summary)."
                )
            parts.append("")
    if n == 0:
        return (
            "PER-MEETING DIGEST: No calendar events under the included connections.\n"
        )
    return "\n".join(parts).strip()
