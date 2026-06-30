"""add jobhunt tables — 求职投递专题四张表（P0）。

Revision ID: e4a7c1d9b2f3
Revises: 7c7c50aecd6a
Create Date: 2026-06-27 00:00:00.000000

新增四张表（src/ai_ops/jobhunt/models.py）：
  - resume_profiles  简历结构化结果（一份通用简历 + 顶层冗余高频字段）
  - job_postings     爬来的岗位 JD（(board, external_id) 唯一）
  - job_matches      简历 × 岗位 匹配打分（(resume_id, job_id) 唯一）
  - applications     投递记录（PublishJob 的对应物，(resume_id, job_id) 唯一）

设计见 jobhunt/__init__.py。本迁移只建表，不动存量表；
AssetType 新增 DOCUMENT 枚举值按 String 存，无 DB 级约束，无需迁移。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a7c1d9b2f3'
down_revision: Union[str, None] = '7c7c50aecd6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'resume_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('raw_asset_id', sa.Integer(), nullable=True),
        sa.Column('raw_text', sa.Text(), nullable=False),
        sa.Column('structured', sa.JSON(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('years_of_experience', sa.Float(), nullable=True),
        sa.Column('target_titles', sa.JSON(), nullable=False),
        sa.Column('expected_cities', sa.JSON(), nullable=False),
        sa.Column('expected_salary_min', sa.Integer(), nullable=True),
        sa.Column('expected_salary_max', sa.Integer(), nullable=True),
        sa.Column('skills', sa.JSON(), nullable=False),
        sa.Column('search_keywords', sa.JSON(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['raw_asset_id'], ['assets.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'job_postings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('board', sa.String(length=32), nullable=False),
        sa.Column('external_id', sa.String(length=128), nullable=False),
        sa.Column('url', sa.String(length=512), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('company', sa.String(length=256), nullable=False),
        sa.Column('location', sa.String(length=128), nullable=False),
        sa.Column('salary_text', sa.String(length=128), nullable=False),
        sa.Column('jd_text', sa.Text(), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=False),
        sa.Column('raw', sa.JSON(), nullable=False),
        sa.Column('crawled_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('board', 'external_id', name='uq_job_board_external'),
    )

    op.create_table(
        'job_matches',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('resume_id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('matched_points', sa.JSON(), nullable=False),
        sa.Column('gaps', sa.JSON(), nullable=False),
        sa.Column('reasoning', sa.Text(), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['resume_id'], ['resume_profiles.id'], ),
        sa.ForeignKeyConstraint(['job_id'], ['job_postings.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('resume_id', 'job_id', name='uq_match_resume_job'),
    )

    op.create_table(
        'applications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('resume_id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('match_id', sa.Integer(), nullable=True),
        sa.Column('board', sa.String(length=32), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('greeting', sa.Text(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('max_attempts', sa.Integer(), nullable=False),
        sa.Column('hr_reply', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('raw', sa.JSON(), nullable=False),
        sa.Column('scheduled_at', sa.DateTime(), nullable=True),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('replied_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['resume_id'], ['resume_profiles.id'], ),
        sa.ForeignKeyConstraint(['job_id'], ['job_postings.id'], ),
        sa.ForeignKeyConstraint(['match_id'], ['job_matches.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('resume_id', 'job_id', name='uq_app_resume_job'),
    )


def downgrade() -> None:
    op.drop_table('applications')
    op.drop_table('job_matches')
    op.drop_table('job_postings')
    op.drop_table('resume_profiles')
