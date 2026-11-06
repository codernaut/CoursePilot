"""Build the CoursePilot release zip (used by .github/workflows/release.yml, runnable locally).

    python scripts/build_release.py --tag v0.2.0 [--out dist] [--sha <full commit sha>]

Produces ``dist/coursepilot-<tag>.zip`` and ``dist/coursepilot-<tag>.zip.sha256``.

Tag rule: tag ``vX.Y.Z`` must equal ``[project].version`` in backend/pyproject.toml (``X.Y.Z``);
tag ``vX.Y.Z-rc.N`` must equal ``X.Y.ZrcN`` (PEP 440) or ``X.Y.Z-rc.N``.

The archive never contains ``.env``, ``data/``, ``*.db`` or ``*.sqlite`` files, and the build fails
if any packaged file looks like it contains a secret (API key, private key).
"""

import argparse
import hashlib
import re
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path

ROOT_FILES = (
    "setup.bat",
    "setup.sh",
    "start.bat",
    "start.sh",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    ".env.example",
    "CLAUDE.md",
)
ROOT_DIRS = ("backend", "docs")

# Skipped silently: build and cache output that is never part of a release.
SKIP_DIR_NAMES = frozenset(
    {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        "reports",
        "node_modules",
        ".git",
        "dist",
        "build",
    }
)
SKIP_FILE_SUFFIXES = (".pyc", ".pyo")
SKIP_FILE_NAMES = frozenset({".coverage", "coverage.xml", "junit.xml"})

# Never packaged, and the finished archive is re-checked against the same rule.
FORBIDDEN_DIR_NAMES = frozenset({"data"})
FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")

SECRET_PATTERNS: dict[str, re.Pattern[bytes]] = {
    "Anthropic API key": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI-style API key": re.compile(rb"\bsk-(?!ant-)[A-Za-z0-9][A-Za-z0-9_\-]{19,}"),
    "AWS access key id": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    "Groq API key": re.compile(rb"\bgsk_[A-Za-z0-9]{20,}"),
    "Slack token": re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    "Google API key": re.compile(rb"AIza[0-9A-Za-z_\-]{30,}"),
    "Tavily API key": re.compile(rb"tvly-[A-Za-z0-9_\-]{16,}"),
    "PEM private key / certificate block": re.compile(rb"-----BEGIN [A-Z0-9 ]+-----"),
}
_PLACEHOLDER_MARKERS = (b"xxxx", b"your", b"placeholder", b"example", b"...", b"<")
_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)  # fixed, so identical input gives an identical zip
_NUM = r"(?:0|[1-9]\d*)"  # no leading zeros
_FINAL_TAG = re.compile(rf"^{_NUM}\.{_NUM}\.{_NUM}$")
_RC_TAG = re.compile(rf"^({_NUM}\.{_NUM}\.{_NUM})-rc\.({_NUM})$")


class ReleaseError(Exception):
    """A release rule was violated; the message says which and how to fix it."""


def tag_matches_version(tag: str, version: str) -> bool:
    """True when ``tag`` (e.g. ``v0.2.0`` or ``v0.2.0-rc.1``) corresponds to ``version``."""
    if not tag.startswith("v") or not version:
        return False
    body = tag[1:]
    # Only vX.Y.Z and vX.Y.Z-rc.N are release tags; anything else (v1.0.0rc1, -beta.1, suffixes)
    # is refused so that is_prerelease() and the workflow's --prerelease flag cannot disagree.
    if _FINAL_TAG.match(body):
        return version == body
    match = _RC_TAG.match(body)
    if match is None:
        return False
    return version in {body, f"{match.group(1)}rc{match.group(2)}"}


def is_prerelease(tag: str) -> bool:
    return "-rc" in tag


def read_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def is_forbidden(relative: Path) -> bool:
    """``.env`` (any variant except ``.env.example``), ``data/`` folders, database files."""
    name = relative.name.lower()  # Windows and macOS file systems are case-insensitive
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if any(part in FORBIDDEN_DIR_NAMES for part in relative.parts[:-1]):
        return True
    return name.lower().endswith(FORBIDDEN_SUFFIXES)


