"""Add the durable post-publication metrics collection ledger.

Revision ID: e8b4c6d2a901
Revises: d4e8a1c7b5f2
Create Date: 2026-08-11 00:00:00.000000

APScheduler date callbacks are process-local and disappear on restart.  One
row per publish job/window now records the immutable due time, retry schedule,
and expiring owner lease.  A unique nullable foreign key on ``metrics`` binds
successful tasks to exactly one normalized evidence snapshot.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8b4c6d2a901"
down_revision: Union[str, None] = "d4e8a1c7b5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metrics_collection_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("interval_index", sa.Integer(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("collection_deadline_at", sa.DateTime(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'succeeded', 'failed')",
            name="ck_metrics_collection_tasks_status",
        ),
        sa.CheckConstraint(
            "interval_index >= 0 AND window_seconds > 0 "
            "AND collection_deadline_at > due_at "
            "AND ((interval_index = 0 AND window_seconds = 3600) "
            "OR (interval_index = 1 AND window_seconds = 86400) "
            "OR (interval_index = 2 AND window_seconds = 604800))",
            name="ck_metrics_collection_tasks_window",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND max_attempts <= 20 "
            "AND attempts <= max_attempts",
            name="ck_metrics_collection_tasks_attempts",
        ),
        sa.CheckConstraint(
            "((status = 'queued' AND next_attempt_at IS NOT NULL "
            "AND next_attempt_at >= due_at AND next_attempt_at < collection_deadline_at "
            "AND attempts < max_attempts AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'claimed' AND next_attempt_at IS NULL "
            "AND lease_token IS NOT NULL AND length(lease_token) = 64 "
            "AND lease_expires_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND next_attempt_at IS NULL "
            "AND attempts > 0 "
            "AND lease_token IS NULL AND lease_expires_at IS NULL "
            "AND last_error IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND next_attempt_at IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL "
            "AND last_error IS NOT NULL AND length(last_error) > 0 "
            "AND finished_at IS NOT NULL))",
            name="ck_metrics_collection_tasks_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["publish_jobs.id"],
            name="fk_metrics_collection_tasks_job_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "interval_index",
            name="uq_metrics_collection_tasks_job_interval",
        ),
        sa.UniqueConstraint(
            "job_id",
            "window_seconds",
            name="uq_metrics_collection_tasks_job_window",
        ),
        sa.UniqueConstraint(
            "id",
            "job_id",
            name="uq_metrics_collection_tasks_id_job",
        ),
    )
    op.create_index(
        "ix_metrics_collection_tasks_due",
        "metrics_collection_tasks",
        ["status", "next_attempt_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_metrics_collection_tasks_expired_lease",
        "metrics_collection_tasks",
        ["status", "lease_expires_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_metrics_collection_tasks_deadline",
        "metrics_collection_tasks",
        ["status", "collection_deadline_at", "id"],
        unique=False,
    )

    with op.batch_alter_table("metrics", schema=None) as batch_op:
        batch_op.add_column(sa.Column("collection_task_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_metrics_collection_task_job",
            "metrics_collection_tasks",
            ["collection_task_id", "job_id"],
            ["id", "job_id"],
        )
        batch_op.create_unique_constraint(
            "uq_metrics_collection_task_id",
            ["collection_task_id"],
        )
        batch_op.create_check_constraint(
            "ck_metrics_single_ledger_owner",
            "agent_operation_id IS NULL OR collection_task_id IS NULL",
        )
        batch_op.create_check_constraint(
            "ck_metrics_collection_task_source",
            "collection_task_id IS NULL OR source = 'scheduled'",
        )
        batch_op.create_index(
            "ix_metrics_job_collected_id",
            ["job_id", "collected_at", "id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("metrics", schema=None) as batch_op:
        batch_op.drop_index("ix_metrics_job_collected_id")
        batch_op.drop_constraint("ck_metrics_collection_task_source", type_="check")
        batch_op.drop_constraint("ck_metrics_single_ledger_owner", type_="check")
        batch_op.drop_constraint("uq_metrics_collection_task_id", type_="unique")
        batch_op.drop_constraint("fk_metrics_collection_task_job", type_="foreignkey")
        batch_op.drop_column("collection_task_id")

    op.drop_index(
        "ix_metrics_collection_tasks_deadline",
        table_name="metrics_collection_tasks",
    )
    op.drop_index(
        "ix_metrics_collection_tasks_expired_lease",
        table_name="metrics_collection_tasks",
    )
    op.drop_index(
        "ix_metrics_collection_tasks_due",
        table_name="metrics_collection_tasks",
    )
    op.drop_table("metrics_collection_tasks")
