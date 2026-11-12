"""Import meetings from a private Google Calendar iCal feed as tasks.

Revision ID: 0018_calendar_feed_import
Revises: 0017_task_user_corrections
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_calendar_feed_import"
down_revision = "0017_task_user_corrections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_feeds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("ics_url", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("last_synced_at", sa.Text(), nullable=True),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("owner_user_id", name="uq_calendar_feeds_owner"),
    )
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("external_source", sa.Text(), nullable=True))
        batch.add_column(sa.Column("external_key", sa.Text(), nullable=True))
        batch.add_column(sa.Column("external_start_at", sa.Text(), nullable=True))
    op.create_index(
        "ix_tasks_owner_external_key", "tasks", ["owner_user_id", "external_key"]
    )


def downgrade():
    op.drop_index("ix_tasks_owner_external_key", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("external_start_at")
        batch.drop_column("external_key")
        batch.drop_column("external_source")
    op.drop_table("calendar_feeds")
