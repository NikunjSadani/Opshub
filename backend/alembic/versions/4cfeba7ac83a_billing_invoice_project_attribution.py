"""billing invoice optional project attribution (PO-less P&L)

Revision ID: 4cfeba7ac83a
Revises: b7f3c2a19d84
Create Date: 2026-08-28 18:20:00.000000

Additive-only: one nullable, indexed FK column ``project_id`` on ``billing_invoice`` so a
PO-less invoice can be attributed to a project at upload/review. Plain FK (no cascade —
projects are soft-managed). Touches no existing table/column; existing rows keep
``project_id`` NULL (fully unattributed unless they reach a project through their PO).
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '4cfeba7ac83a'
down_revision: str | None = 'b7f3c2a19d84'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Batch mode so SQLite (copy-and-move) can add the nullable FK column; on Postgres batch
    # emits plain ALTERs. Column is nullable so every existing invoice is unaffected.
    with op.batch_alter_table('billing_invoice') as batch:
        batch.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        batch.create_index(batch.f('ix_billing_invoice_project_id'), ['project_id'],
                           unique=False)
        batch.create_foreign_key('fk_billing_invoice_project', 'project',
                                 ['project_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('billing_invoice') as batch:
        batch.drop_constraint('fk_billing_invoice_project', type_='foreignkey')
        batch.drop_index(batch.f('ix_billing_invoice_project_id'))
        batch.drop_column('project_id')
