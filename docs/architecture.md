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
faculty → POST /api/v1/modules/{id}/documents (multipart, one or more files)
        → per file, while streaming: sanitise name, extension allow-list, magic bytes / UTF-8 check,
          sha256, size cap (UPLOAD_MAX_MB) → Storage (temp file, atomic rename; random key)
        → PPTX/DOCX: zip structure + decompressed-size cap → per-module sha256 dedupe (409)
        → one transaction: documents(uploaded) + jobs(ingest_document, ids only) + audit_log → 202
        (any failure after the object was stored deletes the object again)
delete  → documents.status=deleting (hidden) + jobs(delete_document); reindex → jobs(reindex_document)
download → GET /api/v1/documents/{id}/download streams from Storage (inline for PDF, attachment otherwise,
          nosniff, CSP sandbox; inline PDFs get CSP default-src 'none' instead because
          Chromium's PDF viewer does not render sandboxed documents)
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
