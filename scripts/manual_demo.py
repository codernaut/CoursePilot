# ruff: noqa: E501
"""Throwaway demo instance for taking the user-manual screenshots. Never use for real data.

Starts CoursePilot on port 8767 with a fake LLM and a fake embedder (no API keys, no network),
fresh data in ``<repo>/.manual-demo/data`` (git-ignored, wiped on every start) and sample users,
renamed modules, five documents (one deliberately fails: a "scanned" PDF) and a few student
conversations with feedback. Random demo passwords are written, with the e-mails, to
``<repo>/.manual-demo/credentials.txt``.

Run from the repo root: ``backend/.venv/Scripts/python.exe scripts/manual_demo.py`` (Windows) or
``backend/.venv/bin/python scripts/manual_demo.py``. It pins the settings that change what pages
show (ALLOW_INSECURE_HTTP, BEHIND_PROXY, web search, storage), so the real ``.env`` does not
alter the screenshots. It reuses test helpers from ``backend/tests`` (docfiles).
"""

import os
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / ".manual-demo"
DATA = DEMO / "data"
PORT = 8767
BASE = f"http://localhost:{PORT}"

if DATA.exists():
    shutil.rmtree(DATA)
DATA.mkdir(parents=True)

os.environ.update(
    {
        "APP_ENV": "development",
        "APP_HOST": "127.0.0.1",
        "APP_PORT": str(PORT),
        "BASE_URL": BASE,
        "SECRET_KEY": "demo-" + secrets.token_hex(24),
        "DATA_DIR": str(DATA),
        "DATABASE_URL": f"sqlite+aiosqlite:///{(DATA / 'coursepilot.db').as_posix()}",
        "CHECKPOINT_DB_PATH": str(DATA / "checkpoints.db"),
        "LLM_PROVIDER": "fake",
        "LLM_MODEL": "claude-sonnet-5-5",
        "LLM_FAST_MODEL": "claude-haiku-5-5",
        "EMBEDDING_PROVIDER": "fake",
        "EMBEDDING_MODEL": "fake-hash-v1",
        "EMBEDDING_DIM": "16",
        "WEB_SEARCH_PROVIDER": "none",
        "LOG_LEVEL": "WARNING",
        # Ignore the owner's real .env for anything that changes what the pages show.
        "ALLOW_INSECURE_HTTP": "false",
        "BEHIND_PROXY": "false",
        "WEB_ALLOWED_DOMAINS": "",
        "STORAGE_BACKEND": "local",
    }
)
sys.path.insert(0, str(ROOT / "backend"))

import asyncio  # noqa: E402
import json  # noqa: E402

import httpx  # noqa: E402
from sqlalchemy import update  # noqa: E402

from app import cli  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.llm import fake as fake_llm  # noqa: E402
from app.models import Block, Module, User, Year  # noqa: E402
from app.models.enums import Role  # noqa: E402
from app.services import users  # noqa: E402
from tests.docfiles import make_docx, make_pptx, make_text_pdf  # noqa: E402


def demo_responder(messages):  # type: ignore[no-untyped-def]
    prompt = "\n".join(str(m.content) for m in messages)
    if "standalone search query" in prompt:
        return str(messages[-1].content).splitlines()[-1][:200]
    if "SUFFICIENT if the excerpts" in prompt:
        return "SUFFICIENT"
    if 'id="C1"' in prompt:
        two = 'id="C2"' in prompt
        return (
            "The Frank-Starling mechanism links filling to output: a larger end-diastolic "
            "volume stretches the cardiac muscle fibres, which then contract more forcefully, "
            "so stroke volume rises to match venous return [C1]."
            + (
                "\n\nIn heart failure this curve is flattened, so extra filling raises pressures "
                "without a matching increase in stroke volume [C2]."
                if two
                else ""
            )
            + "\n\nTip: try sketching the curve for a normal and a failing heart side by side."
        )
    return "I could not find this in the uploaded material."


fake_llm.FakeChatModel.model_fields["responder"].default = demo_responder
fake_llm.FakeChatModel.model_rebuild(force=True)

PASSWORDS = {r: "Demo-" + secrets.token_urlsafe(9) for r in ("admin", "faculty", "student")}
EMAILS = {r: f"{r}@demo.coursepilot.test" for r in PASSWORDS}
NAMES = {"admin": "Dr. Amina Admin", "faculty": "Prof. Farah Khan", "student": "Sara Ahmed"}


async def seed(settings: Settings) -> None:
    db = Database(settings.database_url)
    try:
        async with db.session() as s:
            admin = (
                await s.execute(User.__table__.select().where(User.email == EMAILS["admin"]))
            ).first()
            admin_user = await s.get(User, admin.id)
            for role in ("faculty", "student"):
                await users.create_user(
                    s,
                    admin_user,
                    email=EMAILS[role],
                    display_name=NAMES[role],
                    role=Role(role),
                    password=PASSWORDS[role],
                )
            await s.execute(update(User).values(must_change_password=False))
            years = (await s.execute(Year.__table__.select().order_by(Year.number))).all()
            year_titles = ["Year 1 - Foundations", "Year 2 - Systems", "Year 3 - Clinical"]
            for row, title in zip(years, year_titles, strict=False):
                await s.execute(update(Year).where(Year.id == row.id).values(title=title))
            y1 = years[0].id
            blocks = (
                await s.execute(
                    Block.__table__.select().where(Block.year_id == y1).order_by(Block.number)
                )
            ).all()
            await s.execute(
                update(Block).where(Block.id == blocks[0].id).values(title="Physiology I")
            )
            await s.execute(
                update(Block).where(Block.id == blocks[1].id).values(title="Cell Biology")
            )
            mods = (
                await s.execute(
                    Module.__table__.select()
                    .where(Module.block_id == blocks[0].id)
                    .order_by(Module.number)
                )
            ).all()
            for row, title in zip(
                mods,
                ["Cardiovascular System", "Respiratory System", "Renal Physiology"],
                strict=False,
            ):
                await s.execute(update(Module).where(Module.id == row.id).values(title=title))
            await s.commit()
    finally:
        await db.dispose()


