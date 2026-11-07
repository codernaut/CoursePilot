from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_MESSAGE_CHARS = 4000
MAX_COMMENT_CHARS = 1000


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    module_id: str | None = Field(default=None, max_length=64)  # null: "All material"
    conversation_id: str | None = Field(default=None, max_length=64)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: Literal["user", "assistant"]
    content: str
    citations: list[dict[str, Any]]
    used_web: bool
    feedback: Literal["up", "down"] | None
    feedback_comment: str | None
    prompt_version: str | None
    model: str | None
    latency_ms: int | None
    created_at: datetime


class ConversationOut(BaseModel):
    id: str
    title: str
    module_id: str | None
    module_path: str | None  # e.g. Year 1 > Block 2 > Module 3; null: all material
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class FeedbackIn(BaseModel):
    feedback: Literal["up", "down"] | None
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
