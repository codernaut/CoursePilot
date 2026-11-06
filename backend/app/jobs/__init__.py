"""Background job queue (SQLite ``jobs`` table). M2 only enqueues; the runner arrives in M3."""

from app.jobs.queue import enqueue

__all__ = ["enqueue"]
