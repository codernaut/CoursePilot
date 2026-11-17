"""Access code generation, normalisation and the access filter SQL for guests (no database)."""

import math
import re
from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.enums import Role
from app.retrieval.access import AccessContext, access_filter
from app.services import access_codes
from app.services.access_codes import (
    CODE_ALPHABET,
    CODE_LENGTH,
    generate_code,
    hash_code,
    normalize_code,
)

FORMAT = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{3}$")


def test_generated_codes_have_the_documented_format_and_alphabet() -> None:
    codes = {generate_code() for _ in range(500)}
    assert len(codes) == 500  # no collisions in a small sample
    for code in codes:
        assert FORMAT.match(code)
        assert not set(code.replace("-", "")) & set("0O1IL")
    assert not set(CODE_ALPHABET) & set("0O1IL")


def test_code_has_at_least_50_bits_of_entropy() -> None:
    assert math.log2(len(CODE_ALPHABET)) * CODE_LENGTH >= 50


def test_normalisation_ignores_case_spaces_and_dashes() -> None:
    forms = ["abcd-efgh-jkm", "ABCD EFGH JKM", " abcdefghjkm ", "ab-cd.ef_gh jk/m"]
    assert {normalize_code(f) for f in forms} == {"ABCDEFGHJKM"}
    assert len({hash_code(f) for f in forms}) == 1
    assert hash_code("ABCD-EFGH-JKN") != hash_code("ABCD-EFGH-JKM")
    assert len(hash_code("x")) == 64


def test_label_and_guest_name_are_cleaned() -> None:
    assert access_codes.clean_label("  Revision \n session\t ") == "Revision session"
    assert access_codes.clean_label("   ") is None
    assert access_codes.clean_label(None) is None
    assert len(access_codes.clean_label("x" * 500) or "") == 80
    assert access_codes.clean_guest_name("  Ada \n Lovelace ") == "Ada Lovelace"


def test_guest_name_is_required_and_bounded() -> None:
    import pytest

    from app.core.errors import AppError

    for bad in ("", "   ", "x" * 41):
        with pytest.raises(AppError) as info:
            access_codes.clean_guest_name(bad)
        assert info.value.status_code == 422


def test_guest_access_filter_is_module_and_code_bound() -> None:
    guest = SimpleNamespace(id="g1", is_active=True, role=Role.GUEST)
    ctx = AccessContext.from_user(guest)
    assert ctx.is_guest
    now = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
    flt = access_filter(ctx, now=now)
    sql = flt.sql
    for fragment in (
        "guest_accounts",
        "access_codes",
        "revoked_at IS NULL",
        "expires_at > :access_now",
        "ac_access.module_id = c.module_id",
        "u_access.role = 'guest'",
    ):
        assert fragment in sql
    assert flt.params == {"access_user_id": "g1", "access_now": "2026-11-17 09:30:00.000000"}


def test_account_filter_never_admits_a_guest_row() -> None:
    student = SimpleNamespace(id="s1", is_active=True, role=Role.STUDENT)
    flt = access_filter(AccessContext.from_user(student))
    assert "role != 'guest'" in flt.sql
    assert "access_codes" not in flt.sql
    # A guest whose context was (wrongly) built as an account still matches nothing.
    wrong = AccessContext(user_id="g1", is_active=True, is_guest=False)
    assert "role != 'guest'" in access_filter(wrong).sql


def test_anonymous_and_inactive_get_nothing() -> None:
    assert access_filter(AccessContext.anonymous()).sql == "0 = 1"
    inactive = SimpleNamespace(id="g1", is_active=False, role=Role.GUEST)
    assert access_filter(AccessContext.from_user(inactive)).sql == "0 = 1"


def test_manual_demo_creates_a_guest_code_for_the_screenshots() -> None:
    from pathlib import Path

    script = (Path(__file__).resolve().parents[3] / "scripts" / "manual_demo.py").read_text(
        encoding="utf-8"
    )
    assert "/access-codes" in script and '"guest-code "' in script
