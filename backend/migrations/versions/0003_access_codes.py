"""access_codes, guest_accounts and the `guest` user role (temporary module access)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITH_GUEST = "'student', 'guest')"
_WITHOUT_GUEST = "'student')"


def _rewrite_role_check(old: str, new: str) -> None:
    """Change the allowed values of ``users.role`` without rebuilding the table.

    A batch rebuild would have to drop and recreate ``users``, which every other table points at
    (and which fails with foreign keys on). The documented SQLite procedure for a schema change
    that does not alter the stored rows is to edit the table's SQL text under ``writable_schema``
    and bump ``schema_version`` so other connections reload it.
    """
    bind = op.get_bind()
    version = int(bind.exec_driver_sql("PRAGMA schema_version").scalar_one())
    bind.exec_driver_sql("PRAGMA writable_schema=ON")
    bind.execute(
        sa.text(
            "UPDATE sqlite_master SET sql = replace(sql, :old, :new) "
            "WHERE type = 'table' AND name = 'users'"
        ),
        {"old": old, "new": new},
    )
    bind.exec_driver_sql(f"PRAGMA schema_version={version + 1}")
    bind.exec_driver_sql("PRAGMA writable_schema=OFF")
    # The replace is a no-op if the stored text differs from what we expect (hand-edited schema,
    # a different quoting style). Never let the migration "succeed" without changing the check.
    stored = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).scalar_one()
    if new not in stored or old in stored:
        raise RuntimeError(
            "Could not rewrite the users.role CHECK constraint: unexpected table definition. "
            f"Expected to find {old!r} in: {stored}"
        )


def upgrade() -> None:
    _rewrite_role_check(_WITHOUT_GUEST, _WITH_GUEST)
    op.create_table(
        "access_codes",
        sa.Column("module_id", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("code_hint", sa.String(length=4), nullable=False),
        sa.Column("duration_hours", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=32), nullable=True),
        sa.Column("redemptions", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["module_id"], ["modules.id"], name=op.f("fk_access_codes_module_id_modules")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_access_codes_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by"], ["users.id"], name=op.f("fk_access_codes_revoked_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_access_codes_code_hash")),
    )
    op.create_index("ix_access_codes_module_id", "access_codes", ["module_id"])
    op.create_table(
        "guest_accounts",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("access_code_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_guest_accounts_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["access_code_id"],
            ["access_codes.id"],
            name=op.f("fk_guest_accounts_access_code_id_access_codes"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guest_accounts")),
        sa.UniqueConstraint("user_id", name=op.f("uq_guest_accounts_user_id")),
    )
    op.create_index("ix_guest_accounts_access_code_id", "guest_accounts", ["access_code_id"])


_DOWNGRADE_STEPS = (
    "DELETE FROM messages WHERE conversation_id IN "
    "(SELECT id FROM conversations WHERE user_id IN (SELECT id FROM users WHERE role = 'guest'))",
    "DELETE FROM conversations WHERE user_id IN (SELECT id FROM users WHERE role = 'guest')",
    "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE role = 'guest')",
    "UPDATE audit_log SET actor_id = NULL "
    "WHERE actor_id IN (SELECT id FROM users WHERE role = 'guest')",
)


def downgrade() -> None:
    # Guests and everything they own go first: the old role check would reject them.
    bind = op.get_bind()
    for statement in _DOWNGRADE_STEPS:
        bind.exec_driver_sql(statement)
    op.drop_table("guest_accounts")
    op.drop_table("access_codes")
    bind.exec_driver_sql("DELETE FROM users WHERE role = 'guest'")
    _rewrite_role_check(_WITH_GUEST, _WITHOUT_GUEST)
