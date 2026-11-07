import sqlite3
import subprocess
from pathlib import Path

import pytest

from app import cli
from app.core.config import REPO_ROOT, Settings
from app.models.enums import Role
from app.services.security import verify_password
from tests.unit.test_setup_flow import Scripted

PASSWORD = "an admin password 1"


def _db(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.database_url.removeprefix("sqlite+aiosqlite:///"))


def test_check_passes_on_this_machine() -> None:
    p = Scripted([])
    assert cli.run_check(p) == 0
    assert any("OK" in s for s in p.said)


def test_check_reports_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_sqlite_capabilities", lambda: ["no FTS5"])
    p = Scripted([])
    assert cli.run_check(p) == 1
    assert any("no FTS5" in s for s in p.said)


def test_create_admin_creates_admin_with_hidden_password(settings: Settings) -> None:
    p = Scripted(["Admin@Example.edu", "Ada Admin", PASSWORD, PASSWORD])
    assert cli.run_create_admin(settings, p) == 0
    row = (
        _db(settings)
        .execute(
            "SELECT email, display_name, role, is_active, must_change_password, password_hash "
            "FROM users"
        )
        .fetchone()
    )
    assert row[:5] == ("admin@example.edu", "Ada Admin", Role.ADMIN.value, 1, 0)
    assert verify_password(PASSWORD, row[5]) and PASSWORD not in row[5]
    assert [secret for _, secret in p.prompts] == [False, False, True, True]
    assert PASSWORD not in p.everything_shown
    audit = _db(settings).execute("SELECT action, actor_id FROM audit_log").fetchall()
    assert audit == [("user_created", None)]


def test_create_admin_takes_email_from_argument(settings: Settings) -> None:
    p = Scripted(["", PASSWORD, PASSWORD])  # display name default, password twice
    assert cli.run_create_admin(settings, p, email="cli@example.edu") == 0
    assert _db(settings).execute("SELECT email FROM users").fetchone() == ("cli@example.edu",)


def test_create_admin_rejects_duplicate_and_invalid_email(settings: Settings) -> None:
    assert cli.run_create_admin(settings, Scripted(["a@example.edu", "", PASSWORD, PASSWORD])) == 0
    dup = Scripted(["A@example.edu", "", PASSWORD, PASSWORD])
    assert cli.run_create_admin(settings, dup) == 1
    assert any("already exists" in s for s in dup.said)
    bad = Scripted(["not-an-email", "", PASSWORD, PASSWORD])
    assert cli.run_create_admin(settings, bad) == 1
    assert _db(settings).execute("SELECT count(*) FROM users").fetchone() == (1,)


def test_create_admin_never_accepts_a_password_on_the_command_line() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create-admin", "--password", "x"])


def test_seed_structure_command(settings: Settings) -> None:
    assert cli.run_seed(settings) == 39  # 3 years + 9 blocks + 27 modules
    assert cli.run_seed(settings) == 0
    conn = _db(settings)
    assert conn.execute("SELECT count(*) FROM modules").fetchone() == (27,)
    conn.execute("UPDATE years SET title = 'Foundations' WHERE number = 1")
    conn.commit()
    assert cli.run_seed(settings) == 0
    assert conn.execute("SELECT title FROM years WHERE number = 1").fetchone() == ("Foundations",)


def test_seed_structure_honours_configured_size(tmp_path: Path, settings: Settings) -> None:
    small = settings.model_copy(
        update={"program_years": 1, "blocks_per_year": 2, "modules_per_block": 2}
    )
    assert cli.run_seed(small) == 1 + 2 + 4


