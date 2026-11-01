"""0002_media_security_and_cost_ledger

Revision ID: 0002_media_security_and_cost_ledger
Revises: 0001_initial_schema
Create Date: 2026-11-01 14:16:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0002_media_security_and_cost_ledger'
down_revision: Union[str, Sequence[str], None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Add checksum to media_assets
    with op.batch_alter_table('media_assets') as batch_op:
        batch_op.add_column(sa.Column('checksum', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_media_assets_checksum', ['checksum'])

    # 2. Add provider-aware usage ledger fields to cost_records
    with op.batch_alter_table('cost_records') as batch_op:
        batch_op.add_column(sa.Column('shot_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('provider', sa.String(length=64), nullable=False, server_default='offline_engine'))
        batch_op.add_column(sa.Column('model', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('operation', sa.String(length=64), nullable=False, server_default='unknown'))
        batch_op.add_column(sa.Column('input_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('output_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('flow_credits', sa.Float(), nullable=True, server_default='0.0'))
        batch_op.add_column(sa.Column('actual_cost', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('currency', sa.String(length=8), nullable=False, server_default='USD'))
        batch_op.add_column(sa.Column('estimated_cost', sa.Float(), nullable=True, server_default='0.0'))
        batch_op.add_column(sa.Column('actual_cost_known', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.create_foreign_key('fk_cost_records_shot_id', 'shots', ['shot_id'], ['id'], ondelete='SET NULL')

def downgrade() -> None:
    with op.batch_alter_table('cost_records') as batch_op:
        batch_op.drop_constraint('fk_cost_records_shot_id', type_='foreignkey')
        batch_op.drop_column('actual_cost_known')
        batch_op.drop_column('estimated_cost')
        batch_op.drop_column('currency')
        batch_op.drop_column('actual_cost')
        batch_op.drop_column('flow_credits')
        batch_op.drop_column('output_tokens')
        batch_op.drop_column('input_tokens')
        batch_op.drop_column('operation')
        batch_op.drop_column('model')
        batch_op.drop_column('provider')
        batch_op.drop_column('shot_id')

    with op.batch_alter_table('media_assets') as batch_op:
        batch_op.drop_index('ix_media_assets_checksum')
        batch_op.drop_column('checksum')
