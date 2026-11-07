"""scripts/build_release.py (imported by path; the script is not part of the app package)."""

import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "build_release.py"
SHA = "a" * 40


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_release", SCRIPT)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_release"] = module
    spec.loader.exec_module(module)
    return module


br = _load()


def make_repo(root: Path, version: str = "0.2.0") -> Path:
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "backend" / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "backend" / "tests").mkdir()
    (root / "backend" / "tests" / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("docs\n", encoding="utf-8")
    for name in br.ROOT_FILES:
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    return root


def names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as archive:
        return {n.split("/", 1)[1] for n in archive.namelist()}


# --- tag / version matching ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "version", "ok"),
    [
        ("v0.2.0", "0.2.0", True),
        ("v1.0.0", "1.0.0", True),
        ("v0.2.0-rc.1", "0.2.0rc1", True),
        ("v0.2.0-rc.1", "0.2.0-rc.1", True),
        ("v0.2.0-rc.12", "0.2.0rc12", True),
        ("v0.2.0", "0.2.1", False),
        ("v0.2.0", "0.2.0rc1", False),  # a final tag never matches a pre-release version
        ("v0.2.0-rc.1", "0.2.0", False),
        ("v0.2.0-rc.1", "0.2.0rc2", False),
        ("0.2.0", "0.2.0", False),  # the tag needs its "v"
        ("v0.2.0-beta.1", "0.2.0b1", False),  # only -rc.N is understood
        ("v0.2.0-beta.1", "0.2.0-beta.1", False),  # equal strings are not enough
        ("v0.2.0rc1", "0.2.0rc1", False),  # would be published as a final release
        ("v0.2.0-rc.1-evil", "0.2.0-rc.1-evil", False),
        ("v0.02.0", "0.02.0", False),  # no leading zeros
        ("v0.2.0-rc.01", "0.2.0rc01", False),
        ("v", "", False),
        ("v0.2", "0.2.0", False),
    ],
)
def test_tag_matches_version(tag: str, version: str, ok: bool) -> None:
    assert br.tag_matches_version(tag, version) is ok


def test_is_prerelease() -> None:
    assert br.is_prerelease("v0.2.0-rc.1")
    assert not br.is_prerelease("v0.2.0")


def test_real_repo_version_is_readable() -> None:
    assert br.read_version(SCRIPT.parents[1]).count(".") >= 2


def test_the_real_repository_passes_the_release_secret_scan() -> None:
    """Test fixtures and docs must not look like real keys, or tagging a release would fail."""
    repo = SCRIPT.parents[1]
    files = br.collect_files(repo)
    findings = br.scan_for_secrets([(f, (repo / f).read_bytes()) for f in files])
    assert findings == []


# --- exclusion rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "data/coursepilot.db",
        "backend/data/x.txt",
        "backend/x.db",
        "backend/x.sqlite",
        "backend/x.sqlite3",
        "backend/a/b.db-wal",
        "Backend/X.DB",
    ],
)
def test_forbidden_paths(path: str) -> None:
    assert br.is_forbidden(Path(path))


def test_forbidden_is_case_insensitive_for_env_files() -> None:
    assert br.is_forbidden(Path("backend/.ENV"))
    assert br.is_forbidden(Path("backend/.Env.Local"))


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinks_are_not_packaged(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (tmp_path / "outside.txt").write_text("private", "utf-8")
    (repo / "backend" / "link.txt").symlink_to(tmp_path / "outside.txt")
    assert Path("backend/link.txt") not in br.collect_files(repo)


@pytest.mark.parametrize("path", [".env.example", "backend/app/main.py", "backend/database.py"])
def test_allowed_paths(path: str) -> None:
    assert not br.is_forbidden(Path(path))


def test_build_contains_required_files_and_version_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    zip_path, checksum_path = br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)
    assert zip_path.name == "coursepilot-v0.2.0.zip"
    found = names(zip_path)
    for required in (*br.ROOT_FILES, "backend/pyproject.toml", "backend/app/main.py", "docs/a.md"):
        assert required in found
    assert "backend/tests/test_x.py" in found  # tests are fine to include
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.read("coursepilot-v0.2.0/VERSION") == f"0.2.0 {SHA}\n".encode()
    expected = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert checksum_path.read_text(encoding="utf-8") == f"{expected}  {zip_path.name}\n"


def test_build_excludes_env_data_databases_and_caches(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / ".env").write_text("SECRET_KEY=abc\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "coursepilot.db").write_bytes(b"sqlite")
    (repo / "backend" / "local.db").write_bytes(b"x")
    (repo / "backend" / "local.sqlite").write_bytes(b"x")
    (repo / "backend" / "data").mkdir()
    (repo / "backend" / "data" / "x.txt").write_text("x", encoding="utf-8")
    (repo / "backend" / ".venv" / "lib").mkdir(parents=True)
    (repo / "backend" / ".venv" / "lib" / "big.py").write_text("x", encoding="utf-8")
    (repo / "backend" / "app" / "__pycache__").mkdir()
    (repo / "backend" / "app" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"x")
    (repo / "backend" / ".pytest_cache").mkdir()
    (repo / "backend" / ".pytest_cache" / "v").write_text("x", encoding="utf-8")
    (repo / "backend" / ".coverage").write_bytes(b"x")
    (repo / "backend" / "htmlcov").mkdir()
    (repo / "backend" / "htmlcov" / "index.html").write_text("x", encoding="utf-8")
    zip_path, _ = br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)
    found = names(zip_path)
    assert ".env" not in found
    assert ".env.example" in found
    assert not any(n.startswith("data/") or "/data/" in n for n in found)
    assert not any(n.endswith((".db", ".sqlite", ".pyc")) for n in found)
    assert not any(".venv" in n or "__pycache__" in n or "htmlcov" in n for n in found)
    assert "backend/.coverage" not in found
    assert "backend/.pytest_cache/v" not in found


