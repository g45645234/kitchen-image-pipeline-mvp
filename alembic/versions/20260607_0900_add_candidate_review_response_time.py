"""add candidate review response time

Revision ID: 20260607_0900
Revises: 20260607_0800
Create Date: 2026-06-07 09:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260607_0900"
down_revision: Union[str, None] = "20260607_0800"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("candidate_reviews", sa.Column("response_time_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("candidate_reviews", "response_time_ms")
