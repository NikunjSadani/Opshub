"""project_product tags (M:N product<->project for the curated PO picker)

Revision ID: b2c4e6f8a1d3
Revises: a7f3c1b2d4e5
Create Date: 2026-09-14 10:00:00.000000

Additive: a new `project_product` join table tagging a Product to a Project (curates that
project's PO product picker). Both FKs cascade on delete so a hard-deleted project/product
drops its tag rows; a UNIQUE (project_id, product_id) makes tagging idempotent at the DB.

Postgres notes (SQLite round-trip can't exercise these): confirm the two FKs are created
with ON DELETE CASCADE and that the unique constraint uq_project_product rejects a duplicate
(project_id, product_id) tag. Downgrade drops the table wholesale.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c4e6f8a1d3'
down_revision: str | None = 'a7f3c1b2d4e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'project_product',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['product.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['project.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'product_id', name='uq_project_product'),
    )
    op.create_index('ix_project_product_product_id', 'project_product', ['product_id'])
    op.create_index('ix_project_product_project_id', 'project_product', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_project_product_project_id', table_name='project_product')
    op.drop_index('ix_project_product_product_id', table_name='project_product')
    op.drop_table('project_product')
