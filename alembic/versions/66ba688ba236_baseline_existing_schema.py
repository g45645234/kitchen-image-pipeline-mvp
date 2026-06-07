"""baseline existing schema

Revision ID: 66ba688ba236
Revises:
Create Date: 2026-06-06 19:37:00

This baseline represents the schema that already existed in the live
kitchen_assets database before versioned migration files were restored.
"""
from typing import Sequence, Union

revision: str = "66ba688ba236"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
