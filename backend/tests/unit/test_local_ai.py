"""Local AI (M11): chat factory, Ollama / OpenAI-compatible / in-process embedders, prefixes,
settings and the production check. No network, no torch (a fake sentence_transformers module)."""

import json
import subprocess
import sys
import threading
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.ingestion.embedder as embedder_module
from app.core.config import Settings
from app.ingestion.embedder import (
    EmbeddingError,
    EmbeddingNotConfiguredError,
    LocalSentenceTransformerEmbedder,
    LocalServerEmbedder,
    OllamaEmbedder,
    OpenAICompatibleEmbedder,
    get_embedder,
)
from app.ingestion.prefixes import BGE_QUERY, Prefixes, default_prefixes, resolve_prefixes
from app.llm import ProviderNotConfigured, build_chat_models
from app.services import production_check
from tests.conftest import make_settings

DIM = 4
Handler = Callable[[httpx.Request], httpx.Response]


# --- settings ------------------------------------------------------------------------------------


def test_local_settings_defaults_change_nothing() -> None:
    s = Settings(_env_file=None)
    assert (s.llm_provider, s.embedding_provider) == ("anthropic", "gemini")
    assert s.ollama_base_url == "http://localhost:11434"
    assert s.local_llm_base_url == "" and s.local_llm_api_key is None
    assert s.local_embedding_device == "auto"
    assert s.embedding_query_prefix == "" and s.embedding_document_prefix == ""


def test_new_providers_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    s = Settings(_env_file=None)
    assert (s.llm_provider, s.embedding_provider) == ("openai_compatible", "local")


@pytest.mark.parametrize(
    "name", ["OLLAMA_BASE_URL", "LOCAL_LLM_BASE_URL", "LOCAL_EMBEDDING_BASE_URL"]
)
def test_local_urls_must_be_http(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "ftp://example.org")
    with pytest.raises(ValueError, match="http"):
        Settings(_env_file=None)


def test_local_url_trailing_slash_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://host:8001/v1/")
    assert Settings(_env_file=None).local_llm_base_url == "http://host:8001/v1"


def test_empty_local_key_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "")
    assert Settings(_env_file=None).local_llm_api_key is None


# --- chat factory --------------------------------------------------------------------------------


def test_ollama_builds_chat_ollama_with_the_configured_options(tmp_path: Path) -> None:
    from langchain_ollama import ChatOllama

    models = build_chat_models(
        make_settings(
            tmp_path,
            llm_provider="ollama",
            llm_model="qwen2.5:7b-instruct",
            llm_fast_model="qwen2.5:3b",
            ollama_base_url="http://ollama:11434",
            llm_max_output_tokens=500,
            llm_fast_max_output_tokens=100,
            agent_timeout_s=90,
        )
    )
    assert isinstance(models.main, ChatOllama) and isinstance(models.fast, ChatOllama)
    assert models.main.model == "qwen2.5:7b-instruct" and models.fast.model == "qwen2.5:3b"
    assert models.main.base_url == "http://ollama:11434"
    assert models.main.num_predict == 500 and models.fast.num_predict == 100
    assert models.main.client_kwargs == {"timeout": 90.0}
    assert models.provider == "ollama" and models.main_name == "qwen2.5:7b-instruct"


def test_openai_compatible_builds_chat_openai_without_anthropic_options(tmp_path: Path) -> None:
    from langchain_openai import ChatOpenAI

    models = build_chat_models(
        make_settings(
            tmp_path,
            llm_provider="openai_compatible",
            llm_model="llama-3.1-8b",
            llm_fast_model="llama-3.1-8b",
            local_llm_base_url="http://localhost:8001/v1",
            llm_max_output_tokens=700,
            llm_effort="high",
            llm_prompt_cache=True,
        )
    )
    assert isinstance(models.main, ChatOpenAI)
    assert models.main.openai_api_base == "http://localhost:8001/v1"
    assert models.main.max_tokens == 700
    assert models.main.openai_api_key is not None
    assert models.main.openai_api_key.get_secret_value() == "not-needed"  # type: ignore[union-attr]
    assert not models.main.model_kwargs  # no effort / cache options leaked
    assert models.provider == "openai_compatible"


def test_openai_compatible_uses_the_configured_key(tmp_path: Path) -> None:
    from langchain_openai import ChatOpenAI

    models = build_chat_models(
        make_settings(
            tmp_path,
            llm_provider="openai_compatible",
            llm_model="m",
            llm_fast_model="m",
            local_llm_base_url="http://x/v1",
            local_llm_api_key="local-key-1",
        )
    )
    assert isinstance(models.main, ChatOpenAI) and models.main.openai_api_key is not None
    assert models.main.openai_api_key.get_secret_value() == "local-key-1"  # type: ignore[union-attr]


