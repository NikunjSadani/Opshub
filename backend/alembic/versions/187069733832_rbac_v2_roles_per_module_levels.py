"""rbac v2: roles + per-module levels

Revision ID: 187069733832
Revises: 88d04b809c75
Create Date: 2026-08-20 08:41:18.217261
"""
from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa


revision: str = '187069733832'
down_revision: str | None = '88d04b809c75'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # Capture the pre-existing ADMINs BEFORE dropping the old column, so we can map ONLY
    # them onto the Administrator role. Every OTHER pre-existing user is left role-less
    # (fail-closed = no access until an admin assigns a role) — NEVER silently promoted.
    admin_user_ids = [
        row[0] for row in conn.execute(sa.text('SELECT id FROM "user" WHERE role = :r'),
                                       {"r": "ADMIN"})
    ]

    op.drop_index('ix_user_module_access_user_id', table_name='user_module_access')
    op.drop_table('user_module_access')

    # Drop the retired enum column FIRST. On Postgres a table and a type share one
    # namespace, so the old `role` TYPE (from the baseline) must be dropped before we can
    # create the new `role` TABLE — otherwise CREATE TABLE role aborts with
    # "type role already exists". SQLite has no enum type, so the DROP TYPE is PG-only.
    with op.batch_alter_table('user') as batch:
        batch.drop_column('role')
    if is_pg:
        conn.execute(sa.text("DROP TYPE IF EXISTS role"))

    op.create_table('role',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('description', sa.String(length=400), nullable=False),
    sa.Column('is_system', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=128), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('role_module_permission',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('role_id', sa.Integer(), nullable=False),
    sa.Column('module_key', sa.String(length=64), nullable=False),
    sa.Column('level', sa.Enum('VIEW', 'OPERATE', 'MANAGE', name='level'), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['role.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role_id', 'module_key', name='uq_role_module')
    )
    op.create_index(op.f('ix_role_module_permission_role_id'), 'role_module_permission', ['role_id'], unique=False)
    op.create_table('role_platform_permission',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('role_id', sa.Integer(), nullable=False),
    sa.Column('permission_key', sa.Enum('IAM', 'SETTINGS', name='platformperm'), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['role.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role_id', 'permission_key', name='uq_role_platform')
    )
    op.create_index(op.f('ix_role_platform_permission_role_id'), 'role_platform_permission', ['role_id'], unique=False)

    # Guarantee the protected Administrator role exists in EVERY environment (a prod deploy
    # needs one). Presets are added later by `roles_builtin.ensure_builtin_roles` (seed /
    # prod bootstrap), idempotently.
    conn.execute(
        sa.text(
            "INSERT INTO role (name, description, is_system, created_at, created_by) "
            "VALUES ('Administrator', :descr, :is_sys, :now, 'system')"
        ),
        {"descr": "Full access to every module and all platform permissions.",
         "is_sys": True, "now": datetime.now(UTC)},
    )
    admin_id = conn.execute(
        sa.text("SELECT id FROM role WHERE is_system = :t"), {"t": True}
    ).scalar()

    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('role_id', sa.Integer(), nullable=True))
        batch.create_index(batch.f('ix_user_role_id'), ['role_id'], unique=False)
        batch.create_foreign_key('fk_user_role', 'role', ['role_id'], ['id'])

    # Map ONLY the former ADMINs onto Administrator; everyone else stays role-less
    # (fail-closed). Preserves least privilege on any populated DB.
    if admin_user_ids:
        conn.execute(
            sa.text('UPDATE "user" SET role_id = :aid WHERE id IN :ids').bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"aid": admin_id, "ids": admin_user_ids},
        )


def downgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('role', sa.VARCHAR(length=10), nullable=False,
                                   server_default='OPERATIONS'))
        batch.drop_constraint('fk_user_role', type_='foreignkey')
        batch.drop_index(batch.f('ix_user_role_id'))
        batch.drop_column('role_id')
    op.create_table('user_module_access',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('user_id', sa.INTEGER(), nullable=False),
    sa.Column('module_key', sa.VARCHAR(length=64), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'module_key', name='uq_user_module')
    )
    op.create_index('ix_user_module_access_user_id', 'user_module_access', ['user_id'], unique=False)
    op.drop_index(op.f('ix_role_platform_permission_role_id'), table_name='role_platform_permission')
    op.drop_table('role_platform_permission')
    op.drop_index(op.f('ix_role_module_permission_role_id'), table_name='role_module_permission')
    op.drop_table('role_module_permission')
    op.drop_table('role')
    # Drop the Postgres enum types created by the new tables, so a later re-upgrade
    # doesn't collide with an orphaned type. (SQLite has no such types.)
    if is_pg:
        conn.execute(sa.text("DROP TYPE IF EXISTS level"))
        conn.execute(sa.text("DROP TYPE IF EXISTS platformperm"))
    # ### end Alembic commands ###
