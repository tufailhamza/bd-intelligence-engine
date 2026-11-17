import logging
from fastapi import APIRouter, HTTPException

from app.models.schemas.message_modifier import (
    MessageModifierInput,
    MessageModifierOutput,
)
from app.utils.connection_messages.message_modifier import modify_message

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/message_modifier", response_model=MessageModifierOutput)
async def modify_message_endpoint(request: MessageModifierInput):
    """
    Modify a message based on the specified modifier keyword.

    Supported modifiers:
    - Shorter: Condenses the message while preserving key points
    - Longer: Expands the message with more detail and context
    - Simpler: Simplifies the language to make it easier to understand
    - Professional: Makes the message more professional and polished
    - Casual: Makes the message more casual, friendly, and conversational
    """
    try:
        modified_message = await modify_message(
            message=request.message,
            modifier=request.modifier,
            max_length=request.max_length,
        )

        return MessageModifierOutput(
            modified_message=modified_message,
            original_message=request.message,
            modifier=request.modifier,
        )
    except Exception as e:
        logger.error("Error in message_modifier endpoint: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to modify message")
