from __future__ import annotations

from collections import defaultdict
from typing import Iterable


PERMISSION_MATRIX = [
    {
        "module": "dashboard",
        "resource": "overview",
        "label": "Sales Dashboard",
        "icon": "chart",
        "actions": [
            {"action": "view", "key": "page_dashboard", "label": "View sales dashboard"},
        ],
    },
    {
        "module": "farm_dashboard",
        "resource": "overview",
        "label": "Farm Dashboard",
        "icon": "farm",
        "actions": [
            {"action": "view", "key": "page_farm_dashboard", "label": "View farm dashboard"},
        ],
    },
    {
        "module": "reports",
        "resource": "reports",
        "label": "Reports",
        "icon": "reports",
        "actions": [
            {"action": "view", "key": "page_reports", "label": "Open reports"},
            {"action": "view_sales", "key": "tab_reports_sales", "label": "Sales tab"},
            {"action": "view_pl", "key": "tab_reports_pl", "label": "P&L tab"},
            {"action": "view_inventory", "key": "tab_reports_inventory", "label": "Inventory tab"},
            {"action": "view_transactions", "key": "tab_reports_transactions", "label": "Transactions tab"},
            {"action": "view_b2b", "key": "tab_reports_b2b", "label": "B2B Statement tab"},
            {"action": "view_farm", "key": "tab_reports_farm", "label": "Farm Intake tab"},
            {"action": "view_spoilage", "key": "tab_reports_spoilage", "label": "Spoilage tab"},
            {"action": "view_production", "key": "tab_reports_production", "label": "Production tab"},
            {"action": "view_hr", "key": "tab_reports_hr", "label": "HR tab"},
            {"action": "view_utilities", "key": "tab_reports_utilities", "label": "Utilities tab"},
            {"action": "view_animals", "key": "tab_reports_animals", "label": "Animals tab"},
            {"action": "export", "key": "action_export_excel", "label": "Export to Excel"},
        ],
    },
    {
        "module": "pos",
        "resource": "sales",
        "label": "POS",
        "icon": "pos",
        "actions": [
            {"action": "view", "key": "page_pos", "label": "Open POS"},
            {"action": "create", "key": "action_pos_create_sale", "label": "Create sales"},
            {"action": "delete", "key": "action_pos_delete_invoice", "label": "Delete invoices"},
            {"action": "discount_override", "key": "action_pos_discount", "label": "Discount override"},
            {"action": "approve", "key": "action_pos_settle_later", "label": "Approve settle later"},
            {"action": "refund", "key": "action_pos_refund", "label": "Create retail refunds"},
            {"action": "edit_price", "key": "action_pos_edit_price", "label": "Edit unit prices in POS cart"},
        ],
    },
    {
        "module": "b2b",
        "resource": "clients",
        "label": "B2B",
        "icon": "b2b",
        "actions": [
            {"action": "view", "key": "page_b2b", "label": "Open B2B"},
            {"action": "view_clients", "key": "tab_b2b_clients", "label": "Clients tab"},
            {"action": "view_invoices", "key": "tab_b2b_invoices", "label": "Invoices tab"},
            {"action": "view_consignment", "key": "tab_b2b_consignment", "label": "Consignment tab"},
            {"action": "create_client", "key": "action_b2b_clients_create", "label": "Create clients"},
            {"action": "update_client", "key": "action_b2b_clients_update", "label": "Update clients"},
            {"action": "delete_client", "key": "action_b2b_clients_delete", "label": "Delete clients"},
        ],
    },
    {
        "module": "b2b",
        "resource": "invoices",
        "label": "B2B Invoices",
        "icon": "b2b",
        "actions": [
            {"action": "create", "key": "action_b2b_invoices_create", "label": "Create invoices"},
            {"action": "update", "key": "action_b2b_invoices_update", "label": "Update invoices"},
            {"action": "delete", "key": "action_b2b_delete", "label": "Delete invoices"},
            {"action": "approve", "key": "action_b2b_collect", "label": "Collect payments"},
            {"action": "refund", "key": "action_b2b_refund", "label": "Create client refunds"},
            {"action": "settle", "key": "action_b2b_consignment_settle", "label": "Settle consignments"},
        ],
    },
    {
        "module": "inventory",
        "resource": "stock",
        "label": "Inventory",
        "icon": "inventory",
        "actions": [
            {"action": "view", "key": "page_inventory", "label": "Open inventory"},
            {"action": "update", "key": "action_inventory_adjust", "label": "Adjust stock"},
        ],
    },
    {
        "module": "products",
        "resource": "products",
        "label": "Products",
        "icon": "products",
        "actions": [
            {"action": "view", "key": "page_products", "label": "Open products"},
            {"action": "create", "key": "action_products_create", "label": "Create products"},
            {"action": "update", "key": "action_products_edit", "label": "Edit products"},
            {"action": "delete", "key": "action_products_delete", "label": "Delete products"},
        ],
    },
    {
        "module": "import",
        "resource": "imports",
        "label": "Import Data",
        "icon": "import",
        "actions": [
            {"action": "view", "key": "page_import", "label": "Open import data"},
            {"action": "preview", "key": "action_import_preview", "label": "Preview import file"},
            {"action": "import_products", "key": "action_import_products", "label": "Import products"},
            {"action": "import_stock", "key": "action_import_stock", "label": "Import stock"},
            {"action": "import_customers", "key": "action_import_customers", "label": "Import customers"},
            {"action": "import_sales", "key": "action_import_sales", "label": "Import retail sales"},
            {"action": "import_b2b_sales", "key": "action_import_b2b_sales", "label": "Import B2B sales"},
            {"action": "import_farm_intake", "key": "action_import_farm_intake", "label": "Import farm intake"},
            {"action": "import_expenses", "key": "action_import_expenses", "label": "Import expenses"},
            {"action": "import_receive_products", "key": "action_import_receive_products", "label": "Import receive products"},
            {"action": "delete_batch", "key": "action_import_delete_batch", "label": "Delete import batches"},
        ],
    },
    {
        "module": "production",
        "resource": "production",
        "label": "Production",
        "icon": "production",
        "actions": [
            {"action": "view", "key": "page_production", "label": "Open production"},
            {"action": "view_batches", "key": "tab_production_batches", "label": "Batches tab"},
            {"action": "view_packaging", "key": "tab_production_packaging", "label": "Packaging tab"},
            {"action": "view_spoilage", "key": "tab_production_spoilage", "label": "Spoilage tab"},
            {"action": "view_recipes", "key": "tab_production_recipes", "label": "Recipes tab"},
            {"action": "manage_recipes", "key": "action_production_manage_recipes", "label": "Create / delete recipes"},
            {"action": "create_batch", "key": "action_production_create_batch", "label": "Create production batch"},
            {"action": "update_batch", "key": "action_production_update_batch", "label": "Edit production batch"},
            {"action": "delete_batch", "key": "action_production_delete_batch", "label": "Delete production batch"},
            {"action": "log_spoilage", "key": "action_production_log_spoilage", "label": "Log / delete spoilage"},
            {"action": "drying_cancel",   "key": "action_drying_cancel_batch",   "label": "Cancel drying batch"},
            {"action": "drying_complete", "key": "action_drying_complete_batch", "label": "Complete drying batch"},
            {"action": "drying_edit",     "key": "action_drying_edit_batch",     "label": "Edit finalized drying batch"},
            {"action": "drying_spoilage", "key": "action_drying_log_spoilage",   "label": "Log spoilage on drying batch"},
            {"action": "drying_start",    "key": "action_drying_start_batch",    "label": "Start drying batch"},
        ],
    },
    {
        "module": "farm",
        "resource": "intake",
        "label": "Farm Intake",
        "icon": "farm",
        "actions": [
            {"action": "view",   "key": "page_farm",                    "label": "Open farm intake"},
            {"action": "create", "key": "action_farm_create",             "label": "Add / create new farms"},
            {"action": "create_delivery", "key": "action_farm_delivery_create", "label": "Record farm deliveries"},
            {"action": "update_delivery", "key": "action_farm_delivery_update", "label": "Edit farm deliveries"},
            {"action": "delete_delivery", "key": "action_farm_delivery_delete", "label": "Delete farm deliveries"},
            {"action": "weather_log", "key": "action_farm_weather_log",   "label": "Log / edit farm weather"},
            {"action": "view_deliveries", "key": "tab_farm_deliveries",   "label": "Deliveries tab"},
            {"action": "view_history",    "key": "tab_farm_history",      "label": "Farm History tab"},
            {"action": "view_weather",    "key": "tab_farm_weather",      "label": "Weather Log tab"},
            {"action": "view_season",     "key": "tab_farm_season",       "label": "Season Analysis tab"},
        ],
    },
    {
        "module": "animals",
        "resource": "animals",
        "label": "Animals",
        "icon": "farm",
        "actions": [
            {"action": "view",          "key": "page_animals",                  "label": "Open animals"},
            {"action": "create",        "key": "action_animals_create",         "label": "Add animal groups"},
            {"action": "edit",          "key": "action_animals_edit",           "label": "Edit animal groups"},
            {"action": "delete",        "key": "action_animals_delete",         "label": "Archive animal groups"},
            {"action": "feed",          "key": "action_animal_feeding_create",  "label": "Log feedings"},
            {"action": "delete_feed",   "key": "action_animal_feeding_delete",  "label": "Delete feeding entries"},
            {"action": "log_death",     "key": "action_animal_mortality_create","label": "Log animal deaths"},
            {"action": "delete_death",  "key": "action_animal_mortality_delete","label": "Reverse mortality entries"},
            {"action": "analyze",       "key": "action_animals_analyze",        "label": "View cost analysis"},
        ],
    },
    {
        "module": "carbon",
        "resource": "footprint",
        "label": "Carbon Footprint",
        "icon": "carbon",
        "actions": [
            {"action": "view",   "key": "page_carbon",          "label": "Open carbon footprint"},
            {"action": "log",    "key": "action_carbon_log",    "label": "Log emission entries"},
            {"action": "manage", "key": "action_carbon_factors","label": "Manage emission factors"},
        ],
    },
    {
        "module": "hr",
        "resource": "payroll",
        "label": "HR & Payroll",
        "icon": "hr",
        "actions": [
            {"action": "view", "key": "page_hr", "label": "Open HR"},
            {"action": "view_employees", "key": "tab_hr_employees", "label": "Employees tab"},
            {"action": "view_attendance", "key": "tab_hr_attendance", "label": "Attendance tab"},
            {"action": "view_payroll", "key": "tab_hr_payroll", "label": "Payroll tab"},
            {"action": "manage_employees", "key": "action_hr_manage_employees", "label": "Create / edit / deactivate employees"},
            {"action": "manage_attendance", "key": "action_hr_log_attendance", "label": "Log / auto-mark attendance"},
            {"action": "manage_allowances", "key": "action_hr_manage_allowances", "label": "Manage allowance advances"},
            {"action": "approve", "key": "action_hr_run_payroll", "label": "Run payroll"},
            {"action": "mark_paid", "key": "action_hr_mark_paid", "label": "Mark payroll paid"},
            {"action": "view_loans", "key": "action_hr_view_loans", "label": "View employee loans"},
            {"action": "manage_loans", "key": "action_hr_manage_loans", "label": "Manage employee loans"},
            {"action": "delete_loans", "key": "action_hr_delete_loans", "label": "Delete employee loans"},
            {"action": "view_deductions", "key": "action_hr_view_deductions", "label": "View payroll deductions"},
            {"action": "manage_deductions", "key": "action_hr_manage_deductions", "label": "Manage payroll deductions"},
            {"action": "edit_attendance", "key": "action_hr_edit_attendance", "label": "Edit / delete attendance records"},
            {"action": "clear_hr_data", "key": "action_hr_clear_data", "label": "Clear all HR data"},
        ],
    },
    {
        "module": "accounting",
        "resource": "journals",
        "label": "Accounting",
        "icon": "accounting",
        "actions": [
            {"action": "view", "key": "page_accounting", "label": "Open accounting"},
            {"action": "view_pos", "key": "tab_accounting_pos", "label": "POS invoices tab"},
            {"action": "view_b2b", "key": "tab_accounting_b2b", "label": "B2B invoices tab"},
            {"action": "view_journal", "key": "tab_accounting_journal", "label": "Journal tab"},
            {"action": "view_pl", "key": "tab_accounting_pl", "label": "P&L tab"},
            {"action": "create", "key": "action_accounting_post_journal", "label": "Post journal entries"},
        ],
    },
    {
        "module": "expenses",
        "resource": "expenses",
        "label": "Expenses",
        "icon": "accounting",
        "actions": [
            {"action": "view", "key": "page_expenses", "label": "Open expenses"},
            {"action": "create", "key": "action_expenses_create", "label": "Create expenses"},
            {"action": "update", "key": "action_expenses_update", "label": "Edit expenses"},
            {"action": "delete", "key": "action_expenses_delete", "label": "Delete expenses"},
        ],
    },
    {
        "module": "customers",
        "resource": "customers",
        "label": "Customers",
        "icon": "customers",
        "actions": [
            {"action": "view", "key": "page_customers", "label": "Open customers"},
            {"action": "create", "key": "action_customers_create", "label": "Create customers"},
            {"action": "update", "key": "action_customers_update", "label": "Edit customers"},
            {"action": "delete", "key": "action_customers_delete", "label": "Delete customers"},
        ],
    },
    {
        "module": "suppliers",
        "resource": "suppliers",
        "label": "Suppliers",
        "icon": "suppliers",
        "actions": [
            {"action": "view", "key": "page_suppliers", "label": "Open suppliers"},
            {"action": "view_directory", "key": "tab_suppliers_directory", "label": "Suppliers tab"},
            {"action": "view_purchases", "key": "tab_suppliers_purchases", "label": "Purchase orders tab"},
            {"action": "create", "key": "action_suppliers_create", "label": "Create suppliers"},
            {"action": "update", "key": "action_suppliers_update", "label": "Edit suppliers"},
            {"action": "delete", "key": "action_suppliers_delete", "label": "Delete suppliers"},
            {"action": "pay", "key": "action_suppliers_pay", "label": "Pay suppliers"},
            {"action": "create_purchase", "key": "action_suppliers_purchase_create", "label": "Create purchase orders"},
        ],
    },
    {
        "module": "receive_products",
        "resource": "receipts",
        "label": "Receive Products",
        "icon": "suppliers",
        "actions": [
            {"action": "view", "key": "page_receive_products", "label": "Open receive products"},
            {"action": "create", "key": "action_receive_products_create", "label": "Receive stock"},
            {"action": "update", "key": "action_receive_products_update", "label": "Edit received stock"},
            {"action": "delete", "key": "action_receive_products_delete", "label": "Delete received stock"},
            {"action": "export", "key": "action_receive_products_export", "label": "Export receive products"},
        ],
    },
]


