"""Persist deadline provenance without guessing historical timezone or dates.

Revision ID: 0014_deadline_provenance
Revises: 0013_google_oauth_subject
"""

from datetime import date

from alembic import op
import sqlalchemy as sa

revision = "0014_deadline_provenance"
down_revision = "0013_google_oauth_subject"
branch_labels = None
depends_on = None


def upgrade():
    # NULL deliberately means that the historical timezone is unknown.
    op.add_column("inbox_items", sa.Column("time_reference_at", sa.Text(), nullable=True))
    op.add_column("inbox_items", sa.Column("time_reference_timezone", sa.Text(), nullable=True))
    op.add_column("inbox_items", sa.Column("task_analysis_fingerprint", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("deadline_status", sa.Text(), nullable=False, server_default="legacy"))
    op.add_column("tasks", sa.Column("deadline_needs_review", sa.Integer(), nullable=False, server_default="1"))
    for name in ("deadline_note", "deadline_reference_at", "deadline_timezone", "deadline_source_fingerprint"):
        op.add_column("tasks", sa.Column(name, sa.Text(), nullable=True))

    tasks = sa.table(
        "tasks", sa.column("id"), sa.column("due_on"), sa.column("timing_confirmed"),
        sa.column("deadline_status"), sa.column("deadline_needs_review"), sa.column("deadline_note"),
    )
    bind = op.get_bind()
    for row in bind.execute(sa.select(tasks.c.id, tasks.c.due_on, tasks.c.timing_confirmed)).mappings().all():
        try:
            valid_date = date.fromisoformat(row["due_on"]).isoformat() == row["due_on"]
        except (TypeError, ValueError):
            valid_date = False
        confirmed = bool(row["timing_confirmed"])
        bind.execute(tasks.update().where(tasks.c.id == row["id"]).values(
            deadline_status="confirmed" if confirmed else "legacy",
            deadline_needs_review=0 if confirmed and valid_date else 1,
            deadline_note="Saved before capture timezone tracking. Saved date and wording preserved; review if needed.",
        ))


def downgrade():
    raise RuntimeError("Downgrade is disabled to preserve deadline provenance and user corrections")
