"""add mistake side feedback

Revision ID: 20260610_1200
Revises: 20260607_0910
Create Date: 2026-06-10 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260610_1200"
down_revision: Union[str, None] = "20260607_0910"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mistake_side_feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mistake_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=20), nullable=False),
        sa.Column("feedback_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=100), nullable=False, server_default="admin-ui"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["mistake_id"], ["mistakes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mistake_id", "side", name="uq_mistake_side_feedback"),
    )
    op.create_index(op.f("ix_mistake_side_feedback_mistake_id"), "mistake_side_feedback", ["mistake_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_mistake_side_feedback_mistake_id"), table_name="mistake_side_feedback")
    op.drop_table("mistake_side_feedback")
