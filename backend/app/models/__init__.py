"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.chat import Conversation, Message
from app.models.document import Chunk, Document
from app.models.structure import Block, Module, Year
from app.models.system import AppMeta, AuditLog, Job
from app.models.user import User, UserSession

__all__ = [
    "AppMeta",
    "AuditLog",
    "Block",
    "Chunk",
    "Conversation",
    "Document",
    "Job",
    "Message",
    "Module",
    "User",
    "UserSession",
    "Year",
]
