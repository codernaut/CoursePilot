"""Chat API: the SSE endpoint, conversations and message feedback (private to the caller)."""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from app.agent.runtime import AgentRuntime
from app.api.deps import CurrentUser, DatabaseDep, DbDep, SettingsDep
from app.llm.factory import ProviderNotConfigured
from app.retrieval.query_embedding import QueryEmbedder
from app.schemas.chat import (
    ChatRequest,
    ConversationDetail,
    ConversationOut,
    FeedbackIn,
    MessageOut,
)
from app.services import chat as chat_service
from app.services.chat import SseEvent, TurnRegistry

router = APIRouter(tags=["chat"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_runtime(request: Request) -> AgentRuntime:
    runtime: AgentRuntime = request.app.state.agent_runtime
    return runtime


def get_turns(request: Request) -> TurnRegistry:
    turns: TurnRegistry = request.app.state.chat_turns
    return turns


def format_sse(event: str, data: dict[str, Any]) -> bytes:
    """One server-sent event. ``data`` is one JSON line, so it can never break the framing."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _single(event: SseEvent) -> AsyncIterator[bytes]:
    yield format_sse(*event)


async def _frames(
    events: AsyncGenerator[SseEvent, None], turns: TurnRegistry, user_id: str
) -> AsyncIterator[bytes]:
    try:
        async for event in events:
            yield format_sse(*event)
    finally:
        await events.aclose()
        turns.release(user_id)  # also covers a generator that never started


@router.post(
    "/chat",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"},
        404: {"description": "Unknown module, or a conversation that is not yours"},
        429: {"description": "Daily quota used up, or another turn of yours is still running"},
    },
)
async def chat(
    body: ChatRequest,
    request: Request,
    db: DbDep,
    database: DatabaseDep,
    settings: SettingsDep,
    ctx: CurrentUser,
) -> StreamingResponse:
    """Ask a question about one module (``module_id``) or all material (``null``). The response
    is a stream of events: ``status`` (progress text), ``token`` (answer text), ``citations`` (the
    cited sources and the validated answer text), ``done`` (message id, usage) or ``error``
    (``code``, ``message``). The conversation id is also in the ``X-Conversation-Id`` header."""
    runtime = get_runtime(request)
    turns = get_turns(request)
    try:
        prepared = await chat_service.prepare_turn(
            db,
            user=ctx.user,
            settings=settings,
            database=database,
            runtime=runtime,
            embedder=_embedder(request),
            turns=turns,
            message=body.message,
            module_id=body.module_id,
            conversation_id=body.conversation_id,
            web=request.app.state.web_search,
        )
    except chat_service.AgentUnavailable:
        return StreamingResponse(
            _single(
                chat_service.error_event("agent_error", chat_service.AGENT_UNAVAILABLE_MESSAGE)
            ),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    except ProviderNotConfigured as exc:
        event = chat_service.provider_error_event(exc, is_admin=ctx.user.role.value == "admin")
        return StreamingResponse(
            _single(event), media_type="text/event-stream", headers=SSE_HEADERS
        )
    events = chat_service.run_turn(database, runtime, settings, turns, prepared)
    return StreamingResponse(
        _frames(events, turns, prepared.user_id),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Conversation-Id": prepared.conversation_id},
    )


def _embedder(request: Request) -> QueryEmbedder:
    embedder: QueryEmbedder = request.app.state.query_embedder
    return embedder


def _conversation_out(view: chat_service.ConversationView) -> ConversationOut:
    c = view.conversation
    return ConversationOut(
        id=c.id,
        title=c.title,
        module_id=c.module_id,
        module_path=view.module_path,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(db: DbDep, ctx: CurrentUser) -> list[ConversationOut]:
    return [_conversation_out(v) for v in await chat_service.list_conversations(db, ctx.user)]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str, db: DbDep, ctx: CurrentUser) -> ConversationDetail:
    view, messages = await chat_service.get_conversation(db, ctx.user, conversation_id)
    return ConversationDetail(
        **_conversation_out(view).model_dump(),
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str, request: Request, db: DbDep, ctx: CurrentUser
) -> Response:
    thread_id = await chat_service.delete_conversation(db, ctx.user, conversation_id)
    await get_runtime(request).delete_thread(thread_id)
    return Response(status_code=204)


@router.post("/messages/{message_id}/feedback", response_model=MessageOut)
async def message_feedback(
    message_id: str, body: FeedbackIn, db: DbDep, ctx: CurrentUser
) -> MessageOut:
    message = await chat_service.set_feedback(db, ctx.user, message_id, body.feedback, body.comment)
    return MessageOut.model_validate(message)
