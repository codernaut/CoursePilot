from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    FACULTY = "faculty"
    STUDENT = "student"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class JobType(StrEnum):
    INGEST_DOCUMENT = "ingest_document"
    REINDEX_DOCUMENT = "reindex_document"
    DELETE_DOCUMENT = "delete_document"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Feedback(StrEnum):
    UP = "up"
    DOWN = "down"
