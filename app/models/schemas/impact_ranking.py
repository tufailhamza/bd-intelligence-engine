from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ImpactRankingValue = Literal["Strong", "Moderate", "Weak"]


class UpdateImpactRankingInput(BaseModel):
    """
    Manually set impact_ranking for one or more connections.

    ``connection_public_identifiers`` is a dict mapping each connection's
    public identifier to its desired impact ranking value (or null to clear).

    Example:
        {
            "reference_public_identifier": "muhammad-afaq-a12168385",
            "connection_public_identifiers": {
                "uzair-amin100": "Strong",
                "wajahatuiux": "Moderate"
            }
        }
    """

    reference_public_identifier: str
    connection_public_identifiers: Dict[str, Optional[ImpactRankingValue]] = Field(
        ...,
        min_length=1,
        description="Map of connection_public_identifier -> impact_ranking value.",
    )


class UpdateImpactRankingResult(BaseModel):
    connection_public_identifier: str
    impact_ranking: Optional[ImpactRankingValue]
    updated_in_db: bool


class UpdateImpactRankingOutput(BaseModel):
    reference_public_identifier: str
    results: List[UpdateImpactRankingResult]
    not_found_in_db: List[str] = Field(
        default_factory=list,
        description="Connections where no DB row was found (rowcount=0).",
    )
