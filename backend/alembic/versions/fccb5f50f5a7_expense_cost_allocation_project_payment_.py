"""expense cost allocation: project + payment method

Revision ID: fccb5f50f5a7
Revises: 187069733832
Create Date: 2026-08-20 13:25:25.665853
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'fccb5f50f5a7'
down_revision: str | None = '187069733832'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('expense_payment_method',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    # Case-insensitive unique backstop so "UPI"/"upi" can't both persist (a race the
    # app-layer check alone can't close) and split one cost centre on the dashboard.
    op.create_index('uq_expense_payment_method_lower_name', 'expense_payment_method',
                    [sa.text('lower(name)')], unique=True)
    # Batch mode so SQLite (copy-and-move) can add the nullable FK columns; on Postgres
    # batch emits plain ALTERs. Columns are nullable so existing invoices are unaffected.
    with op.batch_alter_table('expense_invoice') as batch:
        batch.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('payment_method_id', sa.Integer(), nullable=True))
        batch.create_index(batch.f('ix_expense_invoice_project_id'), ['project_id'], unique=False)
        batch.create_index(batch.f('ix_expense_invoice_payment_method_id'), ['payment_method_id'],
                           unique=False)
        batch.create_foreign_key('fk_expense_invoice_project', 'project',
                                 ['project_id'], ['id'])
        batch.create_foreign_key('fk_expense_invoice_payment_method', 'expense_payment_method',
                                 ['payment_method_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('expense_invoice') as batch:
        batch.drop_constraint('fk_expense_invoice_payment_method', type_='foreignkey')
        batch.drop_constraint('fk_expense_invoice_project', type_='foreignkey')
        batch.drop_index(batch.f('ix_expense_invoice_project_id'))
        batch.drop_index(batch.f('ix_expense_invoice_payment_method_id'))
        batch.drop_column('payment_method_id')
        batch.drop_column('project_id')
    op.drop_index('uq_expense_payment_method_lower_name', table_name='expense_payment_method')
    op.drop_table('expense_payment_method')
