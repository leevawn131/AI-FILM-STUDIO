"""0011_google_cloud_ai_and_generation_jobs

Revision ID: 0011_google_cloud_ai_and_generation_jobs
Revises: 0010_media_versioning_and_export_modes
Create Date: 2026-11-05 00:00:00.000000

Adds:
- generation_jobs table for asynchronous cloud and local AI generation jobs
- projects.budget_warning_threshold
- shots.generation_job_id
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0011_google_cloud_ai_and_generation_jobs'
down_revision: Union[str, Sequence[str], None] = '0010_media_versioning_and_export_modes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    # 1. projects.budget_warning_threshold
    proj_cols = {c["name"] for c in insp.get_columns("projects")}
    with op.batch_alter_table("projects") as batch_op:
        if "budget_warning_threshold" not in proj_cols:
            batch_op.add_column(sa.Column("budget_warning_threshold", sa.Float(), nullable=False, server_default="0.8"))

    # 2. shots.generation_job_id
    shot_cols = {c["name"] for c in insp.get_columns("shots")}
    with op.batch_alter_table("shots") as batch_op:
        if "generation_job_id" not in shot_cols:
            batch_op.add_column(sa.Column("generation_job_id", sa.String(36), nullable=True))

    # 3. Create generation_jobs table
    if "generation_jobs" not in tables:
        op.create_table(
            "generation_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("shot_id", sa.String(36), sa.ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("provider", sa.String(64), nullable=False, server_default="gemini"),
            sa.Column("model", sa.String(64), nullable=False),
            sa.Column("generation_type", sa.String(64), nullable=False, index=True),
            sa.Column("input_parameters", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending", index=True),
            sa.Column("provider_operation_id", sa.String(256), nullable=True, index=True),
            sa.Column("supports_cancellation", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("result_metadata", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("result_asset_id", sa.String(36), sa.ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True),
            sa.Column("error_details", sa.Text(), nullable=False, server_default=""),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "generation_jobs" in tables:
        op.drop_table("generation_jobs")

    with op.batch_alter_table("shots") as batch_op:
        batch_op.drop_column("generation_job_id")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("budget_warning_threshold")
