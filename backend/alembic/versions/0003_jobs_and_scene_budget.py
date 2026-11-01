"""0003_jobs_and_scene_budget

Revision ID: 0003_jobs_and_scene_budget
Revises: 0002_media_security_and_cost_ledger
Create Date: 2026-11-01 14:35:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0003_jobs_and_scene_budget'
down_revision: Union[str, Sequence[str], None] = '0002_media_security_and_cost_ledger'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Create studio_jobs table for idempotent job lifecycle
    op.create_table(
        'studio_jobs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('job_type', sa.String(length=64), nullable=False),
        sa.Column('idempotency_key', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('payload', sa.Text(), nullable=True),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('internal_error', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_studio_jobs_job_type', 'studio_jobs', ['job_type'])
    op.create_index('ix_studio_jobs_idempotency_key', 'studio_jobs', ['idempotency_key'])
    op.create_index('ix_studio_jobs_status', 'studio_jobs', ['status'])

    # 2. Add budget_spent to scenes for atomic conditional budget updates
    with op.batch_alter_table('scenes') as batch_op:
        batch_op.add_column(sa.Column('budget_spent', sa.Float(), nullable=False, server_default='0.0'))

    # 3. Add flow_credits_spent and generation_attempts to shots for Flow tracking
    with op.batch_alter_table('shots') as batch_op:
        batch_op.add_column(sa.Column('flow_credits_spent', sa.Float(), nullable=False, server_default='0.0'))
        batch_op.add_column(sa.Column('generation_attempts', sa.Integer(), nullable=False, server_default='0'))

def downgrade() -> None:
    with op.batch_alter_table('shots') as batch_op:
        batch_op.drop_column('generation_attempts')
        batch_op.drop_column('flow_credits_spent')

    with op.batch_alter_table('scenes') as batch_op:
        batch_op.drop_column('budget_spent')

    op.drop_index('ix_studio_jobs_status', table_name='studio_jobs')
    op.drop_index('ix_studio_jobs_idempotency_key', table_name='studio_jobs')
    op.drop_index('ix_studio_jobs_job_type', table_name='studio_jobs')
    op.drop_table('studio_jobs')
