"""0006_fencing_token_and_ledger_unique

Revision ID: 0006_fencing_token_and_ledger_unique
Revises: 0005_lease_fencing_and_upload_recovery
Create Date: 2026-11-01 21:00:00.000000

Safe migration of:
- studio_jobs.fencing_token (monotonically increasing integer token)
- cost_records.operation_identity unique constraint with audited historical deduplication.

Deduplication Strategy:
- Empty strings are normalized to NULL.
- For each duplicated operation_identity:
  - Records are sorted chronologically (created_at ASC, id ASC).
  - The earliest record is designated as CANONICAL (the genuine initial financial authorization/charge)
    and retains the original operation_identity.
  - Subsequent records are designated as HISTORICAL DUPLICATES.
  - Rather than discarding identity data (e.g. setting to NULL), each historical duplicate is
    re-keyed to '{orig_identity}#dup-{rec_id[:8]}' and its details column is annotated with
    a full MIGRATION_AUDIT trail linking back to the canonical record ID.
  - This satisfies the unique constraint while preserving complete financial auditability.
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

    # b. Inspect duplicates and perform auditable deduplication preserving historical provenance
    dup_rows = bind.execute(sa.text("""
        SELECT operation_identity, count(*) as cnt
        FROM cost_records
        WHERE operation_identity IS NOT NULL AND operation_identity != ''
        GROUP BY operation_identity
        HAVING count(*) > 1
    """)).fetchall()

    for dup in dup_rows:
        orig_identity = dup[0]
        # Fetch all duplicate records ordered chronologically
        records = bind.execute(sa.text("""
            SELECT id, details, created_at
            FROM cost_records
            WHERE operation_identity = :ident
            ORDER BY created_at ASC, id ASC
        """), {"ident": orig_identity}).fetchall()

        if not records:
            continue

        canonical_id = records[0][0]
        # records[0] is the canonical record; keep its operation_identity intact
        for idx, rec in enumerate(records[1:], start=1):
            rec_id = rec[0]
            rec_details = rec[1] or ""
            suffix = f"#dup-{idx}-{rec_id[:16]}"
            max_base_len = 128 - len(suffix)
            new_identity = f"{orig_identity[:max_base_len]}{suffix}"

            audit_note = (
                f"\n[MIGRATION_AUDIT: Historical duplicate #{idx} of operation_identity '{orig_identity}'. "
                f"Canonical record ID: '{canonical_id}'. Re-keyed to '{new_identity}' to preserve full "
                f"provenance while establishing uniqueness constraint.]"
            )
            new_details = f"{rec_details}{audit_note}"

            bind.execute(sa.text("""
                UPDATE cost_records
                SET operation_identity = :new_identity,
                    details = :new_details
                WHERE id = :rec_id
            """), {
                "new_identity": new_identity,
                "new_details": new_details,
                "rec_id": rec_id
            })

    # c. Add unique constraint on operation_identity
    with op.batch_alter_table('cost_records') as batch_op:
        batch_op.create_unique_constraint('uq_cost_records_operation_identity', ['operation_identity'])

def downgrade() -> None:
    try:
        with op.batch_alter_table('cost_records') as batch_op:
            batch_op.drop_constraint('uq_cost_records_operation_identity', type_='unique')
    except Exception:
        pass

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    job_cols = {c['name'] for c in inspector.get_columns('studio_jobs')}
    if 'fencing_token' in job_cols:
        with op.batch_alter_table('studio_jobs') as batch_op:
            batch_op.drop_column('fencing_token')
