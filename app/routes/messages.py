from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.models.schemas.messages import (
    MessageInput,
    MessageOutput,
    OpportunityMessageInput,
    OpportunityMessageOutput,
)
from app.routes.dependancies import get_es_client_with_compression
from app.utils.connection_messages.messages import main
from app.utils.connection_messages.opportunity_messages import (
    generate_opportunity_messages,
)
from app.utils.profile_fetching.public_identifier_fetch import (
    fetch_profile_by_es_id,
    fetch_profile_by_public_identifier,
)

router = APIRouter()


@router.post("/linkedin_connection_message", response_model=MessageOutput)
async def linkedin_message(
    input_payload: MessageInput,
    es_client=Depends(get_es_client_with_compression),
):
    payload = input_payload.payload
    channel = input_payload.channel
    max_length = input_payload.max_length
    recipient = input_payload.recipient
    sender_name = None
    if recipient and recipient.get("profileId"):
        try:
            _, recipient_profile = await fetch_profile_by_es_id(
                recipient.get("profileId"),
                es_client=es_client,
                source_fields=[
                    "public_identifier",
                    "full_name",
                    "name",
                    "headline",
                    "experience",
                ],
                only_top_experience=False,
            )
            if recipient_profile and isinstance(recipient_profile, dict):
                recipient.setdefault(
                    "fullName",
                    recipient_profile.get("full_name") or recipient_profile.get("name"),
                )
                recipient.setdefault(
                    "publicIdentifier",
                    recipient_profile.get("public_identifier"),
                )
                recipient.setdefault("title", recipient_profile.get("headline"))
                experiences = recipient_profile.get("experience") or []
                if isinstance(experiences, list) and experiences:
                    current = next(
                        (
                            exp
                            for exp in experiences
                            if isinstance(exp, dict) and not exp.get("end")
                        ),
                        experiences[0],
                    )
                    if isinstance(current, dict):
                        recipient.setdefault("currentTitle", current.get("title"))
        except Exception:
            pass
    if input_payload.reference_profile_identifier:
        try:
            _, profile = await fetch_profile_by_public_identifier(
                input_payload.reference_profile_identifier,
                es_client=es_client,
                source_fields=None,
                only_top_experience=False,
            )
            if profile and isinstance(profile, dict):
                sender_name = profile.get("full_name") or profile.get("name") or None
        except Exception:
            pass

    try:
        response = await main(
            payload,
            channel=channel,
            recipient=recipient,
            sender_name=sender_name,
            max_length=max_length,
        )
        return JSONResponse(response)
    except Exception as e:

        return JSONResponse(content={"message": "Error Sceness"}, status_code=500)


@router.post("/opportunity_message", response_model=OpportunityMessageOutput)
async def opportunity_message(
    input_payload: OpportunityMessageInput,
    es_client=Depends(get_es_client_with_compression),
):
    reference_public_identifier = input_payload.reference_public_identifier.strip()
    related_person_public_identifier = (
        input_payload.related_person_public_identifier.strip()
    )

    reference_es_id = input_payload.reference_es_id
    reference_profile = None
    related_person_es_id = None
    related_person_profile = None

    if reference_es_id:
        try:
            _, reference_profile = await fetch_profile_by_es_id(
                reference_es_id,
                es_client=es_client,
                source_fields=None,
                only_top_experience=False,
            )
        except Exception:
            reference_profile = None

    if reference_profile is None:
        try:
            fetched_reference_es_id, reference_profile = (
                await fetch_profile_by_public_identifier(
                    reference_public_identifier,
                    es_client=es_client,
                    source_fields=None,
                    only_top_experience=False,
                )
            )
            reference_es_id = reference_es_id or fetched_reference_es_id
        except Exception:
            pass

    try:
        related_person_es_id, related_person_profile = (
            await fetch_profile_by_public_identifier(
                related_person_public_identifier,
                es_client=es_client,
                source_fields=None,
                only_top_experience=False,
            )
        )
    except Exception:
        pass

    try:
        result = await generate_opportunity_messages(
            reference_public_identifier=reference_public_identifier,
            reference_es_id=reference_es_id,
            reference_profile=reference_profile,
            related_person_public_identifier=related_person_public_identifier,
            related_person_profile=related_person_profile,
            opportunity=input_payload.opportunity,
            channel=input_payload.channel,
            tones=list(input_payload.tones) if input_payload.tones else None,
            max_length=input_payload.max_length,
        )
        return OpportunityMessageOutput(
            reference_public_identifier=reference_public_identifier,
            reference_es_id=reference_es_id,
            related_person_public_identifier=related_person_public_identifier,
            related_person_es_id=related_person_es_id,
            channel=input_payload.channel,
            messages=result["messages"],
            subject_line=result.get("subject_line"),
        )
    except Exception:
        return JSONResponse(
            content={"message": "Failed to generate opportunity message"},
            status_code=500,
        )
