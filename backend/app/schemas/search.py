from typing import Any, Literal

from pydantic import BaseModel

from app.retrieval.hybrid import RetrievedChunk
from app.services import search as search_service

Level = Literal["module", "block", "all"]


class SearchHitOut(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    module_id: str
    year_title: str
    block_title: str
    module_title: str
    path: str
    page_start: int | None
    page_end: int | None
    slide_number: int | None
    location: str | None
    section_path: str | None
    snippet: str
    score: float
    vector_similarity: float | None
    tier: Level

    @classmethod
    def from_chunk(cls, chunk: RetrievedChunk) -> "SearchHitOut":
        return cls(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_title=chunk.document_title,
            module_id=chunk.module_id,
            year_title=chunk.year_title,
            block_title=chunk.block_title,
            module_title=chunk.module_title,
            path=chunk.path,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            slide_number=chunk.slide_number,
            location=chunk.location,
            section_path=chunk.section_path,
            snippet=search_service.snippet(chunk.text),
            score=chunk.score,
            vector_similarity=chunk.vector_similarity,
            tier=chunk.tier,
        )


class TierOut(BaseModel):
    level: Level
    target_id: str | None
    hit_count: int
    sufficient: bool
    best_vector_similarity: float | None


class SearchOut(BaseModel):
    query: str
    tiers: list[TierOut]
    hits: list[SearchHitOut]


class PassageOut(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    document_filename: str
    document_url: str
    module_id: str
    year_title: str
    block_title: str
    module_title: str
    path: str
    page_start: int | None
    page_end: int | None
    slide_number: int | None
    location: str | None
    section_path: str | None
    week: str | None
    ordinal: int
    token_count: int
    text: str
    metadata: dict[str, Any]

    @classmethod
    def from_chunk(cls, chunk: RetrievedChunk) -> "PassageOut":
        return cls(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_title=chunk.document_title,
            document_filename=chunk.document_filename,
            document_url=search_service.document_link(chunk),
            module_id=chunk.module_id,
            year_title=chunk.year_title,
            block_title=chunk.block_title,
            module_title=chunk.module_title,
            path=chunk.path,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            slide_number=chunk.slide_number,
            location=chunk.location,
            section_path=chunk.section_path,
            week=chunk.week,
            ordinal=chunk.ordinal,
            token_count=chunk.token_count,
            text=chunk.text,
            metadata=chunk.metadata,
        )