ROLE_DEFINITIONS = {
    "cashier": {
        "label": "Cashier",
        "description": "Frontline sales role for checkout staff. Includes POS access, creating sales, applying approved discounts, and settle-later handling. Excludes refunds, reporting, accounting, inventory management, and user administration unless added manually.",
        "permissions": {
            "page_pos",
            "action_pos_create_sale",
            "action_pos_discount",
            "action_pos_settle_later",
        },
    },
    "manager": {
        "label": "Manager",
        "description": "Operations leadership role with broad day-to-day control. Covers dashboard, reports, POS including refunds, B2B workflows, inventory adjustments, products, imports, production, farm intake, customers, and suppliers. Does not include accounting journals, HR/payroll, or user administration by default.",
        "permissions": {
            "page_dashboard",
            "page_reports",
            "tab_reports_sales",
            "tab_reports_inventory",
            "tab_reports_transactions",
            "tab_reports_b2b",
            "tab_reports_farm",
            "tab_reports_spoilage",
            "tab_reports_production",
            "tab_reports_utilities",
            "action_export_excel",
            "page_pos",
            "action_pos_create_sale",
            "action_pos_discount",
            "action_pos_settle_later",
            "action_pos_refund",
            "action_pos_edit_price",
            "page_b2b",
            "tab_b2b_clients",
            "tab_b2b_invoices",
            "tab_b2b_consignment",
            "action_b2b_clients_create",
            "action_b2b_clients_update",
            "action_b2b_clients_delete",
            "action_b2b_invoices_create",
            "action_b2b_invoices_update",
            "action_b2b_delete",
            "action_b2b_collect",
            "action_b2b_refund",
            "action_b2b_consignment_settle",
            "page_inventory",
            "action_inventory_adjust",
            "page_products",
            "action_products_create",
            "action_products_edit",
            "page_customers",
            "action_customers_create",
            "action_customers_update",
            "action_customers_delete",
            "page_import",
            "action_import_preview",
            "action_import_products",
            "action_import_stock",
            "action_import_customers",
            "action_import_sales",
            "action_import_b2b_sales",
            "action_import_farm_intake",
            "action_import_receive_products",
            "action_import_delete_batch",
            "page_production",
            "tab_production_batches",
            "tab_production_packaging",
            "tab_production_spoilage",
            "tab_production_recipes",
            "action_production_manage_recipes",
            "action_production_create_batch",
            "action_production_update_batch",
            "action_production_delete_batch",
            "action_production_log_spoilage",
            "action_drying_cancel_batch",
            "action_drying_complete_batch",
            "action_drying_edit_batch",
            "action_drying_log_spoilage",
            "action_drying_start_batch",
            "page_farm",
            "page_farm_dashboard",
            "action_farm_create",
            "action_farm_delivery_create",
            "action_farm_delivery_update",
            "action_farm_delivery_delete",
            "action_farm_weather_log",
            "tab_farm_deliveries",
            "tab_farm_history",
            "tab_farm_weather",
            "tab_farm_season",
            "page_animals",
            "action_animals_create",
            "action_animals_edit",
            "action_animals_delete",
            "action_animal_feeding_create",
            "action_animal_feeding_delete",
            "action_animal_mortality_create",
            "action_animal_mortality_delete",
            "action_animals_analyze",
            "page_carbon",
            "action_carbon_log",
            "page_suppliers",
            "tab_suppliers_directory",
            "tab_suppliers_purchases",
            "action_suppliers_create",
            "action_suppliers_update",
            "action_suppliers_delete",
            "action_suppliers_pay",
            "action_suppliers_purchase_create",
            "page_receive_products",
            "action_receive_products_create",
            "action_receive_products_update",
            "action_receive_products_delete",
            "action_receive_products_export",
        },
    },
    "accountant": {
        "label": "Accountant",
        "description": "Finance-focused role for bookkeeping and financial oversight. Includes dashboard, reporting, accounting tabs, journal posting, and financial review tools. Excludes sales execution, operational stock handling, HR, and user administration unless explicitly granted.",
        "permissions": {
            "page_dashboard",
            "page_reports",
            "tab_reports_sales",
            "tab_reports_pl",
            "tab_reports_inventory",
            "tab_reports_transactions",
            "action_export_excel",
            "page_accounting",
            "tab_accounting_pos",
            "tab_accounting_b2b",
            "tab_accounting_journal",
            "tab_accounting_pl",
            "action_accounting_post_journal",
            "page_expenses",
            "action_expenses_create",
            "action_expenses_update",
            "action_expenses_delete",
            "page_import",
            "action_import_preview",
            "action_import_expenses",
            "action_import_delete_batch",
        },
    },
    "hr": {
        "label": "HR",
        "description": "People operations role for workforce administration. Includes dashboard visibility plus employee records, attendance, payroll processing, and payroll payment actions. Excludes sales, accounting, stock operations, and user management by default.",
        "permissions": {
            "page_dashboard",
            "page_reports",
            "tab_reports_hr",
            "page_hr",
            "tab_hr_employees",
            "tab_hr_attendance",
            "tab_hr_payroll",
            "action_hr_manage_employees",
            "action_hr_log_attendance",
            "action_hr_manage_allowances",
            "action_hr_run_payroll",
            "action_hr_mark_paid",
            "action_hr_view_loans",
            "action_hr_manage_loans",
            "action_hr_view_deductions",
            "action_hr_manage_deductions",
        },
    },
    "viewer": {
        "label": "Viewer",
        "description": "Read-only role for owners, auditors, or supervisors who need visibility without operational control. Includes dashboard access and report tabs only. Cannot create, edit, approve, delete, refund, post journals, or manage users unless extra permissions are added.",
        "permissions": {
            "page_dashboard",
            "page_farm_dashboard",
            "page_reports",
            "tab_reports_sales",
            "tab_reports_pl",
            "tab_reports_inventory",
            "tab_reports_transactions",
            "tab_reports_b2b",
            "tab_reports_farm",
            "tab_reports_spoilage",
            "tab_reports_production",
            "tab_reports_utilities",
        },
    },
    "admin": {
        "label": "Admin",
        "description": "Full system administrator with unrestricted access across all modules, permissions, settings, users, and audit visibility. Use sparingly for trusted system owners only.",
        "permissions": {"*"},
    },
}