def test_openai_compatible_needs_a_base_url(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfigured) as caught:
        build_chat_models(
            make_settings(
                tmp_path, llm_provider="openai_compatible", llm_model="m", llm_fast_model="m"
            )
        )
    assert "LOCAL_LLM_BASE_URL" in caught.value.hint


@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_a_hosted_model_name_left_behind_is_not_configured(tmp_path: Path, provider: str) -> None:
    with pytest.raises(ProviderNotConfigured) as caught:
        build_chat_models(  # the default LLM_MODEL is claude-sonnet-5-5
            make_settings(tmp_path, llm_provider=provider, local_llm_base_url="http://x/v1")
        )
    assert "LLM_MODEL" in caught.value.hint


def test_importing_app_modules_does_not_import_local_provider_packages() -> None:
    code = (
        "import sys, app.llm.factory, app.ingestion.embedder;"
        "bad=[m for m in ('langchain_ollama','langchain_openai','sentence_transformers','torch')"
        " if m in sys.modules]; sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()[-300:]


# --- prefixes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("nomic-embed-text", Prefixes("search_document: ", "search_query: ")),
        ("nomic-embed-text:v1.5", Prefixes("search_document: ", "search_query: ")),
        ("intfloat/multilingual-e5-small", Prefixes("passage: ", "query: ")),
        ("BAAI/bge-small-en-v1.5", Prefixes("", BGE_QUERY)),
        ("mxbai-embed-large", Prefixes("", BGE_QUERY)),
        ("BAAI/bge-m3", Prefixes()),
        ("some-other-model", Prefixes()),
    ],
)
def test_builtin_prefix_table(model: str, expected: Prefixes) -> None:
    assert default_prefixes(model) == expected


def test_prefix_settings_override_the_table(tmp_path: Path) -> None:
    s = make_settings(tmp_path, embedding_model="nomic-embed-text", embedding_query_prefix="Q: ")
    assert resolve_prefixes(s) == Prefixes("search_document: ", "Q: ")
    s2 = make_settings(tmp_path, embedding_model="x", embedding_document_prefix="D: ")
    assert resolve_prefixes(s2) == Prefixes("D: ", "")


# --- server embedders ----------------------------------------------------------------------------


