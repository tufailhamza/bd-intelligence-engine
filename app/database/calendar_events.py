import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.sql import text

from app.routes.dependancies import get_sqlalchemy_session_ai

logger = logging.getLogger(__name__)


def _parse_event_dt(value: Any) -> Optional[datetime]:
    """
    Accepts Google Calendar-like structures:
      {"dateTime": "2025-07-08T10:00:00Z"} or {"date": "2025-07-08"}
    Returns aware UTC datetime when possible.
    """
    if not isinstance(value, dict):
        return None

    dt_str = value.get("dateTime")
    if isinstance(dt_str, str) and dt_str:
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    d_str = value.get("date")
    if isinstance(d_str, str) and d_str:
        try:
            dt = datetime.fromisoformat(d_str)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    return None


def _event_fingerprint(
    *,
    provider: str,
    provider_event_id: Optional[str],
    public_identifier: str,
    event: Dict[str, Any],
) -> str:
    """
    Stable fingerprint per (public_identifier, event).
    Prefers provider_event_id when available; otherwise hashes stable fields.
    """
    if provider_event_id:
        return f"{provider}:{provider_event_id}"

    summary = event.get("summary") if isinstance(event.get("summary"), str) else ""
    organizer_email = ""
    organizer = event.get("organizer")
    if isinstance(organizer, dict) and isinstance(organizer.get("email"), str):
        organizer_email = organizer.get("email") or ""

    start_at = _parse_event_dt(event.get("start"))
    end_at = _parse_event_dt(event.get("end"))

    base = "|".join(
        [
            provider,
            public_identifier,
            (start_at.isoformat() if start_at else ""),
            (end_at.isoformat() if end_at else ""),
            organizer_email.lower(),
            summary.strip().lower(),
        ]
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


async def upsert_calendar_events_for_public_identifier(
    *,
    public_identifier: str,
    calendar_events: List[Dict[str, Any]],
    provider: str = "google",
) -> int:
    """
    Upsert calendar events into calendar_events_bd.
    Returns number of events processed (attempted).
    """
    if not calendar_events:
        return 0

    async with get_sqlalchemy_session_ai() as session:
        try:
            processed = 0
            for ev in calendar_events:
                if not isinstance(ev, dict):
                    continue

                provider_event_id = (
                    ev.get("id") if isinstance(ev.get("id"), str) else None
                )
                fingerprint = _event_fingerprint(
                    provider=provider,
                    provider_event_id=provider_event_id,
                    public_identifier=public_identifier,
                    event=ev,
                )

                summary = (
                    ev.get("summary") if isinstance(ev.get("summary"), str) else None
                )
                organizer_email = None
                organizer = ev.get("organizer")
                if isinstance(organizer, dict) and isinstance(
                    organizer.get("email"), str
                ):
                    organizer_email = organizer.get("email")

                start_at = _parse_event_dt(ev.get("start"))
                end_at = _parse_event_dt(ev.get("end"))

                await session.execute(
                    text("""
                        INSERT INTO calendar_events_bd (
                            public_identifier,
                            provider,
                            provider_event_id,
                            summary,
                            organizer_email,
                            start_at,
                            end_at,
                            raw_event,
                            event_fingerprint,
                            updated_at
                        )
                        VALUES (
                            :public_identifier,
                            :provider,
                            :provider_event_id,
                            :summary,
                            :organizer_email,
                            :start_at,
                            :end_at,
                            CAST(:raw_event AS jsonb),
                            :event_fingerprint,
                            NOW()
                        )
                        ON CONFLICT (public_identifier, event_fingerprint)
                        DO UPDATE SET
                            provider_event_id = EXCLUDED.provider_event_id,
                            summary = EXCLUDED.summary,
                            organizer_email = EXCLUDED.organizer_email,
                            start_at = EXCLUDED.start_at,
                            end_at = EXCLUDED.end_at,
                            raw_event = EXCLUDED.raw_event,
                            updated_at = NOW()
                        """),
                    {
                        "public_identifier": public_identifier,
                        "provider": provider,
                        "provider_event_id": provider_event_id,
                        "summary": summary,
                        "organizer_email": organizer_email,
                        "start_at": start_at,
                        "end_at": end_at,
                        "raw_event": json.dumps(ev, ensure_ascii=False),
                        "event_fingerprint": fingerprint,
                    },
                )
                processed += 1

            await session.commit()
            return processed
        except Exception as e:
            await session.rollback()
            logger.error(
                "Error upserting calendar events for public_identifier=%s: %s",
                public_identifier,
                str(e),
            )
            raise


def _extract_participant_public_identifiers_from_raw_event(
    event: Dict[str, Any],
) -> List[str]:
    """
    Extract all participant public_identifiers from a raw_event.
    Supports: event["public_identifier"], organizer["public_identifier"], attendees[]["public_identifier"].
    """
    pids: List[str] = []
    pid = event.get("public_identifier")
    if isinstance(pid, str) and pid:
        pids.append(pid)
    organizer = event.get("organizer")
    if isinstance(organizer, dict):
        opid = organizer.get("public_identifier")
        if isinstance(opid, str) and opid:
            pids.append(opid)
    attendees = event.get("attendees")
    if isinstance(attendees, list):
        for a in attendees:
            if isinstance(a, dict):
                apid = a.get("public_identifier")
                if isinstance(apid, str) and apid:
                    pids.append(apid)
    return list(dict.fromkeys(pids))


def _parse_raw_event(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse raw_event from DB (dict or JSON string) to a single event dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


async def fetch_calendar_events_by_public_identifiers(
    public_identifiers: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch calendar events from calendar_events_bd for the given public identifiers.
    Returns a dict mapping each public_identifier to a list of event dicts (from raw_event).
    """
    if not public_identifiers:
        return {}

    async with get_sqlalchemy_session_ai() as session:
        try:
            placeholders = ",".join(
                [f":pid_{i}" for i in range(len(public_identifiers))]
            )
            query = text(f"""
                SELECT public_identifier, raw_event
                FROM calendar_events_bd
                WHERE public_identifier IN ({placeholders})
            """)
            params = {f"pid_{i}": pid for i, pid in enumerate(public_identifiers)}
            res = await session.execute(query, params)
            rows = res.mappings().all()

            result: Dict[str, List[Dict[str, Any]]] = {
                pid: [] for pid in public_identifiers
            }
            for row in rows:
                pid = row.get("public_identifier")
                if not pid:
                    continue
                raw = row.get("raw_event")
                if isinstance(raw, dict):
                    event_list = result.get(pid)
                    if event_list is not None:
                        event_list.append(raw)
                elif isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            event_list = result.get(pid)
                            if event_list is not None:
                                event_list.append(parsed)
                    except Exception:
                        continue
            return result
        except Exception as e:
            logger.error(
                "Error fetching calendar events for public_identifiers=%s: %s",
                public_identifiers[:5],
                str(e),
            )
            raise


async def fetch_calendar_events_by_reference_and_extract_participants(
    *,
    reference_public_identifier: str,
    connection_public_identifiers: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch calendar events from calendar_events_bd for the reference profile (calendar owner).
    From each raw_event, extract participants (attendees and organizer public_identifier).
    Returns a dict mapping each connection public_identifier (participant) to list of events
    they participated in. Only includes connection_public_identifiers that appear as participants.
    """
    if not reference_public_identifier:
        return {}
    connection_set = set(connection_public_identifiers or [])

    async with get_sqlalchemy_session_ai() as session:
        try:
            query = text("""
                SELECT raw_event
                FROM calendar_events_bd
                WHERE public_identifier = :reference_public_identifier
            """)
            res = await session.execute(
                query, {"reference_public_identifier": reference_public_identifier}
            )
            rows = res.mappings().all()

            result: Dict[str, List[Dict[str, Any]]] = {}
            for row in rows:
                raw = row.get("raw_event")
                event = _parse_raw_event(raw)
                if not event:
                    continue
                pids = _extract_participant_public_identifiers_from_raw_event(event)
                for pid in pids:
                    if pid in connection_set:
                        result.setdefault(pid, []).append(event)
            return result
        except Exception as e:
            logger.error(
                "Error fetching calendar by reference reference=%s: %s",
                reference_public_identifier,
                str(e),
            )
            raise
