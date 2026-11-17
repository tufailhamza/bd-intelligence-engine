import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from qutils.llm.asynchronous import invoke

from app.utils.daily_email.news_fetch import (
    fetch_bd_news_recent_for_linkedin_identifiers,
)
from app.utils.daily_email.prompts import (
    COMPANY_PARTICIPANTS_NEWS_SUMMARY_SYSTEM,
    COMPANY_PARTICIPANTS_NEWS_SUMMARY_USER_TEMPLATE,
    PARTICIPANT_NEWS_SUMMARY_SYSTEM,
    PARTICIPANT_NEWS_SUMMARY_USER_TEMPLATE,
)
from app.utils.daily_email.summary import _event_identity

logger = logging.getLogger(__name__)

PARTICIPANT_NEWS_SUMMARY_MODEL = "openai/gpt-4.1-mini"
PARTICIPANT_NEWS_SUMMARY_FALLBACKS = ["openai/gpt-4.1"]
PARTICIPANT_NEWS_SUMMARY_CONCURRENCY = 4
PARTICIPANT_NEWS_TITLE_MODEL = "openai/gpt-4.1-mini"
PARTICIPANT_NEWS_TITLE_FALLBACKS = ["openai/gpt-4.1"]
PARTICIPANTS_SUMMARY_MAX_AGE_DAYS = 90


class ConnectionNewsFallbackEntry(NamedTuple):
    """One calendar connection per event: bd_news + summary are for this person only."""

    event_label: str
    connection_public_identifier: str
    connection_display_name: str


class CompanyParticipantsFallbackEntry(NamedTuple):
    """All participants sharing the same company, for a single combined news summary."""

    company_display_name: str
    participants: List[Tuple[str, str]]  # (public_identifier, display_name)


def _published_within_days(value: Any, *, days: int) -> bool:
    """
    True when published_date is within the last ``days`` days.
    Accepts epoch seconds (int/float/string) or ISO datetime strings.
    """
    if days <= 0:
        return True
    if value in (None, "", "null"):
        return False

    dt: Optional[datetime] = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            dt = None
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return False
        try:
            dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except Exception:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
            except Exception:
                dt = None

    if dt is None:
        return False
    return dt >= (datetime.now(timezone.utc) - timedelta(days=days))


def make_meeting_event_key(
    connection_public_identifier: str, event: Dict[str, Any]
) -> str:
    """
    Stable key for a (connection, calendar event) pair. Uses the same event identity
    as _events_for_connection deduping so producer (per-bucket iteration) and consumer
    (merged event list) agree without relying on enumerate indices.
    """
    if not isinstance(event, dict):
        return f"{connection_public_identifier}::invalid"
    return f"{connection_public_identifier}::{_event_identity(event)}"


def extract_participant_public_identifiers(
    event: Dict[str, Any],
    connection_public_identifier: str,
    reference_public_identifier: Optional[str] = None,
) -> List[str]:
    out: List[str] = []

    def add_pid(v: Any) -> None:
        s = str(v).strip() if v is not None else ""
        if not s:
            return
        if reference_public_identifier and s == reference_public_identifier:
            return
        if s not in out:
            out.append(s)

    add_pid(connection_public_identifier)
    add_pid(event.get("public_identifier"))

    organizer = event.get("organizer")
    if isinstance(organizer, dict):
        add_pid(organizer.get("public_identifier"))

    attendees = event.get("attendees")
    if isinstance(attendees, list):
        for a in attendees:
            if isinstance(a, dict):
                add_pid(a.get("public_identifier"))

    return out


def _truncate(val: Any, max_len: int) -> str:
    s = str(val or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3].rstrip() + "..."


def _format_published_date_for_prompt(value: Any) -> str:
    """
    Normalize published_date from ES docs for prompt grounding.
    Accepts epoch seconds (int/float/string) or ISO datetime strings.
    Returns YYYY-MM-DD when parseable, else "Unknown".
    """
    if value in (None, "", "null"):
        return "Unknown"

    dt: Optional[datetime] = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            dt = None
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return "Unknown"
        try:
            dt = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except Exception:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
            except Exception:
                dt = None

    if dt is None:
        return "Unknown"
    return dt.strftime("%Y-%m-%d")