def login(c: httpx.Client, role: str) -> str:
    token = c.get("/api/v1/auth/csrf").json()["csrf_token"]
    r = c.post(
        "/api/v1/auth/login",
        json={"email": EMAILS[role], "password": PASSWORDS[role]},
        headers={"X-CSRF-Token": token},
    )
    return str(r.json()["csrf_token"])


def populate() -> None:
    with httpx.Client(base_url=BASE, headers={"Origin": BASE}, timeout=60) as c:
        for _ in range(240):
            try:
                if c.get("/api/v1/health/live").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        csrf = login(c, "faculty")
        tree = c.get("/api/v1/structure").json()
        b0 = tree["years"][0]["blocks"][0]["modules"]
        cardio, resp = b0[0]["id"], b0[1]["id"]
        docs = [
            (
                cardio,
                "lecture-07-ventricular-mechanics.pdf",
                "application/pdf",
                make_text_pdf(
                    [
                        "Lecture 7 - Ventricular Mechanics. The Frank-Starling mechanism describes "
                        "how an increase in end-diastolic volume stretches cardiac myocytes and "
                        "increases the force of contraction, so stroke volume rises with venous "
                        "return.",
                        "Preload is the end-diastolic stretch of the ventricle; afterload is the "
                        "pressure the ventricle must overcome to eject blood.",
                        "In heart failure the Frank-Starling curve is flattened: further increases "
                        "in preload raise filling pressures without a matching rise in stroke volume.",
                    ],
                    header="Physiology I - Cardiovascular System",
                    footer="CoursePilot demo",
                ),
            ),
            (
                cardio,
                "cardiac-cycle.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                make_pptx(
                    [
                        {
                            "title": "The cardiac cycle",
                            "bullets": [
                                "Atrial systole",
                                "Isovolumetric contraction",
                                "Ejection",
                                "Isovolumetric relaxation",
                                "Filling",
                            ],
                            "notes": "Valve opening and closing mark the transitions between phases.",
                        },
                        {
                            "title": "Pressure-volume loop",
                            "bullets": ["Width = stroke volume", "Area = stroke work"],
                        },
                    ]
                ),
            ),
            (
                cardio,
                "blood-pressure-regulation.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                make_docx(
                    [
                        ("h1", "Blood pressure regulation"),
                        (
                            "p",
                            "The baroreceptor reflex adjusts heart rate and vascular tone within "
                            "seconds when arterial pressure changes.",
                        ),
                        ("h2", "Long-term control"),
                        (
                            "p",
                            "The kidneys regulate blood volume through the renin-angiotensin-"
                            "aldosterone system.",
                        ),
                    ]
                ),
            ),
            (
                resp,
                "gas-exchange-notes.md",
                "text/markdown",
                b"# Gas exchange\n\nOxygen diffuses from the alveoli into pulmonary capillary blood "
                b"down its partial pressure gradient.\n\n## Ventilation-perfusion\n\nA V/Q mismatch "
                b"reduces the efficiency of gas exchange.\n",
            ),
            (resp, "scanned-handout.pdf", "application/pdf", make_text_pdf(["", "", ""])),
        ]
        for module_id, name, mime, data in docs:
            c.post(
                f"/api/v1/modules/{module_id}/documents",
                files={"files": (name, data, mime)},
                headers={"X-CSRF-Token": csrf},
            )
        for _ in range(240):
            items = c.get(f"/api/v1/modules/{cardio}/documents").json()
            items = items.get("items", items) if isinstance(items, dict) else items
            if items and all(d["status"] in ("ready", "failed") for d in items):
                break
            time.sleep(1)
        c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        csrf = login(c, "student")
        conv = None
        for q in [
            "How does the Frank-Starling mechanism relate to stroke volume?",
            "And what changes in heart failure?",
        ]:
            body = {"message": q, "module_id": cardio}
            if conv:
                body["conversation_id"] = conv
            with c.stream("POST", "/api/v1/chat", json=body, headers={"X-CSRF-Token": csrf}) as r:
                conv = conv or r.headers.get("X-Conversation-Id")
                done = None
                event = None
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and event == "done":
                        done = json.loads(line.split(":", 1)[1])
            time.sleep(1.2)
        if done:
            c.post(
                f"/api/v1/messages/{done['message_id']}/feedback",
                json={"feedback": "down", "comment": "Would like a diagram of the curve."},
                headers={"X-CSRF-Token": csrf},
            )
        with c.stream(
            "POST",
            "/api/v1/chat",
            json={"message": "What is the baroreceptor reflex?", "module_id": cardio},
            headers={"X-CSRF-Token": csrf},
        ) as r:
            for _ in r.iter_lines():
                pass
    print("manual demo populated", flush=True)


def main() -> int:
    settings = Settings()
    cli.run_migrate(settings)
    cli.run_seed(settings)
    cli._create_admin(settings, EMAILS["admin"], NAMES["admin"], PASSWORDS["admin"])
    asyncio.run(seed(settings))
    (DEMO / "credentials.txt").write_text(
        "\n".join(f"{r} {EMAILS[r]} {PASSWORDS[r]}" for r in PASSWORDS) + "\n", encoding="utf-8"
    )
    threading.Thread(target=populate, daemon=True).start()
    return cli.run_serve(settings, migrate=False, with_worker=True)


if __name__ == "__main__":
    raise SystemExit(main())
