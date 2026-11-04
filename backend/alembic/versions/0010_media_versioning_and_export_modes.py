"""0010_media_versioning_and_export_modes

Revision ID: 0010_media_versioning_and_export_modes
Revises: 0009_pipeline_fk_restrict
Create Date: 2026-11-04 20:00:00.000000

Adds:
- media_assets: shot_id, version, status
- export_jobs: render_mode, allow_placeholders
- audio_tracks: fade_in_seconds, fade_out_seconds
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0010_media_versioning_and_export_modes'
down_revision: Union[str, Sequence[str], None] = '0009_pipeline_fk_restrict'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1. media_assets
    ma_cols = {c["name"] for c in insp.get_columns("media_assets")}
    with op.batch_alter_table("media_assets") as batch_op:
        if "shot_id" not in ma_cols:
            batch_op.add_column(sa.Column("shot_id", sa.String(36), nullable=True))
        if "version" not in ma_cols:
            batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        if "status" not in ma_cols:
            batch_op.add_column(sa.Column("status", sa.String(32), nullable=False, server_default="ready"))

    if bind.dialect.name == "postgresql":
        fks = {fk["name"] for fk in insp.get_foreign_keys("media_assets")}
        if "fk_media_assets_shot_id" not in fks:
            op.create_foreign_key(
                "fk_media_assets_shot_id",
                "media_assets",
                "shots",
                ["shot_id"],
                ["id"],
                ondelete="SET NULL"
            )

    # 2. export_jobs
    ej_cols = {c["name"] for c in insp.get_columns("export_jobs")}
    with op.batch_alter_table("export_jobs") as batch_op:
        if "render_mode" not in ej_cols:
            batch_op.add_column(sa.Column("render_mode", sa.String(32), nullable=False, server_default="final"))
        if "allow_placeholders" not in ej_cols:
            batch_op.add_column(sa.Column("allow_placeholders", sa.Boolean(), nullable=False, server_default="0"))

    # 3. audio_tracks
    at_cols = {c["name"] for c in insp.get_columns("audio_tracks")}
    with op.batch_alter_table("audio_tracks") as batch_op:
        if "fade_in_seconds" not in at_cols:
            batch_op.add_column(sa.Column("fade_in_seconds", sa.Float(), nullable=False, server_default="0.0"))
        if "fade_out_seconds" not in at_cols:
            batch_op.add_column(sa.Column("fade_out_seconds", sa.Float(), nullable=False, server_default="0.0"))


def downgrade() -> None:
    with op.batch_alter_table("audio_tracks") as batch_op:
        batch_op.drop_column("fade_out_seconds")
        batch_op.drop_column("fade_in_seconds")

    with op.batch_alter_table("export_jobs") as batch_op:
        batch_op.drop_column("allow_placeholders")
        batch_op.drop_column("render_mode")

    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.drop_column("status")
        batch_op.drop_column("version")
        batch_op.drop_column("shot_id")
