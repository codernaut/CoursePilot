"""Embedders: request shapes, batching, retry/backoff, error mapping (mocked HTTP, no network)."""

import json
import math
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.ingestion.embedder import (
    BACKOFF_CAP_S,
    EmbeddingError,
    EmbeddingNotConfiguredError,
    FakeEmbedder,
    GeminiEmbedder,
    HttpEmbedder,
    OpenAIEmbedder,
    VoyageEmbedder,
    get_embedder,
)
from tests.conftest import make_settings

DIM = 8
Handler = Callable[[httpx.Request], httpx.Response]


def vec(seed: float) -> list[float]:
    return [seed + i for i in range(DIM)]


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._handler = handler

    def transport(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def body(self, index: int = 0) -> Any:
        return json.loads(self.requests[index].content)


def build(
    cls: type[HttpEmbedder], handler: Handler, **kwargs: Any
) -> tuple[HttpEmbedder, Recorder]:
    recorder = Recorder(handler)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.transport))
    params: dict[str, Any] = {
        "model": "m",
        "dim": DIM,
        "api_key": "SECRET-KEY-123",
        "batch_size": 2,
        "client": client,
        "sleep": recorder.sleep,
        "rng": lambda: 0.5,
    }
    params.update(kwargs)
    return cls(**params), recorder


