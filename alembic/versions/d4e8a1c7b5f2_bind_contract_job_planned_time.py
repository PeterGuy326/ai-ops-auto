"""Bind exact jobs to an immutable approved not-before timestamp.

``publish_jobs.scheduled_at`` predates the Agent contract and is intentionally
mutable: retry backoff and policy deferrals move it to the next attempt time.
Contract jobs therefore need a separate timestamp copied from the approved
plan.  Existing contract rows are backfilled from their publication plan;
legacy rows keep NULL and preserve their historical scheduling semantics.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e8a1c7b5f2"
down_revision: Union[str, None] = "a2f7c9e4d1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("publish_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("approved_planned_for", sa.DateTime(), nullable=True))

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE publish_jobs
            SET approved_planned_for = (
                SELECT publication_plans.planned_for
                FROM publication_plans
                WHERE publication_plans.id = publish_jobs.plan_id
            )
            WHERE plan_id IS NOT NULL
            """
        )
    )

    missing = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM publish_jobs
            WHERE plan_id IS NOT NULL AND approved_planned_for IS NULL
            """
        )
    ).scalar_one()
    if missing:
        raise RuntimeError("cannot bind existing contract jobs to their approved planned time")

    with op.batch_alter_table("publish_jobs", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_publish_jobs_contract_planned_for",
            "plan_id IS NULL OR approved_planned_for IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("publish_jobs", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_publish_jobs_contract_planned_for",
            type_="check",
        )
        batch_op.drop_column("approved_planned_for")
