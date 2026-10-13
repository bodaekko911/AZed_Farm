"""Give every Animals user the new per-tab permissions.

Revision ID: 20261018_0057_animal_tabs
Revises: 20261017_0056_invoice_quote
Create Date: 2026-10-18

The Animals page's tabs (Groups, Feeding Log, Mortality Log, Receive) each got
their own permission. Without this, every user who could open Animals before
would open it to no tabs at all. Anyone whose stored permissions grant
``page_animals`` gets the four tabs, unless a tab is already explicitly revoked.
Users who have Animals through their role need nothing: the role now includes
the tabs. Analyze keeps the permission it always had.
"""
from alembic import context, op
import sqlalchemy as sa

revision = "20261018_0057_animal_tabs"
down_revision = "20261017_0056_invoice_quote"
branch_labels = None
depends_on = None

ANIMAL_TABS = (
    "tab_animals_groups",
    "tab_animals_feedings",
    "tab_animals_deaths",
    "tab_animals_receive",
)


def _with_tabs(raw: str | None) -> str | None:
    if not raw:
        return raw
    tokens = [t.strip() for t in raw.split(",") if t and t.strip()]
    if "page_animals" not in tokens:
        return raw
    present = set(tokens)
    added = [t for t in ANIMAL_TABS if t not in present and f"-{t}" not in present]
    if not added:
        return raw
    return ",".join(sorted(present | set(added)))


def upgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table("users"):
        return
    if "permissions" not in {c["name"] for c in inspector.get_columns("users")}:
        return

    rows = connection.execute(sa.text("SELECT id, permissions FROM users")).mappings().all()
    for row in rows:
        updated = _with_tabs(row["permissions"])
        if updated != row["permissions"]:
            connection.execute(
                sa.text("UPDATE users SET permissions = :permissions WHERE id = :id"),
                {"id": row["id"], "permissions": updated},
            )


def downgrade() -> None:
    # Unknown permissions are ignored when read, so the extra tokens are harmless.
    pass
