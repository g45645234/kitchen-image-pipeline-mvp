"""add mistake deleted_at

Revision ID: 20260607_0100
Revises: 20260606_2015
Create Date: 2026-06-07 01:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260607_0100"
down_revision: Union[str, None] = "20260606_2015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mistakes", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_mistakes_deleted_at"), "mistakes", ["deleted_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_mistakes_deleted_at"), table_name="mistakes")
    op.drop_column("mistakes", "deleted_at")
