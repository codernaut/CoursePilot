"""Chat business logic: conversations, quota, the one-running-turn rule and the streamed turn.

No FastAPI imports. Conversations and their messages are private to their owner: every lookup
filters on ``user_id`` and answers 404 otherwise, for every role (admins included).

A turn is: validate and persist the user message (short transaction, committed), run the agent
graph with NO database transaction open, then persist the assistant message in a new short
transaction. On an error, a timeout or a client disconnect no assistant message is stored.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import PROMPT_VERSION
from app.agent.runtime import AgentRuntime, TurnResult
from app.agent.state import AgentContext
from app.core.config import Settings
from app.core.errors import (
    PROVIDER_NOT_CONFIGURED_MESSAGE,
    AppError,
    not_found,
    validation_error,
)
from app.core.logging import get_logger
from app.db.base import new_id, utcnow
from app.db.session import Database
from app.ingestion.embedder import EmbeddingError, EmbeddingNotConfiguredError
from app.llm.factory import ProviderNotConfigured
from app.models import Block, Conversation, Message, Module, User, Year
from app.models.enums import Feedback, MessageRole, Role
from app.retrieval.access import AccessContext
from app.retrieval.hybrid import PATH_SEPARATOR, SearchScope
from app.retrieval.query_embedding import QueryEmbedder
from app.schemas.chat import MAX_MESSAGE_CHARS

log = get_logger(__name__)

TITLE_CHARS = 60
TURN_HOLD_PADDING_S = 30.0  # a lost release frees itself after the agent timeout plus this
LIST_LIMIT = 200

SseEvent = tuple[str, dict[str, Any]]


# -- one running turn per user ------------------------------------------------------------


class TurnRegistry:
    """At most one running chat turn per user (in this process, like the login throttle). A hold
    expires on its own so a request cancelled before its generator ever ran cannot lock a user
    out for good."""

    def __init__(self, hold_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._hold_s = hold_s
        self._clock = clock
        self._active: dict[str, float] = {}

    def acquire(self, user_id: str) -> bool:
        now = self._clock()
        expiry = self._active.get(user_id)
        if expiry is not None and expiry > now:
            return False
        self._active[user_id] = now + self._hold_s
        return True

    def release(self, user_id: str) -> None:
        self._active.pop(user_id, None)

    def is_active(self, user_id: str) -> bool:
        expiry = self._active.get(user_id)
        return expiry is not None and expiry > self._clock()


def make_turn_registry(settings: Settings) -> TurnRegistry:
    return TurnRegistry(settings.agent_timeout_s + TURN_HOLD_PADDING_S)


# -- conversations ------------------------------------------------------------


@dataclass(frozen=True)
class ConversationView:
    conversation: Conversation
    module_path: str | None


async def module_paths(db: AsyncSession, module_ids: set[str]) -> dict[str, str]:
    if not module_ids:
        return {}
    rows = (
        await db.execute(
            select(Module.id, Year.title, Block.title, Module.title)
            .join(Block, Block.id == Module.block_id)
            .join(Year, Year.id == Block.year_id)
            .where(Module.id.in_(module_ids))
        )
    ).all()
    return {mid: PATH_SEPARATOR.join((y, b, m)) for mid, y, b, m in rows}


async def list_conversations(db: AsyncSession, user: User) -> list[ConversationView]:
    rows = list(
        (
            await db.execute(
                select(Conversation)
                .where(Conversation.user_id == user.id)
                .order_by(Conversation.updated_at.desc(), Conversation.id)
                .limit(LIST_LIMIT)
            )
        ).scalars()
    )
    paths = await module_paths(db, {c.module_id for c in rows if c.module_id})
    return [ConversationView(c, paths.get(c.module_id or "")) for c in rows]


async def get_owned_conversation(
    db: AsyncSession, user: User, conversation_id: str
) -> Conversation:
    """The caller's conversation, else 404 (also for someone else's, whatever the role)."""
    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise not_found("Conversation not found.")
    return conversation


async def get_conversation(
    db: AsyncSession, user: User, conversation_id: str
) -> tuple[ConversationView, list[Message]]:
    conversation = await get_owned_conversation(db, user, conversation_id)
    messages = list(
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at, Message.role.desc())
            )
        ).scalars()
    )
    paths = await module_paths(db, {conversation.module_id} if conversation.module_id else set())
    return ConversationView(conversation, paths.get(conversation.module_id or "")), messages


async def delete_conversation(db: AsyncSession, user: User, conversation_id: str) -> str:
    """Delete the conversation and its messages. Returns the checkpoint thread id the caller must
    delete after the commit."""
    conversation = await get_owned_conversation(db, user, conversation_id)
    thread_id = conversation.thread_id
    messages = (
        await db.execute(select(Message).where(Message.conversation_id == conversation.id))
    ).scalars()
    for message in messages:
        await db.delete(message)
    await db.flush()
    await db.delete(conversation)
    await db.commit()
    return thread_id


async def set_feedback(
    db: AsyncSession, user: User, message_id: str, feedback: str | None, comment: str | None
) -> Message:
    """Thumbs up/down (or ``None`` to clear) on one of the caller's own assistant messages."""
    message = (
        await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Conversation.user_id == user.id,
                Message.role == MessageRole.ASSISTANT,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise not_found("Message not found.")
    message.feedback = Feedback(feedback) if feedback else None
    cleaned = " ".join((comment or "").split()) if feedback else ""
    message.feedback_comment = cleaned or None
    await db.commit()
    return message


# -- quota ------------------------------------------------------------


async def messages_today(db: AsyncSession, user: User) -> int:
    """The user's messages sent since 00:00 UTC (counted from the ``messages`` rows)."""
    now = utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.user_id == user.id,
                    Message.role == MessageRole.USER,
                    Message.created_at >= start,
                    Message.created_at < start + timedelta(days=1),
                )
            )
        ).scalar_one()
    )


