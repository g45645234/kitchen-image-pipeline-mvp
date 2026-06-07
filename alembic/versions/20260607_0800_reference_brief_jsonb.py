"""reference brief jsonb arrays

Revision ID: 20260607_0800
Revises: 20260607_0100
Create Date: 2026-06-07 08:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260607_0800"
down_revision: Union[str, None] = "20260607_0100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "reference_briefs",
        "important_visual_signs",
        existing_type=sa.Text(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default="[]",
        postgresql_using="""
            CASE
                WHEN important_visual_signs IS NULL OR btrim(important_visual_signs) = '' THEN '[]'::jsonb
                WHEN btrim(important_visual_signs) LIKE '[%%' THEN important_visual_signs::jsonb
                ELSE jsonb_build_array(important_visual_signs)
            END
        """,
    )
    op.alter_column(
        "reference_briefs",
        "do_not_copy",
        existing_type=sa.Text(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default="[]",
        postgresql_using="""
            CASE
                WHEN do_not_copy IS NULL OR btrim(do_not_copy) = '' THEN '[]'::jsonb
                WHEN btrim(do_not_copy) LIKE '[%%' THEN do_not_copy::jsonb
                ELSE jsonb_build_array(do_not_copy)
            END
        """,
    )


def downgrade() -> None:
    op.alter_column(
        "reference_briefs",
        "do_not_copy",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.Text(),
        nullable=True,
        server_default=None,
        postgresql_using="do_not_copy::text",
    )
    op.alter_column(
        "reference_briefs",
        "important_visual_signs",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.Text(),
        nullable=True,
        server_default=None,
        postgresql_using="important_visual_signs::text",
    )
