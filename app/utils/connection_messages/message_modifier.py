import logging
from typing import Literal, Optional
from qutils.llm.asynchronous import invoke

from app.utils.connection_messages.message_modifier_prompts import (
    MESSAGE_MODIFIER_PROMPT_PROFESSIONAL,
    MESSAGE_MODIFIER_PROMPT_SHORTER,
    MESSAGE_MODIFIER_PROMPT_LONGER,
    MESSAGE_MODIFIER_PROMPT_SIMPLER,
    MESSAGE_MODIFIER_PROMPT_CASUAL,
)

logger = logging.getLogger(__name__)

# Map modifier keywords to their corresponding prompts
MODIFIER_PROMPT_MAP = {
    "Shorter": MESSAGE_MODIFIER_PROMPT_SHORTER,
    "Longer": MESSAGE_MODIFIER_PROMPT_LONGER,
    "Simpler": MESSAGE_MODIFIER_PROMPT_SIMPLER,
    "Professional": MESSAGE_MODIFIER_PROMPT_PROFESSIONAL,
    "Casual": MESSAGE_MODIFIER_PROMPT_CASUAL,
}


async def modify_message(
    message: str,
    modifier: Literal["Shorter", "Longer", "Simpler", "Professional", "Casual"],
    max_length: Optional[int] = None,
) -> str:
    """
    Modify a message based on the specified modifier.

    Args:
        message: The original message to be modified
        modifier: One of "Shorter", "Longer", "Simpler", "Professional", or "Casual"

    Returns:
        The modified message, or the original message if modification fails
    """
    if not message or not message.strip():
        logger.warning("Empty message provided for modification")
        return message

    prompt = MODIFIER_PROMPT_MAP.get(modifier)
    if not prompt:
        logger.error(
            f"Invalid modifier: {modifier}. Valid options are: Shorter, Longer, Simpler, Professional, Casual"
        )
        return message

    has_limit = isinstance(max_length, int) and max_length > 0

    length_instruction = (
        f"\n\nCharacter limit: The modified message MUST NOT exceed {max_length} characters "
        "(including spaces and punctuation). Adapt the content to fit within this limit "
        "while still honouring the modifier intent. Do not truncate mid-sentence — finish naturally within the limit."
        if has_limit
        else ""
    )
    full_prompt = prompt + length_instruction

    try:
        response = await invoke(
            messages=[
                {"role": "system", "content": full_prompt},
                {"role": "user", "content": f"Message to modify:\n\n{message}"},
            ],
            model="openai/gpt-4.1",
            temperature=0.3,
            fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4o"],
            verbose=False,
        )

        if not response or not response.strip():
            logger.warning(
                f"Empty response from LLM for modifier '{modifier}', returning original message"
            )
            return message

        modified_message = response.strip()

        # Hard-enforce the character limit: if the model still exceeded it,
        # ask it once more explicitly, then fall back to truncating at a word boundary.
        if has_limit and len(modified_message) > max_length:
            logger.warning(
                f"LLM output exceeded limit ({len(modified_message)} > {max_length}), retrying with stricter prompt"
            )
            retry_prompt = (
                f"Rewrite the following message so it is AT MOST {max_length} characters "
                "(including spaces and punctuation). End at a natural sentence boundary. "
                "Return ONLY the rewritten message, no explanations.\n\n"
                f"Message:\n{modified_message}"
            )
            try:
                retry_response = await invoke(
                    messages=[{"role": "user", "content": retry_prompt}],
                    model="openai/gpt-4.1",
                    temperature=0.2,
                    fallbacks=["anthropic/claude-sonnet-4-5-20250929", "openai/gpt-4o"],
                    verbose=False,
                )
                if retry_response and retry_response.strip():
                    modified_message = retry_response.strip()
            except Exception as retry_err:
                logger.warning(f"Retry for length limit failed: {retry_err}")

            # Last-resort: truncate at the last complete word within the limit.
            if len(modified_message) > max_length:
                truncated = (
                    modified_message[:max_length].rsplit(" ", 1)[0].rstrip(" .,;:-")
                )
                modified_message = truncated

        logger.info(f"Successfully modified message with '{modifier}' modifier")
        return modified_message

    except Exception as e:
        logger.error(f"Error modifying message with '{modifier}': {e}")
        return message
