from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.enums import JobStatus, JobType


class JobOut(BaseModel):
    id: str
    type: JobType
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_at: datetime | None
    locked_by: str | None
    last_error: str | None
    document_id: str | None
    document_title: str | None  # null when the document is gone (e.g. after a delete job)
    created_at: datetime
    updated_at: datetime


class JobsPage(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class DayMessages(BaseModel):
    day: str  # UTC date, YYYY-MM-DD
    questions: int
    answers: int


class DayTokens(BaseModel):
    day: str
    provider: str  # inferred from the model name
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int = 0  # input tokens served from the provider's prompt cache
    cache_creation_tokens: int = 0  # input tokens written to the prompt cache


class UserUsage(BaseModel):
    email: str
    display_name: str
    role: str
    messages_today: int
    messages_in_period: int
    daily_limit: int


class UsageOut(BaseModel):
    days: int
    messages_per_day: list[DayMessages]
    tokens_per_day: list[DayTokens]
    top_users: list[UserUsage]
    documents_by_status: dict[str, int]
    jobs_by_status: dict[str, int]
    storage_bytes: int  # size of all stored (non-deleting) documents
    web_answers: int  # answers in the period that used web search
    total_answers: int


class FeedbackItem(BaseModel):
    message_id: str
    rating: Literal["up", "down"]
    comment: str | None
    module_path: str | None
    question: str | None
    answer: str
    created_at: datetime
