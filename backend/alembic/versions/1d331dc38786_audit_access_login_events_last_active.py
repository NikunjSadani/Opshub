"""audit & access: login_event table + user last_login_at/last_seen_at (inc 38)

Revision ID: 1d331dc38786
Revises: 4cfeba7ac83a
Create Date: 2026-08-31 11:08:56.950897

Additive-only. Backs the admin-only "Audit & Access" report:
  * `login_event` — one row per explicit sign-in (POST /auth/login-event), indexed on
    user_id + occurred_at, FK to user.id (plain FK, no cascade).
  * `user.last_login_at` / `user.last_seen_at` — both nullable timestamptz; existing rows
    keep NULL until the user next logs in / is seen. Touches no other table or column.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '1d331dc38786'
down_revision: str | None = '4cfeba7ac83a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'login_event',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=400), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_login_event_occurred_at'), 'login_event', ['occurred_at'], unique=False)
    op.create_index(
        op.f('ix_login_event_user_id'), 'login_event', ['user_id'], unique=False)
    # Batch so SQLite (copy-and-move) can add — and downgrade drop — the columns on any
    # SQLite version; on Postgres batch emits plain ALTERs. Both nullable, so every
    # existing user row is unaffected (NULL until the user next logs in / is seen).
    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('user') as batch:
        batch.drop_column('last_seen_at')
        batch.drop_column('last_login_at')
    op.drop_index(op.f('ix_login_event_user_id'), table_name='login_event')
    op.drop_index(op.f('ix_login_event_occurred_at'), table_name='login_event')
    op.drop_table('login_event')
