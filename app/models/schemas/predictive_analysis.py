from pydantic import BaseModel, Field


class PredictiveAnalysisInput(BaseModel):
    profile_id: str = Field(
        ...,
        min_length=1,
        description="Elasticsearch profile document id from the people/profile index.",
    )
