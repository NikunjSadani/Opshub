"""challan_qr_access_token_and_client_access_pin

Revision ID: c9f5a4238764
Revises: f28cf5cb8afa
Create Date: 2026-08-22 21:09:25.548760
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c9f5a4238764'
down_revision: str | None = 'f28cf5cb8afa'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both nullable → safe adds on existing rows, no server_default needed.
    with op.batch_alter_table("project_client") as batch:
        batch.add_column(sa.Column("access_pin", sa.String(length=32), nullable=True))
    with op.batch_alter_table("challan") as batch:
        batch.add_column(sa.Column("access_token", sa.String(length=64), nullable=True))
        batch.create_index(
            batch.f("ix_challan_access_token"), ["access_token"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("challan") as batch:
        batch.drop_index(batch.f("ix_challan_access_token"))
        batch.drop_column("access_token")
    with op.batch_alter_table("project_client") as batch:
        batch.drop_column("access_pin")
