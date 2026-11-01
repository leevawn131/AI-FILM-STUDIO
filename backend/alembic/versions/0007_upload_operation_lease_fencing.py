"""0007_upload_operation_lease_fencing

Revision ID: 0007_upload_operation_lease_fencing
Revises: 0006_fencing_token_and_ledger_unique
Create Date: 2026-11-01 22:00:00.000000

Adds worker_id, lease_expires_at, and fencing_token to upload_operations
to enable atomic claim, lease fencing, and safe recovery across multiple instances.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0007_upload_operation_lease_fencing'
down_revision: Union[str, Sequence[str], None] = '0006_fencing_token_and_ledger_unique'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c['name'] for c in inspector.get_columns('upload_operations')}

    with op.batch_alter_table('upload_operations') as batch_op:
        if 'worker_id' not in cols:
            batch_op.add_column(sa.Column('worker_id', sa.String(64), nullable=True))
        if 'lease_expires_at' not in cols:
            batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(), nullable=True))
        if 'fencing_token' not in cols:
            batch_op.add_column(sa.Column('fencing_token', sa.Integer(), nullable=False, server_default='1'))

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c['name'] for c in inspector.get_columns('upload_operations')}

    with op.batch_alter_table('upload_operations') as batch_op:
        if 'fencing_token' in cols:
            batch_op.drop_column('fencing_token')
        if 'lease_expires_at' in cols:
            batch_op.drop_column('lease_expires_at')
        if 'worker_id' in cols:
            batch_op.drop_column('worker_id')