def gemini_ok(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["requests"]
    return httpx.Response(
        200, json={"embeddings": [{"values": vec(i + 1)} for i, _ in enumerate(texts)]}
    )


def norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


# --- Gemini --------------------------------------------------------------------------------------


async def test_gemini_request_shape_batching_and_normalisation() -> None:
    embedder, rec = build(GeminiEmbedder, gemini_ok, model="gemini-embedding-001")
    vectors = await embedder.embed_documents(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert [len(rec.body(i)["requests"]) for i in range(len(rec.requests))] == [2, 2, 1]
    first = rec.requests[0]
    assert str(first.url).endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert first.headers["x-goog-api-key"] == "SECRET-KEY-123"
    assert "SECRET-KEY-123" not in str(first.url)
    item = rec.body(0)["requests"][0]
    assert item["model"] == "models/gemini-embedding-001"
    assert item["taskType"] == "RETRIEVAL_DOCUMENT"
    assert item["outputDimensionality"] == DIM
    assert item["content"] == {"parts": [{"text": "a"}]}
    assert all(abs(norm(v) - 1.0) < 1e-9 for v in vectors)  # cosine-ready


async def test_gemini_query_uses_the_query_task_type() -> None:
    embedder, rec = build(GeminiEmbedder, gemini_ok)
    vector = await embedder.embed_query("what is merge sort?")
    assert len(vector) == DIM
    assert rec.body()["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


async def test_empty_text_is_replaced_by_a_space() -> None:
    embedder, rec = build(GeminiEmbedder, gemini_ok)
    await embedder.embed_documents(["", "  "])
    assert [r["content"]["parts"][0]["text"] for r in rec.body()["requests"]] == [" ", " "]


# --- Voyage / OpenAI -----------------------------------------------------------------------------


def shuffled_ok(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["input"]
    data = [{"index": i, "embedding": vec(i + 1)} for i in range(len(texts))]
    return httpx.Response(200, json={"data": list(reversed(data))})  # out of order on purpose


async def test_voyage_request_shape_and_order() -> None:
    embedder, rec = build(VoyageEmbedder, shuffled_ok, model="voyage-3.5")
    vectors = await embedder.embed_documents(["a", "b"])
    request = rec.requests[0]
    assert str(request.url) == "https://api.voyageai.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer SECRET-KEY-123"
    assert rec.body() == {
        "input": ["a", "b"],
        "model": "voyage-3.5",
        "input_type": "document",
        "output_dimension": DIM,
    }
    first = vec(1)
    assert vectors[0] == pytest.approx([x / norm(first) for x in first])  # index order restored
    await embedder.embed_query("q")
    assert rec.body(1)["input_type"] == "query"


async def test_openai_request_shape_sends_dimensions_only_for_v3_models() -> None:
    embedder, rec = build(OpenAIEmbedder, shuffled_ok, model="text-embedding-3-small")
    await embedder.embed_documents(["a"])
    request = rec.requests[0]
    assert str(request.url) == "https://api.openai.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer SECRET-KEY-123"
    assert rec.body() == {
        "input": ["a"],
        "model": "text-embedding-3-small",
        "encoding_format": "float",
        "dimensions": DIM,
    }
    legacy, rec2 = build(OpenAIEmbedder, shuffled_ok, model="text-embedding-ada-002")
    await legacy.embed_documents(["a"])
    assert "dimensions" not in rec2.body()


# --- retry, backoff, error mapping --------------------------------------------------------------


async def test_rate_limit_is_retried_honouring_retry_after_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow down"})
        if calls == 2:
            return httpx.Response(503)
        return gemini_ok(request)

    embedder, rec = build(GeminiEmbedder, handler)
    assert len(await embedder.embed_documents(["a"])) == 1
    assert calls == 3
    assert rec.sleeps[0] == 7.0  # Retry-After wins
    assert rec.sleeps[1] == pytest.approx(2.0)  # 1s * 2**1, jitter factor 1.0 at rng=0.5


async def test_backoff_grows_exponentially_and_is_capped() -> None:
    embedder, rec = build(GeminiEmbedder, lambda r: httpx.Response(500), max_retries=8)
    with pytest.raises(EmbeddingError) as info:
        await embedder.embed_documents(["a"])
    assert info.value.retryable is True
    assert "will be retried later" in info.value.message
    assert rec.sleeps[:4] == pytest.approx([1.0, 2.0, 4.0, 8.0])
    assert max(rec.sleeps) <= BACKOFF_CAP_S * 1.25
    assert len(rec.requests) == 9


async def test_timeouts_and_connection_errors_are_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    embedder, rec = build(GeminiEmbedder, handler, max_retries=2)
    with pytest.raises(EmbeddingError, match="timed out") as info:
        await embedder.embed_documents(["a"])
    assert info.value.retryable
    assert len(rec.requests) == 3


async def test_auth_failure_is_permanent_and_never_leaks_the_key() -> None:
    embedder, rec = build(
        VoyageEmbedder,
        lambda r: httpx.Response(401, json={"detail": "Bearer SECRET-KEY-123 is invalid"}),
    )
    with pytest.raises(EmbeddingError) as info:
        await embedder.embed_documents(["a"])
    assert info.value.retryable is False
    assert "rejected the API key" in info.value.message
    assert "SECRET-KEY-123" not in info.value.message
    assert len(rec.requests) == 1  # no retries for a permanent error


async def test_bad_request_reports_the_providers_message() -> None:
    embedder, _ = build(
        OpenAIEmbedder,
        lambda r: httpx.Response(400, json={"error": {"message": "input is too long"}}),
    )
    with pytest.raises(
        EmbeddingError, match=r"rejected the request \(HTTP 400\): input is too long"
    ):
        await embedder.embed_documents(["a"])


async def test_wrong_dimension_and_count_and_garbage_are_permanent_errors() -> None:
    short = lambda r: httpx.Response(200, json={"embeddings": [{"values": [1.0, 2.0]}]})  # noqa: E731
    embedder, _ = build(GeminiEmbedder, short)
    with pytest.raises(EmbeddingError, match="size 2, but EMBEDDING_DIM is 8") as info:
        await embedder.embed_documents(["a"])
    assert not info.value.retryable

    embedder, _ = build(GeminiEmbedder, lambda r: httpx.Response(200, json={"embeddings": []}))
    with pytest.raises(EmbeddingError, match="returned 0 vectors for 1 texts"):
        await embedder.embed_documents(["a"])

    embedder, _ = build(GeminiEmbedder, lambda r: httpx.Response(200, text="<html>nope"))
    with pytest.raises(EmbeddingError, match="unexpected answer"):
        await embedder.embed_documents(["a"])


# --- factory and fake ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "var", "key_field"),
    [
        ("gemini", "GOOGLE_API_KEY", "google_api_key"),
        ("voyage", "VOYAGE_API_KEY", "voyage_api_key"),
        ("openai", "OPENAI_API_KEY", "openai_api_key"),
    ],
)
def test_factory_without_a_key_raises_a_readable_not_configured_error(
    tmp_path: object, provider: str, var: str, key_field: str
) -> None:
    del key_field
    settings = make_settings(tmp_path, embedding_provider=provider)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingNotConfiguredError) as info:
        get_embedder(settings)
    assert var in info.value.message and "not configured" in info.value.message
    assert info.value.retryable is False


def test_factory_builds_each_provider_lazily(tmp_path: object) -> None:
    for provider, key_field, cls in (
        ("gemini", "google_api_key", GeminiEmbedder),
        ("voyage", "voyage_api_key", VoyageEmbedder),
        ("openai", "openai_api_key", OpenAIEmbedder),
    ):
        settings = make_settings(
            tmp_path,  # type: ignore[arg-type]
            embedding_provider=provider,
            embedding_dim=DIM,
            embedding_batch_size=16,
            **{key_field: "k"},
        )
        embedder = get_embedder(settings)
        assert isinstance(embedder, cls)
        assert embedder.dim == DIM and embedder.model == settings.embedding_model
        assert embedder._client is None  # no connection yet
    fake = get_embedder(make_settings(tmp_path, embedding_provider="fake", embedding_dim=5))  # type: ignore[arg-type]
    assert isinstance(fake, FakeEmbedder) and fake.dim == 5


async def test_fake_embedder_is_deterministic_normalised_and_sized() -> None:
    a, b = FakeEmbedder(dim=64), FakeEmbedder(dim=64)
    texts = ["merge sort halves a list", "merge sort halves a list", "completely different topic"]
    va = await a.embed_documents(texts)
    vb = await b.embed_documents(texts)
    assert va == vb  # same inputs, same vectors, across instances
    assert va[0] == va[1] != va[2]
    assert all(len(v) == 64 and abs(norm(v) - 1.0) < 1e-9 for v in va)
    assert await a.embed_query("merge sort halves a list") == va[0]

    def cos(x: list[float], y: list[float]) -> float:
        return sum(p * q for p, q in zip(x, y, strict=True))

    related = await a.embed_query("how does merge sort work")
    assert cos(related, va[0]) > cos(related, va[2])
    assert a.document_calls == [3] and a.query_calls == 2


@pytest.mark.parametrize("bad", ["zero", "nan", "inf"])
async def test_degenerate_vectors_are_rejected_not_stored(bad: str) -> None:
    """A zero or non-finite vector makes cosine distances meaningless (NaN); refuse it."""

    def handler(request: httpx.Request) -> httpx.Response:
        values = {"zero": [0.0] * DIM, "nan": [float("nan")] * DIM, "inf": [float("inf")] * DIM}[
            bad
        ]
        body = json.dumps({"embeddings": [{"values": values}]}, allow_nan=True)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    embedder, _ = build(GeminiEmbedder, handler)
    with pytest.raises(EmbeddingError) as info:
        await embedder.embed_documents(["x"])
    assert not info.value.retryable


async def test_a_key_echoed_by_the_provider_is_redacted_from_the_error() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad key SECRET-KEY-123 given"}})

    embedder, _ = build(GeminiEmbedder, handler)
    with pytest.raises(EmbeddingError) as info:
        await embedder.embed_documents(["x"])
    assert "SECRET-KEY-123" not in info.value.message
