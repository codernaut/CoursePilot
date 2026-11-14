"""Build the user manual (docs/manual) as DOCX, HTML and PDF from CoursePilot-User-Manual.md.

Needs pandoc (``--pandoc``, the ``PANDOC`` environment variable, or ``pandoc`` on PATH). The PDF is
printed from the HTML by a headless Chromium-based browser (Edge, Chrome, Chromium; ``--browser``
overrides the search) and is skipped with a message when none is found. The outputs are git-ignored.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

MANUAL_DIR = Path(__file__).resolve().parents[1] / "docs" / "manual"
SOURCE = "CoursePilot-User-Manual.md"
TITLE = "CoursePilot User Manual"

BROWSER_NAMES = ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser")
BROWSER_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def find_pandoc(explicit: str | None) -> str | None:
    candidate = explicit or os.environ.get("PANDOC")
    if candidate:
        return candidate if Path(candidate).exists() else shutil.which(candidate)
    return shutil.which("pandoc")


def find_browser(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else shutil.which(explicit)
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return next((p for p in BROWSER_PATHS if Path(p).exists()), None)


def run(command: Sequence[str], cwd: Path) -> int:
    # The command is built from fixed arguments and user-chosen tool paths only.
    return subprocess.run(list(command), cwd=cwd, check=False).returncode  # noqa: S603


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--pandoc", help="path to pandoc (default: $PANDOC or PATH)")
    parser.add_argument("--browser", help="Chromium-based browser for the PDF (default: auto)")
    args = parser.parse_args(argv)

    pandoc = find_pandoc(args.pandoc)
    if pandoc is None:
        print("pandoc not found: install it or pass --pandoc / set PANDOC.", file=sys.stderr)
        return 1
    if not (MANUAL_DIR / SOURCE).exists():
        print(f"{MANUAL_DIR / SOURCE} not found.", file=sys.stderr)
        return 1

    stem = SOURCE.removesuffix(".md")
    docx = f"{stem}.docx"
    html = f"{stem}.html"
    jobs = {
        docx: [pandoc, SOURCE, "-o", docx, "--toc", "--toc-depth=2", "--resource-path=."],
        html: [
            pandoc, SOURCE, "-s", "-o", html, "--toc", "--toc-depth=2", "--embed-resources",
            "--css", "manual.css", "--metadata", f"title={TITLE}",
        ],
    }  # fmt: skip
    for output, command in jobs.items():
        if run(command, MANUAL_DIR) != 0:
            print(f"pandoc failed while building {output}.", file=sys.stderr)
            return 1
        print(f"built {MANUAL_DIR / output}")

    browser = find_browser(args.browser)
    if browser is None:
        print("No Chromium-based browser found: skipping the PDF (use --browser).")
        return 0
    pdf = MANUAL_DIR / f"{stem}.pdf"
    code = run(
        [
            browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
            f"--print-to-pdf={pdf}", (MANUAL_DIR / html).as_uri(),
        ],
        MANUAL_DIR,
    )  # fmt: skip
    if code != 0 or not pdf.exists():
        print("PDF export failed.", file=sys.stderr)
        return 1
    print(f"built {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
