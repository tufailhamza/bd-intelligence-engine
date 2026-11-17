from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class ProfileInput(BaseModel):
    uuid: Optional[UUID]
    title: str
    description: str
    key_titles: List[str]
    industries: List[str]
    locations: List[str]
    company_size: str
    revenue: str
    funding: str


class ProfilesOutput(BaseModel):
    company_profiles: List[Dict[str, Any]]
    company_details: Dict[str, Dict[str, Any]]


class PublicIdentifierProfileInput(BaseModel):
    public_identifier: str
    # Optional list of ES _source fields to return. If omitted, returns full _source.
    source_fields: Optional[List[str]] = None


class PublicIdentifierProfileOutput(BaseModel):
    public_identifier: str
    es_id: Optional[str] = None
    profile: Optional[Dict[str, Any]] = None


class CalendarEventAttendee(BaseModel):
    email: str
    responseStatus: Optional[str] = None
    public_identifier: Optional[str] = None


class CalendarEvent(BaseModel):
    id: Optional[str] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    start: Optional[Dict[str, Any]] = None
    end: Optional[Dict[str, Any]] = None
    attendees: Optional[List[CalendarEventAttendee]] = None
    organizer: Optional[Dict[str, Any]] = None
    location: Optional[str] = None
    status: Optional[str] = None
    public_identifier: Optional[str] = None


class GenerateScoringMetricsInput(BaseModel):
    public_identifier: str
    calendar_events: Optional[List[CalendarEvent]] = (
        None  # Optional calendar events array
    )


class GenerateScoringMetricsOutput(BaseModel):
    public_identifier: str
    es_id: Optional[str] = None
    # Parsed rubric JSON converted from the XML output
    rubric_json: Dict[str, Any]
    # Optional: parsing error message (if XML could not be parsed fully)
    parse_error: Optional[str] = None


class RankConnectionsInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    reference_public_identifier: str
    connection_public_identifiers: List[str]  # Exactly 5 connection public identifiers
    calendar_events: Optional[Dict[str, List[CalendarEvent]]] = (
        None  # Optional: map of public_identifier -> list of calendar events
    )
    # When true: return cached rows from connection_rankings_bd (no LLM re-score); includes priority.
    # Missing identifiers get score 0, empty metric_scores, priority null (see not_cached_connection_public_identifiers).
    # When false: full re-score and persist (default); priority is omitted from ranked_connections.
    return_cache: bool = Field(
        default=False,
        alias="returnCache",
    )


class RankedConnection(BaseModel):
    public_identifier: str
    es_id: Optional[str] = None
    score: float
    metric_scores: Dict[str, float]  # Individual metric scores
    reasoning: Optional[str] = None
    top_metric: Optional[str] = None
    nullify: Optional[bool] = (
        None  # Whether this profile should be nullified (None = unknown/not evaluated)
    )
    priority: Optional[str] = (
        None  # High / Medium / Low; set when returnCache is true (cache read path)
    )
    impact_ranking: Optional[str] = (
        None  # Strong / Moderate / Weak from connection_rankings_bd.impact_ranking
    )


class RankConnectionsOutput(BaseModel):
    reference_public_identifier: str
    reference_es_id: Optional[str] = None
    ranked_connections: List[RankedConnection]  # Sorted by score descending


class RankConnectionsReasoningInput(BaseModel):
    reference_public_identifier: str
    connection_public_identifiers: List[str]


class RankConnectionReasoningItem(BaseModel):
    public_identifier: str
    reasoning: Optional[str] = None


class RankConnectionsReasoningOutput(BaseModel):
    reference_public_identifier: str
    results: List[RankConnectionReasoningItem]
    not_found: List[str] = []


class TopConnectionsInput(BaseModel):
    reference_public_identifier: str


class TopConnectionItem(BaseModel):
    connection_identifier: str
    score: float
    priority: Optional[str] = Field(
        default=None,
        description="High / Medium / Low from connection_rankings_bd.priority",
    )
    impact_ranking: Optional[str] = Field(
        default=None,
        description="Strong / Moderate / Weak from connection_rankings_bd.impact_ranking",
    )


class TopConnectionsOutput(BaseModel):
    connections: List[TopConnectionItem]  # Top 20 by score descending


class UpdateRankConnectionsInput(BaseModel):
    reference_public_identifier: str
    connection_public_identifiers: List[str]
    calendar_events: Optional[Dict[str, List[CalendarEvent]]] = (
        None  # Updated calendar events: map of public_identifier -> list of calendar events
    )


class UpdateRankConnectionsOutput(BaseModel):
    reference_public_identifier: str
    reference_es_id: Optional[str] = None
    updated_connections: List[
        RankedConnection
    ]  # Connections that were found in DB and re-scored
    not_found_in_db: List[str]  # Connections not found in DB (never ranked before)


class GenerateProfileDescriptionInput(BaseModel):
    public_identifier: str


class GenerateProfileDescriptionOutput(BaseModel):
    public_identifier: str
    es_id: Optional[str] = None
    description: str
    tagline: Optional[str] = None
