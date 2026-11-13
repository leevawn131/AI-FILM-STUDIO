"""0012_generation_job_dispatch_state

Revision ID: 0012_generation_job_dispatch_state
Revises: 0011_google_cloud_ai_and_generation_jobs
Create Date: 2026-11-13 00:00:00.000000

Adds `generation_jobs.dispatch_state` for durable crash-safe remote dispatch
and provider billing accountability tracking, plus index and safe backfill.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "0012_generation_job_dispatch_state"
down_revision: Union[str, Sequence[str], None] = "0011_google_cloud_ai_and_generation_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "generation_jobs" not in tables:
        return

    cols = {c["name"] for c in insp.get_columns("generation_jobs")}
    if "dispatch_state" not in cols:
        with op.batch_alter_table("generation_jobs") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "dispatch_state",
                    sa.String(32),
                    nullable=False,
                    server_default="not_dispatched",
                )
            )
            batch_op.create_index("ix_generation_jobs_dispatch_state", ["dispatch_state"])

        # Backfill existing historical rows accurately
        op.execute(
            sa.text(
                "UPDATE generation_jobs SET dispatch_state = 'provider_completed' "
                "WHERE status = 'completed'"
            )
        )
        op.execute(
            sa.text(
                "UPDATE generation_jobs SET dispatch_state = 'provider_operation_known' "
                "WHERE status = 'processing' AND provider_operation_id IS NOT NULL "
                "AND trim(provider_operation_id) != ''"
            )
        )
        op.execute(
            sa.text(
                "UPDATE generation_jobs SET dispatch_state = 'dispatch_started' "
                "WHERE status = 'processing' AND (provider_operation_id IS NULL "
                "OR trim(provider_operation_id) = '')"
            )
        )
        op.execute(
            sa.text(
                "UPDATE generation_jobs SET dispatch_state = 'provider_outcome_unknown' "
                "WHERE status = 'provider_outcome_unknown'"
            )
        )
        op.execute(
            sa.text(
                "UPDATE generation_jobs SET dispatch_state = 'provider_failed' "
                "WHERE status IN ('failed', 'cancelled') AND provider_operation_id IS NOT NULL "
                "AND trim(provider_operation_id) != ''"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "generation_jobs" not in set(insp.get_table_names()):
        return

    cols = {c["name"] for c in insp.get_columns("generation_jobs")}
    if "dispatch_state" in cols:
        existing_indexes = {idx["name"] for idx in insp.get_indexes("generation_jobs")}
        with op.batch_alter_table("generation_jobs") as batch_op:
            if "ix_generation_jobs_dispatch_state" in existing_indexes:
                batch_op.drop_index("ix_generation_jobs_dispatch_state")
            batch_op.drop_column("dispatch_state")
