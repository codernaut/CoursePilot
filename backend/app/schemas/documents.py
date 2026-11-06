from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DocumentStatus


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    module_id: str
    uploaded_by: str
    title: str
    original_filename: str
    mime_type: str
    size_bytes: int
    content_sha256: str
    status: DocumentStatus
    error: str | None
    page_count: int | None
    week: str | None
    created_at: datetime
    updated_at: datetime


class DocumentPatch(BaseModel):
    """``None`` leaves a field unchanged; an empty ``week`` clears it."""

    title: str | None = Field(default=None, max_length=500)
    week: str | None = Field(default=None, max_length=500)


class UploadErrorOut(BaseModel):
    filename: str
    code: str
    message: str
    status: int
    details: dict[str, Any] = Field(default_factory=dict)


class UploadResponse(BaseModel):
    """Per-file results of an upload request: ``documents`` were accepted and queued,
    ``errors`` were rejected. The request is 202 when at least one file was accepted."""

    documents: list[DocumentOut]
    errors: list[UploadErrorOut]
