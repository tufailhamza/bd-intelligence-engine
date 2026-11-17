from typing import List, Optional
from pydantic import BaseModel


class RefinedICPInput(BaseModel):
    roles: List[str]
    industries: List[str]
    locations: List[str]
    minRevenue: Optional[float]
    maxRevenue: Optional[float]
    minEmployees: Optional[int]
    maxEmployees: Optional[int]


class RefinedICPOutput(BaseModel):
    title: str
    description: str
