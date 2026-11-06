# 0001. SQLite, FastAPI-rendered UI and a fixed programme structure

Date: 2026-11-06. Status: accepted (owner decision).

## Context

The first plan assumed Docker, Postgres + pgvector, MinIO, a Next.js frontend, per-course enrolment and self-service registration. The owner wants something a non-specialist can install and run on one machine (and later on one small host) with no Docker and no separately installed servers, for one programme with a fixed Years → Blocks → Modules structure.

## Decision

- **Database**: SQLite (one file under `DATA_DIR`, WAL mode) via SQLAlchemy 2 async + aiosqlite, Alembic with `render_as_batch=True`. Vectors in a sqlite-vec `vec0` table `chunk_vectors`; keywords in an FTS5 table `chunks_fts`; hybrid ranking with Reciprocal Rank Fusion (k=60).
- **Jobs**: a `jobs` table claimed with a single `UPDATE … WHERE id = (SELECT … LIMIT 1) RETURNING *`, processed by a separate worker process.
- **Chat memory**: `langgraph-checkpoint-sqlite` in its own file; `messages` rows stay the user-visible record.
- **Storage**: `Storage` interface; LocalStorage by default, S3Storage (R2-compatible) for production.
- **UI**: server-rendered Jinja2 + vendored HTMX + vanilla JS + plain CSS, served by the same FastAPI app (same origin, no CORS, no Node). The JSON API under `/api/v1` remains and shares the service layer.
- **Auth**: admin-created accounts only (`admin`, `faculty`, `student`), server-side sessions with CSRF tokens, argon2.
- **Structure**: fixed Years → Blocks → Modules (default 3 × 3 × 3 = 27), seeded from config with editable titles. Courses, institutions, memberships and invite codes are dropped.
- **Access**: every logged-in active user can search and read all material. The filter still lives only in `retrieval/access.py` so permissions can be added later.
- **Search scope**: module first, then its block, then all material, then the web.
- **Providers**: chat via Anthropic or Gemini; embeddings via Gemini (default), Voyage or OpenAI; web search via Tavily. Ollama and local embeddings are planned behind the same interfaces.

## Consequences

- Install is `pip`/`uv` only; tests run locally with temp SQLite files and fakes.
- SQLite allows one writer at a time. Keep write transactions short, use WAL and a busy timeout, and never hold a transaction across network calls. This is ample for one programme's upload and chat traffic, but heavy concurrent ingestion would queue.
- The app and worker must share a disk (one host). Horizontal scaling is not supported. If that becomes a limit, the migration path is libSQL/Turso (same SQL dialect) or Postgres + pgvector; the service layer, `retrieval/` and `jobs/` are the parts that would change.
- Changing the embedding model or dimension requires `reindex-all`, which recreates `chunk_vectors`.
- Because all logged-in users can search and read all uploaded material, faculty could expose copyrighted content to every account holder. The upload page must say so explicitly, uploaders must have the right to share, accounts are admin-created, and material is never served to anonymous visitors.
- Prompt-injection and access boundaries stay as in CLAUDE.md §1 and §13.
