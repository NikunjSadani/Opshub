"""PO line pricing tiers + agency fee + optional po_number

Revision ID: a7f3c1b2d4e5
Revises: 1d331dc38786
Create Date: 2026-09-07 10:00:00.000000

PO line-item pricing overhaul (money + RBAC):

  * po_line_item — five NEW nullable BigInteger columns: original_cost_price_paise,
    client_sell_price_paise, vendor_sell_price_paise, client_freight_paise,
    vendor_freight_paise. The existing sell_price_paise / freight_paise become the
    ADMIN-ONLY "actual" figures; the new client_* columns are the visible client-quoted
    values. BACKFILL client_sell_price_paise := sell_price_paise and
    client_freight_paise := freight_paise for every existing row so the newly
    client-facing money path (matcher, action-center, client-sell total) is non-breaking.
  * purchase_order — agency_fee_type String(8) NOT NULL server_default 'NONE' with a
    CHECK in ('NONE','PERCENT','FIXED'); agency_fee_percent Numeric(6,3) nullable;
    agency_fee_amount_paise BigInteger nullable. po_number becomes NULLABLE. The old
    uq_purchase_order_client_number UNIQUE is swapped for a PARTIAL unique index
    uq_po_client_number_present ON (client_id, po_number) WHERE po_number IS NOT NULL —
    so two NULL-number POs for one client both persist while a duplicate NON-null number
    still collides. The partial index works on SQLite and Postgres alike (sqlite_where /
    postgresql_where).

Postgres-specific notes for the orchestrator to verify on real Postgres (SQLite in the
test round-trip cannot exercise these):
  * The CHECK constraint ck_purchase_order_agency_fee_type is added inside a batch_alter_
    table (a table rebuild on SQLite); on Postgres it emits a plain ADD CONSTRAINT. Verify
    the constraint exists and rejects an out-of-domain agency_fee_type.
  * The partial unique index carries a WHERE clause — a genuine Postgres partial index.
    Confirm it is created (and dropped on downgrade) and that NULL po_number rows are
    excluded from its uniqueness.
  * DOWNGRADE restores po_number to NOT NULL and re-creates the plain UNIQUE constraint —
    it will FAIL on Postgres if any purchase_order row has a NULL po_number, or if the
    swap re-introduces a duplicate (client_id, po_number) that the partial index tolerated
    only because of NULLs. Downgrade is intended only for a clean rollback before any
    NULL-number PO is written.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a7f3c1b2d4e5'
down_revision: str | None = '1d331dc38786'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- po_line_item: five new nullable pricing columns (additive; existing rows NULL). ---
    op.add_column('po_line_item', sa.Column('original_cost_price_paise', sa.BigInteger(), nullable=True))
    op.add_column('po_line_item', sa.Column('client_sell_price_paise', sa.BigInteger(), nullable=True))
    op.add_column('po_line_item', sa.Column('vendor_sell_price_paise', sa.BigInteger(), nullable=True))
    op.add_column('po_line_item', sa.Column('client_freight_paise', sa.BigInteger(), nullable=True))
    op.add_column('po_line_item', sa.Column('vendor_freight_paise', sa.BigInteger(), nullable=True))
    # BACKFILL the visible client-facing figures from the (now admin-only) actuals so the
    # re-pointed money path is a no-op for existing data.
    op.execute("UPDATE po_line_item SET client_sell_price_paise = sell_price_paise")
    op.execute("UPDATE po_line_item SET client_freight_paise = freight_paise")

    # --- purchase_order: agency fee + optional po_number + constraint swap. ---
    # batch_alter_table so SQLite (copy-and-move rebuild) can add the CHECK, relax the
    # po_number nullability, and drop the old UNIQUE; on Postgres these emit plain ALTERs.
    with op.batch_alter_table('purchase_order') as batch:
        batch.add_column(sa.Column(
            'agency_fee_type', sa.String(length=8), nullable=False, server_default='NONE'))
        batch.add_column(sa.Column('agency_fee_percent', sa.Numeric(precision=6, scale=3), nullable=True))
        batch.add_column(sa.Column('agency_fee_amount_paise', sa.BigInteger(), nullable=True))
        batch.alter_column('po_number', existing_type=sa.String(length=64), nullable=True)
        batch.drop_constraint('uq_purchase_order_client_number', type_='unique')
        batch.create_check_constraint(
            'ck_purchase_order_agency_fee_type',
            "agency_fee_type in ('NONE', 'PERCENT', 'FIXED')")

    # Partial unique index: one number per client, but only over rows that HAVE a number.
    op.create_index(
        'uq_po_client_number_present', 'purchase_order', ['client_id', 'po_number'],
        unique=True,
        sqlite_where=sa.text('po_number IS NOT NULL'),
        postgresql_where=sa.text('po_number IS NOT NULL'))


def downgrade() -> None:
    op.drop_index(
        'uq_po_client_number_present', table_name='purchase_order',
        sqlite_where=sa.text('po_number IS NOT NULL'),
        postgresql_where=sa.text('po_number IS NOT NULL'))
    with op.batch_alter_table('purchase_order') as batch:
        batch.drop_constraint('ck_purchase_order_agency_fee_type', type_='check')
        batch.create_unique_constraint(
            'uq_purchase_order_client_number', ['client_id', 'po_number'])
        # Restores NOT NULL — fails if any row has a NULL po_number (see module note).
        batch.alter_column('po_number', existing_type=sa.String(length=64), nullable=False)
        batch.drop_column('agency_fee_amount_paise')
        batch.drop_column('agency_fee_percent')
        batch.drop_column('agency_fee_type')
    op.drop_column('po_line_item', 'vendor_freight_paise')
    op.drop_column('po_line_item', 'client_freight_paise')
    op.drop_column('po_line_item', 'vendor_sell_price_paise')
    op.drop_column('po_line_item', 'client_sell_price_paise')
    op.drop_column('po_line_item', 'original_cost_price_paise')
