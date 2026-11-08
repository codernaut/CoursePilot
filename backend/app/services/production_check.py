"""``python -m app.cli check --production``: a readable go-live checklist.

Each check returns OK, WARN (works, but look at it) or FAIL (do not go live). The checks do not
need ``APP_ENV=production`` to run, so they can be used before the switch.
"""

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.config import (
    INSECURE_SECRET_KEYS,
    MIN_PRODUCTION_SECRET_KEY_LENGTH,
    Settings,
)
from app.storage import Storage, get_storage

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_SQLITE_PREFIX = "sqlite+aiosqlite:///"
_NETWORK_FS = {
    "nfs",
    "nfs4",
    "cifs",
    "smb",
    "smb3",
    "smbfs",
    "afs",
    "ceph",
    "glusterfs",
    "fuse.sshfs",
}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class CheckResult:
    status: str
    name: str
    message: str


def is_network_path(path: Path) -> bool:
    """True when ``path`` is on a network share (UNC path, mapped drive, NFS/SMB mount)."""
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True
    if sys.platform == "win32":
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(text))[0]
        if drive:
            try:
                return bool(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4)
            except (AttributeError, OSError):
                return False
        return False
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    resolved = str(path.resolve())
    best = ("", "")
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount, fstype = parts[1].replace("\\040", " "), parts[2]
        if (resolved == mount or resolved.startswith(mount.rstrip("/") + "/")) and len(
            mount
        ) >= len(best[0]):
            best = (mount, fstype)
    return best[1] in _NETWORK_FS


def _key_checks(settings: Settings) -> list[CheckResult]:
    key = settings.secret_key.get_secret_value()
    if key.strip().lower() in INSECURE_SECRET_KEYS:
        return [
            CheckResult(
                FAIL, "SECRET_KEY", "is empty or a default value; run setup or set a random one"
            )
        ]
    if len(key) < MIN_PRODUCTION_SECRET_KEY_LENGTH:
        return [
            CheckResult(
                FAIL, "SECRET_KEY", f"is shorter than {MIN_PRODUCTION_SECRET_KEY_LENGTH} characters"
            )
        ]
    return [CheckResult(OK, "SECRET_KEY", "is set and long enough")]