def test_build_is_deterministic(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    first, _ = br.build_zip(repo, tmp_path / "one", "v0.2.0", SHA)
    second, _ = br.build_zip(repo, tmp_path / "two", "v0.2.0", SHA)
    assert first.read_bytes() == second.read_bytes()


def test_shell_scripts_are_executable_in_the_zip(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    zip_path, _ = br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)
    with zipfile.ZipFile(zip_path) as archive:
        mode = archive.getinfo("coursepilot-v0.2.0/setup.sh").external_attr >> 16
    assert mode & 0o111


def test_verify_archive_rejects_forbidden_entries(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("coursepilot-v1/.env", "SECRET_KEY=x")
    with pytest.raises(br.ReleaseError, match="Forbidden"):
        br.verify_archive(bad)


def test_missing_required_file_fails(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "LICENSE").unlink()
    with pytest.raises(br.ReleaseError, match="LICENSE"):
        br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)


# --- tag check -------------------------------------------------------------------------------


def test_build_refuses_mismatching_tag(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", version="0.2.0")
    with pytest.raises(br.ReleaseError, match="does not match"):
        br.build_zip(repo, tmp_path / "dist", "v0.3.0", SHA)
    assert not (tmp_path / "dist").exists()


def test_build_accepts_release_candidate(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", version="0.2.0rc1")
    zip_path, _ = br.build_zip(repo, tmp_path / "dist", "v0.2.0-rc.1", SHA)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.read("coursepilot-v0.2.0-rc.1/VERSION").startswith(b"0.2.0rc1 ")


def test_build_requires_a_full_sha(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    with pytest.raises(br.ReleaseError, match="40-character"):
        br.build_zip(repo, tmp_path / "dist", "v0.2.0", "abc123")


# --- secret scan -----------------------------------------------------------------------------
# The sample secrets are assembled at run time so that this file itself never trips the scan.

SECRETS = {
    "Anthropic API key": "sk-" + "ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2",
    "OpenAI-style API key": "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3",
    "Google API key": "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",
    "Tavily API key": "tvly" + "-dev-A1b2C3d4E5f6G7h8I9j0",
    "PEM private key / certificate block": "-----" + "BEGIN RSA PRIVATE KEY" + "-----",
    "AWS access key id": "AK" + "IA" + "IOSFODNN7ABCDEFG",
    "GitHub token": "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "Groq API key": "gs" + "k_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
    "Slack token": "xo" + "xb-" + "1234567890-abcdefghij",
}


def test_secret_scan_detects_hyphenated_openai_project_keys() -> None:
    key = ("sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0_K1l2-M3n4").encode()
    assert br.scan_for_secrets([(Path("backend/x.py"), b"KEY=" + key)])


@pytest.mark.parametrize("kind", list(SECRETS))
def test_secret_scan_detects_each_kind(tmp_path: Path, kind: str) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "backend" / "app" / "leak.py").write_text(f'KEY = "{SECRETS[kind]}"\n', "utf-8")
    with pytest.raises(br.ReleaseError, match=f"leak.py: looks like a {kind}") as info:
        br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)
    assert SECRETS[kind] not in str(info.value)  # the secret itself is never printed
    assert not (tmp_path / "dist").exists()


def test_secret_scan_catches_secrets_in_docs_and_root_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "docs" / "notes.md").write_text(SECRETS["Anthropic API key"], "utf-8")
    (repo / "README.md").write_text(SECRETS["Google API key"], "utf-8")
    with pytest.raises(br.ReleaseError) as info:
        br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)
    assert "docs/notes.md" in str(info.value)
    assert "README.md" in str(info.value)


@pytest.mark.parametrize(
    "harmless",
    [
        "sk-ant-abcdefghij1234",  # short test fixture
        "AIza-google-key-9876",
        "sk-learn is a library",
        "task-list-item-with-a-long-name-here",
        "-----BEGIN",  # incomplete marker
    ],
)
def test_secret_scan_ignores_short_or_unrelated_text(tmp_path: Path, harmless: str) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "backend" / "app" / "fine.py").write_text(f"X = '{harmless}'\n", "utf-8")
    zip_path, _ = br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)
    assert zip_path.exists()


def test_env_example_placeholders_are_allowed(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / ".env.example").write_text(
        "ANTHROPIC_API_KEY=\nGOOGLE_API_KEY=\nX=sk-" + "ant-" + "your-key-goes-here-xxxxxxxx\n",
        encoding="utf-8",
    )
    zip_path, _ = br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)
    assert zip_path.exists()


def test_env_example_with_a_real_looking_key_is_refused(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / ".env.example").write_text(f"K={SECRETS['Tavily API key']}\n", encoding="utf-8")
    with pytest.raises(br.ReleaseError, match=r"\.env\.example"):
        br.build_zip(repo, tmp_path / "dist", "v0.2.0", SHA)


def test_scan_for_secrets_reports_one_finding_per_kind_and_file() -> None:
    blob = (SECRETS["Anthropic API key"] + "\n") * 3
    found = br.scan_for_secrets([(Path("a.txt"), blob.encode())])
    assert found == ["a.txt: looks like a Anthropic API key"]


# --- command line ----------------------------------------------------------------------------


def test_main_success_and_failure_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    args = ["--repo-root", str(repo), "--out", str(tmp_path / "dist"), "--sha", SHA]
    assert br.main(["--tag", "v0.2.0", *args]) == 0
    assert "coursepilot-v0.2.0.zip" in capsys.readouterr().out
    assert br.main(["--tag", "v9.9.9", *args]) == 1
    assert "does not match" in capsys.readouterr().err
