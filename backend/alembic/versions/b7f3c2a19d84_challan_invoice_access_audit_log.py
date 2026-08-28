"""challan invoice access audit log

Revision ID: b7f3c2a19d84
Revises: c9f5a4238764
Create Date: 2026-08-27 10:00:00.000000
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b7f3c2a19d84'
down_revision: str | None = 'c9f5a4238764'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive: one brand-new table for the public challan-QR invoice-viewer audit log.
    # Touches no existing table.
    op.create_table(
        'challan_invoice_access',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('challan_id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('accessed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=False),
        sa.Column('viewer_hash', sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "outcome in ('VIEWED', 'WRONG_PIN', 'NOT_AVAILABLE', 'RATE_LIMITED', 'NO_PIN')",
            name='ck_challan_invoice_access_outcome',
        ),
        sa.ForeignKeyConstraint(['challan_id'], ['challan.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_challan_invoice_access_challan_id'),
        'challan_invoice_access', ['challan_id'], unique=False,
    )
    op.create_index(
        op.f('ix_challan_invoice_access_client_id'),
        'challan_invoice_access', ['client_id'], unique=False,
    )
    op.create_index(
        op.f('ix_challan_invoice_access_accessed_at'),
        'challan_invoice_access', ['accessed_at'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_challan_invoice_access_accessed_at'), table_name='challan_invoice_access',
    )
    op.drop_index(
        op.f('ix_challan_invoice_access_client_id'), table_name='challan_invoice_access',
    )
    op.drop_index(
        op.f('ix_challan_invoice_access_challan_id'), table_name='challan_invoice_access',
    )
    op.drop_table('challan_invoice_access')
