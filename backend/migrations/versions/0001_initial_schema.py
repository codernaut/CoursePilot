"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-06 20:16:16.139106
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import Settings, get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _settings() -> Settings:
    configured = op.get_context().config.attributes.get("settings")  # type: ignore[union-attr]
    return configured if isinstance(configured, Settings) else get_settings()


def upgrade() -> None:
    op.create_table(
        "app_meta",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_meta")),
    )
    op.create_table(
        "jobs",
        sa.Column(
            "type",
            sa.Enum(
                "ingest_document",
                "reindex_document",
                "delete_document",
                name="job_type",
                native_enum=False,
                create_constraint=True,
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="job_status",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(), nullable=False),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_status_run_after", ["status", "run_after"], unique=False)

    op.create_table(
        "users",
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "admin",
                "faculty",
                "student",
                name="user_role",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "years",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_years")),
        sa.UniqueConstraint("number", name=op.f("uq_years_number")),
    )
    op.create_table(
        "audit_log",
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name=op.f("fk_audit_log_actor_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index("ix_audit_log_created_at", ["created_at"], unique=False)

    op.create_table(
        "blocks",
        sa.Column("year_id", sa.String(length=32), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["year_id"], ["years.id"], name=op.f("fk_blocks_year_id_years")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blocks")),
        sa.UniqueConstraint("year_id", "number", name=op.f("uq_blocks_year_id")),
    )
    op.create_table(
        "sessions",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index("ix_sessions_user_id", ["user_id"], unique=False)

    op.create_table(
        "modules",
        sa.Column("block_id", sa.String(length=32), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("web_search_enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["block_id"], ["blocks.id"], name=op.f("fk_modules_block_id_blocks")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_modules")),
        sa.UniqueConstraint("block_id", "number", name=op.f("uq_modules_block_id")),
    )
    op.create_table(
        "conversations",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("module_id", sa.String(length=32), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["module_id"], ["modules.id"], name=op.f("fk_conversations_module_id_modules")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_conversations_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.create_index("ix_conversations_user_id", ["user_id"], unique=False)

    op.create_table(
        "documents",
        sa.Column("module_id", sa.String(length=32), nullable=False),
        sa.Column("uploaded_by", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("original_filename", sa.String(length=300), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "uploaded",
                "processing",
                "ready",
                "failed",
                "deleting",
                name="document_status",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("week", sa.String(length=60), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["module_id"], ["modules.id"], name=op.f("fk_documents_module_id_modules")
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"], name=op.f("fk_documents_uploaded_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("module_id", "content_sha256", name=op.f("uq_documents_module_id")),
    )
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.create_index("ix_documents_module_id", ["module_id"], unique=False)

    op.create_table(
        "chunks",
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("module_id", sa.String(length=32), nullable=False),
        sa.Column("block_id", sa.String(length=32), nullable=False),
        sa.Column("year_id", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embed_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("slide_number", sa.Integer(), nullable=True),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(length=120), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["block_id"], ["blocks.id"], name=op.f("fk_chunks_block_id_blocks")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("fk_chunks_document_id_documents")
        ),
        sa.ForeignKeyConstraint(
            ["module_id"], ["modules.id"], name=op.f("fk_chunks_module_id_modules")
        ),
        sa.ForeignKeyConstraint(["year_id"], ["years.id"], name=op.f("fk_chunks_year_id_years")),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_chunks")),
        sa.UniqueConstraint("id", name=op.f("uq_chunks_id")),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("chunks", schema=None) as batch_op:
        batch_op.create_index("ix_chunks_block_id", ["block_id"], unique=False)
        batch_op.create_index(
            "ix_chunks_document_id_ordinal", ["document_id", "ordinal"], unique=False
        )
        batch_op.create_index("ix_chunks_module_id", ["module_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "user",
                "assistant",
                name="message_role",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("used_web", sa.Boolean(), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column(
            "feedback",
            sa.Enum(
                "up",
                "down",
                name="message_feedback",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=True,
        ),
        sa.Column("feedback_comment", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.create_index("ix_messages_conversation_id", ["conversation_id"], unique=False)

    # Virtual tables: created by hand (excluded from autogenerate, see migrations/env.py).
    settings = _settings()
    op.execute(
        "CREATE VIRTUAL TABLE chunk_vectors USING vec0("
        f"embedding float[{int(settings.embedding_dim)}] distance_metric=cosine)"
    )
    op.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(embed_text, tokenize='porter unicode61')"
    )
    app_meta = sa.table("app_meta", sa.column("key", sa.String), sa.column("value", sa.Text))
    op.bulk_insert(
        app_meta,
        [
            {"key": "embedding_provider", "value": settings.embedding_provider},
            {"key": "embedding_model", "value": settings.embedding_model},
            {"key": "embedding_dim", "value": str(settings.embedding_dim)},
        ],
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunks_fts")
    op.execute("DROP TABLE IF EXISTS chunk_vectors")
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_index("ix_messages_conversation_id")

    op.drop_table("messages")
    with op.batch_alter_table("chunks", schema=None) as batch_op:
        batch_op.drop_index("ix_chunks_module_id")
        batch_op.drop_index("ix_chunks_document_id_ordinal")
        batch_op.drop_index("ix_chunks_block_id")

    op.drop_table("chunks")
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index("ix_documents_module_id")

    op.drop_table("documents")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index("ix_conversations_user_id")

    op.drop_table("conversations")
    op.drop_table("modules")
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_sessions_user_id")

    op.drop_table("sessions")
    op.drop_table("blocks")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_log_created_at")

    op.drop_table("audit_log")
    op.drop_table("years")
    op.drop_table("users")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_jobs_status_run_after")

    op.drop_table("jobs")
    op.drop_table("app_meta")
