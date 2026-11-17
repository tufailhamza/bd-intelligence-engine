from pydantic import BaseModel, Field
from typing import Optional


class DeleteConnectionRankingsInput(BaseModel):
    reference_public_identifier: str = Field(
        ..., description="The reference profile public identifier"
    )
    connection_public_identifier: Optional[str] = Field(
        None,
        description="Optional. If provided, deletes only this specific connection ranking. If not provided, deletes all rankings for the reference profile.",
    )


class DeleteConnectionRankingsOutput(BaseModel):
    reference_public_identifier: str = Field(
        ..., description="The reference profile identifier"
    )
    connection_public_identifier: Optional[str] = Field(
        None,
        description="The connection identifier if a specific connection was deleted",
    )
    connection_rankings_deleted: int = Field(
        ..., description="Number of rows deleted from connection_rankings_bd table"
    )
    profile_scores_deleted: int = Field(
        ..., description="Number of rows deleted from profile_scores_bd table"
    )
    ranked_news_deleted: int = Field(
        ..., description="Number of rows deleted from ranked_news_bd table"
    )


class DeleteConnectionRankingsBdOnlyInput(BaseModel):
    """Body for deleting rows only from `connection_rankings_bd`."""

    reference_public_identifier: str = Field(
        ..., description="The reference profile public identifier"
    )
    connection_public_identifier: Optional[str] = Field(
        None,
        description=(
            "If set, delete only this connection’s row for the reference. "
            "If omitted, delete all `connection_rankings_bd` rows for the reference."
        ),
    )


class DeleteConnectionRankingsBdOnlyOutput(BaseModel):
    reference_public_identifier: str
    connection_public_identifier: Optional[str] = None
    connection_rankings_deleted: int = Field(
        ...,
        description="Rows removed from connection_rankings_bd only (no other tables).",
    )
