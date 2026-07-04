"""Pre-migration guard, run by entrypoint.sh BEFORE `alembic upgrade head`.

Fixes a chicken-and-egg problem: the alembic_version.version_num column was
created as VARCHAR(32), but revision ids grew longer (e.g.
'20260630_0040_animal_sex_and_birth' is 34 chars). Once a long id needs to be
written, `alembic upgrade head` dies with StringDataRightTruncationError and the
whole migration chain freezes. Widening it from inside a migration can't help,
because alembic can't even record that migration. So we widen it here first,
using the app's own async engine (asyncpg) so no extra driver is needed.

Idempotent and defensive: if the table doesn't exist yet (fresh DB) or is
already wide, this is a harmless no-op, and it never fails the boot.
"""
import asyncio
import sys


async def _widen() -> None:
    from sqlalchemy import text
    from app.database import engine  # app's async engine (asyncpg)

    async with engine.begin() as conn:
        exists = (await conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'alembic_version'"
        ))).first()
        if not exists:
            print("[prepare_migrations] alembic_version not present yet — skipping")
            return
        await conn.execute(text(
            "ALTER TABLE alembic_version "
            "ALTER COLUMN version_num TYPE VARCHAR(255)"
        ))
        print("[prepare_migrations] alembic_version.version_num ensured VARCHAR(255)")


def main() -> int:
    try:
        asyncio.run(_widen())
    except Exception as exc:  # never block boot
        print(f"[prepare_migrations] WARNING: could not widen alembic_version: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())