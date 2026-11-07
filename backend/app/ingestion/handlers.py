"""Wires job types to the pipeline: the handlers and the failure/release hooks the worker's
``JobRunner`` uses."""

from collections.abc import Mapping

from app.ingestion import pipeline
from app.ingestion.pipeline import PipelineDeps
from app.jobs.base import ClaimedJob, JobError
from app.jobs.runner import FailureHook, Handler, ReleaseHook
from app.models.enums import JobType

_DOCUMENT_JOBS = (JobType.INGEST_DOCUMENT, JobType.REINDEX_DOCUMENT)


def _document_id(job: ClaimedJob) -> str:
    value = job.payload.get("document_id")
    if not isinstance(value, str) or not value:
        raise JobError("The job has no document id.", permanent=True)
    return value


def build_handlers(deps: PipelineDeps) -> Mapping[JobType, Handler]:
    async def process(job: ClaimedJob) -> None:
        await pipeline.process_document(deps, _document_id(job))

    async def delete(job: ClaimedJob) -> None:
        await pipeline.delete_document(deps, _document_id(job))

    return {
        JobType.INGEST_DOCUMENT: process,
        JobType.REINDEX_DOCUMENT: process,
        JobType.DELETE_DOCUMENT: delete,
    }


def build_failure_hook(deps: PipelineDeps) -> FailureHook:
    async def on_failure(job: ClaimedJob, message: str, terminal: bool) -> None:
        if job.type not in _DOCUMENT_JOBS:
            return  # a failed delete leaves the document 'deleting'; the admin can retry the job
        document_id = job.payload.get("document_id")
        if not isinstance(document_id, str):
            return
        if terminal:
            await pipeline.mark_failed(deps.database, document_id, message)
        else:
            await pipeline.mark_queued(deps.database, document_id)

    return on_failure


def build_release_hook(deps: PipelineDeps) -> ReleaseHook:
    async def on_release(job: ClaimedJob) -> None:
        document_id = job.payload.get("document_id")
        if job.type in _DOCUMENT_JOBS and isinstance(document_id, str):
            await pipeline.mark_queued(deps.database, document_id)

    return on_release