# -- preparing a turn ------------------------------------------------------------


@dataclass(frozen=True)
class PreparedTurn:
    conversation_id: str
    thread_id: str
    user_message_id: str
    question: str
    context: AgentContext
    is_admin: bool
    user_id: str


def clean_message(message: str) -> str:
    cleaned = "".join(c for c in message if c.isprintable() or c in "\n\t").strip()
    if not cleaned:
        raise validation_error("The message must not be empty.")
    if len(cleaned) > MAX_MESSAGE_CHARS:
        raise validation_error(f"The message is too long (at most {MAX_MESSAGE_CHARS} characters).")
    return cleaned


def title_from(message: str) -> str:
    flat = " ".join(message.split())
    return flat if len(flat) <= TITLE_CHARS else flat[: TITLE_CHARS - 1].rstrip() + "…"


async def prepare_turn(
    db: AsyncSession,
    *,
    user: User,
    settings: Settings,
    database: Database,
    runtime: AgentRuntime,
    embedder: QueryEmbedder,
    turns: TurnRegistry,
    message: str,
    module_id: str | None,
    conversation_id: str | None,
) -> PreparedTurn:
    """Validate, check quota and providers, take the per-user turn slot and persist the user
    message (committed). Raises ``AppError`` (404/409/422/429) or ``ProviderNotConfigured``; in
    every failure case no slot is held and nothing was stored. The caller must pass the result to
    ``run_turn``, which releases the slot."""
    question = clean_message(message)

    conversation: Conversation | None = None
    if conversation_id:
        conversation = await get_owned_conversation(db, user, conversation_id)
        if module_id and module_id != conversation.module_id:
            raise validation_error("This conversation belongs to a different module.")
        module_id = conversation.module_id
    module: Module | None = None
    if module_id:
        module = await db.get(Module, module_id)
        if module is None:
            raise not_found("Module not found.")
    path = (await module_paths(db, {module.id})).get(module.id) if module else None

    if await messages_today(db, user) >= settings.chat_daily_limit_per_user:
        raise AppError(
            "quota_exceeded",
            "You have reached the daily message limit. It resets at 00:00 UTC.",
            status_code=429,
            details={"limit": settings.chat_daily_limit_per_user},
        )

    # Providers are checked before anything is stored or any slot is taken.
    try:
        models = runtime.models()
        embedder.ensure_configured()
    except EmbeddingNotConfiguredError as exc:
        raise ProviderNotConfigured(exc.message) from exc
    context = AgentContext(
        access=AccessContext.from_user(user),
        scope=SearchScope("module", module.id) if module else SearchScope("all"),
        module_path=path,
        scope_label=module.title if module else "all material",
        database=database,
        settings=settings,
        embedder=embedder,
        models=models,
    )

    if not turns.acquire(user.id):
        raise AppError(
            "rate_limited",
            "Your previous question is still being answered. Wait for it to finish.",
            status_code=429,
        )
    try:
        if conversation is None:
            new = new_id()
            conversation = Conversation(
                id=new,
                user_id=user.id,
                module_id=module.id if module else None,
                title=title_from(question),
                thread_id=new,
            )
            db.add(conversation)
            await db.flush()
        user_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=question,
            citations=[],
        )
        db.add(user_message)
        conversation.updated_at = utcnow()
        await db.commit()
    except BaseException:
        turns.release(user.id)
        raise
    return PreparedTurn(
        conversation_id=conversation.id,
        thread_id=conversation.thread_id,
        user_message_id=user_message.id,
        question=question,
        context=context,
        is_admin=user.role == Role.ADMIN,
        user_id=user.id,
    )


