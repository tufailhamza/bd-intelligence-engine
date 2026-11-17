from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ConnectionPriority(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class SetPrioritiesLlmInput(BaseModel):
    reference_public_identifier: str = Field(..., min_length=1)


class PriorityAssignment(BaseModel):
    connection_public_identifier: str
    priority: ConnectionPriority


class SetPrioritiesLlmOutput(BaseModel):
    reference_public_identifier: str
    updated: List[PriorityAssignment] = Field(default_factory=list)
    message: Optional[str] = None


class UpdateConnectionPriorityInput(BaseModel):
    reference_public_identifier: str = Field(..., min_length=1)
    connection_public_identifier: str = Field(..., min_length=1)
    priority: ConnectionPriority


class UpdateConnectionPriorityOutput(BaseModel):
    success: bool
    message: str = ""
