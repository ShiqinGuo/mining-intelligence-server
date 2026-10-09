from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3a39f510590c"
down_revision: str | None = "136e253238b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    budgets = op.create_table(
        "task_retry_budgets",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "failure_retry_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task_runs.id"],
            name=op.f("fk_task_retry_budgets_task_id_task_runs"),
        ),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_task_retry_budgets")),
    )
    tasks = sa.table("task_runs", sa.column("id", sa.Uuid()))
    op.execute(budgets.insert().from_select(["task_id"], sa.select(tasks.c.id)))


def downgrade() -> None:
    op.drop_table("task_retry_budgets")