def _build_summary_prompt(
    event_label: str,
    docs: List[Dict[str, Any]],
    *,
    connection_display_name: str,
    connection_public_identifier: str,
) -> str:
    lines: List[str] = []
    for d in docs:
        pid = str(d.get("linkedin_identifier") or "").strip() or "unknown_participant"
        title = _truncate(d.get("news_title"), 200) or "(untitled)"
        content = _truncate(d.get("content"), 350)
        link = str(d.get("link") or "").strip()
        published_date = _format_published_date_for_prompt(d.get("published_date"))
        line = (
            f"- subject_person={connection_display_name}; "
            f"internal_linkedin_identifier={pid}; title={title}; published_date={published_date}"
        )
        if content:
            line += f"; content={content}"
        if link:
            line += f"; link={link}"
        lines.append(line)

    joined = "\n".join(lines) if lines else "- none"
    return PARTICIPANT_NEWS_SUMMARY_USER_TEMPLATE.format(
        event_label=event_label,
        connection_display_name=(connection_display_name or "").strip() or "Contact",
        connection_public_identifier=connection_public_identifier.strip() or "unknown",
        participant_news_lines=joined,
    )


def _fallback_participant_news_title(
    docs: List[Dict[str, Any]], connection_display_name: str
) -> str:
    for d in docs:
        t = str(d.get("news_title") or "").strip()
        if t:
            return t[:120]
    name = (connection_display_name or "").strip() or "Connection"
    return f"{name} update"


async def generate_participant_news_title(
    *,
    docs: List[Dict[str, Any]],
    connection_display_name: str,
    summary: str,
) -> str:
    fallback_title = _fallback_participant_news_title(docs, connection_display_name)
    if not summary or not summary.strip():
        return fallback_title
    bullets: List[str] = []
    for d in docs[:5]:
        t = str(d.get("news_title") or "").strip()
        if t:
            bullets.append(f"- {t}")
    user_content = (
        "Write one short, specific news headline (max 10 words) for this connection's recent updates.\n"
        "Rules: do not use generic phrases like 'Connection news', do not add quotes or emojis.\n"
        f"Connection: {connection_display_name}\n"
        f"Summary: {summary.strip()}\n"
        f"Original titles:\n{chr(10).join(bullets) if bullets else '- none'}\n"
        "Return headline text only."
    )
    try:
        response = await invoke(
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise editor writing concise, factual headlines.",
                },
                {"role": "user", "content": user_content},
            ],
            model=PARTICIPANT_NEWS_TITLE_MODEL,
            temperature=0.1,
            fallbacks=PARTICIPANT_NEWS_TITLE_FALLBACKS,
        )
        if isinstance(response, str):
            title = response.strip().replace("\n", " ").strip(" \"'")
            if title:
                return title[:120]
    except Exception as e:
        logger.debug("Participant news title generation failed: %s", e)
    return fallback_title


