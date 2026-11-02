"""0008_pipeline_orchestration_tables

Revision ID: 0008_pipeline_orchestration_tables
Revises: 0007_upload_operation_lease_fencing
Create Date: 2026-11-02 00:00:00.000000

Adds tables and columns for persistent pipeline orchestration:
- pipeline_stage_states (tracks 11 discrete filmmaking production stages)
- pipeline_checkpoints (tracks 4 human approval checkpoints bound to artifact content hashes)
- pipeline_checkpoint_audit (immutable, append-only journal with DB-level UPDATE/DELETE prevention triggers)
- projects.deleted_at (soft deletion to preserve audit trail)
- projects.current_pipeline_stage (derived projection of current stage)
- projects.pipeline_overall_status (derived projection of overall pipeline status)
"""
import os
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0008_pipeline_orchestration_tables'
down_revision: Union[str, Sequence[str], None] = '0007_upload_operation_lease_fencing'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. Add columns to projects table
    proj_cols = {c['name'] for c in inspector.get_columns('projects')}
    with op.batch_alter_table('projects') as batch_op:
        if 'deleted_at' not in proj_cols:
            batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))
        if 'current_pipeline_stage' not in proj_cols:
            batch_op.add_column(sa.Column('current_pipeline_stage', sa.String(64), nullable=False, server_default='story_development'))
        if 'pipeline_overall_status' not in proj_cols:
            batch_op.add_column(sa.Column('pipeline_overall_status', sa.String(32), nullable=False, server_default='in_progress'))

    # 2. Create pipeline_stage_states table
    if 'pipeline_stage_states' not in tables:
        op.create_table(
            'pipeline_stage_states',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('stage_name', sa.String(64), nullable=False),
            sa.Column('status', sa.String(32), nullable=False, server_default='NOT_STARTED'),
            sa.Column('active_job_id', sa.String(36), sa.ForeignKey('studio_jobs.id', ondelete='SET NULL'), nullable=True),
            sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('content_hash', sa.String(64), nullable=True),
            sa.Column('consumed_input_hash', sa.String(64), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('stage_metadata', sa.Text(), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('project_id', 'stage_name', name='uq_project_stage_name')
        )
        op.create_index('ix_pipeline_stage_states_project_id', 'pipeline_stage_states', ['project_id'])
        op.create_index('ix_pipeline_stage_states_stage_name', 'pipeline_stage_states', ['stage_name'])
        op.create_index('ix_pipeline_stage_states_status', 'pipeline_stage_states', ['status'])

    # 3. Create pipeline_checkpoints table
    if 'pipeline_checkpoints' not in tables:
        op.create_table(
            'pipeline_checkpoints',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('checkpoint_type', sa.String(64), nullable=False),
            sa.Column('status', sa.String(32), nullable=False, server_default='LOCKED'),
            sa.Column('artifact_type', sa.String(64), nullable=False),
            sa.Column('artifact_version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('artifact_content_hash', sa.String(64), nullable=True),
            sa.Column('reviewer_name', sa.String(128), nullable=False, server_default='Director / User'),
            sa.Column('feedback_notes', sa.Text(), nullable=True),
            sa.Column('decided_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('project_id', 'checkpoint_type', name='uq_project_checkpoint_type')
        )
        op.create_index('ix_pipeline_checkpoints_project_id', 'pipeline_checkpoints', ['project_id'])
        op.create_index('ix_pipeline_checkpoints_checkpoint_type', 'pipeline_checkpoints', ['checkpoint_type'])
        op.create_index('ix_pipeline_checkpoints_status', 'pipeline_checkpoints', ['status'])

    # 4. Create pipeline_checkpoint_audit table (append-only journal)
    if 'pipeline_checkpoint_audit' not in tables:
        op.create_table(
            'pipeline_checkpoint_audit',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('project_id', sa.String(36), nullable=False),
            sa.Column('project_name_snapshot', sa.String(128), nullable=False),
            sa.Column('checkpoint_id', sa.String(36), nullable=False),
            sa.Column('checkpoint_type', sa.String(64), nullable=False),
            sa.Column('decision', sa.String(32), nullable=False),
            sa.Column('artifact_content_hash', sa.String(64), nullable=False),
            sa.Column('artifact_version', sa.Integer(), nullable=False),
            sa.Column('reviewer_name', sa.String(128), nullable=False),
            sa.Column('feedback_notes', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now())
        )
        op.create_index('ix_pipeline_checkpoint_audit_project_id', 'pipeline_checkpoint_audit', ['project_id'])
        op.create_index('ix_pipeline_checkpoint_audit_checkpoint_id', 'pipeline_checkpoint_audit', ['checkpoint_id'])

    # 5. Database-level immutability triggers for pipeline_checkpoint_audit
    dialect_name = bind.dialect.name
    if dialect_name == 'sqlite':
        bind.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
            BEFORE UPDATE ON pipeline_checkpoint_audit
            BEGIN
                SELECT RAISE(ABORT, 'pipeline_checkpoint_audit is immutable and append-only');
            END;
        """))
        bind.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
            BEFORE DELETE ON pipeline_checkpoint_audit
            BEGIN
                SELECT RAISE(ABORT, 'pipeline_checkpoint_audit is immutable and append-only');
            END;
        """))
    elif dialect_name == 'postgresql':
        bind.execute(sa.text("""
            CREATE OR REPLACE FUNCTION trg_audit_prevent_modification()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'pipeline_checkpoint_audit is immutable and append-only';
            END;
            $$ LANGUAGE plpgsql;
        """))
        bind.execute(sa.text("""
            DROP TRIGGER IF EXISTS trg_audit_prevent_mod ON pipeline_checkpoint_audit;
            CREATE TRIGGER trg_audit_prevent_mod
            BEFORE UPDATE OR DELETE ON pipeline_checkpoint_audit
            FOR EACH ROW EXECUTE FUNCTION trg_audit_prevent_modification();
        """))

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # Protect against silent destruction of audit history with validated administrative archival
    if 'pipeline_checkpoint_audit' in tables:
        count = bind.execute(sa.text("SELECT count(*) FROM pipeline_checkpoint_audit")).scalar() or 0
        if count > 0:
            archive_path_str = os.environ.get("AUDIT_ARCHIVE_PATH")
            if not archive_path_str:
                raise RuntimeError(
                    f"Refusing destructive downgrade: 'pipeline_checkpoint_audit' contains {count} audit record(s). "
                    "Audit records are immutable compliance journals and cannot be silently destroyed. "
                    "To downgrade, you must first archive the audit records using: "
                    "AUDIT_ARCHIVE_PATH=/path/to/audit_archive.json alembic downgrade <rev>"
                )

            import json
            from pathlib import Path
            archive_path = Path(archive_path_str)
            if not archive_path.parent.exists():
                archive_path.parent.mkdir(parents=True, exist_ok=True)

            rows = bind.execute(sa.text("SELECT * FROM pipeline_checkpoint_audit")).mappings().all()
            serializable_rows = [dict(r) for r in rows]
            for r in serializable_rows:
                for k, v in r.items():
                    if hasattr(v, "isoformat"):
                        r[k] = v.isoformat()

            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(serializable_rows, f, indent=2)

            if not archive_path.exists() or archive_path.stat().st_size == 0:
                raise RuntimeError(f"Audit archive verification failed: archive file '{archive_path}' is missing or empty.")

            with open(archive_path, "r", encoding="utf-8") as f:
                archived_data = json.load(f)

            if len(archived_data) != count:
                raise RuntimeError(
                    f"Audit archive verification failed: expected {count} records in '{archive_path}', found {len(archived_data)}."
                )

    dialect_name = bind.dialect.name
    if dialect_name == 'sqlite':
        bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_no_update"))
        bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_no_delete"))
    elif dialect_name == 'postgresql':
        bind.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_prevent_mod ON pipeline_checkpoint_audit"))
        bind.execute(sa.text("DROP FUNCTION IF EXISTS trg_audit_prevent_modification()"))

    if 'pipeline_checkpoint_audit' in tables:
        op.drop_table('pipeline_checkpoint_audit')

    if 'pipeline_checkpoints' in tables:
        op.drop_table('pipeline_checkpoints')

    if 'pipeline_stage_states' in tables:
        op.drop_table('pipeline_stage_states')

    proj_cols = {c['name'] for c in inspector.get_columns('projects')}
    with op.batch_alter_table('projects') as batch_op:
        if 'pipeline_overall_status' in proj_cols:
            batch_op.drop_column('pipeline_overall_status')
        if 'current_pipeline_stage' in proj_cols:
            batch_op.drop_column('current_pipeline_stage')
        if 'deleted_at' in proj_cols:
            batch_op.drop_column('deleted_at')
