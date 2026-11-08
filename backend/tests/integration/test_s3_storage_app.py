"""The app with STORAGE_BACKEND=s3 (moto): upload -> stored in the bucket -> download streams it."""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from moto import mock_aws
from pydantic import ValidationError
from sqlalchemy import select

from app.main import create_app
from app.models import Document, Module
from app.models.enums import Role
from app.storage import S3Storage
from tests.conftest import Actor, api_login, insert_user, make_settings
from tests.files import SAMPLES

BUCKET = "coursepilot-test"
ActorFactory = Callable[..., Awaitable[Actor]]


@pytest.fixture
def bucket() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
async def s3_app(tmp_path: Path, template_db: Path, bucket: Any) -> AsyncIterator[FastAPI]:
    import shutil

    shutil.copyfile(template_db, tmp_path / "test.db")
    settings = make_settings(
        tmp_path,
        storage_backend="s3",
        s3_bucket=BUCKET,
        s3_region="us-east-1",
        s3_access_key_id="test",
        s3_secret_access_key="test",
    )
    application = create_app(settings)
    yield application
    await application.state.database.dispose()


async def test_upload_download_round_trip_through_s3(s3_app: FastAPI, bucket: Any) -> None:
    from app.services import structure

    assert isinstance(s3_app.state.storage, S3Storage)
    settings = s3_app.state.settings
    async with s3_app.state.database.session() as db:
        await structure.seed_structure(
            db,
            years=settings.program_years,
            blocks_per_year=settings.blocks_per_year,
            modules_per_block=settings.modules_per_block,
        )
    await insert_user(s3_app, email="fac@example.edu", role=Role.FACULTY)
    async with AsyncClient(
        transport=ASGITransport(app=s3_app),
        base_url="http://test",
        headers={"Origin": "http://test"},
    ) as http:
        csrf = await api_login(http, "fac@example.edu")
        async with s3_app.state.database.read_session() as db:
            module = (await db.execute(select(Module).order_by(Module.number))).scalars().first()
        assert module is not None
        data = SAMPLES["pdf"]
        response = await http.post(
            f"/api/v1/modules/{module.id}/documents",
            files=[("file", ("Lecture 1.pdf", data, "application/pdf"))],
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 202, response.text
        doc_id = response.json()["documents"][0]["id"]

        async with s3_app.state.database.read_session() as db:
            document = (await db.execute(select(Document))).scalars().one()
        names = [o["Key"] for o in bucket.list_objects_v2(Bucket=BUCKET)["Contents"]]
        assert names == [f"files/{document.storage_key[:2]}/{document.storage_key}"]
        assert (
            hashlib.sha256(
                bucket.get_object(Bucket=BUCKET, Key=names[0])["Body"].read()
            ).hexdigest()
            == hashlib.sha256(data).hexdigest()
        )

        download = await http.get(f"/api/v1/documents/{doc_id}/download")
        assert download.status_code == 200
        assert download.content == data
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["content-disposition"].startswith("inline")

        ready = await http.get("/api/v1/health/ready")
        assert ready.status_code == 200 and ready.json()["checks"]["storage"] == "ok"


async def test_ready_reports_storage_failure_when_the_bucket_is_gone(
    s3_app: FastAPI, bucket: Any
) -> None:
    bucket.delete_bucket(Bucket=BUCKET)
    async with AsyncClient(transport=ASGITransport(app=s3_app), base_url="http://test") as http:
        response = await http.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["error"]["details"]["checks"]["storage"] == "error"


def test_s3_backend_needs_credentials_and_bucket(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="S3_ACCESS_KEY_ID"):
        make_settings(tmp_path, storage_backend="s3")
    with pytest.raises(ValidationError, match="S3_BUCKET"):
        make_settings(
            tmp_path,
            storage_backend="s3",
            s3_bucket="",
            s3_access_key_id="k",
            s3_secret_access_key="s",
        )
    ok = make_settings(
        tmp_path, storage_backend="s3", s3_access_key_id="k", s3_secret_access_key="s"
    )
    assert ok.storage_backend == "s3"
