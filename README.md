# CoursePilot

A RAG study assistant for a university programme's teaching material. Faculty upload slides, PDFs and notes into a fixed **Year › Block › Module** structure; students chat with an assistant that answers **from the uploaded material first, with citations**, and falls back to the web (clearly labelled) only when the material is insufficient.

> Status: early development (milestones M0-M2 done: accounts, login, user admin, the programme structure, and material upload with a materials page; processing and chat come next). See [CLAUDE.md](CLAUDE.md) for scope, architecture and the build plan.

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

### Letting other computers connect (no HTTPS)

By default CoursePilot listens on this computer only. `setup` asks "Will other computers connect to this machine over the network without HTTPS?". Answer yes only on a closed network you trust: passwords and session cookies then travel unencrypted. Setup sets `APP_HOST=0.0.0.0`, `ALLOW_INSECURE_HTTP=true` and `BASE_URL` (for example `http://192.168.1.20:8000`); admins see a warning banner on every page. Anywhere else, put CoursePilot behind HTTPS (a reverse proxy) instead.

## Manual commands

If `uv` is not on PATH, use `python -m uv` instead.

```bash
cd backend
uv sync
uv run python -m app.cli setup                       # what setup.* calls
uv run python -m app.cli create-admin                # add an administrator (hidden password prompt)
uv run alembic upgrade head                          # or: python -m app.cli migrate
uv run uvicorn app.main:app --reload --port 8000     # http://localhost:8000 (API docs at /docs after login)
uv run python -m app.worker                          # background worker
```

## Checks

```bash
cd backend
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests ../scripts/build_release.py && uv run pytest
```

## Versions and releases

CoursePilot follows [SemVer](https://semver.org/) and stays on `0.x` while it is in development (`-rc.N` for pre-releases). The version lives in `backend/pyproject.toml`; the running version and git commit are shown in the page footer and at `GET /api/v1/health/version`.

Releases are cut by the owner only: bump `[project].version` in a commit, push, wait for green CI, then `git tag vX.Y.Z && git push origin vX.Y.Z` (or GitHub: Releases > Draft a new release). The release workflow checks the tag is on `main` and matches the version (`v0.2.0` = `0.2.0`, `v0.2.0-rc.1` = `0.2.0rc1`), runs the checks, builds `coursepilot-<tag>.zip` with a SHA-256 file (`python scripts/build_release.py --tag vX.Y.Z` does the same locally) and publishes a GitHub Release (a pre-release for `-rc` tags). Details are in [CLAUDE.md](CLAUDE.md) section 16.

## Layout

- `backend/` — FastAPI app (API + server-rendered web UI), processing worker, retrieval and agent (Python 3.12, uv)
- `setup.*`, `start.*` — thin install/start scripts
- `scripts/` — release packaging (`build_release.py`)
- `docs/` — architecture notes and ADRs

## License

GNU Affero General Public License v3.0 (AGPL-3.0-only) — see [LICENSE](LICENSE). If you run a modified version of CoursePilot as a network service, you must offer its source code to its users.
