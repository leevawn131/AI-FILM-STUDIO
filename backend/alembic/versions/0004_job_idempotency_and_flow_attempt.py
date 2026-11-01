"""0004_job_idempotency_and_flow_attempt

Revision ID: 0004_job_idempotency_and_flow_attempt
Revises: 0003_jobs_and_scene_budget
Create Date: 2026-11-01 15:05:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0004_job_idempotency_and_flow_attempt'
down_revision: Union[str, Sequence[str], None] = '0003_jobs_and_scene_budget'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Update studio_jobs table: add worker_id, lease_expires_at, and unique constraint
    with op.batch_alter_table('studio_jobs') as batch_op:
        batch_op.add_column(sa.Column('worker_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(), nullable=True))
        batch_op.create_unique_constraint('uq_studio_jobs_proj_type_key', ['project_id', 'job_type', 'idempotency_key'])
    op.create_index('ix_studio_jobs_worker_id', 'studio_jobs', ['worker_id'])

    # 2. Update cost_records table: add generation_attempt_id, operation_identity, and unique constraint
    with op.batch_alter_table('cost_records') as batch_op:
        batch_op.add_column(sa.Column('generation_attempt_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('operation_identity', sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint('uq_cost_records_gen_attempt', ['generation_attempt_id'])
    op.create_index('ix_cost_records_operation_identity', 'cost_records', ['operation_identity'])

def downgrade() -> None:
    op.drop_index('ix_cost_records_operation_identity', table_name='cost_records')
    with op.batch_alter_table('cost_records') as batch_op:
        batch_op.drop_constraint('uq_cost_records_gen_attempt', type_='unique')
        batch_op.drop_column('operation_identity')
        batch_op.drop_column('generation_attempt_id')

    op.drop_index('ix_studio_jobs_worker_id', table_name='studio_jobs')
    with op.batch_alter_table('studio_jobs') as batch_op:
        batch_op.drop_constraint('uq_studio_jobs_proj_type_key', type_='unique')
        batch_op.drop_column('lease_expires_at')
        batch_op.drop_column('worker_id')