def iter_known_permissions() -> Iterable[str]:
    for entry in PERMISSION_MATRIX:
        for action in entry["actions"]:
            yield action["key"]


KNOWN_PERMISSIONS = set(iter_known_permissions())


def get_role_permissions(role: str | None) -> set[str]:
    return set(ROLE_DEFINITIONS.get(role or "", {}).get("permissions", set()))


def is_known_permission(permission: str) -> bool:
    return permission == "*" or permission in KNOWN_PERMISSIONS


def get_permission_key(module: str, resource: str, action: str) -> str:
    for entry in PERMISSION_MATRIX:
        if entry["module"] != module or entry["resource"] != resource:
            continue
        for action_entry in entry["actions"]:
            if action_entry["action"] == action:
                return action_entry["key"]
    raise KeyError(f"Unknown permission action: {module}.{resource}.{action}")


PAGE_ALIASES = {}


def _build_page_catalog() -> list[dict]:
    pages: dict[str, dict] = {}
    module_page_map: dict[str, str] = {}
    for entry in PERMISSION_MATRIX:
        page_key = None
        page_label = entry["label"]
        icon = entry["icon"]
        children = []
        for action in entry["actions"]:
            if action["action"] == "view" and action["key"].startswith("page_"):
                page_key = action["key"]
                page_label = entry["label"]
            else:
                children.append({"key": action["key"], "label": action["label"]})
        if page_key is None:
            continue
        page_entry = pages.setdefault(
            page_key,
            {"key": page_key, "label": page_label, "icon": icon, "actions": []},
        )
        page_entry["actions"].extend(children)
        module_page_map[entry["module"]] = page_key

    for entry in PERMISSION_MATRIX:
        has_page_view = any(
            action["action"] == "view" and action["key"].startswith("page_")
            for action in entry["actions"]
        )
        if has_page_view:
            continue
        page_key = module_page_map.get(entry["module"])
        if page_key is None or page_key not in pages:
            continue
        pages[page_key]["actions"].extend(
            {"key": action["key"], "label": action["label"]}
            for action in entry["actions"]
        )

    for page_entry in pages.values():
        deduped = {action["key"]: action for action in page_entry["actions"]}
        page_entry["actions"] = list(deduped.values())
        page_entry["aliases"] = PAGE_ALIASES.get(page_entry["key"], [])
    return list(pages.values())


