"""Chooses the web-search provider from settings. ``None`` means web search is unavailable
(``WEB_SEARCH_PROVIDER=none`` or no ``TAVILY_API_KEY``); that is not an error: the agent then
says the material was insufficient and that web search is not configured."""

from collections.abc import Callable

from app.core.config import Settings
from app.websearch.base import WebSearchProvider

WebSearchFactory = Callable[[Settings], WebSearchProvider | None]


def build_web_search(settings: Settings) -> WebSearchProvider | None:
    if settings.web_search_provider != "tavily" or settings.web_max_queries < 1:
        return None
    secret = settings.tavily_api_key
    key = secret.get_secret_value().strip() if secret else ""
    if not key:
        return None
    from app.websearch.tavily import TavilySearch

    return TavilySearch(key, allowed_domains=settings.web_allowed_domains)
