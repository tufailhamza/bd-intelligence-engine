from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field

FEATURE_KEY_ASSIGNMENT_CREATION = "assignment_creation"
FEATURE_KEY_CONTENT_CHAT = "content_chat"

ChatType = Literal["assignment", "Assignment", "content", "Content"]


class AssignmentChatUpdate(BaseModel):
    key: str = Field(..., min_length=1, description="Assignment field key to update")
    current_value: str = Field(
        ..., min_length=1, description="Current value for the requested field update"
    )


class AssignmentChatInput(BaseModel):
    entity_urn: str = Field(..., min_length=1, description="User profile entity URN")
    message: str = Field(..., min_length=1, description="Latest user message")
    update: Optional[AssignmentChatUpdate] = None
    client_context: Optional[Dict[str, Any]] = None
    # Frontend sends snake_case keys: chat_type, entity_id.
    chat_type: ChatType = Field(
        default="assignment",
        description="Chat mode for this turn: 'assignment' or 'content'.",
    )
    # External FS-provided identifier for the current assignment/content entity.
    entity_id: str = Field(
        ...,
        min_length=1,
        description="External entity identifier used to persist assignment/content context.",
    )


class AssignmentPeopleInvolvedPersonInput(BaseModel):
    public_identifier: str = Field(
        ...,
        min_length=1,
        description="LinkedIn/profile public identifier",
        validation_alias=AliasChoices("public_identifier", "publicIdentifier"),
    )
    id: Optional[str] = Field(
        default=None, description="Optional profile id from the selected result"
    )
    es_id: Optional[str] = Field(
        default=None, description="Optional Elasticsearch id from the selected result"
    )
    name: Optional[str] = Field(
        default=None, description="Optional display name from the selected result"
    )
    full_name: Optional[str] = Field(
        default=None, description="Optional full name from the selected result"
    )
    company: Optional[str] = Field(
        default=None, description="Optional company from the selected result"
    )
    company_name: Optional[str] = Field(
        default=None,
        description="Optional company display name from the selected result",
    )


class AssignmentPeopleInvolvedAddInput(BaseModel):
    assignment_id: str = Field(
        ..., min_length=1, description="External assignment/context id"
    )
    people: List[AssignmentPeopleInvolvedPersonInput] = Field(
        default_factory=list,
        description="People selected by the user to add to people_involved",
    )
    public_identifiers: List[str] = Field(
        default_factory=list,
        description="Alternative shorthand when only public identifiers are available",
    )


class AssignmentChatInitInput(BaseModel):
    entity_urn: str = Field(..., min_length=1, description="User profile entity URN")
    chat_type: ChatType = Field(
        default="assignment",
        description="Chat mode for this turn: 'assignment' or 'content'.",
    )
    entity_id: str = Field(
        ...,
        min_length=1,
        description="External entity identifier used to persist assignment/content context.",
    )


class AssignmentChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: Optional[datetime] = None


class AssignmentChatOutput(BaseModel):
    entity_urn: str
    session_id: str
    assistant_message: str
    assignment_draft: Dict[str, Optional[str]]
    missing_fields: List[str]
    completion_score: float = Field(
        ..., ge=0.0, le=1.0, description="Fraction of fields captured [0..1]"
    )
    ready_to_save: bool = False
    show_add_people_cta: bool = False


class AssignmentChatInitOutput(BaseModel):
    entity_urn: str
    session_id: str
    greeting_message: str


class AssignmentChatStateOutput(BaseModel):
    entity_urn: str
    feature_key: str
    session_id: Optional[str] = None
    assignment_draft: Dict[str, Optional[str]]
    missing_fields: List[str]
    completion_score: float = Field(..., ge=0.0, le=1.0)
    ready_to_save: bool = False
    show_add_people_cta: bool = False
    messages: List[AssignmentChatMessage] = Field(default_factory=list)