PERMISSION_PAGES = _build_page_catalog()


def get_permission_catalog() -> dict:
    role_permissions = {
        role_key: set(role_data["permissions"])
        for role_key, role_data in ROLE_DEFINITIONS.items()
    }
    matrix = []
    for entry in PERMISSION_MATRIX:
        actions = []
        for action in entry["actions"]:
            allowed_roles = [
                role_key
                for role_key, permissions in role_permissions.items()
                if "*" in permissions or action["key"] in permissions
            ]
            actions.append(
                {
                    "action": action["action"],
                    "key": action["key"],
                    "label": action["label"],
                    "roles": allowed_roles,
                }
            )
        matrix.append(
            {
                "module": entry["module"],
                "resource": entry["resource"],
                "label": entry["label"],
                "icon": entry["icon"],
                "actions": actions,
            }
        )

    grouped_roles: dict[str, list[str]] = defaultdict(list)
    for entry in matrix:
        for action in entry["actions"]:
            grouped_roles[action["key"]] = list(action["roles"])

    return {
        "pages": PERMISSION_PAGES,
        "matrix": matrix,
        "roles": [
            {
                "key": role_key,
                "label": role_data["label"],
                "description": role_data["description"],
                "permissions": sorted(role_data["permissions"]),
            }
            for role_key, role_data in ROLE_DEFINITIONS.items()
        ],
        "role_access": grouped_roles,
    }