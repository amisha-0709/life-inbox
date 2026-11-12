"""Add google_sub for Google OAuth identity linking.

Revision ID: 0013_google_oauth_subject
Revises: 0012_topics_per_owner_unique
"""

from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0013_google_oauth_subject"
down_revision: Optional[str] = "0012_topics_per_owner_unique"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "google_sub" not in columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("google_sub", sa.Text(), nullable=True))
            batch.create_unique_constraint(
                "uq_users_google_sub",
                ["google_sub"],
            )


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade is disabled to avoid dropping Google identity links"
    )
