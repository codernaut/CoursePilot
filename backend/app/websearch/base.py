"""The web-search provider interface, the result type and the client-side result filters.

Only the query text ever goes to a provider (never a user id, a document or a conversation).
Results are filtered here whatever the provider did: only ``http(s)`` URLs without userinfo are
kept, and when ``WEB_ALLOWED_DOMAINS`` is set only exact hosts or their subdomains.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

MAX_QUERY_CHARS = 300
MAX_RESULTS = 5
SNIPPET_CHARS = 500
TITLE_CHARS = 200
MAX_URL_CHARS = 2000

_BAD_URL_CHARS = re.compile(r"[\s\x00-\x1f\x7f-\x9f\\]")  # blanks, controls, backslash
_HOST_OK = re.compile(r"^[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?$")


class WebSearchError(Exception):
    """A search failed. ``message`` is safe to log (it never holds the API key)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class WebResult:
    url: str
    title: str
    snippet: str
    site: str
    retrieved_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "site": self.site,
            "retrieved_at": self.retrieved_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebResult":
        return cls(
            url=str(data["url"]),
            title=str(data["title"]),
            snippet=str(data["snippet"]),
            site=str(data["site"]),
            retrieved_at=datetime.fromisoformat(str(data["retrieved_at"])),
        )


class WebSearchProvider(Protocol):
    async def search(self, query: str, *, max_results: int = MAX_RESULTS) -> list[WebResult]:
        """Search the web for ``query``. Raises ``WebSearchError``."""
        ...

    async def aclose(self) -> None: ...


# -- text and URL hygiene ------------------------------------------------------------


def clean_line(text: str, limit: int) -> str:
    """One line: control, format (bidi, zero-width) and unassigned characters dropped, blanks
    collapsed, cut to ``limit`` characters."""
    spaced = "".join(" " if c.isspace() else c for c in text)
    kept = "".join(c for c in spaced if unicodedata.category(c)[0] != "C")
    flat = " ".join(kept.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def normalize_host(host: str) -> str | None:
    """Lowercase ASCII (punycode) host without a trailing dot, or ``None`` when unusable."""
    host = host.strip().rstrip(".").lower()
    if not host or len(host) > 253:
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def safe_url_host(url: str) -> str | None:
    """The normalised host of an ``http(s)`` URL, or ``None`` when the URL must not be used:
    other schemes, no host, userinfo, whitespace or control characters, absurd length."""
    if not url or len(url) > MAX_URL_CHARS or _BAD_URL_CHARS.search(url):
        return None
    if any(unicodedata.category(c)[0] == "C" for c in url):  # zero-width, bidi, unassigned
        return None
    if any(unicodedata.category(c)[0] == "C" for c in url):  # zero-width, bidi, unassigned
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed port
        host = parts.hostname
        userinfo = "@" in parts.netloc
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or userinfo:
        return None
    normalised = normalize_host(host)
    # Letters, digits, "-", "_" and dots only (after punycode): no "$", "\\" or other characters
    # that a browser may read differently from this parser.
    return normalised if normalised and _HOST_OK.match(normalised) else None


def normalize_allowed(domains: Sequence[str]) -> list[str]:
    """Allow-list entries as bare hosts (``https://*.Example.edu/x`` becomes ``example.edu``)."""
    out: list[str] = []
    for raw in domains:
        entry = raw.strip()
        if "//" in entry:
            entry = entry.split("//", 1)[1]
        entry = entry.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        entry = entry.rsplit("@", 1)[-1]
        if entry.count(":") == 1:
            entry = entry.split(":", 1)[0]
        entry = entry.lstrip("*").lstrip(".")
        host = normalize_host(entry)
        if host and host not in out:
            out.append(host)
    return out


def host_allowed(host: str, allowed: Sequence[str]) -> bool:
    """Exact host or a subdomain of an allowed domain; ``evil-example.edu`` is not ``example.edu``.
    ``allowed`` must be normalised (``normalize_allowed``). An empty list allows every host."""
    if not allowed:
        return True
    return any(host == a or host.endswith("." + a) for a in allowed)


def site_of(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def build_result(
    *, url: str, title: str, snippet: str, allowed: Sequence[str], now: datetime | None = None
) -> WebResult | None:
    """A cleaned ``WebResult``, or ``None`` when the URL is unusable or not allowed."""
    host = safe_url_host(url.strip())
    if host is None or not host_allowed(host, allowed):
        return None
    clean_title = clean_line(title, TITLE_CHARS) or site_of(host)
    return WebResult(
        url=url.strip(),
        title=clean_title,
        snippet=clean_line(snippet, SNIPPET_CHARS),
        site=site_of(host),
        retrieved_at=now or datetime.now(UTC),
    )


def sanitize_results(
    results: Sequence[WebResult], allowed: Sequence[str], limit: int = MAX_RESULTS
) -> list[WebResult]:
    """Re-apply the URL and allow-list filters (defence in depth) and drop duplicate URLs."""
    seen: set[str] = set()
    out: list[WebResult] = []
    for r in results:
        rebuilt = build_result(
            url=r.url, title=r.title, snippet=r.snippet, allowed=allowed, now=r.retrieved_at
        )
        if rebuilt is None or rebuilt.url in seen:
            continue
        seen.add(rebuilt.url)
        out.append(rebuilt)
        if len(out) >= limit:
            break
    return out
