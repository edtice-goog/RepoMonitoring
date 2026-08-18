"""rendered_event

Revision ID: c7d1e2f3a4b5
Revises: bc542dd40e26
Create Date: 2026-08-18

Materialized cache of rendered (triaged) commit events per project, so the dashboard
renders instantly at startup and a replay can reconcile in the background (mark pending
-> upsert fresh -> sweep stale) while reads keep serving the previous render.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c7d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = 'bc542dd40e26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'rendered_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_name', sa.String(length=200), nullable=False),
        sa.Column('commit_sha', sa.String(length=64), nullable=False),
        sa.Column('component', sa.String(length=200), nullable=True),
        sa.Column('committed_at', sa.String(length=40), nullable=True),
        sa.Column('label', sa.String(length=40), nullable=True),
        sa.Column('pending', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_name', 'commit_sha', name='uq_rendered_project_commit'),
    )
    op.create_index(op.f('ix_rendered_event_project_name'), 'rendered_event',
                    ['project_name'], unique=False)
    op.create_index(op.f('ix_rendered_event_pending'), 'rendered_event',
                    ['pending'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_rendered_event_pending'), table_name='rendered_event')
    op.drop_index(op.f('ix_rendered_event_project_name'), table_name='rendered_event')
    op.drop_table('rendered_event')
