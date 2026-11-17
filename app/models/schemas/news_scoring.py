from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class NewsScoringInput(BaseModel):
    """
    Request for scoring news:
    - reference person's LinkedIn public_identifier
    - Either provide connection_identifiers directly OR use top_connections to fetch from DB
    - Optionally provide connection_public_identifier to score news only for that specific connection
    Optional knobs are provided for controllability (safe defaults).
    """

    public_identifier: str = Field(
        ..., description="Reference profile public_identifier"
    )
    connection_public_identifier: Optional[str] = Field(
        None,
        description="Optional: Score news only for this specific connection profile. If provided, only news for this connection will be scored.",
    )
    connection_identifiers: Optional[List[str]] = Field(
        None,
        description="Optional: List of connection public_identifiers to fetch news for. If provided, top_connections is ignored. If connection_public_identifier is also provided, only that connection will be used.",
    )
    top_connections: int = Field(20, ge=1, le=50)
    news_per_connection: int = Field(5, ge=1, le=50)


class RankedNewsItem(BaseModel):
    connection_public_identifier: str
    news_id: Optional[str] = None
    news_title: Optional[str] = None
    content: Optional[str] = None
    link: Optional[str] = None
    published_date: Optional[int] = None
    published_date_relative: Optional[str] = None
    news_type: Optional[str] = None
    llm_score: float
    llm_reasoning: Optional[str] = None


class NewsScoringOutput(BaseModel):
    reference_public_identifier: str
    reference_es_id: Optional[str] = None
    top_connections: List[str]
    ranked_news: List[RankedNewsItem]
    should_notify: Optional[bool] = Field(
        None, description="Whether to notify based on news comparison logic"
    )
    total_ranked_last_60_days: Optional[int] = Field(
        None,
        description="Total number of ranked news items from last 60 days in database",
    )
    total_stored: Optional[int] = Field(
        None, description="Total number of news items stored in database"
    )
    debug: Optional[Dict[str, Any]] = None
