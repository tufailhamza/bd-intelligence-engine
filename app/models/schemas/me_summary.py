from typing import List, Optional

from pydantic import BaseModel, Field


class MeSummaryInput(BaseModel):
    public_identifier: str = Field(
        ..., min_length=1, description="LinkedIn profile public identifier"
    )


class MeSummaryOutput(BaseModel):
    public_identifier: str
    es_id: Optional[str] = None
    ai_summary: str
    specialization_areas: List[str]
