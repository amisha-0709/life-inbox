"""Allow user corrections before the first successful analysis.

Revision ID: 0016_pending_item_corrections
Revises: 0015_incremental_analysis

Existing insights and run links are preserved. A NULL run link means pending
organization, never a fabricated successful model result.
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_pending_item_corrections"
down_revision = "0015_incremental_analysis"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("item_insights") as batch:
        batch.alter_column("analysis_run_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    raise RuntimeError("Downgrade is disabled to preserve pre-analysis user corrections")
