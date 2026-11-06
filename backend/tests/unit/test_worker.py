import asyncio
from datetime import timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app import worker
from app.core.config import Settings
from app.db.base import utcnow
from app.models import User, UserSession
from app.models.enums import Role
from tests.conftest import insert_user


async def _add_sessions(app: FastAPI) -> None:
    user = await insert_user(app, email="w@example.edu", role=Role.STUDENT)
    async with app.state.database.session() as db:
        for name, delta in (("old", -1), ("new", 3600)):
            db.add(
                UserSession(
                    user_id=user.id,
                    token_hash=name * 32,
                    csrf_token="c",
                    expires_at=utcnow() + timedelta(seconds=delta),
                    last_seen_at=utcnow(),
                )
            )
        await db.commit()


async def test_purge_once_removes_only_expired_sessions(app: FastAPI) -> None:
    await _add_sessions(app)
    assert await worker.purge_once(app.state.database) == 1
    async with app.state.database.read_session() as db:
        remaining = (await db.execute(select(UserSession))).scalars().all()
        assert len(remaining) == 1 and remaining[0].token_hash.startswith("new")
        assert (await db.execute(select(User))).scalars().all()  # users untouched


async def test_worker_loop_purges_repeatedly_and_stops_on_event(
    app: FastAPI, settings: Settings
) -> None:
    await _add_sessions(app)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_worker(settings, stop, interval_s=0.05))
    await asyncio.sleep(0.3)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # exits cleanly, promptly
    async with app.state.database.read_session() as db:
        assert len((await db.execute(select(UserSession))).scalars().all()) == 1


async def test_worker_survives_a_failing_purge(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def boom(_database: object) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(worker, "purge_once", boom)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_worker(settings, stop, interval_s=0.02))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert calls >= 2
