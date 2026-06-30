"""add job_accounts — 招聘平台账号表（P2 地基）。

Revision ID: f1c3b8a7d2e4
Revises: e4a7c1d9b2f3
Create Date: 2026-06-28 00:00:00.000000

新增 job_accounts（src/ai_ops/jobhunt/models.JobAccount）：
与内容平台 accounts 表刻意分离（board 是招聘平台枚举，不污染 dispatcher/health_monitor）。
凭证 encrypted_credential 复用 accounts/store.py 的 Fernet 加密，不裸存 cookie。

applications.account_id 保持裸 Integer（不在此加 DB FK，避免 batch-alter 已建表），
引用完整性由 jobhunt/accounts.py manager 层保证。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1c3b8a7d2e4'
down_revision: Union[str, None] = 'e4a7c1d9b2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_accounts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('board', sa.String(length=32), nullable=False),
        sa.Column('nickname', sa.String(length=128), nullable=False),
        sa.Column('profile', sa.JSON(), nullable=False),
        sa.Column('encrypted_credential', sa.LargeBinary(), nullable=False),
        sa.Column('health', sa.String(length=32), nullable=False),
        sa.Column('daily_quota', sa.Integer(), nullable=False),
        sa.Column('last_apply_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('job_accounts')
