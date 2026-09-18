"""product gst_rate (GST % on the product master)

Revision ID: e5a7c1f9b3d2
Revises: d4e6f8a1b3c5
Create Date: 2026-09-18 10:00:00.000000

Additive: one nullable `gst_rate` Numeric(5,2) column on `product`. Existing rows
carry no rate (NULL) until one is entered — NO backfill. The rate is the source of
truth for the challan HSN master (`md_hsn`): saving a product with both `hsn` and
`gst_rate` upserts that HSN's rate (application logic, not this migration).

Downgrade drops the column. Numeric(5,2) matches the PO line item's `tax_rate`.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e5a7c1f9b3d2'
down_revision: str | None = 'd4e6f8a1b3c5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'product', sa.Column('gst_rate', sa.Numeric(precision=5, scale=2), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('product', 'gst_rate')
