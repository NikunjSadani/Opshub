"""credit_note_review_envelope_needs_ocr_review_reasons_fields

Revision ID: f28cf5cb8afa
Revises: 86fd55b35152
Create Date: 2026-08-21 17:27:55.880092
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f28cf5cb8afa'
down_revision: str | None = '86fd55b35152'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Two review columns onto the EXISTING billing_credit_note (parity with billing_invoice).
    # Add with a server_default so existing rows are valid, then drop the default so the final
    # schema matches the model (which carries Python-side defaults only) — keeps alembic no-drift.
    with op.batch_alter_table("billing_credit_note") as batch:
        batch.add_column(
            sa.Column("needs_ocr", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("review_reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
    with op.batch_alter_table("billing_credit_note") as batch:
        batch.alter_column("needs_ocr", server_default=None)
        batch.alter_column("review_reasons", server_default=None)

    # Per-field extraction envelope for a CN (mirrors billing_invoice_field). Fresh table → no
    # server_defaults, matching the model's Python-side defaults.
    op.create_table(
        "billing_credit_note_field",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cn_id", sa.Integer(), nullable=False),
        sa.Column("field_path", sa.String(length=64), nullable=False),
        sa.Column("value_raw", sa.String(length=500), nullable=True),
        sa.Column("value_norm", sa.String(length=500), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_engine", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["cn_id"], ["billing_credit_note.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cn_id", "field_path", name="uq_billing_cn_field"),
    )
    op.create_index(
        op.f("ix_billing_credit_note_field_cn_id"),
        "billing_credit_note_field", ["cn_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_billing_credit_note_field_cn_id"), table_name="billing_credit_note_field"
    )
    op.drop_table("billing_credit_note_field")
    with op.batch_alter_table("billing_credit_note") as batch:
        batch.drop_column("review_reasons")
        batch.drop_column("needs_ocr")
