# CoursePilot

A RAG study assistant for university course material. Instructors upload slides, PDFs and notes; students chat with an assistant that answers **from the course material first, with citations**, and falls back to the web (clearly labelled) only when the material is insufficient.

> Status: early development (milestone M0). See [CLAUDE.md](CLAUDE.md) for scope, architecture and the build plan.

## Quick start (local dev)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker, Node 20+.

```bash
cp .env.example .env
docker compose up -d db minio

cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000   # http://localhost:8000/docs
```

## Checks

```bash
cd backend
uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest
```

## Layout

- `backend/` — FastAPI API, ingestion worker, retrieval and agent (Python 3.12, uv)
- `frontend/` — Next.js web app (from milestone M7)
- `docs/` — architecture notes and ADRs

## License

GNU Affero General Public License v3.0 (AGPL-3.0-only) — see [LICENSE](LICENSE). If you run a modified version of CoursePilot as a network service, you must offer its source code to its users.
