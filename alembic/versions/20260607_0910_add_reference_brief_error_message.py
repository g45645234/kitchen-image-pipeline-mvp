"""add reference brief error message

Revision ID: 20260607_0910
Revises: 20260607_0900
Create Date: 2026-06-07 09:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260607_0910"
down_revision: Union[str, None] = "20260607_0900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("reference_briefs", sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("reference_briefs", "error_message")
