"""Static checks of the setup/start scripts (no network, nothing is executed except `sh -n`)."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SETUP = {
    "setup.bat": "install.ps1",
    "setup.sh": "install.sh",
}


def _text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(("name", "installer"), SETUP.items())
def test_setup_asks_before_installing_uv_and_python(name: str, installer: str) -> None:
    text = _text(name)
    assert installer in text and "astral.sh/uv/" in text
    assert "Run it now?" in text and "[y/N]" in text  # consent before the official uv installer
    assert "Download Python 3.12 now?" in text  # consent before downloading Python
    assert "python find" in text and ">=3.12,<3.13" in text  # same range as requires-python
    assert "python install 3.12" in text
    assert "python.org/downloads" in text  # manual fallback when the user says no


@pytest.mark.parametrize("name", SETUP)
def test_setup_still_syncs_and_runs_cli_setup(name: str) -> None:
    text = _text(name)
    assert "sync" in text and "app.cli setup" in text
    assert "pip install --user uv" in text  # the pip route stays the first attempt


@pytest.mark.parametrize("name", SETUP)
def test_setup_finds_uv_in_the_usual_places(name: str) -> None:
    text = _text(name)
    assert ".local" in text and ".cargo" in text and "-m uv" in text


def test_setup_sh_has_wget_fallback() -> None:
    assert "wget -qO-" in _text("setup.sh")


def test_setup_asks_before_running_the_downloaded_installer() -> None:
    nl = chr(10)
    sh = _text("setup.sh")
    assert sh.index('confirm "Run it now?"') < sh.index(nl + "      curl -LsSf")
    bat = _text("setup.bat")
    assert bat.index("Run it now?") < bat.index(":run_installer" + nl + "powershell")


@pytest.mark.parametrize("name", ["start.bat", "start.sh"])
def test_start_scripts_prefer_standalone_uv_over_python_module(name: str) -> None:
    text = _text(name)
    assert "app.cli start" in text
    standalone = min(text.find("command -v uv"), text.find("uv --version"), key=lambda i: i < 0)
    assert 0 <= standalone < text.index("-m uv run python -m app.cli start")
    assert ".local" in text and ".cargo" in text


@pytest.mark.parametrize("name", ["setup.sh", "start.sh"])
def test_shell_scripts_have_valid_posix_syntax(name: str) -> None:
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh executable")
    result = subprocess.run(
        [sh, "-n", str(REPO_ROOT / name)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_setup_bat_keeps_crlf_and_rem_lines_have_no_redirection() -> None:
    raw = (REPO_ROOT / "setup.bat").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")  # CRLF only (.gitattributes: eol=crlf)
    for line in raw.decode("utf-8").splitlines():
        if line.lower().startswith("rem "):
            assert "<" not in line and ">" not in line and "|" not in line, line
