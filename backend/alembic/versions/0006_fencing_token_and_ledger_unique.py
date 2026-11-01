"""0006_fencing_token_and_ledger_unique

Revision ID: 0006_fencing_token_and_ledger_unique
Revises: 0005_lease_fencing_and_upload_recovery
Create Date: 2026-11-01 21:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0006_fencing_token_and_ledger_unique'
down_revision: Union[str, Sequence[str], None] = '0005_lease_fencing_and_upload_recovery'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Update studio_jobs table: add fencing_token column and populate from lease_token
    job_cols = {c['name'] for c in inspector.get_columns('studio_jobs')}
    if 'fencing_token' not in job_cols:
        with op.batch_alter_table('studio_jobs') as batch_op:
            batch_op.add_column(sa.Column('fencing_token', sa.Integer(), nullable=False, server_default='1'))

    op.execute(sa.text("UPDATE studio_jobs SET fencing_token = lease_token WHERE lease_token IS NOT NULL"))

    # 2. Update cost_records table:
    # a. Convert empty strings to NULL
    op.execute(sa.text("UPDATE cost_records SET operation_identity = NULL WHERE operation_identity = ''"))

    # b. Deduplicate any existing duplicate operation_identity rows by nullifying the duplicates
    op.execute(sa.text("""
        UPDATE cost_records 
        SET operation_identity = NULL 
        WHERE id NOT IN (
            SELECT min(id) FROM cost_records WHERE operation_identity IS NOT NULL GROUP BY operation_identity
        ) AND operation_identity IS NOT NULL
    """))

    # c. Add unique constraint on operation_identity
    with op.batch_alter_table('cost_records') as batch_op:
        batch_op.create_unique_constraint('uq_cost_records_operation_identity', ['operation_identity'])

def downgrade() -> None:
    with op.batch_alter_table('cost_records') as batch_op:
        batch_op.drop_constraint('uq_cost_records_operation_identity', type_='unique')

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    job_cols = {c['name'] for c in inspector.get_columns('studio_jobs')}
    if 'fencing_token' in job_cols:
        with op.batch_alter_table('studio_jobs') as batch_op:
            batch_op.drop_column('fencing_token')
