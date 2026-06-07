"""add candidate reviews

Revision ID: 20260606_1938
Revises: 66ba688ba236
Create Date: 2026-06-06 19:38:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260606_1938"
down_revision: Union[str, None] = "66ba688ba236"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("mistake_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=20), nullable=False),
        sa.Column("reviewer_name", sa.String(length=100), nullable=False),
        sa.Column("reviewer_version", sa.String(length=100), nullable=True),
        sa.Column("score", sa.Numeric(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["image_candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mistake_id"], ["mistakes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "reviewer_name", name="uq_candidate_review_reviewer"),
    )
    op.create_index(op.f("ix_candidate_reviews_candidate_id"), "candidate_reviews", ["candidate_id"], unique=False)
    op.create_index(op.f("ix_candidate_reviews_mistake_id"), "candidate_reviews", ["mistake_id"], unique=False)
    op.create_index(op.f("ix_candidate_reviews_reviewer_name"), "candidate_reviews", ["reviewer_name"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_candidate_reviews_reviewer_name"), table_name="candidate_reviews")
    op.drop_index(op.f("ix_candidate_reviews_mistake_id"), table_name="candidate_reviews")
    op.drop_index(op.f("ix_candidate_reviews_candidate_id"), table_name="candidate_reviews")
    op.drop_table("candidate_reviews")
