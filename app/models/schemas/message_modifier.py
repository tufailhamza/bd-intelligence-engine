from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class MessageModifierInput(BaseModel):
    message: str = Field(..., description="The original message to be modified")
    modifier: Literal["Shorter", "Longer", "Simpler", "Professional", "Casual"] = Field(
        ...,
        description="The type of modification to apply.",
    )
    # Accept both snake_case and camelCase from the frontend.
    max_length: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional character limit for the modified message.",
    )
    maxMessageLength: Optional[int] = Field(
        default=None,
        ge=1,
        description="Alias for max_length (camelCase variant sent by some clients).",
    )

    @model_validator(mode="after")
    def _coerce_max_length(self) -> "MessageModifierInput":
        """Use maxMessageLength as max_length when max_length is not provided."""
        if self.max_length is None and self.maxMessageLength is not None:
            self.max_length = self.maxMessageLength
        return self


class MessageModifierOutput(BaseModel):
    modified_message: str = Field(..., description="The modified message")
    original_message: str = Field(..., description="The original message")
    modifier: Literal["Shorter", "Longer", "Simpler", "Professional", "Casual"] = Field(
        ..., description="The modifier that was applied"
    )
