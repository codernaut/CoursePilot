"""Document and query prefixes for embedding models that need them (CLAUDE.md section 7.4).

Some open embedding models are trained with a task prefix: the same text is embedded differently
as a passage than as a search query, and leaving the prefix out measurably hurts retrieval.
``EMBEDDING_DOCUMENT_PREFIX`` / ``EMBEDDING_QUERY_PREFIX`` override the built-in table below when
set (an empty setting means "use the model's built-in default"; the table has no entry for a
model that needs none). The prefixes are applied only by the local-AI embedders (``ollama``,
``openai_compatible``, ``local``): the hosted providers have their own task-type parameters.

The prefixes used are NOT recorded in ``app_meta``. Changing them (or the model) changes the
vectors, so run ``python -m app.cli reindex-all`` afterwards, like for a model change.
"""

from dataclasses import dataclass

from app.core.config import Settings

BGE_QUERY = "Represent this sentence for searching relevant passages: "


@dataclass(frozen=True)
class Prefixes:
    document: str = ""
    query: str = ""


# (substring of the lower-cased model name, prefixes). The first match wins, so the more specific
# entries come first. Models not listed (bge-m3, snowflake-arctic-embed-l-v2.0, ...) need none.
_TABLE: tuple[tuple[str, Prefixes], ...] = (
    ("nomic-embed-text", Prefixes("search_document: ", "search_query: ")),
    ("bge-m3", Prefixes()),
    ("e5", Prefixes("passage: ", "query: ")),
    ("mxbai-embed-large", Prefixes("", BGE_QUERY)),
    ("bge-", Prefixes("", BGE_QUERY)),
)


def default_prefixes(model: str) -> Prefixes:
    name = model.lower()
    for fragment, prefixes in _TABLE:
        if fragment in name:
            return prefixes
    return Prefixes()


def resolve_prefixes(settings: Settings) -> Prefixes:
    """The effective prefixes: a non-empty setting wins over the built-in default."""
    default = default_prefixes(settings.embedding_model)
    return Prefixes(
        document=settings.embedding_document_prefix or default.document,
        query=settings.embedding_query_prefix or default.query,
    )
