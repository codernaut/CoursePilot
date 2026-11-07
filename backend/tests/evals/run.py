"""Manual retrieval quality run with the CONFIGURED embedding provider (costs API calls).

    cd backend
    uv run python -m tests.evals.run          # prints hit@1 / hit@3 / hit@k / MRR / facts
    uv run pytest -m eval                     # the same run as a pytest check (see test_quality.py)

It reads ``.env`` like the application does (EMBEDDING_PROVIDER, EMBEDDING_MODEL, EMBEDDING_DIM and
the provider's API key) but writes only to a temporary database that is deleted afterwards.
"""

import asyncio
import sys

from app.core.config import get_settings
from app.ingestion.embedder import EmbeddingError, get_embedder
from tests.evals.harness import evaluate


async def main() -> int:
    settings = get_settings()
    if settings.embedding_provider == "fake":
        print("EMBEDDING_PROVIDER=fake gives meaningless quality numbers; use a real provider.")
        return 2
    try:
        embedder = get_embedder(settings)
    except EmbeddingError as exc:
        print(exc.message)
        return 2
    print(f"embedding: {settings.embedding_provider} / {settings.embedding_model} / "
          f"{settings.embedding_dim}")  # fmt: skip
    report = await evaluate(settings, embedder)
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