def provider_problem(runtime: AgentRuntime, embedder: QueryEmbedder) -> str | None:
    """Why chat cannot work right now (which setting is missing), or ``None`` when it can. The
    text is for administrators; everyone else only gets the short message."""
    try:
        runtime.models()
        embedder.ensure_configured()
    except ProviderNotConfigured as exc:
        return exc.hint
    except EmbeddingNotConfiguredError as exc:
        return exc.message
    return None


# -- running a turn ------------------------------------------------------------


def error_event(code: str, message: str) -> SseEvent:
    return "error", {"code": code, "message": message}


def provider_error_event(exc: ProviderNotConfigured, *, is_admin: bool) -> SseEvent:
    message = (
        f"AI provider not configured: {exc.hint}" if is_admin else PROVIDER_NOT_CONFIGURED_MESSAGE
    )
    return error_event("provider_not_configured", message)


def _map_failure(exc: BaseException, *, is_admin: bool) -> SseEvent:
    if isinstance(exc, ProviderNotConfigured):
        return provider_error_event(exc, is_admin=is_admin)
    if isinstance(exc, EmbeddingNotConfiguredError):
        return provider_error_event(ProviderNotConfigured(exc.message), is_admin=is_admin)
    if isinstance(exc, EmbeddingError):
        log.warning("chat_embedding_failed", reason=exc.message, retryable=exc.retryable)
        return error_event(
            "embedding_unavailable",
            "The search service is temporarily unavailable. Please try again later.",
        )
    if isinstance(exc, AppError):
        return error_event(exc.code, exc.message_for(is_admin))
    log.error("chat_turn_failed", error=type(exc).__name__)
    return error_event("agent_error", "Something went wrong while answering. Please try again.")


async def _store_answer(
    database: Database, prepared: PreparedTurn, result: TurnResult, *, model: str, latency_ms: int
) -> Message | None:
    async with database.session() as db:
        conversation = await db.get(Conversation, prepared.conversation_id)
        if conversation is None:  # deleted while the answer was being written
            return None
        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=result.answer,
            citations=result.citations,
            used_web=result.used_web,
            prompt_version=PROMPT_VERSION,
            model=model,
            latency_ms=latency_ms,
            token_usage=result.usage,
        )
        db.add(message)
        conversation.updated_at = utcnow()
        await db.commit()
        return message


async def run_turn(
    database: Database,
    runtime: AgentRuntime,
    settings: Settings,
    turns: TurnRegistry,
    prepared: PreparedTurn,
) -> AsyncGenerator[SseEvent, None]:
    """Run the agent and yield the SSE events (``status``, ``token``, ``citations``, ``done`` or
    ``error``). The graph runs in its own task so the turn can be timed out and cancelled; closing
    or cancelling this generator (client disconnect) cancels the graph and stores nothing."""
    started = time.monotonic()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for kind, payload in runtime.stream_turn(
                thread_id=prepared.thread_id,
                question=prepared.question,
                context=prepared.context,
            ):
                await queue.put((kind, payload))
        except Exception as exc:
            await queue.put(("failure", exc))
        else:
            await queue.put(("end", None))

    task = asyncio.create_task(produce())
    deadline = started + settings.agent_timeout_s
    result: TurnResult | None = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                kind, payload = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError:
                log.warning("chat_turn_timeout", timeout_s=settings.agent_timeout_s)
                yield error_event(
                    "timeout", "The answer took too long and was stopped. Please try again."
                )
                return
            if kind in ("status", "token"):
                yield kind, {"text": payload}
            elif kind == "result":
                result = payload
            elif kind == "failure":
                yield _map_failure(payload, is_admin=prepared.is_admin)
                return
            else:  # "end"
                break
        if result is None:  # pragma: no cover - the graph always ends with a result
            yield error_event("agent_error", "Something went wrong while answering.")
            return
        latency_ms = int((time.monotonic() - started) * 1000)
        stored = await _store_answer(
            database,
            prepared,
            result,
            model=prepared.context.models.main_name,
            latency_ms=latency_ms,
        )
        if stored is None:
            # The graph re-created the checkpoint thread of a conversation deleted meanwhile.
            with suppress(Exception):
                await runtime.delete_thread(prepared.thread_id)
            yield error_event("not_found", "This conversation was deleted.")
            return
        yield "citations", {"citations": result.citations, "content": result.answer}
        yield (
            "done",
            {
                "message_id": stored.id,
                "conversation_id": prepared.conversation_id,
                "usage": result.usage,
                "latency_ms": latency_ms,
                "searched": result.searched,
            },
        )
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        turns.release(prepared.user_id)
