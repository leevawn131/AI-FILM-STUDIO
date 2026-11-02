"""0009_pipeline_fk_restrict

Revision ID: 0009_pipeline_fk_restrict
Revises: 0008_pipeline_orchestration_tables
Create Date: 2026-11-02 08:30:00.000000

Safely converts foreign key constraints on:
- pipeline_stage_states.project_id (CASCADE -> RESTRICT)
- pipeline_checkpoints.project_id (CASCADE -> RESTRICT)

Prevents hard deletion of a project from silently deleting pipeline history.
Preserves existing stage states, checkpoints, and audit records with zero data loss.
Supports both SQLite (via table recreation) and PostgreSQL (via constraint alteration).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0009_pipeline_fk_restrict'
down_revision: Union[str, Sequence[str], None] = '0008_pipeline_orchestration_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        # Drop CASCADE foreign keys and recreate with RESTRICT
        op.drop_constraint('pipeline_stage_states_project_id_fkey', 'pipeline_stage_states', type_='foreignkey')
        op.create_foreign_key(
            'pipeline_stage_states_project_id_fkey',
            'pipeline_stage_states',
            'projects',
            ['project_id'],
            ['id'],
            ondelete='RESTRICT'
        )
        op.drop_constraint('pipeline_checkpoints_project_id_fkey', 'pipeline_checkpoints', type_='foreignkey')
        op.create_foreign_key(
            'pipeline_checkpoints_project_id_fkey',
            'pipeline_checkpoints',
            'projects',
            ['project_id'],
            ['id'],
            ondelete='RESTRICT'
        )
    elif dialect_name == 'sqlite':
        # 1. Recreate pipeline_stage_states with ON DELETE RESTRICT
        bind.execute(sa.text("""
            CREATE TABLE _new_pipeline_stage_states (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                stage_name VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'NOT_STARTED',
                active_job_id VARCHAR(36) REFERENCES studio_jobs(id) ON DELETE SET NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                content_hash VARCHAR(64),
                consumed_input_hash VARCHAR(64),
                error_message TEXT,
                stage_metadata TEXT,
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (project_id, stage_name)
            );
        """))
        bind.execute(sa.text("""
            INSERT INTO _new_pipeline_stage_states (
                id, project_id, stage_name, status, active_job_id, retry_count,
                content_hash, consumed_input_hash, error_message, stage_metadata,
                started_at, completed_at, created_at, updated_at
            )
            SELECT id, project_id, stage_name, status, active_job_id, retry_count,
                   content_hash, consumed_input_hash, error_message, stage_metadata,
                   started_at, completed_at, created_at, updated_at
            FROM pipeline_stage_states;
        """))
        bind.execute(sa.text("DROP TABLE pipeline_stage_states;"))
        bind.execute(sa.text("ALTER TABLE _new_pipeline_stage_states RENAME TO pipeline_stage_states;"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_stage_states_project_id ON pipeline_stage_states (project_id);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_stage_states_stage_name ON pipeline_stage_states (stage_name);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_stage_states_status ON pipeline_stage_states (status);"))

        # 2. Recreate pipeline_checkpoints with ON DELETE RESTRICT
        bind.execute(sa.text("""
            CREATE TABLE _new_pipeline_checkpoints (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                checkpoint_type VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'LOCKED',
                artifact_type VARCHAR(64) NOT NULL,
                artifact_version INTEGER NOT NULL DEFAULT 1,
                artifact_content_hash VARCHAR(64),
                reviewer_name VARCHAR(128) NOT NULL DEFAULT 'Director / User',
                feedback_notes TEXT,
                decided_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (project_id, checkpoint_type)
            );
        """))
        bind.execute(sa.text("""
            INSERT INTO _new_pipeline_checkpoints (
                id, project_id, checkpoint_type, status, artifact_type,
                artifact_version, artifact_content_hash, reviewer_name,
                feedback_notes, decided_at, created_at, updated_at
            )
            SELECT id, project_id, checkpoint_type, status, artifact_type,
                   artifact_version, artifact_content_hash, reviewer_name,
                   feedback_notes, decided_at, created_at, updated_at
            FROM pipeline_checkpoints;
        """))
        bind.execute(sa.text("DROP TABLE pipeline_checkpoints;"))
        bind.execute(sa.text("ALTER TABLE _new_pipeline_checkpoints RENAME TO pipeline_checkpoints;"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_checkpoints_project_id ON pipeline_checkpoints (project_id);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_checkpoints_checkpoint_type ON pipeline_checkpoints (checkpoint_type);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_checkpoints_status ON pipeline_checkpoints (status);"))


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == 'postgresql':
        op.drop_constraint('pipeline_stage_states_project_id_fkey', 'pipeline_stage_states', type_='foreignkey')
        op.create_foreign_key(
            'pipeline_stage_states_project_id_fkey',
            'pipeline_stage_states',
            'projects',
            ['project_id'],
            ['id'],
            ondelete='CASCADE'
        )
        op.drop_constraint('pipeline_checkpoints_project_id_fkey', 'pipeline_checkpoints', type_='foreignkey')
        op.create_foreign_key(
            'pipeline_checkpoints_project_id_fkey',
            'pipeline_checkpoints',
            'projects',
            ['project_id'],
            ['id'],
            ondelete='CASCADE'
        )
    elif dialect_name == 'sqlite':
        # Recreate pipeline_stage_states with ON DELETE CASCADE
        bind.execute(sa.text("""
            CREATE TABLE _old_pipeline_stage_states (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                stage_name VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'NOT_STARTED',
                active_job_id VARCHAR(36) REFERENCES studio_jobs(id) ON DELETE SET NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                content_hash VARCHAR(64),
                consumed_input_hash VARCHAR(64),
                error_message TEXT,
                stage_metadata TEXT,
                started_at DATETIME,
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (project_id, stage_name)
            );
        """))
        bind.execute(sa.text("""
            INSERT INTO _old_pipeline_stage_states
            SELECT id, project_id, stage_name, status, active_job_id, retry_count,
                   content_hash, consumed_input_hash, error_message, stage_metadata,
                   started_at, completed_at, created_at, updated_at
            FROM pipeline_stage_states;
        """))
        bind.execute(sa.text("DROP TABLE pipeline_stage_states;"))
        bind.execute(sa.text("ALTER TABLE _old_pipeline_stage_states RENAME TO pipeline_stage_states;"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_stage_states_project_id ON pipeline_stage_states (project_id);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_stage_states_stage_name ON pipeline_stage_states (stage_name);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_stage_states_status ON pipeline_stage_states (status);"))

        # Recreate pipeline_checkpoints with ON DELETE CASCADE
        bind.execute(sa.text("""
            CREATE TABLE _old_pipeline_checkpoints (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                checkpoint_type VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'LOCKED',
                artifact_type VARCHAR(64) NOT NULL,
                artifact_version INTEGER NOT NULL DEFAULT 1,
                artifact_content_hash VARCHAR(64),
                reviewer_name VARCHAR(128) NOT NULL DEFAULT 'Director / User',
                feedback_notes TEXT,
                decided_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (project_id, checkpoint_type)
            );
        """))
        bind.execute(sa.text("""
            INSERT INTO _old_pipeline_checkpoints
            SELECT id, project_id, checkpoint_type, status, artifact_type,
                   artifact_version, artifact_content_hash, reviewer_name,
                   feedback_notes, decided_at, created_at, updated_at
            FROM pipeline_checkpoints;
        """))
        bind.execute(sa.text("DROP TABLE pipeline_checkpoints;"))
        bind.execute(sa.text("ALTER TABLE _old_pipeline_checkpoints RENAME TO pipeline_checkpoints;"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_checkpoints_project_id ON pipeline_checkpoints (project_id);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_checkpoints_checkpoint_type ON pipeline_checkpoints (checkpoint_type);"))
        bind.execute(sa.text("CREATE INDEX ix_pipeline_checkpoints_status ON pipeline_checkpoints (status);"))
