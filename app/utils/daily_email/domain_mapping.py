"""
Domain-mapping helpers for the daily email endpoint.

Resolves a company universal name from an attendee's email domain using the
caller-supplied company_domain_mappings dict, then injects that universal into
a copy of the LinkedIn profile so all downstream logic uses the correct company.
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from app.utils.daily_email.helpers import normalize_company

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level parsing helpers
# ---------------------------------------------------------------------------


def extract_domain_from_email(email: str) -> Optional[str]:
    """Return the lowercase domain from an email address, or None if unparseable."""
    if not isinstance(email, str) or "@" not in email:
        return None
    parts = email.strip().rsplit("@", 1)
    if len(parts) != 2 or not parts[1].strip():
        return None
    return parts[1].strip().lower()


def reference_employer_sets_from_profile_email(
    email: str,
    company_domain_mappings: Optional[Dict[str, Any]],
) -> Tuple[Set[str], Set[str]]:
    """
    Resolve reference employer display name(s) and LinkedIn company universal(s)
    from the reference person's email domain and caller-supplied company_domain_mappings.

    Returns (normalized_company_names, lowercase_universals). Empty sets if the email
    is blank, domain cannot be parsed, domain is missing from the map, or the map
    entry has no usable company_name / linkedin_url.
    """
    names: Set[str] = set()
    universals: Set[str] = set()
    if not isinstance(email, str) or not email.strip() or not company_domain_mappings:
        return names, universals
    domain = extract_domain_from_email(email.strip())
    if not domain:
        return names, universals
    entry = company_domain_mappings.get(domain)
    if not isinstance(entry, dict):
        return names, universals
    linkedin_url = entry.get("linkedin_url")
    if isinstance(linkedin_url, str) and linkedin_url.strip():
        parsed = universal_name_from_linkedin_url(linkedin_url.strip())
        if parsed:
            universals.add(parsed.lower())
    company_name = entry.get("company_name")
    if isinstance(company_name, str) and company_name.strip():
        n = normalize_company(company_name)
        if n:
            names.add(n)
    return names, universals


def universal_name_from_linkedin_url(linkedin_url: str) -> Optional[str]:
    """
    Extract the company universal name from a LinkedIn company URL.

    'https://www.linkedin.com/company/openai'  ->  'openai'
    'https://www.linkedin.com/company/openai/admin'  ->  'openai'
    """
    if not isinstance(linkedin_url, str) or not linkedin_url.strip():
        return None
    url = linkedin_url.strip().rstrip("/")
    marker = "/company/"
    idx = url.find(marker)
    if idx == -1:
        return None
    tail = url[idx + len(marker) :]
    universal = tail.split("/")[0].strip()
    return universal or None


# ---------------------------------------------------------------------------
# PID → email lookup built from calendar event attendees/organizers
# ---------------------------------------------------------------------------


def build_global_pid_email_map(
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, str]:
    """
    Build a global public_identifier -> email map by scanning every attendee and
    organizer across *all* connections' calendar events.

    A person's email rarely appears in their own calendar bucket; it shows up in
    other connections' events instead. First occurrence wins.
    """
    pid_to_email: Dict[str, str] = {}
    for events in calendar_events_by_connection.values():
        for ev in events:
            if not isinstance(ev, dict):
                continue
            for attendee in ev.get("attendees") or []:
                if not isinstance(attendee, dict):
                    continue
                att_pid = attendee.get("public_identifier")
                att_email = attendee.get("email")
                if (
                    isinstance(att_pid, str)
                    and att_pid.strip()
                    and isinstance(att_email, str)
                    and att_email.strip()
                    and att_pid not in pid_to_email
                ):
                    pid_to_email[att_pid.strip()] = att_email.strip()
            organizer = ev.get("organizer")
            if isinstance(organizer, dict):
                org_pid = organizer.get("public_identifier")
                org_email = organizer.get("email")
                if (
                    isinstance(org_pid, str)
                    and org_pid.strip()
                    and isinstance(org_email, str)
                    and org_email.strip()
                    and org_pid not in pid_to_email
                ):
                    pid_to_email[org_pid.strip()] = org_email.strip()
    return pid_to_email


# ---------------------------------------------------------------------------
# Main resolution function
# ---------------------------------------------------------------------------


def build_domain_mapped_universals(
    connection_public_identifiers: List[str],
    calendar_events_by_connection: Dict[str, List[Dict[str, Any]]],
    company_domain_mappings: Optional[Dict[str, Any]],
) -> Dict[str, Tuple[str, Optional[str]]]:
    """
    For each connection, resolve a company universal name via the domain mapping.

    Strategy:
    1. Build a global pid -> email lookup from all attendees/organizers across
       every connection's events (a person's email appears in other connections'
       events, not their own bucket).
    2. Extract the email domain and look it up in company_domain_mappings.
    3. Parse the universal name from the LinkedIn URL in the mapping entry.

    Returns:
        Dict[connection_pid -> (universal_name, company_name)]
        Connections that cannot be resolved are silently skipped
        (caller falls back to profile-derived company).
    """
    if not company_domain_mappings:
        return {}

    global_pid_email = build_global_pid_email_map(calendar_events_by_connection)
    logger.info(
        "Domain mapping: global pid->email map built with %d entries: %s",
        len(global_pid_email),
        global_pid_email,
    )

    result: Dict[str, Tuple[str, Optional[str]]] = {}
    for pid in connection_public_identifiers:
        email = global_pid_email.get(pid)
        if not email:
            logger.info(
                "Domain mapping: no email found for pid=%s (not in any attendee/organizer list)",
                pid,
            )
            continue
        domain = extract_domain_from_email(email)
        if not domain:
            logger.info(
                "Domain mapping: could not extract domain from email=%s for pid=%s",
                email,
                pid,
            )
            continue
        entry = company_domain_mappings.get(domain)
        if not isinstance(entry, dict):
            logger.info(
                "Domain mapping: domain=%s not in company_domain_mappings for pid=%s — using profile fallback",
                domain,
                pid,
            )
            continue
        linkedin_url = entry.get("linkedin_url")
        universal = universal_name_from_linkedin_url(linkedin_url or "")
        if universal:
            company_name = entry.get("company_name") or None
            result[pid] = (universal, company_name)
            logger.info(
                "Domain mapping resolved: pid=%s email=%s domain=%s -> universal=%s company=%s",
                pid,
                email,
                domain,
                universal,
                company_name,
            )
        else:
            logger.info(
                "Domain mapping: could not parse universal name from linkedin_url=%s for pid=%s",
                linkedin_url,
                pid,
            )
    return result


def augment_profile_with_universal(
    profile: Optional[Dict[str, Any]],
    universal_name: str,
    company_name: Optional[str],
) -> Dict[str, Any]:
    """
    Return a copy of the profile with the domain-mapped *universal_name* (and
    optionally *company_name*) injected into experience[0].

    The domain mapping always takes priority over whatever the LinkedIn profile
    carries — the whole point is to correct the company when the attendee email
    domain is a reliable signal.

    If the profile has no experience list at all, a synthetic entry is created
    so that profile_company_display_and_universal() can resolve the company.
    """
    base: Dict[str, Any] = dict(profile) if isinstance(profile, dict) else {}
    exp = base.get("experience")
    if isinstance(exp, list) and exp and isinstance(exp[0], dict):
        exp0 = dict(exp[0])
        previous_universal = exp0.get("company_universal_name") or exp0.get(
            "company_universalname"
        )
        exp0["company_universal_name"] = universal_name
        if company_name:
            exp0["company"] = company_name
        if previous_universal and previous_universal != universal_name:
            logger.info(
                "Domain mapping override: replaced profile universal=%s with mapped universal=%s (company=%s)",
                previous_universal,
                universal_name,
                company_name,
            )
        base["experience"] = [exp0] + list(exp[1:])
    else:
        base["experience"] = [
            {
                "company": company_name or universal_name,
                "company_universal_name": universal_name,
            }
        ]
    return base
