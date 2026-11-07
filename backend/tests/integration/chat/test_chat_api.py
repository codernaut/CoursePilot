"""POST /chat (SSE), conversations, feedback: contract, persistence, limits, ownership, failures."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text

from app.agent.prompts import NOT_FOUND_NO_WEB_MESSAGE, PROMPT_VERSION
from app.agent.runtime import AgentRuntime
from app.core.config import Settings
from app.ingestion.embedder import EmbeddingNotConfiguredError, FakeEmbedder
from app.llm.factory import ProviderNotConfigured
from app.models import Conversation, Message
from app.models.enums import MessageRole, Role
from app.retrieval.hybrid import SearchScope
from app.retrieval.query_embedding import QueryEmbedder
from app.services import chat as chat_service
from tests.conftest import Actor
from tests.integration.chat.conftest import Llms, sse_of

ActorFactory = Callable[..., Awaitable[Actor]]
QUESTION = "What does a binary heap keep at the root?"


async def ask(actor: Actor, message: str = QUESTION, **body: Any) -> Response:
    return await actor.client.post(
        "/api/v1/chat", json={"message": message, **body}, headers=actor.headers
    )


async def messages_of(app: FastAPI, conversation_id: str) -> list[Message]:
    async with app.state.database.read_session() as db:
        rows = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, Message.role.desc())
        )
        return list(rows.scalars())


async def count_messages(app: FastAPI) -> int:
    async with app.state.database.read_session() as db:
        return int((await db.execute(select(func.count()).select_from(Message))).scalar_one())


# -- the happy path and the SSE contract --------------------------------------------------


async def test_chat_streams_status_tokens_citations_done(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms
) -> None:
    llms.main.responder = lambda m: "A heap keeps the largest item at the root [C1]."
    student = await make_actor(Role.STUDENT)
    response = await ask(student, module_id=corpus["mods"]["1.1.1"])
    events = sse_of(response)
    names = [name for name, _ in events]

    assert names[-2:] == ["citations", "done"]
    assert set(names) == {"status", "token", "citations", "done"}
    first_token = names.index("token")
    assert names[0] == "status" and all(n == "status" for n in names[:first_token])
    assert all(n == "token" for n in names[first_token:-2])
    statuses = [d["text"] for n, d in events if n == "status"]
    assert statuses[0] == "Searching Module 1…"
    assert "Writing the answer…" in statuses

    streamed = "".join(d["text"] for n, d in events if n == "token")
    assert streamed == "A heap keeps the largest item at the root [C1]."
    citations = dict(events)["citations"]
    assert citations["content"] == streamed
    (cite,) = citations["citations"]
    assert cite["marker"] == "C1" and cite["type"] == "material"
    assert cite["document_title"] == "Heaps" and cite["location"] is None
    assert cite["path"] == "Year 1 › Block 1 › Module 1"
    assert set(cite) >= {"chunk_id", "document_id", "snippet", "section_path", "module_id"}

    done = dict(events)["done"]
    assert done["conversation_id"] == response.headers["x-conversation-id"]
    assert set(done) >= {"message_id", "conversation_id", "usage", "latency_ms"}
    assert done["usage"]["total_tokens"] > 0 and done["latency_ms"] >= 0


async def test_persistence_of_both_messages(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms
) -> None:
    llms.main.responder = lambda m: "Roots hold the max [C1] and nothing else [C5]."
    student = await make_actor(Role.STUDENT)
    mod = corpus["mods"]["1.1.1"]
    events = sse_of(await ask(student, module_id=mod))
    done = dict(events)["done"]

    user_msg, assistant = await messages_of(app, done["conversation_id"])
    assert user_msg.role == MessageRole.USER and user_msg.content == QUESTION
    assert user_msg.citations == [] and user_msg.model is None
    assert assistant.role == MessageRole.ASSISTANT and assistant.id == done["message_id"]
    assert assistant.content == "Roots hold the max [C1] and nothing else."  # [C5] was dropped
    assert [c["marker"] for c in assistant.citations] == ["C1"]
    assert assistant.citations[0]["document_title"] == "Heaps"
    assert assistant.used_web is False
    assert assistant.prompt_version == PROMPT_VERSION
    assert assistant.model == "fake-main"
    assert assistant.latency_ms is not None and assistant.latency_ms >= 0
    assert assistant.token_usage and assistant.token_usage["total_tokens"] > 0

    async with app.state.database.read_session() as db:
        conversation = await db.get(Conversation, done["conversation_id"])
    assert conversation is not None
    assert conversation.user_id == student.user.id and conversation.module_id == mod
    assert conversation.title == QUESTION and conversation.thread_id == conversation.id


async def test_follow_up_continues_the_same_conversation(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms
) -> None:
    student = await make_actor(Role.STUDENT)
    first = dict(sse_of(await ask(student)))["done"]
    second = dict(
        sse_of(await ask(student, "and then?", conversation_id=first["conversation_id"]))
    )["done"]
    assert second["conversation_id"] == first["conversation_id"]
    assert len(await messages_of(app, first["conversation_id"])) == 4
    assert llms.rewrite_calls() == 1  # the follow-up used the history


async def test_nothing_found_stores_the_plain_answer_without_citations(
    app: FastAPI, make_actor: ActorFactory, seeded: None, llms: Llms
) -> None:
    student = await make_actor(Role.STUDENT)
    events = sse_of(await ask(student))  # an empty programme
    assert dict(events)["citations"]["citations"] == []
    assert dict(events)["citations"]["content"] == NOT_FOUND_NO_WEB_MESSAGE
    assert llms.main.calls == []
    done = dict(events)["done"]
    assert (await messages_of(app, done["conversation_id"]))[-1].content == NOT_FOUND_NO_WEB_MESSAGE


async def test_all_material_conversation_has_no_module(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    student = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(student, module_id=None)))["done"]
    assert done["searched"] == ["all"]
    listed = (await student.client.get("/api/v1/conversations")).json()
    assert listed[0]["module_id"] is None and listed[0]["module_path"] is None


# -- validation and access ----------------------------------------------------------------


async def test_validation_errors(
    make_actor: ActorFactory, corpus: dict[str, Any], app: FastAPI
) -> None:
    student = await make_actor(Role.STUDENT)
    assert (await ask(student, "   ")).status_code == 422
    assert (await ask(student, "x" * 4001)).status_code == 422
    assert (await ask(student, "\x00\x01")).status_code == 422  # nothing printable
    assert (await ask(student, module_id="f" * 32)).status_code == 404
    assert (
        await student.client.post("/api/v1/chat", json={}, headers=student.headers)
    ).status_code == 422
    done = dict(sse_of(await ask(student, module_id=corpus["mods"]["1.1.1"])))["done"]
    other = await ask(
        student, conversation_id=done["conversation_id"], module_id=corpus["mods"]["1.1.2"]
    )
    assert other.status_code == 422
    assert await count_messages(app) == 2  # only the one valid turn was stored


async def test_anonymous_csrf_and_origin(
    client: AsyncClient, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    assert (await client.post("/api/v1/chat", json={"message": "hi"})).status_code == 401
    student = await make_actor(Role.STUDENT)
    no_csrf = await student.client.post("/api/v1/chat", json={"message": "hi"})
    assert no_csrf.status_code == 403
    evil = await student.client.post(
        "/api/v1/chat",
        json={"message": "hi"},
        headers={**student.headers, "Origin": "https://evil.example"},
    )
    assert evil.status_code == 403


async def test_every_role_can_chat(make_actor: ActorFactory, corpus: dict[str, Any]) -> None:
    for role in (Role.STUDENT, Role.FACULTY, Role.ADMIN):
        actor = await make_actor(role)
        assert "done" in dict(sse_of(await ask(actor)))


async def test_deactivated_user_cannot_chat(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    async with app.state.database.session() as db:
        await db.execute(text("UPDATE users SET is_active = 0"))
        await db.commit()
    assert (await ask(actor)).status_code == 401


# -- quota and the one-running-turn rule --------------------------------------------------


async def test_daily_quota_is_429(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], settings: Settings
) -> None:
    settings.chat_daily_limit_per_user = 2
    student = await make_actor(Role.STUDENT)
    other = await make_actor(Role.STUDENT)
    assert "done" in dict(sse_of(await ask(student)))
    assert "done" in dict(sse_of(await ask(student)))
    blocked = await ask(student)
    assert blocked.status_code == 429
    error = blocked.json()["error"]
    assert error["code"] == "quota_exceeded" and error["details"]["limit"] == 2
    assert await count_messages(app) == 4  # nothing was stored for the blocked message
    assert "done" in dict(sse_of(await ask(other)))  # the limit is per user


async def test_quota_counts_only_today(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], settings: Settings
) -> None:
    settings.chat_daily_limit_per_user = 1
    student = await make_actor(Role.STUDENT)
    assert "done" in dict(sse_of(await ask(student)))
    async with app.state.database.session() as db:  # yesterday's messages do not count
        await db.execute(text("UPDATE messages SET created_at = datetime(created_at, '-2 days')"))
        await db.commit()
    assert "done" in dict(sse_of(await ask(student)))


async def test_second_turn_while_one_runs_is_429(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    student = await make_actor(Role.STUDENT)
    other = await make_actor(Role.STUDENT)
    assert app.state.chat_turns.acquire(student.user.id)  # a turn is "running"
    blocked = await ask(student)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "rate_limited"
    assert await count_messages(app) == 0
    assert "done" in dict(sse_of(await ask(other)))  # other users are unaffected
    app.state.chat_turns.release(student.user.id)
    assert "done" in dict(sse_of(await ask(student)))
    assert not app.state.chat_turns.is_active(student.user.id)  # released after the stream


def test_a_lost_slot_expires_on_its_own() -> None:
    now = [0.0]
    turns = chat_service.TurnRegistry(hold_s=10, clock=lambda: now[0])
    assert turns.acquire("u") and not turns.acquire("u") and turns.is_active("u")
    now[0] = 11.0
    assert not turns.is_active("u") and turns.acquire("u")
    turns.release("u")
    assert turns.acquire("u")


# -- failures -----------------------------------------------------------------------------


async def test_timeout_sends_an_error_event_and_stores_no_answer(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms, settings: Settings
) -> None:
    settings.agent_timeout_s = 1
    llms.main.responder = lambda m: "one two three four five six seven eight"
    llms.main.delay_s = 0.4
    student = await make_actor(Role.STUDENT)
    events = sse_of(await ask(student))
    name, data = events[-1]
    assert name == "error" and data["code"] == "timeout"
    assert "citations" not in dict(events) and "done" not in dict(events)
    async with app.state.database.read_session() as db:
        roles = (await db.execute(select(Message.role))).scalars().all()
    assert roles == [MessageRole.USER]  # the question stays, there is no assistant message
    assert not app.state.chat_turns.is_active(student.user.id)


async def test_a_model_failure_is_a_generic_error_event(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms
) -> None:
    llms.main.error = RuntimeError("secret provider detail sk-123")
    student = await make_actor(Role.STUDENT)
    events = sse_of(await ask(student))
    name, data = events[-1]
    assert name == "error" and data["code"] == "agent_error"
    assert "sk-123" not in data["message"] and "secret" not in data["message"]
    async with app.state.database.read_session() as db:
        roles = (await db.execute(select(Message.role))).scalars().all()
    assert roles == [MessageRole.USER]
    assert not app.state.chat_turns.is_active(student.user.id)


async def test_index_mismatch_is_an_error_event(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    async with app.state.database.session() as db:
        await db.execute(text("UPDATE app_meta SET value = 'other' WHERE key = 'embedding_model'"))
        await db.commit()
    student = await make_actor(Role.STUDENT)
    events = sse_of(await ask(student))
    assert events[-1][0] == "error" and events[-1][1]["code"] == "index_mismatch"


async def test_client_disconnect_cancels_the_turn_and_stores_nothing(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms, settings: Settings
) -> None:
    llms.main.responder = lambda m: " ".join(["word"] * 30)
    llms.main.delay_s = 0.2
    student = await make_actor(Role.STUDENT)
    runtime: AgentRuntime = app.state.agent_runtime
    async with app.state.database.session() as db:
        prepared = await chat_service.prepare_turn(
            db,
            user=student.user,
            settings=settings,
            database=app.state.database,
            runtime=runtime,
            embedder=app.state.query_embedder,
            turns=app.state.chat_turns,
            message=QUESTION,
            module_id=None,
            conversation_id=None,
        )
    assert app.state.chat_turns.is_active(student.user.id)
    seen: list[str] = []

    async def consume() -> None:
        async for name, _ in chat_service.run_turn(
            app.state.database, runtime, settings, app.state.chat_turns, prepared
        ):
            seen.append(name)

    task = asyncio.create_task(consume())
    for _ in range(100):  # wait for the first streamed token
        if "token" in seen:
            break
        await asyncio.sleep(0.05)
    assert "token" in seen
    task.cancel()  # what the server does when the client goes away
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not app.state.chat_turns.is_active(student.user.id)
    await asyncio.sleep(0.5)  # a cancelled graph must not keep going and store an answer
    roles = [m.role for m in await messages_of(app, prepared.conversation_id)]
    assert roles == [MessageRole.USER]
    assert "citations" not in seen and "done" not in seen


async def test_closing_the_event_stream_early_releases_the_slot(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms, settings: Settings
) -> None:
    llms.main.delay_s = 0.1
    student = await make_actor(Role.STUDENT)
    async with app.state.database.session() as db:
        prepared = await chat_service.prepare_turn(
            db,
            user=student.user,
            settings=settings,
            database=app.state.database,
            runtime=app.state.agent_runtime,
            embedder=app.state.query_embedder,
            turns=app.state.chat_turns,
            message=QUESTION,
            module_id=None,
            conversation_id=None,
        )
    events = chat_service.run_turn(
        app.state.database, app.state.agent_runtime, settings, app.state.chat_turns, prepared
    )
    await events.__anext__()
    await events.aclose()
    assert not app.state.chat_turns.is_active(student.user.id)


# -- providers not configured -------------------------------------------------------------


def missing_llm(hint: str = "the Anthropic chat provider needs ANTHROPIC_API_KEY in .env.") -> Any:
    def factory() -> Any:
        raise ProviderNotConfigured(hint)

    return factory


async def test_provider_not_configured_short_message_for_students_hint_for_admins(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], settings: Settings
) -> None:
    app.state.agent_runtime = AgentRuntime(settings, missing_llm())
    student = await make_actor(Role.STUDENT)
    faculty = await make_actor(Role.FACULTY)
    admin = await make_actor(Role.ADMIN)

    for actor in (student, faculty):
        events = sse_of(await ask(actor))
        assert [n for n, _ in events] == ["error"]
        assert events[0][1] == {
            "code": "provider_not_configured",
            "message": "AI provider not configured. Ask an administrator.",
        }
    admin_events = sse_of(await ask(admin))
    code, message = admin_events[0][1]["code"], admin_events[0][1]["message"]
    assert code == "provider_not_configured"
    assert message.startswith("AI provider not configured:") and "ANTHROPIC_API_KEY" in message
    assert await count_messages(app) == 0  # nothing stored, no slot held
    assert not app.state.chat_turns.is_active(student.user.id)


async def test_real_factory_without_a_key_is_not_configured(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], settings: Settings
) -> None:
    settings.llm_provider = "gemini"
    settings.google_api_key = None
    app.state.agent_runtime = AgentRuntime(settings)
    student = await make_actor(Role.STUDENT)
    admin = await make_actor(Role.ADMIN)
    assert sse_of(await ask(student))[0][1]["code"] == "provider_not_configured"
    assert "GOOGLE_API_KEY" in sse_of(await ask(admin))[0][1]["message"]


async def test_missing_embedding_key_is_also_provider_not_configured(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], settings: Settings
) -> None:
    def broken() -> FakeEmbedder:
        raise EmbeddingNotConfiguredError("The Gemini embedding provider needs GOOGLE_API_KEY.")

    app.state.query_embedder = QueryEmbedder(settings, factory=broken)
    student = await make_actor(Role.STUDENT)
    admin = await make_actor(Role.ADMIN)
    assert sse_of(await ask(student))[0][1]["message"] == (
        "AI provider not configured. Ask an administrator."
    )
    assert "GOOGLE_API_KEY" in sse_of(await ask(admin))[0][1]["message"]
    assert await count_messages(app) == 0


# -- conversations: privacy ---------------------------------------------------------------


async def test_list_and_read_own_conversations_only(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    alice = await make_actor(Role.STUDENT)
    bob = await make_actor(Role.STUDENT)
    mine = dict(sse_of(await ask(alice, module_id=corpus["mods"]["1.1.1"])))["done"]
    dict(sse_of(await ask(bob, "plants question")))

    listed = (await alice.client.get("/api/v1/conversations")).json()
    assert [c["id"] for c in listed] == [mine["conversation_id"]]
    assert listed[0]["module_path"] == "Year 1 › Block 1 › Module 1"
    assert listed[0]["title"] == QUESTION

    detail = (await alice.client.get(f"/api/v1/conversations/{mine['conversation_id']}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assistant = detail["messages"][1]
    assert assistant["citations"][0]["marker"] == "C1" and assistant["used_web"] is False
    assert assistant["prompt_version"] == PROMPT_VERSION and assistant["feedback"] is None


@pytest.mark.parametrize("intruder_role", [Role.STUDENT, Role.FACULTY, Role.ADMIN])
async def test_foreign_conversations_are_404_for_every_role(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], intruder_role: Role
) -> None:
    owner = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(owner)))["done"]
    cid, mid = done["conversation_id"], done["message_id"]
    intruder = await make_actor(intruder_role)
    h = intruder.headers

    assert (await intruder.client.get(f"/api/v1/conversations/{cid}")).status_code == 404
    assert (
        await intruder.client.delete(f"/api/v1/conversations/{cid}", headers=h)
    ).status_code == 404
    feedback = await intruder.client.post(
        f"/api/v1/messages/{mid}/feedback", json={"feedback": "up"}, headers=h
    )
    assert feedback.status_code == 404
    assert (await ask(intruder, "hello", conversation_id=cid)).status_code == 404
    assert (await intruder.client.get("/api/v1/conversations")).json() == []

    # nothing of the owner's changed
    assert len(await messages_of(app, cid)) == 2
    detail = (await owner.client.get(f"/api/v1/conversations/{cid}")).json()
    assert detail["messages"][1]["feedback"] is None
    unknown = "0" * 32
    assert (await intruder.client.get(f"/api/v1/conversations/{unknown}")).status_code == 404


async def test_delete_conversation_removes_messages_and_checkpoint_thread(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    student = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(student)))["done"]
    cid = done["conversation_id"]
    runtime: AgentRuntime = app.state.agent_runtime
    assert await runtime.thread_exists(cid)

    response = await student.client.delete(f"/api/v1/conversations/{cid}", headers=student.headers)
    assert response.status_code == 204
    assert await messages_of(app, cid) == []
    assert not await runtime.thread_exists(cid)
    assert (await student.client.get(f"/api/v1/conversations/{cid}")).status_code == 404
    assert (await student.client.get("/api/v1/conversations")).json() == []
    again = await student.client.delete(f"/api/v1/conversations/{cid}", headers=student.headers)
    assert again.status_code == 404
    other = dict(sse_of(await ask(student)))["done"]  # a deleted conversation id is not reusable
    assert other["conversation_id"] != cid


async def test_deleting_one_conversation_keeps_the_others_memory(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    student = await make_actor(Role.STUDENT)
    first = dict(sse_of(await ask(student)))["done"]["conversation_id"]
    second = dict(sse_of(await ask(student)))["done"]["conversation_id"]
    await student.client.delete(f"/api/v1/conversations/{first}", headers=student.headers)
    runtime: AgentRuntime = app.state.agent_runtime
    assert not await runtime.thread_exists(first) and await runtime.thread_exists(second)


# -- feedback -----------------------------------------------------------------------------


async def test_feedback_up_down_and_clear(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    student = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(student)))["done"]
    url = f"/api/v1/messages/{done['message_id']}/feedback"

    up = await student.client.post(url, json={"feedback": "up"}, headers=student.headers)
    assert up.status_code == 200 and up.json()["feedback"] == "up"
    down = await student.client.post(
        url, json={"feedback": "down", "comment": "  too   vague "}, headers=student.headers
    )
    assert down.json()["feedback"] == "down" and down.json()["feedback_comment"] == "too vague"
    cleared = await student.client.post(
        url, json={"feedback": None, "comment": "ignored"}, headers=student.headers
    )
    assert cleared.json()["feedback"] is None and cleared.json()["feedback_comment"] is None

    assert (
        await student.client.post(url, json={"feedback": "sideways"}, headers=student.headers)
    ).status_code == 422
    assert (
        await student.client.post(
            url, json={"feedback": "up", "comment": "x" * 1001}, headers=student.headers
        )
    ).status_code == 422
    assert (await student.client.post(url, json={}, headers=student.headers)).status_code == 422


async def test_feedback_only_on_own_assistant_messages(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    student = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(student)))["done"]
    user_message = (await messages_of(app, done["conversation_id"]))[0]
    assert user_message.role == MessageRole.USER
    response = await student.client.post(
        f"/api/v1/messages/{user_message.id}/feedback",
        json={"feedback": "up"},
        headers=student.headers,
    )
    assert response.status_code == 404  # only answers can be rated


# -- review: empty model output --------------------------------------------


async def test_an_empty_model_answer_is_replaced_by_a_plain_message(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms
) -> None:
    llms.main.responder = lambda m: "   "
    student = await make_actor(Role.STUDENT)
    events = dict(sse_of(await ask(student)))
    assert events["citations"]["content"].strip() != ""
    assert (await messages_of(app, events["done"]["conversation_id"]))[-1].content.strip() != ""


async def test_the_checkpoint_does_not_keep_copies_of_the_material(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    from app.agent.graph import build_graph

    student = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(student)))["done"]
    runtime: AgentRuntime = app.state.agent_runtime
    async with runtime._checkpointer() as saver:
        state = await build_graph(saver).aget_state(
            {"configurable": {"thread_id": done["conversation_id"]}}
        )
    assert state.values["chunks"] == []
    assert len(state.values["messages"]) == 2


async def test_a_conversation_deleted_during_its_turn_leaves_no_checkpoint(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any], llms: Llms
) -> None:
    from dataclasses import replace

    from tests.integration.chat.conftest import build_context

    student = await make_actor(Role.STUDENT)
    done = dict(sse_of(await ask(student)))["done"]
    conversation_id = done["conversation_id"]
    context = await build_context(app, llms, scope=SearchScope("all"), user=student.user)
    prepared = chat_service.PreparedTurn(
        conversation_id=conversation_id,
        thread_id=conversation_id,
        user_message_id="x",
        question=QUESTION,
        context=context,
        is_admin=False,
        user_id=student.user.id,
    )
    assert (
        await student.client.delete(
            f"/api/v1/conversations/{conversation_id}", headers=student.headers
        )
    ).status_code == 204
    runtime: AgentRuntime = app.state.agent_runtime
    assert not await runtime.thread_exists(conversation_id)
    assert app.state.chat_turns.acquire(student.user.id)
    events = [
        e
        async for e in chat_service.run_turn(
            app.state.database, runtime, app.state.settings, app.state.chat_turns, replace(prepared)
        )
    ]
    assert events[-1][0] == "error" and events[-1][1]["code"] == "not_found"
    assert not await runtime.thread_exists(conversation_id)
