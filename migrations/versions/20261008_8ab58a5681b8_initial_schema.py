from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8ab58a5681b8"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=100), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("STORED", "READY", "FAILED", name="document_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("sha256", name=op.f("uq_documents_sha256")),
    )
    op.create_table(
        "model_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DISCONNECTED",
                "AWAITING_AUTH",
                "CONNECTED",
                "REFRESHING",
                "REAUTH_REQUIRED",
                "UNAVAILABLE",
                name="connection_status",
            ),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=True),
        sa.Column("email", sa.String(length=500), nullable=True),
        sa.Column("client_id", sa.String(length=300), nullable=True),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("installation_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("token_generation", sa.Integer(), nullable=False),
        sa.Column(
            "refresh_state",
            sa.Enum("NONE", "CLAIMED", "COMPLETE", "UNKNOWN", name="refresh_state"),
            nullable=False,
        ),
        sa.Column("refresh_owner", sa.Uuid(), nullable=True),
        sa.Column("refresh_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("catalog_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_connections")),
        sa.UniqueConstraint("slot", name=op.f("uq_model_connections_slot")),
    )
    op.create_table(
        "price_instruments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_instruments")),
        sa.UniqueConstraint("slug", name=op.f("uq_price_instruments_slug")),
    )
    op.create_table(
        "runtime_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_config")),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "ENABLED", "DISABLED", "ARCHIVED", name="sourcestate", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("name", name=op.f("uq_sources_name")),
    )
    op.create_index(
        op.f("ix_sources_next_due_at"), "sources", ["next_due_at"], unique=False
    )
    op.create_index(op.f("ix_sources_state"), "sources", ["state"], unique=False)
    op.create_table(
        "task_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "COLLECT_SOURCE",
                "FETCH_ARTICLE",
                "DOCUMENT_INGEST",
                "RESOURCE_EXTRACTION",
                "COLLECT_PRICES",
                "NEWS_ANALYSIS",
                name="task_kind",
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "QUEUED",
                "RUNNING",
                "WAITING_AUTH",
                "WAITING_QUOTA",
                "RETRY_WAIT",
                "PARTIAL",
                "SUCCEEDED",
                "FAILED",
                "CANCEL_REQUESTED",
                "CANCELLED",
                "UNKNOWN",
                name="task_state",
            ),
            nullable=False,
        ),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("workflow_version", sa.String(length=100), nullable=False),
        sa.Column("input_revision", sa.String(length=100), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("owner", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint_seq", sa.Integer(), nullable=False),
        sa.Column("next_step", sa.String(length=200), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column(
            "error_code",
            sa.Enum(
                "INVALID_INPUT",
                "NOT_FOUND",
                "CONFLICT",
                "UNAUTHORIZED",
                "FORBIDDEN",
                "UPSTREAM_FAILURE",
                "NO_QUOTE",
                "COVERAGE_INSUFFICIENT",
                "STANDARD_MISMATCH",
                "WAITING_AUTH",
                "WAITING_QUOTA",
                "UNKNOWN_RESULT",
                "BUSY",
                "LEASE_LOST",
                "CANCELLED",
                "INTERNAL",
                name="error_code",
            ),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runs")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_task_runs_idempotency_key")
        ),
    )
    op.create_table(
        "articles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("project", sa.String(length=200), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_articles_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_articles")),
        sa.UniqueConstraint("url", name=op.f("uq_articles_url")),
    )
    op.create_index(
        op.f("ix_articles_discovered_at"), "articles", ["discovered_at"], unique=False
    )
    op.create_index(op.f("ix_articles_project"), "articles", ["project"], unique=False)
    op.create_index(
        op.f("ix_articles_published_at"), "articles", ["published_at"], unique=False
    )
    op.create_index(
        op.f("ix_articles_source_id"), "articles", ["source_id"], unique=False
    )
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("workflow_version", sa.String(length=100), nullable=False),
        sa.Column("input_revision", sa.String(length=100), nullable=False),
        sa.Column("completed_step", sa.String(length=200), nullable=False),
        sa.Column("next_step", sa.String(length=200), nullable=True),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task_runs.id"], name=op.f("fk_checkpoints_task_id_task_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checkpoints")),
        sa.UniqueConstraint("task_id", "sequence", name=op.f("uq_checkpoints_task_id")),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_collection_runs_source_id_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_runs")),
    )
    op.create_index(
        op.f("ix_collection_runs_source_id"),
        "collection_runs",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_collection_runs_task_id"), "collection_runs", ["task_id"], unique=True
    )
    op.create_table(
        "document_pages",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_pages_document_id_documents"),
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "page_number", name=op.f("pk_document_pages")
        ),
    )
    op.create_table(
        "model_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "INTENT", "COMPLETE", "UNKNOWN", "REJECTED", name="invocation_state"
            ),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task_runs.id"],
            name=op.f("fk_model_invocations_task_id_task_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_invocations")),
        sa.UniqueConstraint(
            "task_id", "name", "attempt", name=op.f("uq_model_invocations_task_id")
        ),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task_runs.id"],
            name=op.f("fk_outbox_messages_task_id_task_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_messages")),
    )
    op.create_table(
        "price_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("observed_date", sa.Date(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asof_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["price_instruments.id"],
            name=op.f("fk_price_observations_instrument_id_price_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_observations")),
        sa.UniqueConstraint(
            "instrument_id",
            "observed_date",
            "evidence_hash",
            name=op.f("uq_price_observations_instrument_id"),
        ),
    )
    op.create_index(
        op.f("ix_price_observations_asof_at"),
        "price_observations",
        ["asof_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_price_observations_instrument_id"),
        "price_observations",
        ["instrument_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_price_observations_observed_date"),
        "price_observations",
        ["observed_date"],
        unique=False,
    )
    op.create_table(
        "resource_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_resource_extractions_document_id_documents"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task_runs.id"],
            name=op.f("fk_resource_extractions_task_id_task_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_extractions")),
        sa.UniqueConstraint("task_id", name=op.f("uq_resource_extractions_task_id")),
    )
    op.create_table(
        "source_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_revisions_source_id_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_revisions")),
        sa.UniqueConstraint(
            "source_id", "revision", name=op.f("uq_source_revisions_source_id")
        ),
    )
    op.create_table(
        "step_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["task_runs.id"], name=op.f("fk_step_runs_task_id_task_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_step_runs")),
        sa.UniqueConstraint("task_id", "name", name=op.f("uq_step_runs_task_id")),
    )
    op.create_index(
        "ix_outbox_messages_pending",
        "outbox_messages",
        ["published_at", "claimed_until", "created_at"],
    )
    op.create_index(
        "ix_task_runs_recovery", "task_runs", ["state", "lease_until", "retry_at"]
    )
    op.create_table(
        "article_analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("article_revision", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_analyses_article_id_articles"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_article_analyses")),
        sa.UniqueConstraint(
            "article_id",
            "article_revision",
            "model",
            "prompt_version",
            name=op.f("uq_article_analyses_article_id"),
        ),
    )
    op.create_index(
        op.f("ix_article_analyses_article_id"),
        "article_analyses",
        ["article_id"],
        unique=False,
    )
    op.create_table(
        "article_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_revisions_article_id_articles"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_article_revisions")),
        sa.UniqueConstraint(
            "article_id", "content_hash", name=op.f("uq_article_revisions_article_id")
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_messages_pending", table_name="outbox_messages")
    op.drop_index("ix_task_runs_recovery", table_name="task_runs")
    op.drop_table("article_revisions")
    op.drop_index(op.f("ix_article_analyses_article_id"), table_name="article_analyses")
    op.drop_table("article_analyses")
    op.drop_table("step_runs")
    op.drop_table("source_revisions")
    op.drop_table("resource_extractions")
    op.drop_index(
        op.f("ix_price_observations_observed_date"), table_name="price_observations"
    )
    op.drop_index(
        op.f("ix_price_observations_instrument_id"), table_name="price_observations"
    )
    op.drop_index(
        op.f("ix_price_observations_asof_at"), table_name="price_observations"
    )
    op.drop_table("price_observations")
    op.drop_table("outbox_messages")
    op.drop_table("model_invocations")
    op.drop_table("document_pages")
    op.drop_index(op.f("ix_collection_runs_task_id"), table_name="collection_runs")
    op.drop_index(op.f("ix_collection_runs_source_id"), table_name="collection_runs")
    op.drop_table("collection_runs")
    op.drop_table("checkpoints")
    op.drop_index(op.f("ix_articles_source_id"), table_name="articles")
    op.drop_index(op.f("ix_articles_published_at"), table_name="articles")
    op.drop_index(op.f("ix_articles_project"), table_name="articles")
    op.drop_index(op.f("ix_articles_discovered_at"), table_name="articles")
    op.drop_table("articles")
    op.drop_table("task_runs")
    op.drop_index(op.f("ix_sources_state"), table_name="sources")
    op.drop_index(op.f("ix_sources_next_due_at"), table_name="sources")
    op.drop_table("sources")
    op.drop_table("runtime_config")
    op.drop_table("price_instruments")
    op.drop_table("model_connections")
    op.drop_table("documents")
    for name in (
        "invocation_state",
        "error_code",
        "task_state",
        "task_kind",
        "refresh_state",
        "connection_status",
        "document_status",
    ):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
