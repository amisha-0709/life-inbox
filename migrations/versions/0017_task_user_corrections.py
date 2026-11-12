"""Store explicit task title and priority corrections separately from inference.

Revision ID: 0017_task_user_corrections
Revises: 0016_pending_item_corrections
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_task_user_corrections"
down_revision = "0016_pending_item_corrections"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("user_text", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("user_priority", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("Downgrade is disabled to preserve task corrections")
