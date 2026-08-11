"""add durable Agent contract plans, approvals, and idempotency ledger.

Revision ID: a2f7c9e4d1b6
Revises: f1c3b8a7d2e4
Create Date: 2026-08-11 00:00:00.000000

The migration is deliberately additive for legacy callers:

* existing ``publish_jobs`` rows keep ``plan_id=NULL``;
* contract-v1 jobs gain one row per ``(plan_id, account_id)``;
* approval decisions bind to an exact plan digest and authenticated identities;
* an Agent operation may first claim an idempotency key with a null response;
  external reads use an expiring ownership lease and bind their normalized
  metric row before finalizing the replay response.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2f7c9e4d1b6"
down_revision: Union[str, None] = "f1c3b8a7d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "publication_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("content_snapshot", sa.JSON(), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("planned_for", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_by_type", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('draft', 'approval_pending', 'approved', 'rejected', "
            "'scheduled', 'cancelled', 'expired')",
            name="ck_publication_plans_state",
        ),
        sa.CheckConstraint(
            "length(content_digest) = 64",
            name="ck_publication_plans_content_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_publication_plans_plan_digest_sha256",
        ),
        sa.ForeignKeyConstraint(["article_id"], ["articles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_by_type", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_by_type", sa.String(length=32), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled', 'expired')",
            name="ck_approval_requests_status",
        ),
        sa.CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_approval_requests_plan_digest_sha256",
        ),
        sa.CheckConstraint(
            "((decided_by IS NULL AND decided_by_type IS NULL) OR "
            "(decided_by IS NOT NULL AND decided_by_type IS NOT NULL))",
            name="ck_approval_requests_decider_identity",
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'rejected') OR "
            "(decided_by IS NOT NULL AND decided_by_type IS NOT NULL "
            "AND decided_at IS NOT NULL)",
            name="ck_approval_requests_decision_complete",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["publication_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_requests_plan_id",
        "approval_requests",
        ["plan_id"],
        unique=False,
    )

    op.create_table(
        "agent_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(request_digest) = 64",
            name="ck_agent_operations_request_digest_sha256",
        ),
        sa.CheckConstraint(
            "response_status_code IS NULL OR "
            "(response_status_code >= 100 AND response_status_code <= 599)",
            name="ck_agent_operations_response_status_code",
        ),
        sa.CheckConstraint(
            "(response_status_code IS NULL AND response_json IS NULL) OR "
            "(response_status_code IS NOT NULL AND response_json IS NOT NULL)",
            name="ck_agent_operations_response_complete",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND length(lease_token) = 64)",
            name="ck_agent_operations_lease_complete",
        ),
        sa.CheckConstraint(
            "response_json IS NULL OR (lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_agent_operations_completed_not_leased",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_id",
            "operation",
            "idempotency_key",
            name="uq_agent_operations_principal_operation_key",
        ),
    )

    with op.batch_alter_table("metrics", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent_operation_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_metrics_agent_operation_id",
            "agent_operations",
            ["agent_operation_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_metrics_agent_operation_id",
            ["agent_operation_id"],
        )

    with op.batch_alter_table("publish_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plan_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_publish_jobs_plan_id",
            "publication_plans",
            ["plan_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_publish_jobs_plan_account",
            ["plan_id", "account_id"],
        )

    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("content_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("size_bytes", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("storage_kind", sa.String(length=32), nullable=True))
        batch_op.create_check_constraint(
            "ck_assets_vault_metadata_complete",
            "(storage_kind IS NULL AND content_sha256 IS NULL AND size_bytes IS NULL) OR "
            "(storage_kind IS NOT NULL AND content_sha256 IS NOT NULL "
            "AND size_bytes IS NOT NULL AND storage_kind = 'agent_vault_v1' "
            "AND length(content_sha256) = 64 AND size_bytes >= 0)",
        )


def downgrade() -> None:
    with op.batch_alter_table("metrics", schema=None) as batch_op:
        batch_op.drop_constraint("uq_metrics_agent_operation_id", type_="unique")
        batch_op.drop_constraint("fk_metrics_agent_operation_id", type_="foreignkey")
        batch_op.drop_column("agent_operation_id")

    with op.batch_alter_table("assets", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_assets_vault_metadata_complete",
            type_="check",
        )
        batch_op.drop_column("storage_kind")
        batch_op.drop_column("size_bytes")
        batch_op.drop_column("content_sha256")

    with op.batch_alter_table("publish_jobs", schema=None) as batch_op:
        batch_op.drop_constraint("uq_publish_jobs_plan_account", type_="unique")
        batch_op.drop_constraint("fk_publish_jobs_plan_id", type_="foreignkey")
        batch_op.drop_column("plan_id")

    op.drop_table("agent_operations")
    op.drop_index("ix_approval_requests_plan_id", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_table("publication_plans")
