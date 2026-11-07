"""Manual retrieval quality check with the configured real embedding provider.

Deselected by default (marker ``eval``); it costs embedding API calls. Run it with
``uv run pytest -m eval`` or ``uv run python -m tests.evals.run``.
"""

import pytest

from app.core.config import get_settings
from app.ingestion.embedder import EmbeddingNotConfiguredError, get_embedder
from tests.evals.harness import evaluate

pytestmark = pytest.mark.eval


async def test_real_embedder_retrieval_quality() -> None:
    settings = get_settings()
    if settings.embedding_provider == "fake":
        pytest.skip("EMBEDDING_PROVIDER=fake gives meaningless quality numbers")
    try:
        embedder = get_embedder(settings)
    except EmbeddingNotConfiguredError as exc:
        pytest.skip(exc.message)
    report = await evaluate(settings, embedder)
    print()
    print(report.summary())
    # Conservative floors so a broken setup fails; track the printed numbers over time.
    assert report.hit_at(report.k) >= 0.8, report.summary()
    assert report.mrr >= 0.5, report.summary()
