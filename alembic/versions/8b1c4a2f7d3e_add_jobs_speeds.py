"""add jobs.speeds

Revision ID: 8b1c4a2f7d3e
Revises: 5de8f26ee5b7
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b1c4a2f7d3e'
down_revision: Union[str, Sequence[str], None] = '5de8f26ee5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable with no backfill: existing jobs keep NULL, which means "every
    # time control" — exactly how they behaved before the column existed.
    op.add_column('jobs', sa.Column('speeds', sa.String(length=60), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('jobs', 'speeds')
