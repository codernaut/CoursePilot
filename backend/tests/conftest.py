import hashlib
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.main import create_app
from app.models import Document, User
from app.models.enums import DocumentStatus, Role
from app.services import structure
from app.storage import new_key

if TYPE_CHECKING:
    from tests.ingest_support import Env

TEST_PASSWORD = "correct horse battery"
TEST_SECRET = "test-secret-key-" + "x" * 32
# Cheap argon2 parameters keep the suite fast; verification cost follows the stored hash.
_PASSWORD_HASH = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1),)).hash(
    TEST_PASSWORD
)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "base_url": "http://test",
        "secret_key": TEST_SECRET,
        "data_dir": str(tmp_path),
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        "checkpoint_db_path": str(tmp_path / "checkpoints.db"),
        "login_rate_limit_per_min": 50,
        "chat_rate_limit_per_min": 1000,
        "upload_rate_limit_per_min": 1000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated, empty database created once and copied for every test."""
    folder = tmp_path_factory.mktemp("template")
    settings = make_settings(folder)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(upgrade_to_head, settings).result()
    return folder / "test.db"


@pytest.fixture
def settings(tmp_path: Path, template_db: Path) -> Settings:
    shutil.copyfile(template_db, tmp_path / "test.db")
    return make_settings(tmp_path)


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    yield application
    await application.state.database.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": "http://test"},
    ) as ac:
        yield ac


@pytest.fixture
async def seeded(app: FastAPI, settings: Settings) -> None:
    async with app.state.database.session() as db:
        await structure.seed_structure(
            db,
            years=settings.program_years,
            blocks_per_year=settings.blocks_per_year,
            modules_per_block=settings.modules_per_block,
        )


async def insert_user(
    app: FastAPI,
    *,
    email: str,
    role: Role,
    active: bool = True,
    must_change_password: bool = False,
    password_hash: str | None = None,
) -> User:
    async with app.state.database.session() as db:
        user = User(
            email=email,
            display_name=email.split("@")[0].title(),
            role=role,
            password_hash=password_hash or _PASSWORD_HASH,
            is_active=active,
            must_change_password=must_change_password,
        )
        db.add(user)
        await db.commit()
        return user


@dataclass
class Actor:
    client: AsyncClient
    user: User
    csrf: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}


async def api_login(client: AsyncClient, email: str, password: str = TEST_PASSWORD) -> str:
    """Log in through the JSON API and return the session CSRF token."""
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


@pytest.fixture
async def make_actor(app: FastAPI) -> AsyncIterator[Callable[..., Awaitable[Actor]]]:
    """Factory: create a user with the given role and return a logged-in client for it."""
    stack = AsyncExitStack()
    counter = 0

    async def factory(role: Role = Role.STUDENT, **kwargs: object) -> Actor:
        nonlocal counter
        counter += 1
        email = str(kwargs.pop("email", f"{role.value}{counter}@example.edu"))
        user = await insert_user(app, email=email, role=role, **kwargs)  # type: ignore[arg-type]
        http = await stack.enter_async_context(
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Origin": "http://test"},
            )
        )
        csrf = await api_login(http, email)
        return Actor(client=http, user=user, csrf=csrf)

    yield factory
    await stack.aclose()


async def insert_document(
    app: FastAPI,
    *,
    module_id: str,
    uploaded_by: str,
    content: bytes = b"%PDF-1.4\n%test\n",
    filename: str = "notes.pdf",
    title: str = "Notes",
    mime_type: str = "application/pdf",
    status: DocumentStatus = DocumentStatus.READY,
    write_object: bool = True,
) -> Document:
    """Insert a documents row (and, by default, its stored object) without going through upload."""
    key = new_key()
    if write_object:

        async def _chunks() -> AsyncIterator[bytes]:
            yield content

        await app.state.storage.put(key, _chunks())
    async with app.state.database.session() as db:
        document = Document(
            module_id=module_id,
            uploaded_by=uploaded_by,
            title=title,
            original_filename=filename,
            mime_type=mime_type,
            size_bytes=len(content),
            storage_key=key,
            content_sha256=hashlib.sha256(content).hexdigest(),
            status=status,
        )
        db.add(document)
        await db.commit()
        return document


@pytest.fixture
async def ingest_env(tmp_path: Path, template_db: Path) -> AsyncIterator["Env"]:
    """Temp database + local storage + fake embedder for pipeline and worker tests."""
    from tests.ingest_support import build_env

    env = await build_env(tmp_path, template_db)
    yield env
    await env.close()
