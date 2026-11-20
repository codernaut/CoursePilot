"""Embedding providers behind one small interface (CLAUDE.md section 7.4).

Providers: ``gemini`` (gemini-embedding-001), ``voyage`` (voyage-3.5), ``openai``
(text-embedding-3-small), the local-AI providers ``ollama`` (an Ollama server),
``openai_compatible`` (any server with ``/v1/embeddings``) and ``local`` (sentence-transformers
inside this process, an optional extra) and ``fake`` (tests only).

Why plain ``httpx`` and not the vendor SDKs: each provider needs exactly one JSON POST. The SDKs
(google-genai, voyageai, openai) would add three large dependency trees, import-time cost and their
own retry/timeout behaviour for no benefit, while ``httpx`` is already a dependency. Requests,
batching, retries and error mapping are therefore ours, in one place, and easy to fake in tests
with ``httpx.MockTransport``.

The client is created on first use (importing this module opens no connection). Vectors are
L2-normalised so cosine distance behaves the same for every provider, and their length is checked
against ``EMBEDDING_DIM``. The API key only ever travels in a request header.
"""

import asyncio
import hashlib
import math
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.ingestion.prefixes import resolve_prefixes

log = get_logger(__name__)

RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAX_RETRIES = 5
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
MAX_RETRY_AFTER_S = 60.0
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
MAX_ERROR_DETAIL = 200


