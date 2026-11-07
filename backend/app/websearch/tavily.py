"""Tavily search over plain httpx (one JSON POST; no vendor SDK).

Tavily takes the key as ``Authorization: Bearer <key>`` on ``POST /search``. Only the query, the
result limit and the optional domain allow-list are sent. Retries: 429, 5xx, timeouts and
connection errors, with exponential backoff (``Retry-After`` honoured, capped). The key is never
put in a URL, a log line or an exception message.
"""

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from app.core.logging import get_logger
from app.websearch.base import (
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    WebResult,
    WebSearchError,
    build_result,
    normalize_allowed,
    sanitize_results,
)

log = get_logger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
MAX_RETRY_AFTER_S = 20.0
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class TavilySearch:
    def __init__(
        self,
        api_key: str,
        *,
        allowed_domains: Sequence[str] = (),
        url: str = TAVILY_URL,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key = api_key
        self._allowed = normalize_allowed(allowed_domains)
        self._url = url
        self._timeout_s = timeout_s
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:  # created on first use
            self._client = httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _payload(self, query: str, max_results: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": " ".join(query.split())[:MAX_QUERY_CHARS],
            "max_results": max(1, min(max_results, MAX_RESULTS)),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if self._allowed:
            payload["include_domains"] = list(self._allowed)
        return payload

    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        delay = BACKOFF_BASE_S * (2 ** (attempt - 1))
        if response is not None:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(response.headers.get("retry-after", "0")))
        jitter = random.uniform(0.9, 1.1)  # noqa: S311 - retry jitter, not security
        return float(min(delay, MAX_RETRY_AFTER_S) * jitter)

    async def search(self, query: str, *, max_results: int = MAX_RESULTS) -> list[WebResult]:
        payload = self._payload(query, max_results)
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        failure = "The web search service is unavailable."
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response: httpx.Response | None = None
            try:
                response = await self._http().post(self._url, json=payload, headers=headers)
            except httpx.TimeoutException:
                failure = "The web search service timed out."
            except httpx.HTTPError as exc:
                failure = f"The web search service could not be reached ({type(exc).__name__})."
            else:
                if response.status_code == 200:
                    return self._parse(response, max_results)
                if response.status_code in (401, 403):
                    raise WebSearchError("The web search service rejected the API key.")
                if response.status_code not in _RETRYABLE_STATUS:
                    raise WebSearchError(
                        f"The web search service refused the request (HTTP {response.status_code})."
                    )
                failure = (
                    f"The web search service is busy or failing (HTTP {response.status_code})."
                )
            if attempt < MAX_ATTEMPTS:
                log.warning("web_search_retry", attempt=attempt, reason=failure)
                await self._sleep(self._delay(attempt, response))
        raise WebSearchError(failure, retryable=True)

    def _parse(self, response: httpx.Response, max_results: int) -> list[WebResult]:
        try:
            data = response.json()
            rows = data["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WebSearchError("The web search service returned an unusable reply.") from exc
        if not isinstance(rows, list):
            raise WebSearchError("The web search service returned an unusable reply.")
        results: list[WebResult] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            built = build_result(
                url=str(row.get("url") or ""),
                title=str(row.get("title") or ""),
                snippet=str(row.get("content") or ""),
                allowed=self._allowed,
            )
            if built is not None:
                results.append(built)
        return sanitize_results(results, self._allowed, max_results)
