import logging
import traceback
import json
import re
import time
import asyncio
from typing import Any, List

from fastapi import APIRouter, Body, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from app.models.schemas.me_assignment_chat import (
    AssignmentChatInput,
    AssignmentChatInitInput,
    AssignmentChatInitOutput,
    AssignmentChatOutput,
    AssignmentPeopleInvolvedAddInput,
    AssignmentChatStateOutput,
    FEATURE_KEY_ASSIGNMENT_CREATION,
    FEATURE_KEY_CONTENT_CHAT,
)
from app.routes.dependancies import get_es_client_with_compression
from app.utils.me_assignment_chat.service import (
    add_people_involved_to_assignment,
    build_lookalike_companies_opportunity,
    fetch_assignment_chat_assignments,
    fetch_assignment_chat_state,
    initialize_assignment_chat,
    process_assignment_chat_turn,
    process_content_chat_turn,
    review_assignment_payload,
    stream_assignment_chat_turn,
    LIVE_PREVIEW_TIMING_DEBUG,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_STREAM_TEXT_CHUNK_SIZE = 80
_STREAM_REASONING_CHUNK_SIZE = 18


def _stream_reasoning_text_chunks(text: str, *, max_size: int) -> List[str]:
    """Split reasoning text into small word-aware chunks for a live stream feel."""
    message = str(text or "").strip()
    if not message:
        return []
    words = message.split()
    chunks: List[str] = []
    buf = ""
    for word in words:
        candidate = f"{buf} {word}".strip() if buf else word
        if len(candidate) <= max_size:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if len(word) <= max_size:
            buf = word
        else:
            for i in range(0, len(word), max_size):
                chunks.append(word[i : i + max_size])
            buf = ""
    if buf:
        chunks.append(buf)
    return chunks


# Init greeting always uses response_id 1. Chat turn phases allocate from 2 upward.
RESPONSE_ID_INIT_TEXT = 1
STREAM_RESPONSE_ID_START = 2


class _StreamResponseIdAllocator:
    """Allocate monotonic response_id values for one chat stream turn."""

    def __init__(self, start: int = STREAM_RESPONSE_ID_START) -> None:
        self._next = start
        self._reasoning_para_ids: dict[int, int] = {}

    def reasoning(self, paragraph_index: int) -> int:
        if paragraph_index not in self._reasoning_para_ids:
            self._reasoning_para_ids[paragraph_index] = self._next
            self._next += 1
        return self._reasoning_para_ids[paragraph_index]

    def take(self) -> int:
        response_id = self._next
        self._next += 1
        return response_id


def _assignment_stream_text_payloads(text: str, *, response_id: int):
    """Emit assignment chat text in the original SSE shape (type=text only)."""
    message = str(text or "")
    if message == "":
        return
    for i in range(0, len(message), _STREAM_TEXT_CHUNK_SIZE):
        yield {
            "type": "text",
            "data": {
                "response_id": response_id,
                "text": message[i : i + _STREAM_TEXT_CHUNK_SIZE],
            },
        }


def _assignment_stream_reasoning_payloads(text: str, *, response_id: int):
    """Emit assignment chat reasoning in SSE shape (type=reasoning)."""
    for chunk in _stream_reasoning_text_chunks(
        text, max_size=_STREAM_REASONING_CHUNK_SIZE
    ):
        yield {
            "type": "reasoning",
            "data": {
                "response_id": response_id,
                "text": chunk if chunk.endswith(" ") else f"{chunk} ",
            },
        }


@router.post("/me/bd_assignment/review")
async def me_assignment_review(
    payload: Any = Body(...),
    es_client=Depends(get_es_client_with_compression),
):
    """
    Accept any assignment JSON and return a synthesized review with key sections.
    """
    try:
        result = await review_assignment_payload(payload, es_client=es_client)
        return JSONResponse(content=jsonable_encoder(result))
    except Exception as e:
        logger.error("Unhandled exception in /me/bd_assignment/review: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.post("/me/bd_assignment/lookalike_companies")
async def me_assignment_lookalike_companies(
    payload: Any = Body(...),
    es_client=Depends(get_es_client_with_compression),
):
    """
    FS sends assignment_id; return lookalike card with type, title, action, companies.
    """
    try:
        if isinstance(payload, dict):
            assignment_id = payload.get("assignment_id") or payload.get("entity_id")
        else:
            assignment_id = None

        result = await build_lookalike_companies_opportunity(
            assignment_id or "",
            es_client=es_client,
        )
        if result.get("found") is False:
            status_code = 404 if result.get("error") == "Assignment not found" else 400
            return JSONResponse(
                content=jsonable_encoder(result), status_code=status_code
            )
        return JSONResponse(content=jsonable_encoder(result))
    except Exception as e:
        logger.error(
            "Unhandled exception in /me/bd_assignment/lookalike_companies: %s", str(e)
        )
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.post("/me/bd_assignment/people_involved/add")
async def me_assignment_add_people_involved(
    request: AssignmentPeopleInvolvedAddInput,
    es_client=Depends(get_es_client_with_compression),
):
    """
    Add user-selected people to an assignment without going through chat.
    """
    try:
        logger.info(
            "Incoming /me/bd_assignment/people_involved/add payload=%s",
            request.model_dump(mode="json"),
        )
        result = await add_people_involved_to_assignment(
            assignment_id=request.assignment_id.strip(),
            people=[p.model_dump(mode="json") for p in request.people],
            public_identifiers=list(request.public_identifiers or []),
            es_client=es_client,
        )
        if result.get("found") is False:
            return JSONResponse(
                content=jsonable_encoder(result),
                status_code=404,
            )
        return JSONResponse(content=jsonable_encoder(result))
    except ValueError as e:
        return JSONResponse(
            content={"error": str(e)},
            status_code=400,
        )
    except Exception as e:
        logger.error(
            "Unhandled exception in /me/bd_assignment/people_involved/add: %s",
            str(e),
        )
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(
            content={
                "error": "Internal Server Error",
                "detail": str(e),
            },
            status_code=500,
        )


@router.post("/me/bd_assignment/chat/init", response_model=AssignmentChatInitOutput)
async def me_assignment_chat_init(
    request: AssignmentChatInitInput,
    es_client=Depends(get_es_client_with_compression),
):
    try:
        logger.info(
            "Incoming /me/bd_assignment/chat/init payload=%s",
            request.model_dump(mode="json"),
        )
        if not (request.entity_id and request.entity_id.strip()):
            logger.warning(
                "Bad request /me/bd_assignment/chat/init: missing or empty entity_id for entity_urn=%s payload=%s",
                request.entity_urn,
                request.model_dump(mode="json"),
            )
            return JSONResponse(
                content={"error": "entityId is required for assignment chat init"},
                status_code=400,
            )

        result = await initialize_assignment_chat(
            entity_urn=request.entity_urn.strip(),
            feature_key=(
                FEATURE_KEY_CONTENT_CHAT
                if (request.chat_type or "").lower() == "content"
                else FEATURE_KEY_ASSIGNMENT_CREATION
            ),
            es_client=es_client,
            client_context=None,
            external_entity_id=(request.entity_id or "").strip() or None,
            chat_type=request.chat_type or "assignment",
        )
        return JSONResponse(content=jsonable_encoder(result))
    except Exception as e:
        logger.error("Unhandled exception in /me/bd_assignment/chat/init: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(
            content={
                "error": "Internal Server Error",
                "detail": str(e),
            },
            status_code=500,
        )


@router.post("/me/bd_assignment/chat/init/stream")
async def me_assignment_chat_init_stream(
    request: AssignmentChatInitInput,
    es_client=Depends(get_es_client_with_compression),
):
    logger.info(
        "Incoming /me/bd_assignment/chat/init/stream payload=%s",
        request.model_dump(mode="json"),
    )
    if not (request.entity_id and request.entity_id.strip()):
        logger.warning(
            "Bad request /me/bd_assignment/chat/init/stream: missing or empty entity_id for entity_urn=%s payload=%s",
            request.entity_urn,
            request.model_dump(mode="json"),
        )
        return JSONResponse(
            content={"error": "entityId is required for assignment chat init"},
            status_code=400,
        )

    async def _generator():
        # Emit an immediate heartbeat so clients receive the first byte
        # before DB/LLM work completes.
        yield ": stream-start\n\n"
        try:
            is_content = (request.chat_type or "").lower() == "content"
            result = await initialize_assignment_chat(
                entity_urn=request.entity_urn.strip(),
                feature_key=(
                    FEATURE_KEY_CONTENT_CHAT
                    if is_content
                    else FEATURE_KEY_ASSIGNMENT_CREATION
                ),
                es_client=es_client,
                client_context=None,
                external_entity_id=(request.entity_id or "").strip() or None,
                chat_type=request.chat_type or "assignment",
            )
            result = jsonable_encoder(result)

            greeting_message = str(result.get("greeting_message") or "")
            # Preserve raw greeting formatting (\n, \t, spacing) for frontend rendering.
            if greeting_message == "":
                chunks = [""]
            else:
                chunk_size = 80
                chunks = [
                    greeting_message[i : i + chunk_size]
                    for i in range(0, len(greeting_message), chunk_size)
                ]
            for chunk in chunks:
                payload = {
                    "type": "text",
                    "data": {
                        "response_id": RESPONSE_ID_INIT_TEXT,
                        "text": chunk,
                    },
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(
                "Unhandled exception in /me/bd_assignment/chat/init/stream: %s",
                str(e),
            )
            logger.error("Traceback:\n%s", traceback.format_exc())
            err_payload = {
                "type": "error",
                "message": "Internal Server Error",
                "detail": str(e),
            }
            yield f"data: {json.dumps(err_payload)}\n\n"

    return StreamingResponse(_generator(), media_type="text/event-stream")


@router.post("/me/bd_assignment/chat", response_model=AssignmentChatOutput)
async def me_assignment_chat(
    request: AssignmentChatInput,
    es_client=Depends(get_es_client_with_compression),
):
    try:
        logger.info(
            "Incoming /me/bd_assignment/chat payload=%s",
            request.model_dump(mode="json"),
        )
        if not (request.entity_id and request.entity_id.strip()):
            logger.warning(
                "Bad request /me/bd_assignment/chat: missing or empty entity_id for entity_urn=%s payload=%s",
                request.entity_urn,
                request.model_dump(mode="json"),
            )
            return JSONResponse(
                content={"error": "entityId is required for assignment/content chat"},
                status_code=400,
            )

        result = await process_assignment_chat_turn(
            entity_urn=request.entity_urn.strip(),
            message=request.message.strip(),
            update=request.update.model_dump() if request.update else None,
            feature_key=FEATURE_KEY_ASSIGNMENT_CREATION,
            es_client=es_client,
            client_context=request.client_context,
            external_entity_id=(request.entity_id or "").strip() or None,
        )
        return JSONResponse(content=jsonable_encoder(result))
    except Exception as e:
        logger.error("Unhandled exception in /me/bd_assignment/chat: %s", str(e))
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(
            content={
                "error": "Internal Server Error",
                "detail": str(e),
            },
            status_code=500,
        )


@router.post("/me/bd_assignment/chat/stream")
async def me_assignment_chat_stream(
    request: AssignmentChatInput,
    es_client=Depends(get_es_client_with_compression),
):
    logger.info(
        "Incoming /me/bd_assignment/chat/stream payload=%s",
        request.model_dump(mode="json"),
    )
    if not (request.entity_id and request.entity_id.strip()):
        logger.warning(
            "Bad request /me/bd_assignment/chat/stream: missing or empty entity_id for entity_urn=%s payload=%s",
            request.entity_urn,
            request.model_dump(mode="json"),
        )
        return JSONResponse(
            content={"error": "entityId is required for assignment/content chat"},
            status_code=400,
        )

    chat_type = (request.chat_type or "assignment").lower()

    async def _generator():
        # Emit an immediate heartbeat so clients receive the first byte
        # before DB/LLM work completes.
        yield ": stream-start\n\n"
        try:
            if chat_type == "content":
                # Content chat: route to content agent (non-streaming LLM for now).
                result = await process_content_chat_turn(
                    entity_urn=request.entity_urn.strip(),
                    message=request.message.strip(),
                    update=request.update.model_dump() if request.update else None,
                    feature_key=FEATURE_KEY_CONTENT_CHAT,
                    es_client=es_client,
                    client_context=request.client_context,
                    external_entity_id=(request.entity_id or "").strip() or None,
                )
                result = jsonable_encoder(result or {})
                response_ids = _StreamResponseIdAllocator()

                assistant_message = str(result.get("assistant_message") or "")
                if assistant_message:
                    for payload in _assignment_stream_text_payloads(
                        assistant_message,
                        response_id=response_ids.take(),
                    ):
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                content_draft = result.get("content_draft") or {}
                content_points = [
                    {"key": str(k), "value": str(v).strip()}
                    for k, v in content_draft.items()
                    if v is not None and str(v).strip()
                ]
                live_preview_payload = {
                    "type": "live_preview",
                    "data": {
                        "response_id": response_ids.take(),
                        "chat_type": "content",
                        "content_points": content_points,
                    },
                }
                yield f"data: {json.dumps(live_preview_payload, ensure_ascii=False)}\n\n"
            else:
                # Default: assignment chat flow (existing behavior).
                result = None
                stream_started = time.perf_counter()
                response_ids = _StreamResponseIdAllocator()
                async for event in stream_assignment_chat_turn(
                    entity_urn=request.entity_urn.strip(),
                    message=request.message.strip(),
                    update=request.update.model_dump() if request.update else None,
                    feature_key=FEATURE_KEY_ASSIGNMENT_CREATION,
                    es_client=es_client,
                    client_context=request.client_context,
                    external_entity_id=(request.entity_id or "").strip() or None,
                ):
                    if event.get("type") == "reasoning_delta":
                        text_chunk = str(event.get("text") or "")
                        paragraph_index = int(event.get("paragraph_index") or 0)
                        response_id = response_ids.reasoning(paragraph_index)
                        for payload in _assignment_stream_reasoning_payloads(
                            text_chunk,
                            response_id=response_id,
                        ):
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            await asyncio.sleep(0)
                    elif event.get("type") == "assistant_final":
                        final_text = str(event.get("text") or "")
                        for payload in _assignment_stream_text_payloads(
                            final_text,
                            response_id=response_ids.take(),
                        ):
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "result":
                        result = jsonable_encoder(event.get("result") or {})

                if result is None:
                    raise ValueError("Streaming result missing final payload")

                assignment_draft = result.get("assignment_draft") or {}
                assignment_points = [
                    {"key": str(k), "value": str(v).strip()}
                    for k, v in assignment_draft.items()
                    if v is not None and str(v).strip()
                ]
                live_preview_payload = {
                    "type": "live_preview",
                    "data": {
                        "response_id": response_ids.take(),
                        "assignment_points": assignment_points,
                        "missing_fields": result.get("missing_fields") or [],
                    },
                }
                if LIVE_PREVIEW_TIMING_DEBUG:
                    logger.info(
                        "[live_preview_timing] label=sse_emit phase=live_preview_sse total_sse_ms=%.1f",
                        (time.perf_counter() - stream_started) * 1000.0,
                    )
                yield f"data: {json.dumps(live_preview_payload, ensure_ascii=False)}\n\n"

                progress_payload = {
                    "type": "progress",
                    "data": {
                        "response_id": response_ids.take(),
                        "completion_score": result.get("completion_score"),
                        "ready_to_save": result.get("ready_to_save"),
                    },
                }
                yield f"data: {json.dumps(progress_payload, ensure_ascii=False)}\n\n"

                if result.get("show_add_people_cta"):
                    add_people_payload = {
                        "type": "show_add_people_cta",
                        "data": {
                            "response_id": response_ids.take(),
                            "assignment_id": result.get("assignment_feature_key"),
                            "field": "people_involved",
                            "title": "Add people involved",
                            "instruction": (
                                "Enter each person's name and current company, "
                                "or select people manually so we can map them."
                            ),
                        },
                    }
                    yield f"data: {json.dumps(add_people_payload, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(
                "Unhandled exception in /me/bd_assignment/chat/stream: %s", str(e)
            )
            logger.error("Traceback:\n%s", traceback.format_exc())
            err_payload = {
                "type": "error",
                "message": "Internal Server Error",
                "detail": str(e),
            }
            yield f"data: {json.dumps(err_payload)}\n\n"

    return StreamingResponse(_generator(), media_type="text/event-stream")


@router.get("/me/bd_assignment/chat/{entity_urn}/assignments")
async def me_assignment_chat_assignments(entity_urn: str):
    try:
        result = await fetch_assignment_chat_assignments(
            entity_urn=entity_urn.strip(),
            feature_key=FEATURE_KEY_ASSIGNMENT_CREATION,
        )
        return JSONResponse(content=jsonable_encoder(result))
    except Exception as e:
        logger.error(
            "Unhandled exception in /me/bd_assignment/chat/{entity_urn}/assignments: %s",
            str(e),
        )
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)


@router.get(
    "/me/bd_assignment/chat/{entity_urn}",
    response_model=AssignmentChatStateOutput,
)
async def me_assignment_chat_state(entity_urn: str):
    try:
        state = await fetch_assignment_chat_state(
            entity_urn=entity_urn.strip(),
            feature_key=FEATURE_KEY_ASSIGNMENT_CREATION,
        )
        return JSONResponse(content=jsonable_encoder(state))
    except Exception as e:
        logger.error(
            "Unhandled exception in /me/bd_assignment/chat/{entity_urn}: %s",
            str(e),
        )
        logger.error("Traceback:\n%s", traceback.format_exc())
        return JSONResponse(content={"error": "Internal Server Error"}, status_code=500)