class EmbeddingError(Exception):
    """An embedding request failed. ``message`` is readable by faculty; ``retryable`` tells the
    job queue whether trying again later can help."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class EmbeddingNotConfiguredError(EmbeddingError):
    """No API key for the configured provider (permanent until an administrator sets one)."""


class Embedder(Protocol):
    provider: str
    model: str
    dim: int

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


def normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return [float(v) for v in vector]
    return [float(v) / norm for v in vector]


def validate_vectors(
    vectors: list[list[float]], *, count: int, dim: int, label: str
) -> list[list[float]]:
    """Check count, length and sanity of the vectors a provider returned; return them normalised."""
    if len(vectors) != count:
        raise EmbeddingError(
            f"The {label} embedding service returned {len(vectors)} vectors for {count} texts."
        )
    for vector in vectors:
        if len(vector) != dim:
            raise EmbeddingError(
                f"The {label} embedding service returned vectors of size {len(vector)}, "
                f"but EMBEDDING_DIM is {dim}. Check EMBEDDING_MODEL and EMBEDDING_DIM, "
                "then run 'python -m app.cli reindex-all'."
            )
    for vector in vectors:
        if not all(math.isfinite(v) for v in vector) or not any(vector):
            raise EmbeddingError(
                f"The {label} embedding service returned an unusable (zero or non-numeric) vector."
            )
    return [normalize(v) for v in vectors]


class HttpEmbedder(ABC):
    """Batching, retry with exponential backoff, error mapping and normalisation. Subclasses only
    describe their request and response format."""

    provider = ""
    label = ""  # shown in error messages

    def __init__(
        self,
        *,
        model: str,
        dim: int,
        api_key: str,
        batch_size: int = 64,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        max_retries: int = MAX_RETRIES,
        document_prefix: str = "",
        query_prefix: str = "",
    ) -> None:
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self.model = model
        self.dim = dim
        self._api_key = api_key
        self._batch_size = max(1, batch_size)
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        self._rng = rng
        self._max_retries = max_retries

    # -- provider specifics -------------------------------------------------------------------

    @abstractmethod
    def _request(self, texts: list[str], kind: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """(url, headers, JSON body) for one batch. ``kind`` is ``"document"`` or ``"query"``."""

    @abstractmethod
    def _parse(self, payload: Any, count: int) -> list[list[float]]:
        """The vectors, in input order. Raise ``ValueError`` for an unexpected payload."""

    # -- shared machinery ---------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = [
                self.document_prefix + (t if t.strip() else " ")
                for t in texts[start : start + self._batch_size]
            ]
            vectors.extend(await self._embed_batch(batch, "document"))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        prefixed = self.query_prefix + (text if text.strip() else " ")
        return (await self._embed_batch([prefixed], "query"))[0]

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(MAX_RETRY_AFTER_S, max(0.0, retry_after))
        base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt)
        return float(base * (0.75 + 0.5 * self._rng()))

    async def _embed_batch(self, texts: list[str], kind: str) -> list[list[float]]:
        url, headers, body = self._request(texts, kind)
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            try:
                response = await self._http().post(url, headers=headers, json=body)
            except httpx.TimeoutException:
                reason = "timed out"
            except httpx.TransportError as exc:
                reason = f"could not be reached ({type(exc).__name__})"
            else:
                if response.status_code == 200:
                    return self._finish(response, len(texts))
                if response.status_code not in RETRY_STATUS:
                    raise self._permanent_error(response)
                reason = f"answered HTTP {response.status_code}"
                header = response.headers.get("retry-after", "")
                retry_after = float(header) if re.fullmatch(r"\d{1,5}(\.\d+)?", header) else None
            if attempt >= self._max_retries:
                raise EmbeddingError(
                    f"The {self.label} embedding service {reason} after {attempt + 1} tries "
                    "(rate limit or outage). It will be retried later.",
                    retryable=True,
                )
            delay = self._delay(attempt, retry_after)
            log.warning(
                "embedding_retry", provider=self.provider, reason=reason, attempt=attempt + 1,
                delay_s=round(delay, 2),
            )  # fmt: skip
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _finish(self, response: httpx.Response, count: int) -> list[list[float]]:
        try:
            vectors = self._parse(response.json(), count)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise EmbeddingError(
                f"The {self.label} embedding service returned an unexpected answer."
            ) from exc
        return validate_vectors(vectors, count=count, dim=self.dim, label=self.label)

    def _permanent_error(self, response: httpx.Response) -> EmbeddingError:
        status = response.status_code
        if status in (401, 403):
            return EmbeddingError(
                f"The {self.label} embedding service rejected the API key (HTTP {status}). "
                "Check the key in .env."
            )
        detail = ""
        try:
            payload = response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else {}
            detail = str(error.get("message", "") if isinstance(error, dict) else error)
            if not detail and isinstance(payload, dict):
                detail = str(payload.get("detail", ""))
        except ValueError:
            pass
        if self._api_key:
            detail = detail.replace(self._api_key, "[key]")
        detail = " ".join(detail.split())[:MAX_ERROR_DETAIL]
        suffix = f": {detail}" if detail else ""
        return EmbeddingError(
            f"The {self.label} embedding service rejected the request (HTTP {status}){suffix}"
        )


class GeminiEmbedder(HttpEmbedder):
    provider = "gemini"
    label = "Gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _request(self, texts: list[str], kind: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        task = "RETRIEVAL_DOCUMENT" if kind == "document" else "RETRIEVAL_QUERY"
        body = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task,
                    "outputDimensionality": self.dim,
                }
                for text in texts
            ]
        }
        url = f"{self.base_url}/models/{self.model}:batchEmbedContents"
        return url, {"x-goog-api-key": self._api_key}, body

    def _parse(self, payload: Any, count: int) -> list[list[float]]:
        return [[float(v) for v in item["values"]] for item in payload["embeddings"]]


class VoyageEmbedder(HttpEmbedder):
    provider = "voyage"
    label = "Voyage"
    url = "https://api.voyageai.com/v1/embeddings"

    def _request(self, texts: list[str], kind: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        body = {
            "input": texts,
            "model": self.model,
            "input_type": kind,
            "output_dimension": self.dim,
        }
        return self.url, {"Authorization": f"Bearer {self._api_key}"}, body

    def _parse(self, payload: Any, count: int) -> list[list[float]]:
        items = sorted(payload["data"], key=lambda item: item["index"])
        return [[float(v) for v in item["embedding"]] for item in items]


class OpenAIEmbedder(HttpEmbedder):
    provider = "openai"
    label = "OpenAI"
    url = "https://api.openai.com/v1/embeddings"

    def _request(self, texts: list[str], kind: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        body: dict[str, Any] = {
            "input": texts,
            "model": self.model,
            "encoding_format": "float",
        }
        if self.model.startswith("text-embedding-3"):  # only these accept a shortened size
            body["dimensions"] = self.dim
        return self.url, {"Authorization": f"Bearer {self._api_key}"}, body

    def _parse(self, payload: Any, count: int) -> list[list[float]]:
        items = sorted(payload["data"], key=lambda item: item["index"])
        return [[float(v) for v in item["embedding"]] for item in items]


class LocalServerEmbedder(HttpEmbedder):
    """Base for embedding servers the owner runs: no key needed, a short retry budget (a server
    that is down should fail fast, not for minutes) and the address in every message."""

    def __init__(self, *, base_url: str, **kwargs: Any) -> None:
        kwargs.setdefault("max_retries", 2)
        super().__init__(**kwargs)
        self.base_url = base_url.rstrip("/")
        self.label = f"{self.label} ({self.base_url})"


class OllamaEmbedder(LocalServerEmbedder):
    provider = "ollama"
    label = "Ollama"

    def _request(self, texts: list[str], kind: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        return f"{self.base_url}/api/embed", {}, {"model": self.model, "input": texts}

    def _parse(self, payload: Any, count: int) -> list[list[float]]:
        return [[float(v) for v in vector] for vector in payload["embeddings"]]

    def _permanent_error(self, response: httpx.Response) -> EmbeddingError:
        if response.status_code == 404:
            return EmbeddingError(
                f"The {self.label} embedding service does not have the model '{self.model}' "
                f"(or the address is wrong). Run 'ollama pull {self.model}' on that server, "
                "or check OLLAMA_BASE_URL and EMBEDDING_MODEL."
            )
        return super()._permanent_error(response)


class OpenAICompatibleEmbedder(LocalServerEmbedder):
    provider = "openai_compatible"
    label = "OpenAI-compatible"

    def _request(self, texts: list[str], kind: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        return f"{self.base_url}/embeddings", headers, {"input": texts, "model": self.model}

    def _parse(self, payload: Any, count: int) -> list[list[float]]:
        items = sorted(payload["data"], key=lambda item: item["index"])
        return [[float(v) for v in item["embedding"]] for item in items]

    def _permanent_error(self, response: httpx.Response) -> EmbeddingError:
        if response.status_code == 404:
            return EmbeddingError(
                f"The {self.label} embedding service answered 404: the address or the model "
                f"'{self.model}' is not available. Check LOCAL_EMBEDDING_BASE_URL (or "
                "LOCAL_LLM_BASE_URL, it must end in /v1) and EMBEDDING_MODEL."
            )
        return super()._permanent_error(response)


INSTALL_HINT = (
    "In-process embeddings need the optional PyTorch extra. Install it with "
    "'cd backend && python -m uv sync --extra local-embeddings' (about 1-2 GB), "
    "then restart CoursePilot."
)
_MODELS: dict[tuple[str, str, str], Any] = {}  # loaded once per process


def _pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch
    except ImportError:  # sentence-transformers cannot load without torch anyway
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


class LocalSentenceTransformerEmbedder:
    """``EMBEDDING_PROVIDER=local``: sentence-transformers inside this process (optional extra).

    The package is imported on first use only. Loading and encoding are heavy and blocking, so
    both run in a worker thread (``asyncio.to_thread``), never on the event loop. The model is
    loaded once per process and cached on disk under ``DATA_DIR/models``."""

    provider = "local"

    def __init__(
        self,
        *,
        model: str,
        dim: int,
        device: str = "auto",
        cache_dir: Path | None = None,
        batch_size: int = 64,
        document_prefix: str = "",
        query_prefix: str = "",
    ) -> None:
        self.model = model
        self.dim = dim
        self._device = device
        self._cache_dir = cache_dir
        self._batch_size = max(1, batch_size)
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix

    def _load(self) -> Any:
        try:
            import sentence_transformers
        except ImportError as exc:
            raise EmbeddingNotConfiguredError(INSTALL_HINT) from exc
        device = _pick_device(self._device)
        key = (self.model, device, str(self._cache_dir))
        loaded = _MODELS.get(key)
        if loaded is None:
            try:
                loaded = sentence_transformers.SentenceTransformer(
                    self.model,
                    device=device,
                    cache_folder=None if self._cache_dir is None else str(self._cache_dir),
                    trust_remote_code=False,
                )
            except Exception as exc:  # download, offline or a wrong model name: say which model
                detail = " ".join(str(exc).split())[:MAX_ERROR_DETAIL]
                raise EmbeddingError(
                    f"Could not load the local embedding model '{self.model}': {detail}. "
                    "The first start downloads the model, so the server needs internet access "
                    "once."
                ) from exc
            _MODELS[key] = loaded
        return loaded

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        raw = model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=self._batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(v) for v in row] for row in raw.tolist()]

    async def _run(self, texts: list[str]) -> list[list[float]]:
        vectors = await asyncio.to_thread(self._encode, texts)
        return validate_vectors(vectors, count=len(texts), dim=self.dim, label="local")

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._run([self.document_prefix + (t if t.strip() else " ") for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await self._run([self.query_prefix + (text if text.strip() else " ")]))[0]

    async def aclose(self) -> None:
        return None


_WORD = re.compile(r"\w+")


class FakeEmbedder:
    """Deterministic, offline embeddings for tests: a hashed bag of words, L2-normalised, so texts
    that share words are close and identical texts are identical. No network, no randomness."""

    provider = "fake"

    def __init__(self, *, model: str = "fake-hash-v1", dim: int = 32) -> None:
        self.model = model
        self.dim = dim
        self.document_calls: list[int] = []  # batch sizes, for tests
        self.query_calls = 0

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in _WORD.findall(text.lower()) or [text]:
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            vector[index] += 1.0 if digest[4] & 1 else -1.0
            vector[(index + 1) % self.dim] += 0.5
        return normalize(vector)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls.append(len(texts))
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._vector(text)

    async def aclose(self) -> None:
        return None


NOT_CONFIGURED = (
    "The {label} embedding provider is not configured: {var} is missing in .env. "
    "Ask an administrator to set it and restart CoursePilot, then re-index this document."
)


def get_embedder(settings: Settings) -> Embedder:
    """The embedder for the configured provider. Cheap: no network until the first request."""
    provider = settings.embedding_provider
    if provider == "fake":
        return FakeEmbedder(model=settings.embedding_model, dim=settings.embedding_dim)
    if provider in ("ollama", "openai_compatible", "local"):
        return _get_local_embedder(settings)
    keys = {
        "gemini": (settings.google_api_key, "GOOGLE_API_KEY", "Gemini", GeminiEmbedder),
        "voyage": (settings.voyage_api_key, "VOYAGE_API_KEY", "Voyage", VoyageEmbedder),
        "openai": (settings.openai_api_key, "OPENAI_API_KEY", "OpenAI", OpenAIEmbedder),
    }
    secret, var, label, cls = keys[provider]
    key = secret.get_secret_value().strip() if secret else ""
    if not key:
        raise EmbeddingNotConfiguredError(NOT_CONFIGURED.format(label=label, var=var))
    return cls(
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        api_key=key,
        batch_size=settings.embedding_batch_size,
    )


def _get_local_embedder(settings: Settings) -> Embedder:
    """The local-AI embedders: no API key; the server address (or the extra) must be there."""
    prefixes = resolve_prefixes(settings)
    provider = settings.embedding_provider
    if provider == "local":
        return LocalSentenceTransformerEmbedder(
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            device=settings.local_embedding_device,
            cache_dir=Path(settings.data_dir) / "models",
            batch_size=settings.embedding_batch_size,
            document_prefix=prefixes.document,
            query_prefix=prefixes.query,
        )
    secret = settings.local_llm_api_key
    key = secret.get_secret_value().strip() if secret else ""
    base = settings.ollama_base_url
    cls: type[LocalServerEmbedder] = OllamaEmbedder
    if provider == "openai_compatible":
        cls = OpenAICompatibleEmbedder
        base = settings.local_embedding_base_url or settings.local_llm_base_url
        if not base:
            raise EmbeddingNotConfiguredError(
                "The OpenAI-compatible embedding provider needs LOCAL_EMBEDDING_BASE_URL (or "
                "LOCAL_LLM_BASE_URL) in .env. Ask an administrator to set it and restart "
                "CoursePilot, then re-index this document."
            )
    else:
        key = ""
    return cls(
        base_url=base,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        api_key=key,
        batch_size=settings.embedding_batch_size,
        document_prefix=prefixes.document,
        query_prefix=prefixes.query,
    )
