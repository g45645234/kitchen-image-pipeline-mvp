"""add review and final asset constraints

Revision ID: 20260606_2015
Revises: 20260606_1938
Create Date: 2026-06-06 20:15:00

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260606_2015"
down_revision: Union[str, None] = "20260606_1938"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_candidate_reviews_score_range",
        "candidate_reviews",
        "score >= 0 AND score <= 1",
    )
    op.create_check_constraint(
        "ck_candidate_reviews_verdict",
        "candidate_reviews",
        "verdict IN ('pass', 'maybe', 'fail')",
    )
    op.create_index(
        op.f("ix_final_assets_candidate_id"),
        "final_assets",
        ["candidate_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_final_asset_mistake_side",
        "final_assets",
        ["mistake_id", "side"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_final_asset_mistake_side", "final_assets", type_="unique")
    op.drop_index(op.f("ix_final_assets_candidate_id"), table_name="final_assets")
    op.drop_constraint("ck_candidate_reviews_verdict", "candidate_reviews", type_="check")
    op.drop_constraint("ck_candidate_reviews_score_range", "candidate_reviews", type_="check")
