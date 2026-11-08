# Deploying CoursePilot

CoursePilot is one Python application (web app + JSON API) plus one background worker, with SQLite files on a local disk. Nothing needs Docker or an installed database server. Pick the guide that fits:

| Guide | When |
|---|---|
| [A. Linux VPS with Caddy](#a-linux-vps-with-caddy-recommended) | Recommended: public or campus-wide use, automatic HTTPS |
| [B. University Windows server](#b-university-windows-server) | Your IT department gives you a Windows machine |
| [C. One machine on a trusted LAN](#c-one-machine-on-a-trusted-lan-no-https) | Small closed network, no TLS (`ALLOW_INSECURE_HTTP`) |
| [D. PaaS with a persistent disk](#d-paas-with-a-persistent-disk-flyio-render) | Fly.io volume or Render disk |

The common topics (R2 bucket, backups, upgrades, monitoring, rotating keys) are at the end.

## What runs where

- **Web app**: `python -m app.cli serve` (migrates, then serves on `APP_HOST:APP_PORT`; no browser). `start.*` is the local dev launcher and stays that.
- **Worker**: `python -m app.worker` (parses and embeds uploads). It must see the same `data/` folder as the web app (same machine).
- **Data**: `data/coursepilot.db`, `data/checkpoints.db`, and uploaded files in `data/files` (or an S3/R2 bucket with `STORAGE_BACKEND=s3`). SQLite must be on a local disk, never a network share.
- **Downloads stream through the app** (also with S3/R2): the bucket is private, there are no presigned URLs, so every download passes the same login check in one place.
- Run **one** web process and **one** worker (SQLite is single-writer; login throttling is in-process).

Run `python -m app.cli check --production` (from `backend/`, as `uv run python -m app.cli check --production`) before going live and after every config change. It prints a checklist and exits non-zero on failures.

## Production `.env`

`setup` writes `.env`. For production make sure of:

```
APP_ENV=production
APP_HOST=127.0.0.1          # behind a proxy; 0.0.0.0 only for guide C
APP_PORT=8000
BASE_URL=https://study.example.edu   # exactly what users type; must be https (except guide C)
SECRET_KEY=<64 random characters>    # setup generates it
BEHIND_PROXY=true                    # only when a trusted proxy (Caddy, cloudflared) is in front
ANTHROPIC_API_KEY=...  (or GOOGLE_API_KEY)
GOOGLE_API_KEY / VOYAGE_API_KEY / OPENAI_API_KEY for the embedding provider
TAVILY_API_KEY=...   (optional, web fallback)
```

`/docs` is disabled in production. `.env` is written owner-only; keep it out of backups you share and keep a copy in a password manager.

## Cloudflare R2 bucket (optional, for uploaded files)

Local disk storage is the default and is fine on a VPS with backups. Use R2 when the host disk is small or ephemeral.

1. Cloudflare dashboard > R2 > Create bucket (e.g. `coursepilot-materials`). Keep it **private** (no public access, no custom domain).
2. R2 > Manage API tokens > Create API token: permission **Object Read & Write**, scoped to that bucket only. Copy the Access Key ID and Secret Access Key (shown once).
3. Note the account id; the endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
4. In `.env`:
   ```
   STORAGE_BACKEND=s3
   S3_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
   S3_BUCKET=coursepilot-materials
   S3_ACCESS_KEY_ID=...
   S3_SECRET_ACCESS_KEY=...
   S3_REGION=auto
   ```
5. `check --production` tests the bucket. Existing files are **not** moved automatically when you switch backends: copy `data/files/<aa>/<key>` to the bucket as `files/<aa>/<key>` (e.g. with `rclone copy data/files r2:coursepilot-materials/files`) before switching.

The bucket is not part of `backup`; enable R2 bucket versioning/replication or copy it with rclone on a schedule. The worker also aborts multipart uploads left over from interrupted uploads. (Only tested against the moto S3 emulator in CI; try one upload on your real bucket after setup.)

## A. Linux VPS with Caddy (recommended)

**Prerequisites**: a Linux VPS (Ubuntu/Debian, 1 vCPU / 2 GB is enough), Python 3.12, a DNS name pointing at it, ports 80/443 open.

1. **User and install**
   ```bash
   sudo useradd --system --create-home --home-dir /opt/coursepilot --shell /bin/bash coursepilot
   cd /tmp && curl -LO https://github.com/<owner>/CoursePilot/releases/download/vX.Y.Z/coursepilot-vX.Y.Z.zip
   sha256sum -c coursepilot-vX.Y.Z.zip.sha256     # after downloading the .sha256 file too
   sudo -u coursepilot unzip coursepilot-vX.Y.Z.zip -d /tmp/cp && sudo -u coursepilot cp -a /tmp/cp/coursepilot-vX.Y.Z/. /opt/coursepilot/
   sudo -u coursepilot -H /opt/coursepilot/setup.sh   # asks for admin, keys; writes .env, migrates
   ```
2. **Edit `/opt/coursepilot/.env`** for production (see above). Run `check --production`.
3. **Services**: `sudo cp /opt/coursepilot/deploy/coursepilot-*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now coursepilot-web coursepilot-worker`.
4. **HTTPS**: install Caddy (<https://caddyserver.com/docs/install>), copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile`, set your hostname, `sudo systemctl reload caddy`. Certificates are automatic.
5. **Firewall**: allow only 22, 80, 443 (`sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable`). Port 8000 stays closed (the app listens on 127.0.0.1).
6. **First admin**: created by `setup.sh`. Or `cd /opt/coursepilot/backend && sudo -u coursepilot .venv/bin/python -m app.cli create-admin`. Log in, create faculty and student accounts.
7. **Backups**: cron for the `coursepilot` user (`sudo -u coursepilot crontab -e`):
   ```
   15 2 * * * cd /opt/coursepilot/backend && .venv/bin/python -m app.cli backup --out /var/backups/coursepilot --keep 14 >> /opt/coursepilot/data/backup.log 2>&1
   ```
   (create `/var/backups/coursepilot` owned by `coursepilot`), then copy that folder off the machine (rsync, rclone to another bucket). Optional continuous replication of the databases: `deploy/litestream.yml`.
8. **Logs**: `journalctl -u coursepilot-web -u coursepilot-worker -f` (JSON lines, no secrets).

Alternative to Caddy and open ports: a Cloudflare Tunnel (`deploy/cloudflared-config.yml`); keep `BEHIND_PROXY=true`.

## B. University Windows server

**Prerequisites**: Windows Server/10/11, Python 3.12 from python.org (add to PATH), an administrator account, and HTTPS in front. Typical options: IIS with the URL Rewrite + ARR modules as reverse proxy and your university certificate, or a Cloudflare Tunnel (`deploy/cloudflared-config.yml`, runs as a Windows service), or Caddy for Windows. In every case the app listens on `127.0.0.1:8000` and `.env` has `BEHIND_PROXY=true`. IIS/ARR: turn response buffering off for the site (server-sent events).

1. Unzip the release to `C:\CoursePilot`, run `setup.bat` in a normal PowerShell/cmd (asks for admin and keys, writes `.env`).
2. Edit `.env` for production; run `cd C:\CoursePilot\backend && python -m uv run python -m app.cli check --production`.
3. **Run as services** (either):
   - Task Scheduler (no extra software): elevated PowerShell, `powershell -ExecutionPolicy Bypass -File C:\CoursePilot\deploy\register-windows-tasks.ps1 -InstallDir C:\CoursePilot`. Logs go to `C:\CoursePilot\logs`. It runs as SYSTEM unless you pass `-User 'DOMAIN\svc-coursepilot' -Password (Read-Host -AsSecureString)`; prefer a dedicated low-privilege account that can write to the install folder. Stopping a task is a hard kill; an interrupted job is retried automatically about 2 minutes later.
   - NSSM (<https://nssm.cc>, graceful stops): `nssm install CoursePilotWeb C:\CoursePilot\backend\.venv\Scripts\python.exe -m app.cli serve`, set the startup directory to `C:\CoursePilot\backend`; `nssm install CoursePilotWorker ...python.exe -m app.worker` likewise; set "I/O" to log files and "Exit actions" to restart.
4. **Firewall**: do not open 8000; only the proxy port (443). `New-NetFirewallRule -DisplayName CoursePilot-HTTPS -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow`.
5. **Backups**: Task Scheduler, daily: program `C:\CoursePilot\backend\.venv\Scripts\python.exe`, arguments `-m app.cli backup --out D:\Backups\CoursePilot --keep 14`, start in `C:\CoursePilot\backend`. Copy the folder to another machine. The database itself must stay on a local disk (not a mapped drive); backups may go anywhere.
6. **Logs**: `C:\CoursePilot\logs\web.log` and `worker.log` (Task Scheduler script) or the NSSM log files.

## C. One machine on a trusted LAN (no HTTPS)

For a closed network you trust, where passwords and cookies crossing in clear text is acceptable.

1. Unzip, run `setup.sh`/`setup.bat`; answer **yes** to "Will other computers connect... without HTTPS?" and give the `BASE_URL` (e.g. `http://192.168.1.20:8000`). This sets `APP_HOST=0.0.0.0`, `ALLOW_INSECURE_HTTP=true`.
2. Set `APP_ENV=production` if you want production behaviour; `check --production` reports the plain-http choice as a warning.
3. Start with `start.sh` / `start.bat` (dev launcher, runs both processes) or the systemd/Task Scheduler setup from A/B with `serve` and `BASE_URL` unchanged.
4. Firewall: allow port 8000 from the LAN only; never expose it to the internet. Admins see a banner about the unencrypted traffic.
5. Backups: as in A or B.

## D. PaaS with a persistent disk (Fly.io, Render)

These platforms build a container image **on their servers** from your repository or a `Dockerfile`/buildpack: nothing runs locally and you do not need Docker on your machine. Requirements: a persistent disk/volume (SQLite must live on it), a single instance, and the web app and worker in the **same** instance because they share that disk. Use `python -m app.cli serve --with-worker`, which runs the worker as a child process.

- **Build**: Python 3.12, `uv sync --frozen` in `backend/` (buildpack or Dockerfile that the platform builds remotely).
- **Start command**: `cd backend && python -m app.cli serve --with-worker` (or with uv: `uv run python -m app.cli serve --with-worker`).
- **Disk**: mount it at `/data` and set `DATA_DIR=/data`, `DATABASE_URL=sqlite+aiosqlite:////data/coursepilot.db`, `CHECKPOINT_DB_PATH=/data/checkpoints.db`. Or keep only the databases on the disk and use R2 for files.
- **Environment**: set everything from `.env` as platform secrets/env vars (`APP_ENV=production`, `APP_HOST=0.0.0.0`, `APP_PORT` = the platform's port, `BASE_URL=https://<your app host>`, `BEHIND_PROXY=true`, keys, `SECRET_KEY`). The platform terminates HTTPS. Note `APP_HOST=0.0.0.0` with `BEHIND_PROXY=true` is expected here; `check --production` warns about it, which you can accept because the platform only routes the proxy to the port.
- **First admin**: open a shell on the instance (`fly ssh console`, Render shell) and run `python -m app.cli create-admin`.
- **Backups**: platform volume snapshots plus `python -m app.cli backup --out /data/backups --keep 7` on a schedule (Render cron job needs the same disk, so prefer Fly machines' scheduled commands or Litestream to R2 from `deploy/litestream.yml`).
- Free tiers that sleep or have ephemeral disks are not suitable.

## Backups and the restore drill

`python -m app.cli backup [--out DIR] [--keep N]` makes a dated folder `coursepilot-backup-<UTC>` with consistent copies of both databases (SQLite online backup API, safe while the app runs), the uploaded files (local storage only) and `manifest.json` (version, commit, sizes, SHA-256). It does **not** include `.env` or an S3/R2 bucket.

**Drill** (do it once on a spare machine before you need it):

1. Install the same release on the spare machine and copy `.env`.
2. Stop the web app and worker.
3. `python -m app.cli restore /path/to/coursepilot-backup-<stamp>` (verifies every checksum, asks for confirmation; `--yes` skips). Existing files are kept as `*.pre-restore-<stamp>`.
4. Start web and worker (migrations run), log in, open a document and a chat. Check `/api/v1/health/ready`.

## Upgrading to a new release

1. `python -m app.cli backup` (see above).
2. Stop the worker and the web app (`systemctl stop coursepilot-worker coursepilot-web`, or stop the tasks).
3. Unzip the new release **over** the install folder. The zip contains no `.env` and no `data/`, so both are kept. Then `./setup.sh` again only if dependencies changed (it keeps your `.env`), or run `cd backend && uv sync --frozen`.
4. Start the services. Migrations run on start (`ExecStartPre`, or inside `serve`).
5. Check `/api/v1/health/version` and `/api/v1/health/ready`, and `check --production`.

Rollback: stop, `restore` the backup from step 1, put the previous release files back, start.

## Monitoring

- `GET /api/v1/health/live` (process up) and `GET /api/v1/health/ready` (database, sqlite-vec, storage; 503 with details when something fails). Point an uptime monitor (UptimeRobot, Healthchecks, Caddy health checks) at `ready`.
- Admin pages: jobs and usage in the web UI.
- Logs are structured JSON on stdout (journald, or the log files above); passwords, tokens and prompts are not logged at INFO.

## Rotating `SECRET_KEY`

Edit `SECRET_KEY` in `.env` (>= 32 random characters, e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`) and restart web and worker. It only signs the pre-login CSRF token, so the effect is that login forms open before the restart stop working; reload the page. Rotate after a suspected leak, and also reset the admin passwords if the whole `.env` leaked (and revoke the API keys in it).

## Changing the embedding provider, model or dimension

Edit `EMBEDDING_PROVIDER`/`EMBEDDING_MODEL`/`EMBEDDING_DIM` and the key, **stop the worker**, run `python -m app.cli reindex-all`, then start the worker. Documents are not searchable until they are re-processed; until then search answers `index_mismatch`. Costs embedding API calls for every document.

## Changing keys

The worker reads `.env` once at startup: restart it after adding an API key, then re-index failed documents from the Materials page.