def build(
    cls: type[LocalServerEmbedder], handler: Handler, **kwargs: Any
) -> tuple[LocalServerEmbedder, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def no_sleep(_s: float) -> None:
        return None

    params: dict[str, Any] = {
        "base_url": "http://srv:1234",
        "model": "m",
        "dim": DIM,
        "api_key": "",
        "batch_size": 2,
        "client": httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        "sleep": no_sleep,
    }
    params.update(kwargs)
    return cls(**params), seen


def ollama_ok(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["input"]
    return httpx.Response(200, json={"embeddings": [[3.0, 4.0, 0.0, 0.0] for _ in texts]})


async def test_ollama_request_shape_batching_and_normalisation() -> None:
    embedder, seen = build(OllamaEmbedder, ollama_ok, model="nomic-embed-text")
    vectors = await embedder.embed_documents(["a", "b", "c"])
    assert len(vectors) == 3 and vectors[0] == pytest.approx([0.6, 0.8, 0.0, 0.0])
    assert [len(json.loads(r.content)["input"]) for r in seen] == [2, 1]
    assert str(seen[0].url) == "http://srv:1234/api/embed"
    assert json.loads(seen[0].content)["model"] == "nomic-embed-text"
    assert "authorization" not in seen[0].headers


async def test_ollama_applies_document_and_query_prefixes() -> None:
    embedder, seen = build(
        OllamaEmbedder,
        ollama_ok,
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )
    await embedder.embed_documents(["alpha"])
    await embedder.embed_query("beta")
    assert json.loads(seen[0].content)["input"] == ["search_document: alpha"]
    assert json.loads(seen[1].content)["input"] == ["search_query: beta"]


async def test_ollama_model_not_pulled_suggests_the_pull_command() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": 'model "x" not found, try pulling it first'})

    embedder, _ = build(OllamaEmbedder, handler, model="nomic-embed-text")
    with pytest.raises(EmbeddingError, match="ollama pull nomic-embed-text") as caught:
        await embedder.embed_query("q")
    assert not caught.value.retryable


async def test_unreachable_server_error_names_the_address_and_fails_fast() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    embedder, seen = build(OllamaEmbedder, handler)
    with pytest.raises(EmbeddingError, match=r"srv:1234.*could not be reached") as caught:
        await embedder.embed_query("q")
    assert caught.value.retryable
    assert len(seen) == 3  # 1 try + 2 retries, not minutes of backoff


async def test_dimension_mismatch_names_both_numbers() -> None:
    embedder, _ = build(OllamaEmbedder, ollama_ok, dim=768)
    with pytest.raises(EmbeddingError) as caught:
        await embedder.embed_query("q")
    assert "size 4" in caught.value.message and "EMBEDDING_DIM is 768" in caught.value.message


def compat_handler(request: httpx.Request) -> httpx.Response:
    texts = json.loads(request.content)["input"]
    data = [{"index": i, "embedding": [float(i + 1), 0.0, 0.0, 0.0]} for i in range(len(texts))]
    return httpx.Response(200, json={"data": list(reversed(data))})  # out of order


async def test_openai_compatible_request_shape_and_ordering() -> None:
    embedder, seen = build(OpenAICompatibleEmbedder, compat_handler, model="bge")
    vectors = await embedder.embed_documents(["a", "b"])
    assert str(seen[0].url) == "http://srv:1234/embeddings"
    assert json.loads(seen[0].content) == {"input": ["a", "b"], "model": "bge"}
    assert vectors == [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]  # sorted by index, normalised
    assert "authorization" not in seen[0].headers


async def test_openai_compatible_sends_the_key_only_when_set() -> None:
    embedder, seen = build(OpenAICompatibleEmbedder, compat_handler, api_key="local-key-1")
    await embedder.embed_query("q")
    assert seen[0].headers["authorization"] == "Bearer local-key-1"


async def test_openai_compatible_404_hints_at_the_v1_address() -> None:
    embedder, _ = build(OpenAICompatibleEmbedder, lambda _r: httpx.Response(404))
    with pytest.raises(EmbeddingError, match="/v1"):
        await embedder.embed_query("q")


def test_get_embedder_builds_the_local_providers(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path,
        embedding_provider="ollama",
        embedding_model="nomic-embed-text",
        ollama_base_url="http://ollama:11434",
    )
    e = get_embedder(s)
    assert isinstance(e, OllamaEmbedder) and e.base_url == "http://ollama:11434"
    assert e.document_prefix == "search_document: " and e.query_prefix == "search_query: "

    s = make_settings(
        tmp_path,
        embedding_provider="openai_compatible",
        embedding_model="m",
        local_llm_base_url="http://llm/v1",
        local_embedding_base_url="http://emb/v1",
    )
    e2 = get_embedder(s)
    assert isinstance(e2, OpenAICompatibleEmbedder) and e2.base_url == "http://emb/v1"
    s = make_settings(
        tmp_path, embedding_provider="openai_compatible", local_llm_base_url="http://llm/v1"
    )
    e3 = get_embedder(s)
    assert isinstance(e3, OpenAICompatibleEmbedder) and e3.base_url == "http://llm/v1"


def test_openai_compatible_embedder_without_an_address_is_not_configured(tmp_path: Path) -> None:
    with pytest.raises(EmbeddingNotConfiguredError, match="LOCAL_LLM_BASE_URL"):
        get_embedder(make_settings(tmp_path, embedding_provider="openai_compatible"))


# --- in-process embedder (fake sentence_transformers) --------------------------------------------


class FakeArray:
    def __init__(self, rows: list[list[float]]) -> None:
        self._rows = rows

    def tolist(self) -> list[list[float]]:
        return self._rows


class FakeModel:
    instances = 0

    def __init__(self, name: str, **kwargs: Any) -> None:
        FakeModel.instances += 1
        self.name = name
        self.kwargs = kwargs
        self.calls: list[tuple[list[str], dict[str, Any], str]] = []

    def encode(self, texts: list[str], **kwargs: Any) -> FakeArray:
        self.calls.append((texts, kwargs, threading.current_thread().name))
        return FakeArray([[1.0, 2.0, 2.0, 0.0] for _ in texts])


@pytest.fixture
def fake_st(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(embedder_module, "_MODELS", {})
    FakeModel.instances = 0
    return module


async def test_local_embedder_loads_once_encodes_off_the_event_loop(
    fake_st: types.ModuleType, tmp_path: Path
) -> None:
    e = LocalSentenceTransformerEmbedder(
        model="BAAI/bge-small-en-v1.5",
        dim=DIM,
        device="cpu",
        cache_dir=tmp_path / "models",
        batch_size=7,
        query_prefix="Q: ",
    )
    docs = await e.embed_documents(["a", "b"])
    query = await e.embed_query("c")
    assert FakeModel.instances == 1  # cached per process
    assert docs[0] == pytest.approx([1 / 3, 2 / 3, 2 / 3, 0.0]) and len(query) == DIM
    model = embedder_module._MODELS[("BAAI/bge-small-en-v1.5", "cpu", str(tmp_path / "models"))]
    assert model.name == "BAAI/bge-small-en-v1.5"
    assert model.kwargs == {
        "device": "cpu",
        "cache_folder": str(tmp_path / "models"),
        "trust_remote_code": False,
    }
    (texts_a, opts, thread_a), (texts_q, _, _) = model.calls
    assert texts_a == ["a", "b"] and texts_q == ["Q: c"]
    assert opts["normalize_embeddings"] is True and opts["batch_size"] == 7
    assert thread_a != threading.main_thread().name  # asyncio.to_thread


async def test_local_embedder_dimension_mismatch(fake_st: types.ModuleType) -> None:
    e = LocalSentenceTransformerEmbedder(model="m", dim=384, device="cpu")
    with pytest.raises(EmbeddingError, match=r"size 4.*EMBEDDING_DIM is 384"):
        await e.embed_query("q")


async def test_local_embedder_without_the_package_explains_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # import raises ImportError
    e = LocalSentenceTransformerEmbedder(model="m", dim=DIM, device="cpu")
    with pytest.raises(EmbeddingNotConfiguredError, match="--extra local-embeddings"):
        await e.embed_query("q")


def test_auto_device_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingestion.embedder import _pick_device

    def torch_with(cuda: bool, mps: bool) -> types.ModuleType:
        torch = types.ModuleType("torch")
        torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)  # type: ignore[attr-defined]
        torch.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
            mps=types.SimpleNamespace(is_available=lambda: mps)
        )
        return torch

    assert _pick_device("cpu") == "cpu"
    monkeypatch.setitem(sys.modules, "torch", torch_with(True, False))
    assert _pick_device("auto") == "cuda"
    monkeypatch.setitem(sys.modules, "torch", torch_with(False, True))
    assert _pick_device("auto") == "mps"
    monkeypatch.setitem(sys.modules, "torch", torch_with(False, False))
    assert _pick_device("auto") == "cpu"


def test_get_embedder_local_uses_data_dir_models(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path,
        embedding_provider="local",
        embedding_model="BAAI/bge-small-en-v1.5",
        embedding_dim=384,
        local_embedding_device="cpu",
    )
    e = get_embedder(s)
    assert isinstance(e, LocalSentenceTransformerEmbedder)
    assert e._cache_dir == Path(s.data_dir) / "models"
    assert e.query_prefix == BGE_QUERY and e.document_prefix == ""


# --- production check ----------------------------------------------------------------------------


def _messages(settings: Settings) -> dict[str, str]:
    return {r.name: r.status for r in production_check._provider_checks(settings)}


def test_production_check_local_chat_needs_an_address(tmp_path: Path) -> None:
    s = make_settings(tmp_path, llm_provider="openai_compatible", llm_model="m", llm_fast_model="m")
    assert _messages(s)["LOCAL_LLM_BASE_URL"] == production_check.FAIL


def test_production_check_warns_about_plain_http_to_another_host(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path,
        llm_provider="ollama",
        llm_model="qwen2.5:7b-instruct",
        llm_fast_model="qwen2.5:7b-instruct",
        ollama_base_url="http://gpu-box.lan:11434",
    )
    assert _messages(s)["OLLAMA_BASE_URL (chat)"] == production_check.WARN
    ok = s.model_copy(update={"ollama_base_url": "http://localhost:11434"})
    assert _messages(ok)["OLLAMA_BASE_URL (chat)"] == production_check.OK
    tls = s.model_copy(update={"ollama_base_url": "https://gpu-box.lan"})
    assert _messages(tls)["OLLAMA_BASE_URL (chat)"] == production_check.OK


def test_production_check_local_embeddings_need_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = make_settings(tmp_path, embedding_provider="local")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert _messages(s)["Embeddings (local)"] == production_check.FAIL
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.ModuleType("x"))
    assert _messages(s)["Embeddings (local)"] == production_check.OK


# --- licence: CPU-only PyTorch -------------------------------------------------------------------


def test_lock_has_no_proprietary_cuda_packages_and_torch_is_cpu_only() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[2]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    names = {p["name"] for p in lock["package"]}
    assert not [n for n in names if n.startswith("nvidia-")]
    assert "triton" not in names
    torches = [p for p in lock["package"] if p["name"] == "torch"]
    assert torches
    assert all(p["source"] == {"registry": "https://download.pytorch.org/whl/cpu"} for p in torches)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    extra = project["project"]["optional-dependencies"]["local-embeddings"]
    assert any(dep.startswith("torch") for dep in extra)  # needed for tool.uv.sources to apply
    assert project["tool"]["uv"]["sources"]["torch"] == [{"index": "pytorch-cpu"}]
    assert project["tool"]["uv"]["index"][0]["explicit"] is True
