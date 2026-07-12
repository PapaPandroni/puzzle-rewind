"""phase 3: jobs table and game engine columns

Revision ID: 5de8f26ee5b7
Revises: 240edd61b6f7
Create Date: 2026-07-13 00:29:17.802735

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5de8f26ee5b7'
down_revision: Union[str, Sequence[str], None] = '240edd61b6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('player_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('progress', sa.Integer(), nullable=False),
    sa.Column('total', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['player_id'], ['players.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_jobs_player_id'), 'jobs', ['player_id'], unique=False)
    op.create_index(op.f('ix_jobs_status'), 'jobs', ['status'], unique=False)
    # server_default backfills existing rows in-place on both SQLite (ADD
    # COLUMN DEFAULT) and Postgres (PG11+ fast default, no table rewrite).
    op.add_column('games', sa.Column('eval_source', sa.String(length=10), server_default='lichess', nullable=False))
    op.add_column('games', sa.Column('moves_san', sa.Text(), nullable=True))
    op.add_column('games', sa.Column('analysis_json', sa.Text(), nullable=True))
    op.add_column('games', sa.Column('analyzed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('games', 'analyzed_at')
    op.drop_column('games', 'analysis_json')
    op.drop_column('games', 'moves_san')
    op.drop_column('games', 'eval_source')
    op.drop_index(op.f('ix_jobs_status'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_player_id'), table_name='jobs')
    op.drop_table('jobs')