def _url_checks(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    if settings.app_env != "production":
        results.append(
            CheckResult(FAIL, "APP_ENV", f"is '{settings.app_env}', must be 'production'")
        )
    else:
        results.append(CheckResult(OK, "APP_ENV", "is production"))
    https = settings.base_url.startswith("https://")
    if https:
        results.append(CheckResult(OK, "BASE_URL", f"is https ({settings.base_url})"))
    elif settings.allow_insecure_http and settings.base_url.startswith("http://"):
        results.append(
            CheckResult(
                WARN,
                "BASE_URL",
                "is http:// with ALLOW_INSECURE_HTTP=true; passwords and cookies cross the "
                "network unencrypted, use only on a closed trusted LAN",
            )
        )
    else:
        results.append(
            CheckResult(
                FAIL,
                "BASE_URL",
                "must be https:// (or set ALLOW_INSECURE_HTTP=true for a trusted LAN)",
            )
        )
    if settings.allow_insecure_http and https:
        results.append(
            CheckResult(
                WARN,
                "ALLOW_INSECURE_HTTP",
                "is true but has no effect with an https:// BASE_URL; set it to false",
            )
        )

    loopback = settings.app_host in _LOOPBACK_HOSTS
    if settings.behind_proxy and not loopback:
        results.append(
            CheckResult(
                WARN,
                "BEHIND_PROXY",
                f"is true but APP_HOST={settings.app_host} is reachable directly; clients could "
                "forge X-Forwarded-For and dodge login throttling. Bind to 127.0.0.1 or "
                "firewall the port",
            )
        )
    elif settings.behind_proxy:
        results.append(CheckResult(OK, "BEHIND_PROXY", "true and the app listens on loopback only"))
    elif https and loopback:
        results.append(
            CheckResult(
                WARN,
                "BEHIND_PROXY",
                "is false, yet BASE_URL is https and the app listens on loopback, so a reverse "
                "proxy is probably in front: every user would share the proxy's IP for login "
                "throttling. Set BEHIND_PROXY=true",
            )
        )
    else:
        results.append(CheckResult(OK, "BEHIND_PROXY", "false (no proxy in front)"))
    return results


def _provider_checks(settings: Settings) -> list[CheckResult]:
    def has(secret: object) -> bool:
        return secret is not None

    results: list[CheckResult] = []
    llm_keys = {"anthropic": settings.anthropic_api_key, "gemini": settings.google_api_key}
    llm_names = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GOOGLE_API_KEY"}
    if settings.llm_provider == "fake":
        results.append(CheckResult(FAIL, "LLM_PROVIDER", "is 'fake' (tests only)"))
    elif has(llm_keys[settings.llm_provider]):
        results.append(CheckResult(OK, "LLM key", f"{llm_names[settings.llm_provider]} is set"))
    else:
        results.append(
            CheckResult(
                FAIL,
                "LLM key",
                f"{llm_names[settings.llm_provider]} is missing; chat will not work",
            )
        )
    emb_keys = {
        "gemini": ("GOOGLE_API_KEY", settings.google_api_key),
        "voyage": ("VOYAGE_API_KEY", settings.voyage_api_key),
        "openai": ("OPENAI_API_KEY", settings.openai_api_key),
    }
    if settings.embedding_provider == "fake":
        results.append(CheckResult(FAIL, "EMBEDDING_PROVIDER", "is 'fake' (tests only)"))
    else:
        name, secret = emb_keys[settings.embedding_provider]
        if has(secret):
            results.append(CheckResult(OK, "Embedding key", f"{name} is set"))
        else:
            results.append(
                CheckResult(
                    FAIL, "Embedding key", f"{name} is missing; uploads cannot be processed"
                )
            )
    if settings.web_search_provider == "tavily":
        if has(settings.tavily_api_key):
            results.append(CheckResult(OK, "Web search", "TAVILY_API_KEY is set"))
        else:
            results.append(
                CheckResult(
                    WARN, "Web search", "TAVILY_API_KEY is missing; the web fallback is off"
                )
            )
    else:
        results.append(CheckResult(OK, "Web search", "disabled (WEB_SEARCH_PROVIDER=none)"))
    if has(settings.langsmith_api_key):
        results.append(
            CheckResult(
                WARN,
                "LANGSMITH_API_KEY",
                "is set: prompts and material excerpts are sent to LangSmith. "
                "Unset it unless intended",
            )
        )
    else:
        results.append(CheckResult(OK, "LangSmith", "tracing is off"))
    return results


def _data_checks(settings: Settings, network: Callable[[Path], bool]) -> list[CheckResult]:
    results: list[CheckResult] = []
    if not settings.database_url.startswith(_SQLITE_PREFIX):
        return [CheckResult(FAIL, "DATABASE_URL", "must be a sqlite+aiosqlite:/// URL")]
    db_path = Path(settings.database_url[len(_SQLITE_PREFIX) :])
    for label, path in (
        ("Database", db_path),
        ("Checkpoint DB", Path(settings.checkpoint_db_path)),
    ):
        if network(path):
            results.append(
                CheckResult(FAIL, label, f"{path} is on a network share; SQLite needs a local disk")
            )
        else:
            results.append(CheckResult(OK, label, f"{path} is on a local disk"))
    data_dir = Path(settings.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / f".write-test-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        results.append(CheckResult(OK, "DATA_DIR", f"{data_dir} is writable"))
    except OSError as exc:
        results.append(
            CheckResult(
                FAIL,
                "DATA_DIR",
                f"{data_dir} is not writable ({exc.strerror or type(exc).__name__})",
            )
        )
    return results


async def _storage_check(settings: Settings, storage: Storage | None) -> CheckResult:
    label = f"Storage ({settings.storage_backend})"
    try:
        await (storage or get_storage(settings)).check()
    except Exception as exc:
        return CheckResult(FAIL, label, f"not reachable: {exc}")
    return CheckResult(OK, label, "reachable and writable")


async def run_production_checks(
    settings: Settings,
    *,
    storage: Storage | None = None,
    network: Callable[[Path], bool] = is_network_path,
) -> list[CheckResult]:
    results = [
        *_url_checks(settings),
        *_key_checks(settings),
        *_provider_checks(settings),
        *_data_checks(settings, network),
        await _storage_check(settings, storage),
    ]
    return results


def format_results(results: list[CheckResult]) -> list[str]:
    lines = [f"[{r.status:<4}] {r.name}: {r.message}" for r in results]
    fails = sum(1 for r in results if r.status == FAIL)
    warns = sum(1 for r in results if r.status == WARN)
    lines.append(
        f"{fails} problem(s), {warns} warning(s)."
        + (" Fix the problems before going live." if fails else " Ready to go live.")
    )
    return lines


def run(settings: Settings, *, storage: Storage | None = None) -> tuple[list[str], int]:
    results = asyncio.run(run_production_checks(settings, storage=storage))
    return format_results(results), 1 if any(r.status == FAIL for r in results) else 0
