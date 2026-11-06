from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseModel, enum_column
from app.models.enums import Feedback, MessageRole


class Conversation(BaseModel, Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_id", "user_id"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    module_id: Mapped[str | None] = mapped_column(ForeignKey("modules.id"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)


class Message(BaseModel, Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id", "conversation_id"),)

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    role: Mapped[MessageRole] = mapped_column(enum_column(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    used_web: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(120))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    feedback: Mapped[Feedback | None] = mapped_column(enum_column(Feedback, "message_feedback"))
    feedback_comment: Mapped[str | None] = mapped_column(Text)
