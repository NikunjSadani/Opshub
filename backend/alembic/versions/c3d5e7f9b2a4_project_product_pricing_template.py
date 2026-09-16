"""project_product pricing template (per-project PO-line defaults)

Revision ID: c3d5e7f9b2a4
Revises: b2c4e6f8a1d3
Create Date: 2026-09-16 10:00:00.000000

Additive: 14 nullable pricing columns on `project_product` mirroring the PO line item
(minus ordered_qty, which is per-order). They are per-project DEFAULTS a PO pre-fills from;
all nullable so a partial template is valid. `sell_price_paise` / `freight_paise` are the
ADMIN-ONLY actuals (masked on read). No existing table/data is touched; downgrade drops the
columns. Money is BigInteger paise; tax_rate is Numeric(5,2) to match the PO line.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c3d5e7f9b2a4'
down_revision: str | None = 'b2c4e6f8a1d3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY_COLS: tuple[str, ...] = (
    'cost_price_paise', 'original_cost_price_paise', 'client_sell_price_paise',
    'vendor_sell_price_paise', 'sell_price_paise', 'client_freight_paise',
    'vendor_freight_paise', 'freight_paise', 'packaging_paise', 'handling_paise',
    'other_paise',
)


def upgrade() -> None:
    op.add_column('project_product', sa.Column('description', sa.String(length=500), nullable=True))
    op.add_column('project_product', sa.Column('uom', sa.String(length=20), nullable=True))
    for col in _MONEY_COLS:
        op.add_column('project_product', sa.Column(col, sa.BigInteger(), nullable=True))
    op.add_column(
        'project_product', sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('project_product', 'tax_rate')
    for col in reversed(_MONEY_COLS):
        op.drop_column('project_product', col)
    op.drop_column('project_product', 'uom')
    op.drop_column('project_product', 'description')
