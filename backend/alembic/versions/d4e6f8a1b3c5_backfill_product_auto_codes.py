"""backfill product auto-codes (PRD-{id:06d}) for code-less products

Revision ID: d4e6f8a1b3c5
Revises: c3d5e7f9b2a4
Create Date: 2026-09-17 10:00:00.000000

DATA-only (no DDL — `product.code` already exists as a nullable, unique-when-present
String(40)). Every product now carries a stable unique code: this migration mints
`PRD-{id:06d}` for every row whose `code IS NULL`, matching the service's create-time
auto-minting so pre-existing rows and new rows share one namespace.

Portability: written with a per-row SELECT + parameterized UPDATE and the code formatted
in Python (no Postgres-only `lpad`), so it round-trips on SQLite (tests) and runs on
Postgres (prod) identically.

Collision safety: `PRD-######` is a namespace the service reserves from user input, so a
pre-existing user code in that exact shape is near-impossible — but if one exists and it
equals some row's target `PRD-{id:06d}`, minting it here would trip the unique index. So
before assigning, we skip any target already held by another row and leave that product's
code NULL rather than failing the whole migration.

Downgrade clears every code in the auto shape (`PRD-` + 6-or-more digits). This is the
reserved namespace the service forbids from user input, so in practice only auto-minted
codes match — but note the downgrade keys off the shape, not on how the code was created,
so any pre-existing code that happens to be in this exact shape would also be nulled.
"""
import re
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd4e6f8a1b3c5'
down_revision: str | None = 'c3d5e7f9b2a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTO_CODE_RE = re.compile(r"^PRD-\d{6,}$", re.IGNORECASE)


def upgrade() -> None:
    bind = op.get_bind()

    # Every code already taken, so a minted PRD-###### can't collide with a
    # pre-existing (user-entered) code in the auto shape.
    taken: set[str] = {
        row[0]
        for row in bind.execute(sa.text("SELECT code FROM product WHERE code IS NOT NULL"))
    }

    ids = [
        row[0]
        for row in bind.execute(sa.text("SELECT id FROM product WHERE code IS NULL"))
    ]
    for pid in ids:
        target = f"PRD-{pid:06d}"
        if target in taken:
            # A different row already holds this exact code — skip to keep the unique
            # index intact; leave this row NULL rather than failing the migration.
            continue
        bind.execute(
            sa.text("UPDATE product SET code = :code WHERE id = :id"),
            {"code": target, "id": pid},
        )
        taken.add(target)


def downgrade() -> None:
    bind = op.get_bind()
    # Clear codes in the reserved auto shape (PRD- + 6-or-more digits). This shape is
    # forbidden from user input, so in practice only auto-minted codes match.
    rows = bind.execute(sa.text("SELECT id, code FROM product WHERE code IS NOT NULL"))
    auto_ids = [row[0] for row in rows if _AUTO_CODE_RE.match(row[1] or "")]
    for pid in auto_ids:
        bind.execute(
            sa.text("UPDATE product SET code = NULL WHERE id = :id"),
            {"id": pid},
        )
