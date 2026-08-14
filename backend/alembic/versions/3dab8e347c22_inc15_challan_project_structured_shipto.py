"""inc15 challan project + structured shipto

Revision ID: 3dab8e347c22
Revises: d61ab46c13de
Create Date: 2026-08-14 15:07:16.696024

Increment 15: the challan references a Project (printed) and captures a STRUCTURED
ship-to address (line1/line2/city/pincode). `consignee_brand` is retired — made
NULLABLE so pre-inc-15 issued rows keep their snapshot while new rows stop writing
it. New string columns carry a server_default of '' so the ALTER applies cleanly
over the existing issued-challan rows in prod (no NOT NULL violation).

Wrapped in a single `batch_alter_table` so SQLite (local/CI) rebuilds the table
for the nullability change; on Postgres (prod) alembic emits direct ALTERs.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '3dab8e347c22'
down_revision: str | None = 'd61ab46c13de'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('challan') as batch:
        batch.add_column(
            sa.Column('project_code', sa.String(length=24), nullable=False, server_default='')
        )
        batch.add_column(
            sa.Column('ship_to_address_line1', sa.String(length=300),
                      nullable=False, server_default='')
        )
        batch.add_column(
            sa.Column('ship_to_address_line2', sa.String(length=300),
                      nullable=False, server_default='')
        )
        batch.add_column(
            sa.Column('ship_to_city', sa.String(length=120), nullable=False, server_default='')
        )
        batch.add_column(
            sa.Column('ship_to_pincode', sa.String(length=10), nullable=False, server_default='')
        )
        batch.alter_column(
            'consignee_brand', existing_type=sa.VARCHAR(length=120), nullable=True
        )
        batch.create_index(
            op.f('ix_challan_project_code'), ['project_code'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('challan') as batch:
        batch.drop_index(op.f('ix_challan_project_code'))
        batch.alter_column(
            'consignee_brand', existing_type=sa.VARCHAR(length=120), nullable=False
        )
        batch.drop_column('ship_to_pincode')
        batch.drop_column('ship_to_city')
        batch.drop_column('ship_to_address_line2')
        batch.drop_column('ship_to_address_line1')
        batch.drop_column('project_code')
