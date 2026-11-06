from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditLog
from app.models.enums import Role
from tests.conftest import Actor

ActorFactory = Callable[..., Awaitable[Actor]]


async def _tree(actor: Actor) -> list[dict]:  # type: ignore[type-arg]
    response = await actor.client.get("/api/v1/structure")
    assert response.status_code == 200
    return response.json()["years"]  # type: ignore[no-any-return]


async def test_structure_tree_shape(seeded: None, make_actor: ActorFactory) -> None:
    student = await make_actor(Role.STUDENT)
    years = await _tree(student)
    assert [y["number"] for y in years] == [1, 2, 3]
    assert [y["title"] for y in years] == ["Year 1", "Year 2", "Year 3"]
    assert all(len(y["blocks"]) == 3 for y in years)
    assert all(len(b["modules"]) == 3 for y in years for b in y["blocks"])
    module = years[0]["blocks"][0]["modules"][0]
    assert set(module) == {"id", "number", "title", "description", "web_search_enabled"}
    assert module["web_search_enabled"] is True


async def test_admin_renames_year_and_block_with_audit(
    app: FastAPI, seeded: None, make_actor: ActorFactory
) -> None:
    admin = await make_actor(Role.ADMIN)
    year = (await _tree(admin))[0]
    block = year["blocks"][0]
    r1 = await admin.client.patch(
        f"/api/v1/years/{year['id']}", json={"title": "  Foundations  "}, headers=admin.headers
    )
    assert r1.status_code == 200
    assert r1.json()["title"] == "Foundations"
    r2 = await admin.client.patch(
        f"/api/v1/blocks/{block['id']}", json={"title": "Anatomy"}, headers=admin.headers
    )
    assert r2.status_code == 200
    tree = await _tree(admin)
    assert tree[0]["title"] == "Foundations"
    assert tree[0]["blocks"][0]["title"] == "Anatomy"
    async with app.state.database.session() as db:
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "structure_renamed")))
            .scalars()
            .all()
        )
    assert {(r.target_type, r.details["to"]) for r in rows} == {
        ("year", "Foundations"),
        ("block", "Anatomy"),
    }


async def test_faculty_can_edit_module_but_not_year_or_block(
    app: FastAPI, seeded: None, make_actor: ActorFactory
) -> None:
    faculty = await make_actor(Role.FACULTY)
    year = (await _tree(faculty))[0]
    block = year["blocks"][0]
    module = block["modules"][0]
    patch = await faculty.client.patch(
        f"/api/v1/modules/{module['id']}",
        json={"title": "Cardiology", "description": "Heart", "web_search_enabled": False},
        headers=faculty.headers,
    )
    assert patch.status_code == 200
    assert patch.json()["title"] == "Cardiology"
    assert patch.json()["web_search_enabled"] is False
    assert (
        await faculty.client.patch(
            f"/api/v1/years/{year['id']}", json={"title": "x"}, headers=faculty.headers
        )
    ).status_code == 403
    assert (
        await faculty.client.patch(
            f"/api/v1/blocks/{block['id']}", json={"title": "x"}, headers=faculty.headers
        )
    ).status_code == 403
    detail = await faculty.client.get(f"/api/v1/modules/{module['id']}")
    assert detail.json()["description"] == "Heart"
    assert detail.json()["block_id"] == block["id"]
    async with app.state.database.session() as db:
        actions = [r.action for r in (await db.execute(select(AuditLog))).scalars()]
    assert "structure_renamed" in actions


async def test_student_cannot_edit_module(seeded: None, make_actor: ActorFactory) -> None:
    student = await make_actor(Role.STUDENT)
    module = (await _tree(student))[0]["blocks"][0]["modules"][0]
    response = await student.client.patch(
        f"/api/v1/modules/{module['id']}", json={"title": "Hacked"}, headers=student.headers
    )
    assert response.status_code == 403
    assert (await _tree(student))[0]["blocks"][0]["modules"][0]["title"] == "Module 1"


async def test_rename_validation_and_not_found(seeded: None, make_actor: ActorFactory) -> None:
    admin = await make_actor(Role.ADMIN)
    year = (await _tree(admin))[0]
    blank = await admin.client.patch(
        f"/api/v1/years/{year['id']}", json={"title": "   "}, headers=admin.headers
    )
    assert blank.status_code == 422
    too_long = await admin.client.patch(
        f"/api/v1/years/{year['id']}", json={"title": "x" * 121}, headers=admin.headers
    )
    assert too_long.status_code == 422
    missing = await admin.client.patch(
        "/api/v1/modules/" + "0" * 32, json={"title": "x"}, headers=admin.headers
    )
    assert missing.status_code == 404
