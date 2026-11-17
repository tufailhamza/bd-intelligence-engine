from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

ChannelType = Literal["email", "linkedin_message"]


class MessageInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payload: dict = Field(validation_alias=AliasChoices("payload", "post"))
    channel: ChannelType = "linkedin_message"
    recipient: Optional[Dict[str, Any]] = None
    reference_profile_identifier: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "reference_profile_identifier",
            "referenceProfileIdentifier",
            "publicIdentifier",
        ),
    )
    max_length: Optional[int] = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices(
            "max_length",
            "maxLength",
            "maxLinkedInMessageLength",
        ),
        description="Optional character cap applied to each generated message variant.",
    )


class MessageOutput(BaseModel):
    message: Dict[str, str]
    subject_line: Optional[Dict[str, str]] = (
        None  # catch_up, support, congratulate, suggested
    )


ToneType = Literal["suggested", "catch_up", "support", "congratulate"]


class OpportunityMessageInput(BaseModel):
    reference_public_identifier: str = Field(
        ..., min_length=1, description="Reference/sender profile public identifier"
    )
    reference_es_id: Optional[str] = Field(
        None, description="Optional ES id for the reference/sender profile"
    )
    related_person_public_identifier: str = Field(
        ..., min_length=1, description="Recipient/related person public identifier"
    )
    opportunity: Dict[str, Any] = Field(
        ..., description="Opportunity payload from assignment review opportunities"
    )
    channel: ChannelType = Field(
        default="linkedin_message",
        description="Delivery channel: 'linkedin_message' or 'email'",
    )
    tones: Optional[List[ToneType]] = Field(
        default=None,
        description="Tone variants to generate. Defaults to all four if omitted.",
    )
    max_length: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional word cap for each generated message",
    )


class OpportunityMessageOutput(BaseModel):
    reference_public_identifier: str
    reference_es_id: Optional[str] = None
    related_person_public_identifier: str
    related_person_es_id: Optional[str] = None
    channel: str = "linkedin_message"
    messages: Dict[str, str]
    subject_line: Optional[Dict[str, str]] = None
