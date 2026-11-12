"""Durable incremental analysis state, sourced details and review proposals.

Revision ID: 0015_incremental_analysis
Revises: 0014_deadline_provenance
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_incremental_analysis"
down_revision = "0014_deadline_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "item_analysis_state",
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("inbox_items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("input_fingerprint", sa.Text(), nullable=True),
        sa.Column("analysis_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("invalidation_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_fingerprint", sa.Text(), nullable=True),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("last_run_id", sa.Integer(), sa.ForeignKey("analysis_runs.id"), nullable=True),
    )
    op.create_table(
        "task_details",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_item_id", sa.Integer(), sa.ForeignKey("inbox_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("task_id", "source_item_id", "text", name="uq_task_detail_source_text"),
    )
    op.create_table(
        "analysis_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("inbox_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("candidate_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("dedup_key", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Downgrade is disabled to preserve processing and review provenance")
