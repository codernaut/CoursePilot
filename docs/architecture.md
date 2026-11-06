# Architecture

Keep in sync with [CLAUDE.md](../CLAUDE.md), which is the source of truth.

## Components

```
Browser ──HTTP──▶ FastAPI app (uvicorn)  ── pages (Jinja2 + HTMX), /api/v1 JSON, SSE chat, static files
                      │
                      ├── services ──▶ SQLite  data/coursepilot.db  (WAL)
                      │                 ├── relational tables (users, sessions, years/blocks/modules, documents, chunks, ...)
                      │                 ├── chunk_vectors  (sqlite-vec vec0)
                      │                 └── chunks_fts     (FTS5)
                      ├── Storage ───▶ LocalStorage (data/files)  |  S3Storage (R2)
                      ├── agent ─────▶ LLM (Anthropic | Gemini)  +  checkpoints.db (SQLite)
                      │               └─▶ Tavily (web fallback)
                      └── enqueue ───▶ jobs table
Worker (python -m app.worker) ──claims jobs──▶ parse → chunk → embed (Gemini | Voyage | OpenAI) → write chunks
```

Programme structure: Year › Block › Module (default 3 × 3 × 3). Documents belong to a module; chunks also carry block and year ids.

## Login

```
GET /login → POST /login → verify argon2 → insert sessions(sha256(token), csrf) → HttpOnly cookie → redirect
every other route: cookie → sessions row (not expired, user active) → request user
state-changing requests: CSRF token must match the session
```

## Upload and processing

```
faculty → POST /api/v1/modules/{id}/documents → validate → Storage.put(random key) → documents(uploaded)
        → jobs(ingest_document) → 202
worker  → claim (UPDATE … RETURNING) → parse → chunk → embed → one transaction:
          replace chunks + chunk_vectors + chunks_fts → documents.status=ready
          failure → backoff retry → failed + readable error
```

## Chat

```
POST /api/v1/chat {module_id|null, message} → SSE
  rewrite → retrieve(module) → assess ─ insufficient ─▶ retrieve(block) → assess ─▶ retrieve(all) → assess
                                   └ sufficient ──────────────────────────────────────┐
  all tiers insufficient and web enabled ─▶ web_search (Tavily) ───────────────────────┤
  answer (stream tokens, [C#]/[W#]) ─▶ validate_citations ─▶ persist message ─▶ done  ◀┘
events: status, token, citations, done, error
```

## Retrieval

```
query ─▶ embed (same model as chunks) ─▶ vec0 top-k ┐
query ─▶ sanitised FTS5 MATCH top-k                 ┴─▶ RRF (k=60) ─▶ [rerank] ─▶ top RETRIEVAL_TOP_K
scope (module | block | all) + access predicate are in the SQL WHERE clause (retrieval/access.py)
```
