"""Chat page: module picker, conversation sidebar and the streaming conversation (chat.js)."""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from app.api.chat import get_runtime
from app.api.deps import CurrentUser, DbDep
from app.core.errors import PROVIDER_NOT_CONFIGURED_MESSAGE
from app.models import Message
from app.models.enums import Role
from app.retrieval.query_embedding import QueryEmbedder
from app.services import chat as chat_service
from app.services import structure
from app.web.common import render

router = APIRouter(include_in_schema=False)


async def _page(
    request: Request, db: DbDep, ctx: CurrentUser, conversation_id: str | None, module_id: str
) -> Response:
    embedder: QueryEmbedder = request.app.state.query_embedder
    problem = chat_service.provider_problem(get_runtime(request), embedder)
    is_admin = ctx.user.role == Role.ADMIN
    provider_message = None
    if problem == chat_service.AGENT_UNAVAILABLE_MESSAGE:
        provider_message = problem
    elif problem is not None:
        provider_message = (
            f"AI provider not configured: {problem}"
            if is_admin
            else PROVIDER_NOT_CONFIGURED_MESSAGE
        )
    current: chat_service.ConversationView | None = None
    messages: list[Message] = []
    if conversation_id:
        current, messages = await chat_service.get_conversation(db, ctx.user, conversation_id)
    conversations = await chat_service.list_conversations(db, ctx.user)
    selected = current.conversation.module_id if current else (module_id or None)
    return render(
        request,
        "chat.html",
        {
            "years": await structure.get_tree(db),
            "conversations": conversations,
            "current": current,
            "messages": messages,
            "module_id": selected or "",
            "provider_message": provider_message,
            "max_chars": 4000,
        },
    )


@router.get("/chat")
async def chat_new(
    request: Request,
    db: DbDep,
    ctx: CurrentUser,
    module_id: Annotated[str, Query(max_length=64)] = "",
) -> Response:
    return await _page(request, db, ctx, None, module_id)


@router.get("/chat/{conversation_id}")
async def chat_existing(
    conversation_id: str, request: Request, db: DbDep, ctx: CurrentUser
) -> Response:
    return await _page(request, db, ctx, conversation_id, "")