def _is_skipped(relative: Path) -> bool:
    if any(part in SKIP_DIR_NAMES or part.endswith(".egg-info") for part in relative.parts[:-1]):
        return True
    return relative.name in SKIP_FILE_NAMES or relative.name.endswith(SKIP_FILE_SUFFIXES)


def collect_files(repo_root: Path) -> list[Path]:
    """Repo-relative paths to package, sorted. Forbidden and cache files are left out."""
    found: list[Path] = []
    for name in ROOT_FILES:
        path = repo_root / name
        if not path.is_file():
            raise ReleaseError(f"Required release file is missing: {name}")
        found.append(Path(name))
    for directory in ROOT_DIRS:
        base = repo_root / directory
        if not base.is_dir():
            raise ReleaseError(f"Required release folder is missing: {directory}/")
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(repo_root)
            if path.is_symlink():
                continue  # a link could point at ../.env or outside the repository
            if path.is_file() and not _is_skipped(relative) and not is_forbidden(relative):
                found.append(relative)
    return sorted(found, key=lambda p: p.as_posix())


def _looks_like_placeholder(found: bytes) -> bool:
    lowered = found.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def scan_for_secrets(files: Sequence[tuple[Path, bytes]]) -> list[str]:
    """Findings as ``path: kind`` (never the secret itself). ``.env.example`` placeholders pass."""
    findings: list[str] = []
    for relative, content in files:
        for kind, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(content):
                if relative.name == ".env.example" and _looks_like_placeholder(match.group()):
                    continue
                findings.append(f"{relative.as_posix()}: looks like a {kind}")
                break
    return findings


def _iter_file_contents(repo_root: Path, files: Sequence[Path]) -> Iterator[tuple[Path, bytes]]:
    for relative in files:
        yield relative, (repo_root / relative).read_bytes()


def build_zip(repo_root: Path, out_dir: Path, tag: str, sha: str) -> tuple[Path, Path]:
    """Validate and write the zip plus its SHA-256 file. Returns ``(zip_path, checksum_path)``."""
    version = read_version(repo_root)
    if not tag_matches_version(tag, version):
        raise ReleaseError(
            f"Tag {tag} does not match [project].version {version} in backend/pyproject.toml. "
            "Bump the version in a commit first (v0.2.0 <-> 0.2.0, v0.2.0-rc.1 <-> 0.2.0rc1)."
        )
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReleaseError("--sha must be a full 40-character lowercase commit hash.")

    files = collect_files(repo_root)
    contents = list(_iter_file_contents(repo_root, files))
    findings = scan_for_secrets(contents)
    if findings:
        raise ReleaseError("Possible secrets found, refusing to build:\n  " + "\n  ".join(findings))

    folder = f"coursepilot-{tag}"
    entries: dict[str, bytes] = {f"{folder}/VERSION": f"{version} {sha}\n".encode()}
    entries.update({f"{folder}/{rel.as_posix()}": data for rel, data in contents})

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{folder}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
            archive.writestr(info, entries[name])

    verify_archive(zip_path)
    checksum = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    checksum_path = out_dir / f"{zip_path.name}.sha256"
    checksum_path.write_text(f"{checksum}  {zip_path.name}\n", encoding="utf-8")
    return zip_path, checksum_path


def verify_archive(zip_path: Path) -> None:
    """Defence in depth: re-check the written archive for forbidden entries."""
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            inner = Path(*Path(name).parts[1:])
            if is_forbidden(inner):
                raise ReleaseError(f"Forbidden file in release archive: {name}")


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "rev-parse", "HEAD"],  # noqa: S607 - git on PATH is expected
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build the CoursePilot release zip.")
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.2.0 or v0.2.0-rc.1")
    parser.add_argument("--repo-root", type=Path, default=default_root)
    parser.add_argument("--out", type=Path, default=None, help="output folder (default: dist)")
    parser.add_argument("--sha", default=None, help="full commit sha (default: git HEAD)")
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    out_dir: Path = args.out or repo_root / "dist"
    try:
        sha = args.sha or _git_head(repo_root)
        zip_path, checksum_path = build_zip(repo_root, out_dir, args.tag, sha)
    except (ReleaseError, subprocess.CalledProcessError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {zip_path}")
    print(f"Wrote {checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