def test_main_migrate_and_seed(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    p = Scripted([])
    assert cli.main(["migrate"], prompter=p) == 0
    assert cli.main(["seed-structure"], prompter=p) == 0
    assert any("39" in s for s in p.said)


def test_main_create_admin_runs_migrations_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.conftest import make_settings

    fresh = make_settings(tmp_path)  # no tables yet
    monkeypatch.setattr(cli, "get_settings", lambda: fresh)
    p = Scripted(["boot@example.edu", "", PASSWORD, PASSWORD])
    assert cli.main(["create-admin"], prompter=p) == 0
    assert _db(fresh).execute("SELECT email FROM users").fetchone() == ("boot@example.edu",)


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.main(["frobnicate"])


class FakeProc:
    """Stands in for ``subprocess.Popen``: records how it was asked to stop."""

    def __init__(self, *, exits_on_stop: bool = True) -> None:
        self.stopped_with: list[str] = []
        self.killed = False
        self.exits_on_stop = exits_on_stop
        self.alive = True
        self.wait_timeouts: list[float | None] = []

    @property
    def terminated(self) -> bool:
        return bool(self.stopped_with)

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.stopped_with.append("terminate")

    def send_signal(self, sig: int) -> None:
        self.stopped_with.append(f"signal:{int(sig)}")

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.killed or (self.exits_on_stop and self.stopped_with):
            self.alive = False
            return 0
        raise subprocess.TimeoutExpired("worker", timeout or 0)

    def kill(self) -> None:
        self.killed = True


def test_worker_is_started_in_its_own_group_and_stopped_gracefully() -> None:
    win = cli.worker_popen_options(windows=True)
    assert win == {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    assert cli.worker_popen_options(windows=False) == {"start_new_session": True}

    import signal

    proc = FakeProc()
    cli.stop_worker(proc, windows=True, grace_s=25)
    assert proc.stopped_with == [
        f"signal:{int(getattr(signal, 'CTRL_BREAK_EVENT', signal.SIGTERM))}"
    ]
    assert not proc.killed and proc.wait_timeouts == [25]

    posix = FakeProc()
    cli.stop_worker(posix, windows=False)
    assert posix.stopped_with == ["terminate"] and not posix.killed
    assert posix.wait_timeouts == [cli.WORKER_STOP_GRACE_S] and cli.WORKER_STOP_GRACE_S >= 25


def test_worker_that_ignores_the_stop_request_is_killed_after_the_grace_period() -> None:
    proc = FakeProc(exits_on_stop=False)
    cli.stop_worker(proc, windows=False, grace_s=25)
    assert proc.killed and proc.wait_timeouts[0] == 25


def test_stopping_a_worker_that_already_exited_does_nothing() -> None:
    proc = FakeProc()
    proc.alive = False
    cli.stop_worker(proc, windows=False)
    assert proc.stopped_with == [] and not proc.killed


def test_start_migrates_runs_worker_and_server_then_cleans_up(settings: Settings) -> None:
    proc = FakeProc()
    spawned: list[list[str]] = []
    served: list[dict[str, object]] = []
    opened: list[str] = []

    def popen(cmd: list[str], **_kw: object) -> FakeProc:
        spawned.append(cmd)
        return proc

    def serve(target: str, **kw: object) -> None:
        served.append({"target": target, **kw})

    import threading

    done = threading.Event()

    def open_browser(url: str) -> None:
        opened.append(url)
        done.set()

    code = cli.run_start(
        settings,
        popen=popen,
        serve=serve,
        open_browser=open_browser,
        wait_until_ready=lambda _url: True,
    )
    assert code == 0
    assert done.wait(2)
    assert spawned[0][1:] == ["-m", "app.worker"]
    assert served[0]["target"] == "app.main:app"
    assert served[0]["host"] == settings.app_host and served[0]["port"] == settings.app_port
    assert opened == [f"http://{settings.app_host}:{settings.app_port}"]
    assert proc.terminated
    assert _db(settings).execute("SELECT count(*) FROM alembic_version").fetchone() == (1,)


def test_start_stops_worker_on_ctrl_c(settings: Settings) -> None:
    proc = FakeProc()

    def serve(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    code = cli.run_start(
        settings,
        popen=lambda *_a, **_k: proc,
        serve=serve,
        open_browser=lambda _u: None,
        wait_until_ready=lambda _u: False,
    )
    assert code == 0 and proc.terminated


@pytest.mark.parametrize("script", ["setup.sh", "start.sh"])
def test_shell_scripts_use_lf_line_endings(script: str) -> None:
    """A CRLF shebang ('sh\r') makes the script fail on Linux and macOS."""
    data = (REPO_ROOT / script).read_bytes()
    assert data.startswith(b"#!/usr/bin/env sh\n")
    assert b"\r" not in data


def test_gitattributes_pins_script_line_endings() -> None:
    """Without this a Windows checkout (core.autocrlf) turns the .sh files back into CRLF."""
    rules = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in rules
    assert "*.bat text eol=crlf" in rules