async def summarize_participant_news_for_meeting(
    *,
    event_label: str,
    docs: List[Dict[str, Any]],
    connection_display_name: str,
    connection_public_identifier: str,
) -> Optional[str]:
    if not docs:
        return None
    user_content = _build_summary_prompt(
        event_label,
        docs,
        connection_display_name=connection_display_name,
        connection_public_identifier=connection_public_identifier,
    )
    try:
        response = await invoke(
            messages=[
                {"role": "system", "content": PARTICIPANT_NEWS_SUMMARY_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            model=PARTICIPANT_NEWS_SUMMARY_MODEL,
            temperature=0.2,
            fallbacks=PARTICIPANT_NEWS_SUMMARY_FALLBACKS,
        )
    except Exception as e:
        logger.warning("Participant news summary failed: %s", e)
        return None
    if not isinstance(response, str):
        return None
    text = response.strip()
    if text:
        logger.debug("Participant news summary generated (content omitted from logs)")
    return text or None


async def build_participant_news_fallback_by_event(
    *,
    es_client_bd: Any,
    index_name: str,
    meetings_participants_map: Dict[str, ConnectionNewsFallbackEntry],
) -> Dict[str, Dict[str, Any]]:
    """
    Build event-keyed fallback news snippets from bd_news for the given LinkedIn identifiers.

    Each ``ConnectionNewsFallbackEntry`` names exactly one ``connection_public_identifier``
    (calendar bucket owner); news fetch and summary are grounded to that person only.

    meetings_participants_map:
        event_key -> ConnectionNewsFallbackEntry
    """
    if not meetings_participants_map:
        return {}

    all_pids: List[str] = []
    for entry in meetings_participants_map.values():
        pid = str(entry.connection_public_identifier or "").strip()
        if pid and pid not in all_pids:
            all_pids.append(pid)
    if not all_pids:
        return {}

    docs = await fetch_bd_news_recent_for_linkedin_identifiers(
        es_client_bd=es_client_bd,
        linkedin_identifiers=all_pids,
        index_name=index_name,
        max_hits=400,
    )
    docs = [
        d
        for d in docs
        if _published_within_days(
            d.get("published_date"), days=PARTICIPANTS_SUMMARY_MAX_AGE_DAYS
        )
    ]
    if not docs:
        return {}

    docs_by_pid: Dict[str, List[Dict[str, Any]]] = {}
    for d in docs:
        pid = str(d.get("linkedin_identifier") or "").strip()
        if not pid:
            continue
        docs_by_pid.setdefault(pid, []).append(d)

    event_payloads: List[Tuple[str, str, List[Dict[str, Any]], str, str]] = []
    for event_key, entry in meetings_participants_map.items():
        connection_public_identifier = str(
            entry.connection_public_identifier or ""
        ).strip()
        connection_display_name = (entry.connection_display_name or "").strip()
        event_label = (entry.event_label or "").strip()
        if not connection_public_identifier:
            logger.warning(
                "Skipping participant news fallback: empty connection_public_identifier "
                "(event_key=%s)",
                event_key,
            )
            continue
        if not connection_display_name:
            continue
        event_docs = (docs_by_pid.get(connection_public_identifier, []) or [])[:6]
        if not event_docs:
            continue
        event_payloads.append(
            (
                event_key,
                event_label,
                event_docs,
                connection_display_name,
                connection_public_identifier,
            )
        )

    if not event_payloads:
        return {}

    sem = asyncio.Semaphore(PARTICIPANT_NEWS_SUMMARY_CONCURRENCY)

    async def _summarize_one(
        event_key: str,
        event_label: str,
        event_docs: List[Dict[str, Any]],
        connection_display_name: str,
        connection_public_identifier: str,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        async with sem:
            summary = await summarize_participant_news_for_meeting(
                event_label=event_label,
                docs=event_docs,
                connection_display_name=connection_display_name,
                connection_public_identifier=connection_public_identifier,
            )
        if not summary:
            return None
        generated_title = await generate_participant_news_title(
            docs=event_docs,
            connection_display_name=connection_display_name,
            summary=summary,
        )
        first_link = ""
        for d in event_docs:
            link = str(d.get("link") or "").strip()
            if link:
                first_link = link
                break
        return (
            event_key,
            {
                "source": "bd_news_participants_summary",
                "news_id": None,
                "news_title": generated_title,
                "content": summary,
                "link": first_link or None,
                "published_date": None,
            },
        )

    results = await asyncio.gather(
        *[
            _summarize_one(
                event_key,
                event_label,
                event_docs,
                connection_display_name,
                connection_public_identifier,
            )
            for (
                event_key,
                event_label,
                event_docs,
                connection_display_name,
                connection_public_identifier,
            ) in event_payloads
        ]
    )

    out: Dict[str, Dict[str, Any]] = {}
    for item in results:
        if not item:
            continue
        event_key, payload = item
        out[event_key] = payload
    return out


async def build_company_participants_news_fallback(
    *,
    es_client_bd: Any,
    index_name: str,
    company_participants_map: Dict[str, CompanyParticipantsFallbackEntry],
) -> Dict[str, Dict[str, Any]]:
    """
    For each company, fetch bd_news for ALL its participants and generate ONE combined summary.

    This replaces the per-event single-participant fallback when multiple connections
    share the same company (e.g. all mapped to qlu-ai via domain mapping).

    Returns: Dict[company_universal_name -> news_payload]
    """
    if not company_participants_map:
        return {}

    all_pids: List[str] = []
    for entry in company_participants_map.values():
        for pid, _ in entry.participants:
            if pid and pid not in all_pids:
                all_pids.append(pid)
    if not all_pids:
        return {}

    docs = await fetch_bd_news_recent_for_linkedin_identifiers(
        es_client_bd=es_client_bd,
        linkedin_identifiers=all_pids,
        index_name=index_name,
        max_hits=400,
    )
    docs = [
        d
        for d in docs
        if _published_within_days(
            d.get("published_date"), days=PARTICIPANTS_SUMMARY_MAX_AGE_DAYS
        )
    ]
    if not docs:
        return {}

    docs_by_pid: Dict[str, List[Dict[str, Any]]] = {}
    for d in docs:
        pid = str(d.get("linkedin_identifier") or "").strip()
        if not pid:
            continue
        docs_by_pid.setdefault(pid, []).append(d)

    sem = asyncio.Semaphore(PARTICIPANT_NEWS_SUMMARY_CONCURRENCY)

    async def _summarize_company(
        company_universal: str,
        entry: CompanyParticipantsFallbackEntry,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        # Gather docs for all participants (up to 4 each to keep prompt size reasonable)
        company_docs: List[Dict[str, Any]] = []
        pid_to_display: Dict[str, str] = {pid: dn for pid, dn in entry.participants}
        for pid, _ in entry.participants:
            company_docs.extend((docs_by_pid.get(pid) or [])[:4])
        if not company_docs:
            return None

        lines: List[str] = []
        for d in company_docs:
            pid = str(d.get("linkedin_identifier") or "").strip() or "unknown"
            disp = pid_to_display.get(pid, pid)
            title = _truncate(d.get("news_title"), 200) or "(untitled)"
            content = _truncate(d.get("content"), 350)
            link = str(d.get("link") or "").strip()
            published_date = _format_published_date_for_prompt(d.get("published_date"))
            line = (
                f"- participant={disp}; title={title}; published_date={published_date}"
            )
            if content:
                line += f"; content={content}"
            if link:
                line += f"; link={link}"
            lines.append(line)

        user_content = COMPANY_PARTICIPANTS_NEWS_SUMMARY_USER_TEMPLATE.format(
            company_display_name=entry.company_display_name,
            participant_news_lines="\n".join(lines) if lines else "- none",
        )

        async with sem:
            try:
                response = await invoke(
                    messages=[
                        {
                            "role": "system",
                            "content": COMPANY_PARTICIPANTS_NEWS_SUMMARY_SYSTEM,
                        },
                        {"role": "user", "content": user_content},
                    ],
                    model=PARTICIPANT_NEWS_SUMMARY_MODEL,
                    temperature=0.2,
                    fallbacks=PARTICIPANT_NEWS_SUMMARY_FALLBACKS,
                )
            except Exception as e:
                logger.warning(
                    "Company participants news summary failed for %s: %s",
                    company_universal,
                    e,
                )
                return None

        if not isinstance(response, str) or not response.strip():
            return None

        summary = response.strip()
        all_docs_for_title = [
            d for pid, _ in entry.participants for d in (docs_by_pid.get(pid) or [])[:2]
        ]
        generated_title = await generate_participant_news_title(
            docs=all_docs_for_title,
            connection_display_name=entry.company_display_name,
            summary=summary,
        )
        first_link = next(
            (str(d.get("link") or "").strip() for d in company_docs if d.get("link")),
            None,
        )
        logger.info(
            "Company participants news summary generated: company=%s participants=%d",
            company_universal,
            len(entry.participants),
        )
        return (
            company_universal,
            {
                "source": "bd_news_participants_summary",
                "news_id": None,
                "news_title": generated_title,
                "content": summary,
                "link": first_link,
                "published_date": None,
            },
        )

    results = await asyncio.gather(
        *[
            _summarize_company(u_key, entry)
            for u_key, entry in company_participants_map.items()
        ]
    )

    out: Dict[str, Dict[str, Any]] = {}
    for item in results:
        if not item:
            continue
        u_key, payload = item
        out[u_key] = payload
    return out
