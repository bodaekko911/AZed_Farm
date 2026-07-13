from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.core.config import BASE_DIR, settings
from app.core.log import logger
from app.db.session import engine


_RUNTIME_SCHEMA_PATCHES: tuple[dict[str, str], ...] = (
    {
        "table": "customers",
        "column": "discount_pct",
        "definition": "NUMERIC(6, 2) DEFAULT 0",
        "backfill": "UPDATE customers SET discount_pct = 0 WHERE discount_pct IS NULL",
    },
    {
        "table": "invoices",
        "column": "import_batch_id",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "b2b_invoices",
        "column": "import_batch_id",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    # Data backfill (not a schema change): B2B invoices that net to zero — i.e. a
    # client with a 100% discount — used to be written with status='unpaid' before
    # auto-paid handling existed. There is nothing to collect on them, so mark them
    # paid. This targets the already-existing "status" column, so no ALTER runs;
    # only the idempotent UPDATE below executes on each boot (rows already 'paid'
    # are skipped by the WHERE clause).
    {
        "table": "b2b_invoices",
        "column": "status",
        "definition": "VARCHAR(20) DEFAULT 'unpaid'",
        "backfill": (
            "UPDATE b2b_invoices SET status = 'paid', amount_paid = total "
            "WHERE total <= 0 AND (status IS NULL OR status <> 'paid')"
        ),
    },
    {
        "table": "consignments",
        "column": "import_batch_id",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "products",
        "column": "created_by_import_batch",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "customers",
        "column": "created_by_import_batch",
        "definition": "VARCHAR(64)",
        "backfill": "SELECT 1",
    },
    {
        "table": "employees",
        "column": "attendance_auto_status",
        "definition": "VARCHAR(20) NOT NULL DEFAULT 'present'",
        "backfill": (
            "UPDATE employees SET attendance_auto_status = 'present' "
            "WHERE attendance_auto_status IS NULL "
            "OR attendance_auto_status NOT IN ('present', 'absent')"
        ),
    },
    # Retire the legacy "late"/"leave" attendance statuses (no payroll/vacation
    # calculation ever read them). A late day was worked → 'present'; a leave
    # day was time off → 'absent' (Day Off). Idempotent: re-running matches
    # nothing once converted. ('status' already exists, so no column is added.)
    {
        "table": "attendance",
        "column": "status",
        "definition": "VARCHAR(20)",
        "backfill": (
            "UPDATE attendance SET status = CASE "
            "WHEN status = 'late' THEN 'present' "
            "WHEN status = 'leave' THEN 'absent' "
            "ELSE status END "
            "WHERE status IN ('late', 'leave')"
        ),
    },
    # Per-employee salary basis: 'calendar' (rate = salary / days in month) or
    # 'fixed_30' (rate = salary / 30; deduction-based monthly deal).
    {
        "table": "employees",
        "column": "salary_days_basis",
        "definition": "VARCHAR(12) NOT NULL DEFAULT 'calendar'",
        "backfill": (
            "UPDATE employees SET salary_days_basis = 'calendar' "
            "WHERE salary_days_basis IS NULL "
            "OR salary_days_basis NOT IN ('calendar', 'fixed_30')"
        ),
    },
    # An earlier build wrote loan status 'active', which the check constraint
    # ck_employee_loans_status (open/paid/cancelled) rejects — leaving such
    # rows unusable (repayment logic looks for 'open') and blocking any later
    # UPDATE that touches them. Normalize to the canonical 'open'. Idempotent.
    {
        "table": "employee_loans",
        "column": "status",
        "definition": "VARCHAR(20)",
        "backfill": "UPDATE employee_loans SET status = 'open' WHERE status = 'active'",
    },
    {
        "table": "employees",
        "column": "farm_id",
        "definition": "INTEGER",
        "backfill": "SELECT 1",
    },
    # ── Animal cost-allocation feature (added 2026-05-18) ────────────
    # Idempotently add columns so the app works even if alembic is
    # blocked (e.g. by a multi-head conflict). Each patch is no-op when
    # the column already exists.
    {
        "table": "animal_groups",
        "column": "purchase_cost",
        "definition": "NUMERIC(14, 2)",
        "backfill": "SELECT 1",
    },
    {
        "table": "animal_groups",
        "column": "cost_per_head",
        "definition": "NUMERIC(14, 2)",
        "backfill": "SELECT 1",
    },
    # ── Animal sex breakdown + birth date (added 2026-06-30) ─────────
    {
        "table": "animal_groups",
        "column": "male_count",
        "definition": "INTEGER",
        "backfill": "SELECT 1",
    },
    {
        "table": "animal_groups",
        "column": "female_count",
        "definition": "INTEGER",
        "backfill": "SELECT 1",
    },
    {
        "table": "animal_groups",
        "column": "birth_date",
        "definition": "DATE",
        "backfill": "SELECT 1",
    },
    # ── Sex split captured on intake (added 2026-06-30) ──────────────
    {
        "table": "animal_intake_logs",
        "column": "male_count",
        "definition": "INTEGER",
        "backfill": "SELECT 1",
    },
    {
        "table": "animal_intake_logs",
        "column": "female_count",
        "definition": "INTEGER",
        "backfill": "SELECT 1",
    },
    {
        "table": "expenses",
        "column": "animal_group_id",
        "definition": "INTEGER",
        "backfill": "SELECT 1",
    },
    {
        "table": "expenses",
        "column": "is_animal_expense",
        "definition": "BOOLEAN NOT NULL DEFAULT FALSE",
        "backfill": "UPDATE expenses SET is_animal_expense = FALSE WHERE is_animal_expense IS NULL",
    },
    {
        "table": "employees",
        "column": "works_with_animals",
        "definition": "BOOLEAN NOT NULL DEFAULT FALSE",
        "backfill": "UPDATE employees SET works_with_animals = FALSE WHERE works_with_animals IS NULL",
    },
)
_CRITICAL_AUTH_TABLES = {"users", "refresh_tokens", "activity_logs"}
_REQUIRED_SCHEMA_COLUMNS = {
    "employees": {"attendance_auto_status", "farm_id"},
}


def _alembic_config() -> Config:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def _masked_database_url() -> str:
    url = settings.DATABASE_URL
    if "://" not in url or "@" not in url:
        return url
    prefix, rest = url.split("://", 1)
    credentials, host_part = rest.split("@", 1)
    if ":" not in credentials:
        return f"{prefix}://***@{host_part}"
    username, _password = credentials.split(":", 1)
    return f"{prefix}://{username}:***@{host_part}"


def _format_migration_status(payload: dict[str, Any]) -> str:
    if payload["status"] == "ok":
        return "Database migrations are up to date"
    if payload["status"] == "missing_versions":
        return "No Alembic revisions were found"
    if payload["status"] == "legacy_schema_unversioned":
        return "Database schema exists but is not tracked by Alembic"
    if payload["status"] == "pending":
        return "Database migrations are pending"
    if payload["status"] == "schema_incomplete":
        return "Database schema is missing required tables or columns for the current Alembic revision"
    if payload["status"] == "multiple_heads":
        return "Multiple Alembic heads detected"
    return "Database migration status could not be determined"


async def check_migration_status() -> dict[str, Any]:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = sorted(script.get_heads())
    if not heads:
        return {
            "status": "missing_versions",
            "heads": [],
            "current_revisions": [],
            "database_url": _masked_database_url(),
        }
    if len(heads) > 1:
        return {
            "status": "multiple_heads",
            "heads": heads,
            "current_revisions": [],
            "database_url": _masked_database_url(),
        }

    async with engine.begin() as conn:
        def inspect_db(sync_conn):
            db_inspector = inspect(sync_conn)
            table_names = set(db_inspector.get_table_names())
            context = MigrationContext.configure(sync_conn)
            current_revisions = sorted(context.get_current_heads())
            raw_version_rows: list[str] = []
            if "alembic_version" in table_names:
                rows = sync_conn.execute(text("SELECT version_num FROM alembic_version"))
                raw_version_rows.extend(sorted(row[0] for row in rows))
            user_tables = sorted(name for name in table_names if name != "alembic_version")
            required_columns: dict[str, set[str]] = {}
            for table_name in _REQUIRED_SCHEMA_COLUMNS:
                if table_name in table_names:
                    required_columns[table_name] = {
                        column["name"] for column in db_inspector.get_columns(table_name)
                    }
            return current_revisions, raw_version_rows, user_tables, required_columns

        current_revisions, raw_version_rows, user_tables, required_columns = await conn.run_sync(inspect_db)

    if not current_revisions:
        if user_tables:
            return {
                "status": "legacy_schema_unversioned",
                "heads": heads,
                "current_revisions": [],
                "raw_version_rows": raw_version_rows,
                "table_count": len(user_tables),
                "database_url": _masked_database_url(),
            }
        return {
            "status": "pending",
            "heads": heads,
            "current_revisions": [],
            "raw_version_rows": raw_version_rows,
            "database_url": _masked_database_url(),
        }
    if current_revisions != heads:
        return {
            "status": "pending",
            "heads": heads,
            "current_revisions": current_revisions,
            "raw_version_rows": raw_version_rows,
            "database_url": _masked_database_url(),
        }
    missing_tables = sorted(_CRITICAL_AUTH_TABLES - set(user_tables))
    missing_columns = sorted(
        f"{table_name}.{column_name}"
        for table_name, column_names in _REQUIRED_SCHEMA_COLUMNS.items()
        if table_name in set(user_tables)
        for column_name in column_names - required_columns.get(table_name, set())
    )
    if missing_tables or missing_columns:
        return {
            "status": "schema_incomplete",
            "heads": heads,
            "current_revisions": current_revisions,
            "raw_version_rows": raw_version_rows,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "database_url": _masked_database_url(),
        }
    return {
        "status": "ok",
        "heads": heads,
        "current_revisions": current_revisions,
        "raw_version_rows": raw_version_rows,
        "database_url": _masked_database_url(),
    }


async def ensure_runtime_schema_compatibility() -> None:
    async with engine.begin() as conn:
        def patch_schema(sync_conn):
            db_inspector = inspect(sync_conn)
            applied: list[str] = []

            # The alembic_version.version_num column was originally VARCHAR(32),
            # but our revision ids are longer (e.g. '20260630_0040_animal_sex_and_birth'
            # is 34 chars), which made `alembic upgrade head` fail with
            # StringDataRightTruncationError and freeze the whole migration
            # chain. Widen it once, defensively, so migrations can advance.
            if db_inspector.has_table("alembic_version"):
                try:
                    sync_conn.execute(text(
                        "ALTER TABLE alembic_version "
                        "ALTER COLUMN version_num TYPE VARCHAR(255)"
                    ))
                    applied.append("alembic_version.version_num→VARCHAR(255)")
                except Exception:
                    pass

            for patch in _RUNTIME_SCHEMA_PATCHES:
                table_name = patch["table"]
                if not db_inspector.has_table(table_name):
                    continue
                column_names = {column["name"] for column in db_inspector.get_columns(table_name)}
                if patch["column"] not in column_names:
                    sync_conn.execute(
                        text(
                            f"ALTER TABLE {table_name} ADD COLUMN "
                            f"{patch['column']} {patch['definition']}"
                        )
                    )
                    applied.append(f"{table_name}.{patch['column']}")
                sync_conn.execute(text(patch["backfill"]))
            return applied

        applied = await conn.run_sync(patch_schema)

    if applied:
        logger.warning(
            "Applied runtime schema compatibility patches",
            extra={"schema_patches": applied},
        )


async def verify_migration_status() -> None:
    await ensure_runtime_schema_compatibility()

    if not settings.MIGRATION_CHECK_ON_STARTUP:
        return

    status = await check_migration_status()
    message = _format_migration_status(status)
    log_extra = {"migration_status": status}

    if status["status"] == "ok":
        logger.info(message, extra=log_extra)
        return

    if settings.MIGRATION_CHECK_STRICT:
        logger.error(message, extra=log_extra)
        raise RuntimeError(message)

    logger.warning(message, extra=log_extra)