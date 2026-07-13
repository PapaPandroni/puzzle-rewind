"""jobs period_start scopes engine jobs to searched window

Revision ID: ceedeee33845
Revises: 5de8f26ee5b7
Create Date: 2026-07-14 01:06:17.106719

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ceedeee33845'
down_revision: Union[str, Sequence[str], None] = '5de8f26ee5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no default: pre-existing jobs stay NULL = unscoped, i.e.
    # exactly the pre-fix behavior. Plain ADD COLUMN on SQLite and Postgres.
    op.add_column('jobs', sa.Column('period_start', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('jobs', 'period_start')
