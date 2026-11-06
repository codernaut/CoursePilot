# CoursePilot

A RAG study assistant for a university programme's teaching material. Faculty upload slides, PDFs and notes into a fixed **Year › Block › Module** structure; students chat with an assistant that answers **from the uploaded material first, with citations**, and falls back to the web (clearly labelled) only when the material is insufficient.

> Status: early development (milestone M0 done, M1 next). See [CLAUDE.md](CLAUDE.md) for scope, architecture and the build plan.

No Docker, no Node, no externally installed servers: SQLite (with sqlite-vec and FTS5) for data and search, a FastAPI app that serves the web UI and the JSON API, and a background worker. Everything installs with pip/uv.

## Quick start

Requirements: Python 3.12.

```bash
# Windows
setup.bat
start.bat

# macOS / Linux
./setup.sh
./start.sh
```

`setup` installs [uv](https://docs.astral.sh/uv/) if needed, installs dependencies, asks for the first admin account, your AI provider (Anthropic Claude or Google Gemini), an embedding provider and optional Tavily key (any key may be skipped), writes `.env`, and prepares the database. `start` runs migrations, starts the web app and the worker, and opens your browser at http://localhost:8000. There is no self-registration: the admin creates faculty and student accounts.

(The scripts land with milestone M1. Until then, see the manual commands below.)

## Manual commands

If `uv` is not on PATH, use `python -m uv` instead.

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000   # http://localhost:8000/docs
```

## Checks

```bash
cd backend
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests && uv run pytest
```

## Layout

- `backend/` — FastAPI app (API + server-rendered web UI), processing worker, retrieval and agent (Python 3.12, uv)
- `setup.*`, `start.*` — thin install/start scripts (from M1)
- `docs/` — architecture notes and ADRs

## License

GNU Affero General Public License v3.0 (AGPL-3.0-only) — see [LICENSE](LICENSE). If you run a modified version of CoursePilot as a network service, you must offer its source code to its users.
