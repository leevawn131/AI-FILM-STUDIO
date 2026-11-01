"""0001_initial_schema

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-11-01 14:15:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0001_initial_schema'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. projects
    op.create_table(
        'projects',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('logline', sa.Text(), nullable=True),
        sa.Column('genre', sa.String(length=64), nullable=True),
        sa.Column('aspect_ratio', sa.String(length=16), nullable=True),
        sa.Column('fps', sa.Integer(), nullable=True),
        sa.Column('target_duration_sec', sa.Integer(), nullable=True),
        sa.Column('visual_style', sa.Text(), nullable=True),
        sa.Column('budget_total', sa.Float(), nullable=True),
        sa.Column('budget_spent', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    # 2. characters
    op.create_table(
        'characters',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('role', sa.String(length=64), nullable=True),
        sa.Column('age', sa.String(length=32), nullable=True),
        sa.Column('facial_characteristics', sa.Text(), nullable=True),
        sa.Column('clothing', sa.Text(), nullable=True),
        sa.Column('voice_profile', sa.Text(), nullable=True),
        sa.Column('personality', sa.Text(), nullable=True),
        sa.Column('visual_prompt_tags', sa.Text(), nullable=True),
        sa.Column('reference_image_path', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 3. locations
    op.create_table(
        'locations',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('time_of_day', sa.String(length=64), nullable=True),
        sa.Column('weather', sa.String(length=64), nullable=True),
        sa.Column('lighting_setup', sa.Text(), nullable=True),
        sa.Column('visual_prompt_tags', sa.Text(), nullable=True),
        sa.Column('reference_image_path', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 4. scenes
    op.create_table(
        'scenes',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('scene_number', sa.Integer(), nullable=False),
        sa.Column('slugline', sa.String(length=256), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('script_content', sa.Text(), nullable=True),
        sa.Column('location_id', sa.String(length=36), sa.ForeignKey('locations.id', ondelete='SET NULL'), nullable=True),
        sa.Column('estimated_duration', sa.Integer(), nullable=True),
        sa.Column('budget_limit', sa.Float(), nullable=True),
        sa.Column('order_index', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 5. media_assets
    op.create_table(
        'media_assets',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('filename', sa.String(length=256), nullable=False),
        sa.Column('file_path', sa.String(length=512), nullable=False),
        sa.Column('media_type', sa.String(length=32), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('fps', sa.Float(), nullable=True),
        sa.Column('codec', sa.String(length=64), nullable=True),
        sa.Column('file_size_bytes', sa.Integer(), nullable=True),
        sa.Column('thumbnail_path', sa.String(length=512), nullable=True),
        sa.Column('auto_matched_shot_id', sa.String(length=36), nullable=True),
        sa.Column('is_validated', sa.Boolean(), nullable=True),
        sa.Column('validation_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 6. shots
    op.create_table(
        'shots',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('scene_id', sa.String(length=36), sa.ForeignKey('scenes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('shot_number', sa.Integer(), nullable=False),
        sa.Column('shot_code', sa.String(length=32), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('camera_angle', sa.String(length=64), nullable=True),
        sa.Column('camera_movement', sa.String(length=64), nullable=True),
        sa.Column('visual_description', sa.Text(), nullable=True),
        sa.Column('lighting', sa.String(length=256), nullable=True),
        sa.Column('character_references', sa.Text(), nullable=True),
        sa.Column('video_prompt', sa.Text(), nullable=True),
        sa.Column('negative_prompt', sa.Text(), nullable=True),
        sa.Column('seed', sa.Integer(), nullable=True),
        sa.Column('generation_status', sa.String(length=32), nullable=True),
        sa.Column('video_asset_id', sa.String(length=36), sa.ForeignKey('media_assets.id', ondelete='SET NULL'), nullable=True),
        sa.Column('thumbnail_path', sa.String(length=512), nullable=True),
        sa.Column('order_index', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 7. audio_tracks
    op.create_table(
        'audio_tracks',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('track_type', sa.String(length=32), nullable=True),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('file_path', sa.String(length=512), nullable=False),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('start_time_seconds', sa.Float(), nullable=True),
        sa.Column('volume_level', sa.Float(), nullable=True),
        sa.Column('ducking_enabled', sa.Boolean(), nullable=True),
        sa.Column('ducking_level', sa.Float(), nullable=True),
        sa.Column('scene_id', sa.String(length=36), nullable=True),
        sa.Column('shot_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 8. cost_records
    op.create_table(
        'cost_records',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('scene_id', sa.String(length=36), nullable=True),
        sa.Column('action_type', sa.String(length=64), nullable=False),
        sa.Column('credits_used', sa.Float(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('prompt_hash', sa.String(length=64), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 9. export_jobs
    op.create_table(
        'export_jobs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('project_id', sa.String(length=36), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('preset_name', sa.String(length=64), nullable=True),
        sa.Column('resolution', sa.String(length=32), nullable=True),
        sa.Column('aspect_ratio', sa.String(length=16), nullable=True),
        sa.Column('fps', sa.Integer(), nullable=True),
        sa.Column('include_subtitles', sa.Boolean(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('progress_pct', sa.Integer(), nullable=True),
        sa.Column('output_path', sa.String(length=512), nullable=True),
        sa.Column('file_size_bytes', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )

def downgrade() -> None:
    op.drop_table('export_jobs')
    op.drop_table('cost_records')
    op.drop_table('audio_tracks')
    op.drop_table('shots')
    op.drop_table('media_assets')
    op.drop_table('scenes')
    op.drop_table('locations')
    op.drop_table('characters')
    op.drop_table('projects')
