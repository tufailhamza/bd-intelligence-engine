from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

# News category mappings for UI filters
# Each category maps to a list of values that will be searched in BOTH news_type AND news_sub_type fields
# A news item matches if any value in the list matches either news_type OR news_sub_type
NEWS_CATEGORY_MAPPING = {
    "company_posts": [
        "funding_investment",
        "market_expansion",
        "hiring_talent_acquisition",
        "new_product_launch",
        "new_service_feature",
        "beta_pilot_release",
        "leadership_change",
        "company_linkedin_repost",
    ],
    "company_financial_releases": [
        "earnings_revenue_report",
        "ipo_market_listing",
        "dividend_share_buyback",
        "funding_investment",
    ],
    "company_news_releases": [
        "acquisition_merger",
        "partnership_joint_venture",
        "licensing_collaboration",
        "new_product_launch",
        "new_service_feature",
        "beta_pilot_release",
        "leadership_change",
        "restructuring_hq_relocation",
    ],
    "general_company_news": [
        "layoffs_downsizing",
        "market_expansion",
        "hiring_talent_acquisition",
        "others",
    ],
    "personal_posts": [
        "job_change",
        "achievements",
        "engagement",
        "publication",
        "social_impact",
        "personal_reflection",
    ],
    "predictive_profile_triggers": [
        "predictive_profile_trigger",
        "role_tenure_inflection",
        "industry_authority",
        "board_governance",
        "enterprise_scale_operator",
        "transformation_operator",
        "cross_sector_leader",
        "succession_candidate",
        "strategic_network_trigger",
        "other_high_conviction_trigger",
    ],
}


class FetchNewsInput(BaseModel):
    public_identifier: str = Field(
        ..., description="Public identifier to fetch news for"
    )
    entity_urn: Optional[str] = Field(
        None,
        description="Optional stable entity URN. Reference matching uses public_identifier OR entity_urn.",
    )
    connection_identifiers: Optional[List[str]] = Field(
        None,
        description="Required: List of connection public_identifiers to filter news. If not provided or empty, returns empty results. Can provide single or multiple connections.",
    )
    categories: Optional[List[str]] = Field(
        None,
        description="List of category filters (company_posts, company_financial_releases, company_news_releases, general_company_news, personal_posts). If not provided, returns all news.",
    )
    search_query: Optional[str] = Field(
        None,
        description="Optional text search query to search in news titles and content",
    )
    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    limit: int = Field(20, ge=1, le=100, description="Number of items per page")
    cursor: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional Elasticsearch deep-pagination cursor returned as next_cursor.",
    )


class FetchNewsV2Connection(BaseModel):
    public_identifier: Optional[str] = Field(
        None,
        description="Connection public identifier.",
    )
    entity_urn: Optional[str] = Field(
        None,
        description="Stable LinkedIn entity URN for this connection.",
    )
    profile_id: Optional[str] = Field(
        None,
        description="Elasticsearch profile document id for this connection.",
    )
    score: Optional[float] = Field(
        None,
        ge=0,
        description="Raw BD relevance score for this connection. It is normalized within the request cohort.",
    )
    priority_label: Optional[Literal["HIGH", "MEDIUM", "LOW"]] = Field(
        None,
        description="Cohort-relative priority label.",
    )
    relationship_label: Optional[Literal["STRONG", "MODERATE", "WEAK"]] = Field(
        None,
        description="Relationship strength label derived from seniority and meetings.",
    )


class FetchNewsV2Input(BaseModel):
    public_identifier: Optional[str] = Field(
        None,
        description="Reference public identifier. Kept for compatibility; profile_id is preferred when available.",
    )
    entity_urn: Optional[str] = Field(
        None,
        description="Optional stable entity URN for the reference profile.",
    )
    profile_id: Optional[str] = Field(
        None,
        description="Preferred stable Elasticsearch profile id for the reference profile.",
    )
    connection_identifiers: List[FetchNewsV2Connection] = Field(
        default_factory=list,
        description="Connections to fetch news for, including stable identifiers and ranking metadata.",
    )
    categories: Optional[List[str]] = Field(
        None,
        description="List of category filters. If not provided, returns all news.",
    )
    search_query: Optional[str] = Field(
        None,
        description="Optional text search query to search in news titles and content",
    )
    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    limit: int = Field(20, ge=1, le=100, description="Number of items per page")
    cursor: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional Elasticsearch deep-pagination cursor returned as next_cursor.",
    )


class NewsItem(BaseModel):
    connection_public_identifier: str
    news_id: Optional[str] = None
    news_title: Optional[str] = None
    content: Optional[str] = None
    link: Optional[str] = None
    published_date: Optional[int] = None
    published_date_relative: Optional[str] = None
    news_type: Optional[str] = None
    news_sub_type: Optional[str] = None
    source: Optional[str] = None
    author: Optional[str] = None
    author_description: Optional[str] = None
    score: Optional[float] = None
    es_score: Optional[float] = None
    elastic_score: Optional[float] = None
    final_score: Optional[float] = None
    semantic_score: Optional[float] = None
    recency_score: Optional[float] = None
    recency_gate_score: Optional[float] = None
    personalized_event_score: Optional[float] = None
    event_priority_score: Optional[float] = None
    connection_score: Optional[float] = None
    connection_raw_score: Optional[float] = None
    connection_profile_score: Optional[float] = None
    connection_priority_score: Optional[float] = None
    connection_relationship_score: Optional[float] = None
    connection_priority_label: Optional[str] = None
    connection_relationship_label: Optional[str] = None
    score_components: Optional[Dict[str, Any]] = None
    seen: Optional[bool] = None
    llm_score: Optional[float] = None
    llm_reasoning: Optional[str] = None


class FetchNewsOutput(BaseModel):
    public_identifier: str
    reference_es_id: Optional[str] = None
    total_count: int
    page: int
    limit: int
    total_pages: int
    news_items: List[NewsItem]
    categories_applied: Optional[List[str]] = None
    next_cursor: Optional[Dict[str, Any]] = None
    scoring: Optional[Dict[str, Any]] = None


class UpdateNewsSeenInput(BaseModel):
    news_id: str = Field(..., min_length=1, description="News identifier")
    reference_public_identifier: str = Field(
        ..., min_length=1, description="Reference profile public identifier"
    )
    entity_urn: Optional[str] = Field(
        None,
        description="Optional stable entity URN. Seen-state matching uses reference_public_identifier OR entity_urn.",
    )
    seen: bool = Field(..., description="Seen state to persist")


class UpdateNewsSeenOutput(BaseModel):
    success: bool
    message: str
