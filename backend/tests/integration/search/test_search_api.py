"""GET /search and GET /chunks/{id}: authorization, validation, scope, errors and end-to-end."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from app.core.config import Settings
from app.ingestion.embedder import EmbeddingNotConfiguredError, FakeEmbedder
from app.models.enums import DocumentStatus, Role
from app.retrieval.hybrid import PATH_SEPARATOR
from app.retrieval.query_embedding import QueryEmbedder
from tests.conftest import Actor
from tests.integration.search.conftest import ingest_markdown, module_ids

ActorFactory = Callable[..., Awaitable[Actor]]

HEAPS = "# Heaps\n\nA binary heap keeps the largest item at the root of the tree.\n"
GRAPHS = "# Graphs\n\nDijkstra shortest path algorithm uses a priority queue over weighted edges.\n"
PLANTS = "# Plants\n\nChloroplasts convert sunlight, water and carbon dioxide into glucose.\n"


@pytest.fixture
async def corpus(app: FastAPI, settings: Settings, seeded: None) -> dict[str, Any]:
    mods = await module_ids(app)
    docs = {
        "heaps": await ingest_markdown(
            app, settings, module_id=mods["1.1.1"], title="Heaps", body=HEAPS, week="Week 1"
        ),
        "graphs": await ingest_markdown(
            app, settings, module_id=mods["1.1.2"], title="Graphs", body=GRAPHS
        ),
        "plants": await ingest_markdown(
            app, settings, module_id=mods["1.2.1"], title="Plants", body=PLANTS, week="Week 1"
        ),
    }
    return {"mods": mods, "docs": docs}


async def test_every_role_can_search_all_material(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    for role in (Role.STUDENT, Role.FACULTY, Role.ADMIN):
        actor = await make_actor(role)
        response = await actor.client.get("/api/v1/search", params={"q": "binary heap root"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["query"] == "binary heap root"
        (tier,) = body["tiers"]
        assert tier["level"] == "all" and tier["target_id"] is None and tier["sufficient"] is True
        assert tier["hit_count"] == len(body["hits"])
        assert 0.0 < tier["best_vector_similarity"] <= 1.0
        hit = body["hits"][0]
        assert hit["document_title"] == "Heaps"
        assert hit["path"].count(PATH_SEPARATOR.strip()) == 2
        assert "binary heap" in hit["snippet"]
        assert hit["tier"] == "all" and hit["score"] > 0


async def test_anonymous_gets_401_and_nothing(client: AsyncClient, corpus: dict[str, Any]) -> None:
    for url in ("/api/v1/search?q=heap", f"/api/v1/chunks/{'a' * 32}"):
        response = await client.get(url)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


async def test_deactivated_user_loses_access_immediately(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    assert (await actor.client.get("/api/v1/search", params={"q": "heap"})).status_code == 200
    async with app.state.database.session() as db:
        await db.execute(text("UPDATE users SET is_active = 0"))
        await db.commit()
    assert (await actor.client.get("/api/v1/search", params={"q": "heap"})).status_code == 401


async def test_must_change_password_blocks_search_and_chunks(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT, must_change_password=True)
    for url in ("/api/v1/search?q=heap", f"/api/v1/chunks/{'a' * 32}"):
        response = await actor.client.get(url)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "password_change_required"


async def test_validation_errors_are_422(make_actor: ActorFactory, corpus: dict[str, Any]) -> None:
    actor = await make_actor(Role.STUDENT)
    mod = corpus["mods"]["1.1.1"]
    cases = [
        {},  # missing q
        {"q": ""},
        {"q": "x" * 2001},
        {"q": "   "},
        {"q": "heap", "scope": "galaxy"},
        {"q": "heap", "scope": "module"},  # needs a module_id
        {"q": "heap", "scope": "block"},
        {"q": "heap", "module_id": mod, "week": "w" * 61},
        {"q": "heap", "document_id": "d" * 65},
    ]
    for params in cases:
        response = await actor.client.get("/api/v1/search", params=params)
        assert response.status_code == 422, params
        assert response.json()["error"]["code"] == "validation_error"


async def test_unknown_module_is_404(make_actor: ActorFactory, corpus: dict[str, Any]) -> None:
    actor = await make_actor(Role.STUDENT)
    response = await actor.client.get("/api/v1/search", params={"q": "heap", "module_id": "0" * 32})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_module_block_all_scopes(make_actor: ActorFactory, corpus: dict[str, Any]) -> None:
    actor = await make_actor(Role.STUDENT)
    mods = corpus["mods"]
    query = "heap graph glucose root path"

    def titles(response: Any) -> set[str]:
        return {h["document_title"] for h in response.json()["hits"]}

    module = await actor.client.get(
        "/api/v1/search", params={"q": query, "module_id": mods["1.1.1"]}
    )
    assert titles(module) == {"Heaps"}  # scope defaults to module when module_id is given
    assert module.json()["tiers"][0]["level"] == "module"
    block = await actor.client.get(
        "/api/v1/search", params={"q": query, "module_id": mods["1.1.1"], "scope": "block"}
    )
    assert titles(block) == {"Heaps", "Graphs"}
    everything = await actor.client.get(
        "/api/v1/search", params={"q": query, "module_id": mods["1.1.1"], "scope": "all"}
    )
    assert titles(everything) == {"Heaps", "Graphs", "Plants"}


async def test_filters_and_widen(make_actor: ActorFactory, corpus: dict[str, Any]) -> None:
    actor = await make_actor(Role.STUDENT)
    mods, docs = corpus["mods"], corpus["docs"]
    week = await actor.client.get(
        "/api/v1/search", params={"q": "heap glucose graph", "week": "Week 1"}
    )
    assert {h["document_title"] for h in week.json()["hits"]} == {"Heaps", "Plants"}
    one = await actor.client.get(
        "/api/v1/search", params={"q": "heap glucose graph", "document_id": docs["plants"].id}
    )
    assert {h["document_title"] for h in one.json()["hits"]} == {"Plants"}
    # widening: module 1.3.1 and its block hold no material, so the search goes on to "all"
    widened = await actor.client.get(
        "/api/v1/search",
        params={"q": "glucose", "module_id": mods["1.3.1"], "widen": "true"},
    )
    body = widened.json()
    assert [t["level"] for t in body["tiers"]] == ["module", "block", "all"]
    assert [t["sufficient"] for t in body["tiers"]] == [False, False, True]
    assert body["hits"][0]["document_title"] == "Plants" and body["hits"][0]["tier"] == "all"
    # without widen the empty module simply returns nothing
    plain = await actor.client.get(
        "/api/v1/search", params={"q": "glucose", "module_id": mods["1.3.1"]}
    )
    assert plain.json()["hits"] == [] and len(plain.json()["tiers"]) == 1


async def test_not_ready_documents_are_not_searchable(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    async with app.state.database.session() as db:
        await db.execute(
            text("UPDATE documents SET status = :s WHERE id = :d"),
            {"s": DocumentStatus.DELETING.value, "d": corpus["docs"]["plants"].id},
        )
        await db.commit()
    response = await actor.client.get("/api/v1/search", params={"q": "glucose chloroplasts"})
    assert "Plants" not in {h["document_title"] for h in response.json()["hits"]}


# --- GET /chunks/{id} ----------------------------------------------------------------------------


async def first_hit(actor: Actor, query: str) -> dict[str, Any]:
    response = await actor.client.get("/api/v1/search", params={"q": query})
    hit: dict[str, Any] = response.json()["hits"][0]
    return hit


async def test_chunk_returns_full_passage_and_document_link(
    make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    hit = await first_hit(actor, "binary heap root")
    response = await actor.client.get(f"/api/v1/chunks/{hit['chunk_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["chunk_id"] == hit["chunk_id"]
    assert "largest item at the root" in body["text"]
    assert body["path"] == hit["path"]
    assert body["document_title"] == "Heaps" and body["week"] == "Week 1"
    assert body["document_url"] == f"/api/v1/documents/{body['document_id']}/download"
    assert body["metadata"]["kind"] == "body"
    assert body["ordinal"] == 0 and body["token_count"] > 0


async def test_chunk_with_a_page_links_to_that_page(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    hit = await first_hit(actor, "binary heap root")
    async with app.state.database.session() as db:
        await db.execute(
            text("UPDATE chunks SET page_start = 7, page_end = 8 WHERE id = :id"),
            {"id": hit["chunk_id"]},
        )
        await db.commit()
    body = (await actor.client.get(f"/api/v1/chunks/{hit['chunk_id']}")).json()
    assert body["document_url"].endswith("/download#page=7")
    assert body["location"] == "pp. 7-8"


async def test_chunk_404_for_unknown_and_for_documents_not_ready(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.FACULTY)
    assert (await actor.client.get(f"/api/v1/chunks/{'0' * 32}")).status_code == 404
    assert (await actor.client.get("/api/v1/chunks/not-an-id")).status_code == 404
    hit = await first_hit(actor, "binary heap root")
    for status in (DocumentStatus.DELETING, DocumentStatus.FAILED, DocumentStatus.PROCESSING):
        async with app.state.database.session() as db:
            await db.execute(
                text("UPDATE documents SET status = :s WHERE id = :d"),
                {"s": status.value, "d": hit["document_id"]},
            )
            await db.commit()
        response = await actor.client.get(f"/api/v1/chunks/{hit['chunk_id']}")
        assert response.status_code == 404, status
        assert response.json()["error"]["code"] == "not_found"


# --- service errors through the API --------------------------------------------------------------


async def test_index_mismatch_is_503(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    async with app.state.database.session() as db:
        await db.execute(
            text("UPDATE app_meta SET value = 'other-model' WHERE key = 'embedding_model'")
        )
        await db.commit()
    response = await actor.client.get("/api/v1/search", params={"q": "heap"})
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "index_mismatch"
    assert error["details"]["indexed"]["model"] == "other-model"
    assert "reindex-all" in error["message"]


async def test_provider_not_configured_is_503(
    app: FastAPI, settings: Settings, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    def broken() -> FakeEmbedder:
        raise EmbeddingNotConfiguredError("The Gemini embedding provider is not configured.")

    app.state.query_embedder = QueryEmbedder(settings, factory=broken)
    actor = await make_actor(Role.STUDENT)
    response = await actor.client.get("/api/v1/search", params={"q": "heap"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"
    # no silent keyword-only fallback: nothing is returned
    assert "hits" not in response.json()


async def test_identical_queries_reuse_the_cached_query_vector(
    app: FastAPI, make_actor: ActorFactory, corpus: dict[str, Any]
) -> None:
    actor = await make_actor(Role.STUDENT)
    for query in ("binary heap", "Binary   HEAP", "binary heap"):
        assert (await actor.client.get("/api/v1/search", params={"q": query})).status_code == 200
    assert len(app.state.query_embedder.cache) == 1
