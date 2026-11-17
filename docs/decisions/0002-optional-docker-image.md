# 0002. Optional Docker image, Compose and Kubernetes manifests

Date: 2026-11-17. Status: accepted (owner decision; supersedes the "no Docker" wording of ADR 0001 for hosting only).

## Context

ADR 0001 removed Docker from the first plan so a non-specialist can install CoursePilot with pip/uv on one machine. Hosting providers and many institutions' platform teams, however, run containers (a VPS with Docker, Kubernetes). The owner decided (2026-11-17, "Deployment profiles") that a container image is an optional, additional way to host, never a requirement.

## Decision

- One `Dockerfile` at the repository root (build context = repo root): multi-stage, `python:3.12-slim-bookworm`, `uv sync --frozen --no-dev`, non-root user (uid/gid 10001), `/data` volume, healthcheck on `/api/v1/health/ready`, default command `python -m app.cli serve` (migrates first). The repo layout is kept under `/app` so `REPO_ROOT` resolves to `/app`; there is no `/app/.env`, so the `.env` refresh in `serve` is a no-op.
- One image, two roles: `web` (`app.cli serve`) and `worker` (`app.worker`). Configuration comes only from environment variables; container paths (`DATA_DIR`, `DATABASE_URL`, `CHECKPOINT_DB_PATH`, `APP_HOST`) are set by compose/Kubernetes and override any `.env`.
- `compose.yaml` (web + worker, one named volume, worker waits for web to be healthy) and plain Kubernetes manifests in `deploy/kubernetes/` (one pod, two containers, one `ReadWriteOnce` claim, `replicas: 1`, `strategy: Recreate`).
- Still a single instance on local block storage: SQLite has one writer, login throttling and rate limits are in-process, and the worker needs the same disk as the web app. Scaling out is explicitly unsupported.
- Docker stays optional: `setup.*`, `start.*`, the systemd/Windows service installs, development and the test suite do not use it. CI gets an extra `docker` job (build, `app.cli check` inside the image, compose smoke test, kustomize render).
- OCR (tesseract) is not in the image; `OCR_ENABLED` is off by default and the Dockerfile documents how to add it.

## Consequences

- No images are published yet. Publishing to a registry (for example GHCR from `release.yml`) is a later owner decision; users build locally or in their own CI.
- The release zip contains `Dockerfile`, `.dockerignore`, `compose.yaml` and `deploy/kubernetes/`.
- The uv version in the Dockerfile is pinned; bump it deliberately.
- Cloud profile 3b is available; local AI (profile 2) is still M11.
