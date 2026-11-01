"""0005_lease_fencing_and_upload_recovery

Revision ID: 0005_lease_fencing_and_upload_recovery
Revises: 0004_job_idempotency_and_flow_attempt
Create Date: 2026-11-01 20:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0005_lease_fencing_and_upload_recovery'
down_revision: Union[str, Sequence[str], None] = '0004_job_idempotency_and_flow_attempt'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Update studio_jobs table: add lease_token (fencing token)
    with op.batch_alter_table('studio_jobs') as batch_op:
        batch_op.add_column(sa.Column('lease_token', sa.Integer(), nullable=False, server_default='1'))

    # 2. Create upload_operations table for persistent upload lifecycle & recovery
    op.create_table(
        'upload_operations',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=True),
        sa.Column('entity_type', sa.String(length=64), nullable=False),
        sa.Column('entity_id', sa.String(length=36), nullable=False),
        sa.Column('temp_path', sa.String(length=512), nullable=False),
        sa.Column('destination_path', sa.String(length=512), nullable=False),
        sa.Column('old_asset_path', sa.String(length=512), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='STAGED'),
        sa.Column('checksum', sa.String(length=64), nullable=True),
        sa.Column('file_size_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_message', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True)
    )
    op.create_index('ix_upload_operations_status', 'upload_operations', ['status'])
    op.create_index('ix_upload_operations_entity_id', 'upload_operations', ['entity_id'])
    op.create_index('ix_upload_operations_project_id', 'upload_operations', ['project_id'])

def downgrade() -> None:
    op.drop_index('ix_upload_operations_project_id', table_name='upload_operations')
    op.drop_index('ix_upload_operations_entity_id', table_name='upload_operations')
    op.drop_index('ix_upload_operations_status', table_name='upload_operations')
    op.drop_table('upload_operations')

    with op.batch_alter_table('studio_jobs') as batch_op:
        batch_op.drop_column('lease_token')
