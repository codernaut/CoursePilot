"""A scriptable web-search provider for tests (never selected by ``WEB_SEARCH_PROVIDER``)."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.websearch.base import MAX_RESULTS, WebResult, WebSearchError


def make_result(
    n: int = 1,
    *,
    url: str | None = None,
    title: str | None = None,
    snippet: str | None = None,
) -> WebResult:
    return WebResult(
        url=url or f"https://example.org/page-{n}",
        title=title or f"Web page {n}",
        snippet=snippet or f"Snippet of web page {n}.",
        site="example.org",
        retrieved_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
    )


class FakeWebSearch:
    """Returns ``results`` (or whatever ``responder(query)`` returns) and records every query."""

    def __init__(
        self,
        results: Sequence[WebResult] | None = None,
        *,
        responder: Callable[[str], Sequence[WebResult]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.results = list(results) if results is not None else [make_result(1), make_result(2)]
        self.responder = responder
        self.error = error
        self.queries: list[str] = []
        self.closed = False

    async def search(self, query: str, *, max_results: int = MAX_RESULTS) -> list[WebResult]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        found = self.responder(query) if self.responder else self.results
        return list(found)[:max_results]

    async def aclose(self) -> None:
        self.closed = True


def failing() -> FakeWebSearch:
    return FakeWebSearch(error=WebSearchError("down", retryable=True))
