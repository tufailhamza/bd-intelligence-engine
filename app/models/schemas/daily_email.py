from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.schemas.profile_fetching import CalendarEvent


class MeetingDetailInput(BaseModel):
    """
    Meeting details supplied by the client. Extra keys are allowed for forward compatibility.
    """

    model_config = {"extra": "allow"}

    summary: Optional[str] = None
    description: Optional[str] = None
    start: Optional[Dict[str, Any]] = None
    end: Optional[Dict[str, Any]] = None
    location: Optional[str] = None
    participant_public_identifiers: Optional[List[str]] = Field(
        default=None,
        description="LinkedIn public_identifiers of attendees; enriched from people ES index.",
    )
    company_universal_names: Optional[List[str]] = Field(
        default=None,
        description="Company universal names for this meeting; used for company ES + bd_news.",
    )


class CompanyDomainMappingEntry(BaseModel):
    """Maps an email domain to its LinkedIn company info for faster universal-name resolution."""

    linkedin_url: str = Field(
        ...,
        description="LinkedIn company page URL, e.g. 'https://www.linkedin.com/company/openai'.",
    )
    linkedin_uid: Optional[str] = Field(
        default=None,
        description="LinkedIn company numeric UID when available.",
    )
    company_name: Optional[str] = Field(
        default=None,
        description="Human-readable company name, e.g. 'OpenAI'.",
    )


class DailyEmailInput(BaseModel):
    reference_public_identifier: str
    reference_profile_email: Optional[str] = Field(
        default=None,
        description=(
            "Optional work email for the reference person. When set (non-empty) and "
            "company_domain_mappings contains the email's domain, the reference employer "
            "for same-org filtering is taken from that mapping; otherwise the profile's "
            "current experience is used."
        ),
    )
    connection_public_identifiers: List[str]
    timezone: Optional[str] = Field(
        default=None,
        description=(
            "IANA timezone for the reference person (e.g. 'America/New_York', 'Asia/Karachi'). "
            "When provided, all meeting times in the response are expressed in this timezone "
            "instead of the per-event calendar timezone."
        ),
    )
    calendar_events: Optional[Dict[str, List[CalendarEvent]]] = None
    meetings: Optional[List[MeetingDetailInput]] = Field(
        default=None,
        description="Explicit meeting details (title, times, location, participants, companies).",
    )
    company_domain_mappings: Optional[Dict[str, CompanyDomainMappingEntry]] = Field(
        default=None,
        description=(
            "Optional map of email domain -> LinkedIn company info. "
            "e.g. {'openai.com': {'linkedin_url': 'https://www.linkedin.com/company/openai', ...}}. "
            "When an attendee's email domain is found here the LinkedIn URL is used to derive the "
            "company universal name for news fetching, bypassing the profile experience lookup."
        ),
    )


class DailyEmailNewsSnippet(BaseModel):
    """News for a company group from bd_news (company/participant summary)."""

    source: str = Field(
        ...,
        description="bd_news_company | bd_news_participants_summary | none",
    )
    news_id: Optional[str] = Field(
        default=None,
        description="Article id from bd_news when news is present.",
    )
    news_title: Optional[str] = None
    content: Optional[str] = None
    link: Optional[str] = None
    imagecount: Optional[int] = Field(
        default=None,
        description="Count of images on the underlying news item when available.",
    )
    videocount: Optional[int] = Field(
        default=None,
        description="Count of videos on the underlying news item when available.",
    )
    published_date: Optional[int] = Field(
        default=None,
        description="Unix epoch seconds from bd_news when source is company article; null for summaries/none.",
    )


class DailyEmailMeetingItem(BaseModel):
    """One calendar event row for the frontend (flat; prefer DailyEmailMeetingInCompany for grouped API)."""

    connection_public_identifier: str
    event_title: Optional[str] = Field(
        default=None,
        description="Calendar event summary/title if present.",
    )
    company_name: str
    company_universal_name: Optional[str] = None
    company_website_url: Optional[str] = None
    meeting_time: str = Field(
        ...,
        description='Natural sentence, e.g. "You are meeting Acme Corp at 7:30pm" (local start).',
    )
    news: DailyEmailNewsSnippet


class DailyEmailMeetingConnectionSummary(BaseModel):
    """One connection in a consolidated company row, with employer fields from profile/domain mapping."""

    connection_public_identifier: str
    full_name: str = Field(
        ...,
        description=(
            "Display name from the connection's profile (first/last, full_name, or name). "
            "Literal 'N/A' when no name fields are present."
        ),
    )
    company_name: str
    company_universal_name: Optional[str] = None
    company_website_url: Optional[str] = None
    experience_title: str = Field(
        ...,
        description=(
            "Job title from the connection's top/current LinkedIn experience row when "
            "employer is taken from the profile; empty string when missing or when "
            "employer was resolved via company domain mapping (not profile experience)."
        ),
    )


class DailyEmailMeetingInCompany(BaseModel):
    """
    Consolidated meeting entry for a company group.
    All participants are collapsed into one row per company.
    """

    connection_public_identifier: List[DailyEmailMeetingConnectionSummary] = Field(
        default_factory=list,
        description=(
            "Each participant in this company group with full_name, employer display name, "
            "LinkedIn company universal, website URL when known, and experience_title "
            "(profile current-role title, or empty when missing or employer from domain mapping)."
        ),
    )
    event_title: List[str] = Field(
        default_factory=list,
        description="Unique event titles across all meetings with this company.",
    )
    meeting_time: str = Field(
        ...,
        description=(
            "Combined sentence representing when the reference person meets this company, "
            'e.g. "You are meeting Acme Corp at 9am and 10am".'
        ),
    )


class DailyEmailCompanyMeetingsGroup(BaseModel):
    """All calendar events for one company (same ES company document)."""

    company_name: str
    company_universal_name: str = Field(
        ...,
        description="LinkedIn company universal name; matches ES companies index.",
    )
    company_website_url: Optional[str] = None
    news: DailyEmailNewsSnippet = Field(
        ...,
        description="One snippet for the company; applies to all meetings in this group.",
    )
    meetings: List[DailyEmailMeetingInCompany] = Field(
        default_factory=list,
        description="One consolidated entry per company with all participants and times.",
    )


class DailyEmailOutput(BaseModel):
    """
    Structured daily digest for the reference person (key/value friendly for frontend).
    """

    reference_public_identifier: str
    reference_full_name: str
    subject: str
    greeting: str = Field(
        ...,
        description=(
            "Opening greeting: first line Good morning {name},; blank line; then While you "
            "were away, our AI completed the daily calendar refresh. Here's a quick "
            "overview of your upcoming meetings."
        ),
    )
    meetings: List[DailyEmailCompanyMeetingsGroup] = Field(
        ...,
        description=(
            "Meetings grouped by company. Only companies with a hit in the ES "
            "companies index are included."
        ),
    )
    message: Optional[str] = Field(
        default=None,
        description="Optional note (e.g. empty state when no meetings).",
    )
