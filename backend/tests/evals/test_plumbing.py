"""The eval harness with the fake embedder: checks the plumbing (ingest, search, scoring), not
retrieval quality. Runs in the normal test suite."""

from pathlib import Path

from sqlalchemy import select

from app.ingestion.embedder import FakeEmbedder
from app.models import Document
from app.models.enums import DocumentStatus
from tests.conftest import make_settings
from tests.evals.corpus import DOCS
from tests.evals.harness import (
    EvalReport,
    QuestionResult,
    build_eval_env,
    load_questions,
    run_eval,
)

DIM = 64


async def test_eval_harness_ingests_the_corpus_and_scores_questions(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, embedding_provider="fake", embedding_model="fake-eval", embedding_dim=DIM
    )
    embedder = FakeEmbedder(model="fake-eval", dim=DIM)
    env = await build_eval_env(settings, embedder)
    try:
        async with env.database.read_session() as db:
            statuses = (await db.execute(select(Document.title, Document.status))).all()
        assert len(statuses) == len(DOCS) == 8
        assert all(status is DocumentStatus.READY for _, status in statuses)

        questions = load_questions()
        assert 10 <= len(questions) <= 25
        known = {doc.title for doc in DOCS}
        assert {q.document for q in questions} <= known
        report = await run_eval(env, questions)
    finally:
        await env.close()

    assert len(report.results) == len(questions)
    assert report.k == settings.retrieval_top_k
    # keyword matches make even the fake embedder find most sources; this guards the plumbing
    assert report.hit_at(report.k) >= 0.8, report.summary()
    assert report.hit_at(1) <= report.hit_at(3) <= report.hit_at(report.k) <= 1.0
    assert 0.0 < report.mrr <= 1.0
    assert 0.0 <= report.facts <= 1.0


def test_report_metrics_are_computed_from_ranks() -> None:
    questions = load_questions()[:4]
    ranks = [1, 3, None, 6]
    report = EvalReport(
        [QuestionResult(q, r, r is not None) for q, r in zip(questions, ranks, strict=True)], k=8
    )
    assert report.hit_at(1) == 0.25
    assert report.hit_at(3) == 0.5
    assert report.hit_at(8) == 0.75
    assert report.mrr == (1 + 1 / 3 + 0 + 1 / 6) / 4
    assert report.facts == 0.75
    assert "hit@1: 0.25" in report.summary()


def test_report_prints_the_similarity_distribution() -> None:
    from tests.evals.harness import Question

    q = Question("q", "?", "D", None, None, [])
    report = EvalReport(
        [
            QuestionResult(q, 1, True, [], 0.8),
            QuestionResult(q, 2, True, [], 0.6),
            QuestionResult(q, None, False, [], 0.2),
            QuestionResult(q, None, False, [], None),
        ],
        k=8,
    )
    text = report.similarity_distribution()
    assert "hits (n=2): min 0.600  median 0.800  max 0.800" in text
    assert "misses (n=1): min 0.200  median 0.200  max 0.200" in text
    assert "best vector similarity" in report.summary()
