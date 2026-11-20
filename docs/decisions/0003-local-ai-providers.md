# 0003. Local AI providers (Ollama, OpenAI-compatible servers, in-process embeddings)

Date: 2026-11-20. Status: accepted (owner decision; implements deployment profile 2 of "Deployment profiles" in CLAUDE.md section 1).

## Context

Some institutions cannot or will not send questions and course material to a cloud AI provider. Deployment profile 2 (on-premises, local AI) was planned since 2026-11-17 and needs a chat model and an embedding model that run on hardware the institution controls. The owner will test local models later; Anthropic and Gemini remain the recommended default.

## Decision

- **Chat**: two new `LLM_PROVIDER` values behind `llm/factory.py`. `ollama` uses `langchain-ollama` (`ChatOllama`, `OLLAMA_BASE_URL`). `openai_compatible` uses `langchain-openai` (`ChatOpenAI` with `base_url=LOCAL_LLM_BASE_URL`, optional `LOCAL_LLM_API_KEY`), which covers vLLM, LM Studio, the llama.cpp server, LocalAI and Ollama's own `/v1`. Both send temperature and none of the Anthropic-only options (effort, prompt caching). Both packages are MIT and imported lazily. The agent graph is unchanged: it only needs a LangChain chat model.
- **Embeddings**: three new `EMBEDDING_PROVIDER` values. `ollama` (`POST /api/embed`) and `openai_compatible` (`POST {base}/embeddings`) are plain `httpx` classes like the existing providers (same batching, retry and normalisation, with a shorter retry budget because a local server that is down should fail fast). `local` runs `sentence-transformers` inside the process: loaded once, encoded in a thread (`asyncio.to_thread`), model cache in `DATA_DIR/models`, `trust_remote_code=False`.
- **PyTorch is an optional extra** (`local-embeddings`), never installed by a plain `uv sync`: it is a 1-2 GB download that most installations do not need. `uv run` syncs inexactly by default, so an installed extra survives `start.*`; setup offers to install it when `local` is chosen. The Docker image takes `--build-arg EXTRAS=local-embeddings`. CI does not install it; tests inject a fake module.
- **Prefixes**: models such as nomic-embed-text, the e5 family, bge-*-en and mxbai-embed-large expect a task prefix for passages and/or queries. A built-in table (`ingestion/prefixes.py`) supplies them; `EMBEDDING_DOCUMENT_PREFIX` / `EMBEDDING_QUERY_PREFIX` override. They are applied by the local providers only (hosted providers have task-type parameters).
- **Embedding invariant**: unchanged (`app_meta` holds provider, model, dimension). The prefixes are deliberately not stored there: adding keys would make every existing index report `index_mismatch` after the upgrade. Changing a prefix therefore needs a manual `reindex-all`, documented next to the setting.
- **Docker**: an optional `ollama` service in `compose.yaml` behind the profile `local-ai` (off by default), named volume for models, no published port, GPU block commented out. No Kubernetes manifest: point `OLLAMA_BASE_URL` / `LOCAL_LLM_BASE_URL` at an external service.
- **Setup and checks**: the setup menus list Anthropic and Gemini first and the local options last, labelled for testing; `check --production` fails when a chosen local provider has no address or a hosted model name, and warns about plain http to another machine.

## Alternatives considered

- `llama-cpp-python` in-process for chat: compiles native code per platform, hard to install for non-specialists; a server (Ollama, llama.cpp server) is easier and already covered by `openai_compatible`.
- Hugging Face `transformers` directly for embeddings: more code (pooling, normalisation, batching) than `sentence-transformers`, which wraps it.
- `fastembed` (ONNX, no PyTorch): lighter, but a smaller model list and another dependency tree; may be added later behind the same embedder interface.
- Hugging Face Text Embeddings Inference (TEI) as a server: needs Docker or a Rust build; reachable already through `openai_compatible`.
- Vendor SDKs (`ollama`, `openai`) for embeddings: two more large dependency trees for one JSON POST each (same reasoning as ADR 0001 for the hosted providers). The `ollama` and `openai` packages arrive anyway through the chat integrations, but the embedder does not use them.

## Consequences

- New runtime dependencies: `langchain-ollama`, `langchain-openai` (MIT); this raised `langchain-core` in the lock. Optional: `sentence-transformers` (Apache-2.0, pulls PyTorch, BSD).
- Quality and speed of local models depend on the hardware and the model; there is no automatic fallback to a hosted provider.
- **CPU-only PyTorch (owner decision, 2026-11-20).** The default PyPI PyTorch wheel on Linux pulls about 15 `nvidia-*` CUDA packages (NVIDIA proprietary licence) and `triton`, which CLAUDE.md section 2 excludes. `backend/pyproject.toml` therefore declares `torch` in the extra and pins it to the explicit `pytorch-cpu` index (`https://download.pytorch.org/whl/cpu`, `[tool.uv.sources]`). `uv.lock` contains no `nvidia-*` and no `triton` (test in `test_local_ai.py`). `LOCAL_EMBEDDING_DEVICE=cuda` is still accepted but the bundled CPU build cannot use it (a user who installs a CUDA PyTorch build by themselves can); `auto` picks cpu unless cuda or mps is really available. For GPU speed use Ollama or vLLM: separate programs, not dependencies.
- A local OpenAI-compatible server reached over plain http is the operator's responsibility (the production check warns).
- Not verified in CI or on the development machine: real Ollama, vLLM or PyTorch runs (no GPU, no server). The owner tests these.
