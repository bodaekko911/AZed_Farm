# Test Failure Audit

Run date: 2026-05-25
Total failures: 33
Total passing: 265

---

## Summary by category

| Category | Count | Examples |
|---|---|---|
| A — Stale mock signature | 13 | test_list_expenses_…, test_product_create_requires_explicit_permission (×5), test_create_receipt_… (×5), test_numbers_contains_…, test_recent_activity_… |
| B — Stale model/schema (production code) | 12 | All 12 test_b2b_sales_import.py tests |
| C — Drifted response shape | 3 | test_login_page_renders_…, test_expenses_loaders_…, test_clear_hr_data_… |
| D — Drifted business logic | 3 | test_apply_date_range_… (×2), test_audit_log_contains_… |
| E — Test setup / fixture problem | 1 | test_employee_create_without_farm_… |
| F — Test references deleted/renamed code | 1 | test_log_action_uses_optional_shared_auth_dependency |

---

## Detailed list

---

### tests/test_accounting_journal_filters.py::test_apply_date_range_uses_inclusive_start_and_exclusive_next_day_end

**Category:** D — Drifted business logic
**Error:** `AssertionError: assert "journals.created_at >= '2026-04-02 00:00:00+00:00'" in "...WHERE journals.created_at >= '2026-04-01 22:00:00+00:00'..."`
**Failing line:** `tests/test_accounting_journal_filters.py:21`
**Likely cause:** `_apply_date_range` was updated to convert local-timezone midnight to UTC via `settings.APP_TIMEZONE` (currently UTC+2 in this environment). The test was written assuming UTC = local time, so its hard-coded expected timestamps (`'2026-04-02 00:00:00+00:00'`) are now 2 hours ahead of the actual UTC-converted value (`'2026-04-01 22:00:00+00:00'`).
**Fix difficulty:** small — pin `APP_TIMEZONE=UTC` in a test-scoped env override, or update the assertion strings to account for the configured 2-hour offset.

---

### tests/test_accounting_journal_filters.py::test_apply_date_range_allows_open_ended_ranges

**Category:** D — Drifted business logic
**Error:** `AssertionError: assert "journals.created_at >= '2026-04-02 00:00:00+00:00'" in "...WHERE journals.created_at >= '2026-04-01 22:00:00+00:00'"`
**Failing line:** `tests/test_accounting_journal_filters.py:43`
**Likely cause:** Identical root cause as the test above — same UTC offset mismatch affects both the `from_date` and `to_date` branches.
**Fix difficulty:** small — same fix as above.

---

### tests/test_auth_endpoints.py::test_login_page_renders_valid_escaped_newline_checks

**Category:** C — Drifted response shape
**Error:** `AssertionError: assert 'url.indexOf("\\r") === -1' in '<html>...(full login page)...'`
**Failing line:** `tests/test_auth_endpoints.py:8`
**Likely cause:** The login-page HTML template was refactored and the JavaScript snippet that guarded against carriage-return injection (`url.indexOf("\\r") === -1`) was removed or rewritten. The current rendered page contains no such string.
**Fix difficulty:** trivial — either restore the guard to the template, or update the assertion to match the current security pattern in the template.

---

### tests/test_b2b_sales_import.py::test_real_import_history_only_journal_patterns
### tests/test_b2b_sales_import.py::test_created_at_set_to_sheet_date_at_noon
### tests/test_b2b_sales_import.py::test_consignment_creates_consignment_items
### tests/test_b2b_sales_import.py::test_client_auto_creation
### tests/test_b2b_sales_import.py::test_discount_pct_suggestion_logic
### tests/test_b2b_sales_import.py::test_per_line_discount_math
### tests/test_b2b_sales_import.py::test_two_rows_same_group_merged_into_one_invoice
### tests/test_b2b_sales_import.py::test_different_payment_types_same_client_date_become_separate_invoices
### tests/test_b2b_sales_import.py::test_outstanding_updated_for_credit_types
### tests/test_b2b_sales_import.py::test_client_price_created_for_repeated_product
### tests/test_b2b_sales_import.py::test_unknown_sku_skips_group_imports_others
### tests/test_b2b_sales_import.py::test_duplicate_detection_blocks_second_import

**Category:** B — Stale model/schema (bug is in production code, not the tests)
**Error:** `TypeError: 'account_type' is an invalid keyword argument for Account`
**Failing line:** `app/services/b2b_shared.py:49` (called through `import_b2b_sales → seed_deferred_revenue`)
**Likely cause:** The `Account` SQLAlchemy model's field was renamed from `account_type` to `type` (confirmed: `Account.__tablename__` declares `type = Column(String(30), nullable=False)`). The production helper `seed_deferred_revenue` in `app/services/b2b_shared.py:51` still passes `account_type="liability"` as a kwarg. Every test that calls `import_b2b_sales(..., dry_run=False)` triggers this code path, so all 12 tests fail from the same single-line bug. Note that `receive_service.py:160` contains the correct usage: `Account(code=code, name=name, type=account_type, balance=0)`.
**Fix:** Change `account_type="liability"` → `type="liability"` in `app/services/b2b_shared.py:51`. Search for any other callers using `Account(account_type=` before merging.
**Fix difficulty:** trivial in production code — one-liner, fixes all 12 tests.

---

### tests/test_dashboard_summary_shape.py::test_numbers_contains_expected_additive_entries

**Category:** A — Stale mock signature (secondary C issue after mock is fixed)
**Error (primary):** `TypeError: FakeDB.execute() takes 2 positional arguments but 3 were given` at `dashboard_summary_service.py:654`
**Error (secondary, would surface after fixing primary):** `AssertionError: assert {'b2b_cash', ..., 'profit', ...} == {'sales', 'clients_owe', 'spent', 'stock_alerts', 'margin'}` — two new keys were added to the `numbers` dict.
**Failing line:** `tests/test_dashboard_summary_shape.py:115`
**Likely cause (mock):** `dashboard_summary_service._build_numbers` now calls `db.execute(stmt, bind_params)` with explicit bound parameters as a second positional argument. `FakeDB.execute(self, _stmt)` only accepts one argument; adding `*args` would fix the crash.
**Likely cause (keys):** The dashboard `numbers` section was extended with `b2b_cash` and `profit` entries after the test was written. The test asserts an exact key set rather than a subset.
**Fix difficulty:** small — add `*args, **kwargs` to `FakeDB.execute`; update the assertion to either include the new keys or switch from `==` to `>=` (subset check).

---

### tests/test_dashboard_summary_shape.py::test_recent_activity_sorted_desc

**Category:** A — Stale mock signature
**Error:** `TypeError: test_recent_activity_sorted_desc.<locals>.fake_panels() takes 2 positional arguments but 3 were given` at `dashboard_summary_service.py:1450`; then `IndexError: list index out of range` because the panels never populate.
**Failing line:** `tests/test_dashboard_summary_shape.py:164`
**Likely cause:** `_build_panels` now takes a third argument (likely `user` or a viewer-permissions object). The test's `fake_panels(_db, _rng)` stub declares only two parameters, causing the call to blow up and leaving `recent_activity` empty.
**Fix difficulty:** trivial — add the third parameter (`_user` or `_viewer`) to `fake_panels`.

---

### tests/test_expense_service.py::test_list_expenses_returns_expenses_with_no_filters

**Category:** A — Stale mock signature
**Error:** `AttributeError: 'types.SimpleNamespace' object has no attribute 'animal_group_id'` at `expense_service.py:454`
**Failing line:** `tests/test_expense_service.py:103`
**Likely cause:** `list_expenses` was extended to include animal-module fields (`animal_group_id`, `animal_group_name`, `is_animal_expense`) in the returned dict for each expense. The test's `SimpleNamespace` stub was never updated to include those attributes, so attribute access on the stub raises `AttributeError`.
**Fix difficulty:** trivial — add `animal_group_id=None, animal_group=None, is_animal_expense=False` to the `SimpleNamespace(...)` stub (~line 86), and add the three new keys to the expected-result dict (~line 105).

---

### tests/test_expenses_frontend.py::test_expenses_loaders_check_status_and_log_debug_shapes

**Category:** C — Drifted response shape
**Error:** `AssertionError: assert 'Farm list unavailable' in '...source...'`
**Failing line:** `tests/test_expenses_frontend.py:62`
**Likely cause:** The farm-dropdown error message in `app/routers/expenses.py` was changed. The current source shows `showToast("Failed to load farms", "err")` (plus a `console.error`), not the string `"Farm list unavailable"` that the test checks for.
**Fix difficulty:** trivial — update the assertion string from `"Farm list unavailable"` to `"Failed to load farms"` (or align both to the same string).

---

### tests/test_hr_clear_data.py::test_clear_hr_data_deletes_only_hr_scope

**Category:** C — Drifted response shape
**Error:** `AssertionError: assert {'deleted': {'attendance': 2, 'employees': 1, 'hr_expenses': 1, 'loan_repayments': 0, ...}} == {'deleted': {'attendance': 2, 'employees': 1, 'hr_expenses': 1, 'payroll': 1}}`
**Failing line:** `tests/test_hr_clear_data.py:236`
**Likely cause:** The loan management feature was added to HR; `_clear_hr_data` was extended to also delete `EmployeeLoan`, `EmployeeLoanRepayment`, and `EmployeePayrollDeduction` records, adding three new keys to the `deleted` dict: `loans`, `loan_repayments`, `payroll_deductions`. The test's expected dict was never updated and uses an exact equality check.
**Fix difficulty:** small — update the expected dict to include the three new keys with their expected counts. Since this test fixture seeds no loan records, the counts should be 0 for all three. While there: confirm the exact full set of keys in `_clear_hr_data`'s returned dict.

---

### tests/test_hr_farm_salary_expense_integration.py::test_employee_create_without_farm_returns_unassigned_payload

**Category:** E — Test setup / fixture problem
**Error:** `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: attendance` at `hr.py:222` via `AsyncSessionAdapter.execute`
**Failing line:** `tests/test_hr_farm_salary_expense_integration.py:88`
**Likely cause:** The HR router's `add_employee` handler was updated to call `_backfill_attendance(db, employee_id, hire_date, date.today())` immediately after creating a new employee. This function queries the `attendance` table. The test uses an in-memory SQLite fixture (`make_session()` at line 48–65) that only creates 11 specific tables via `Base.metadata.create_all(tables=[...])` — `Attendance.__table__` is not in that list, so the table doesn't exist.
**Fix difficulty:** small — import `Attendance` (from `app.models.hr`) and add `Attendance.__table__` to the `tables=[...]` list in `make_session()`.

---

### tests/test_permission_lockdown.py::test_product_create_requires_explicit_permission

**Category:** A — Stale mock signature
**Error:** `AttributeError: 'FakeSession' object has no attribute 'execute'` at `app/routers/products.py:131`
**Failing line:** 403 check never reached; crashes during request handling
**Likely cause:** The `FakeSession` in this file only implements `add`, `commit`, and `rollback`. After the lockdown was added, `products.py` (and the production routers) now calls `db.execute(...)` in the request body before the permission check can fire (or the permission infrastructure itself queries the DB), so the test client receives a 500 instead of the expected 403.
**Fix difficulty:** trivial — add a no-op `async def execute(self, *args, **kwargs): return None` and `async def flush(self): return None` to `FakeSession`. The test only cares about the 403 audit row, not about query results.

---

### tests/test_permission_lockdown.py::test_production_create_recipe_requires_explicit_permission

**Category:** A — Stale mock signature
**Error:** `AttributeError: 'FakeSession' object has no attribute 'flush'` at `app/routers/production.py:90`
**Failing line:** 403 check never reached
**Likely cause:** Same `FakeSession` missing `flush`; `production.py` route calls `db.flush()` before the permission guard.
**Fix difficulty:** trivial — same fix as above.

---

### tests/test_permission_lockdown.py::test_production_create_batch_requires_explicit_permission

**Category:** A — Stale mock signature
**Error:** `AttributeError: 'FakeSession' object has no attribute 'execute'` at `app/routers/production.py:148`
**Failing line:** 403 check never reached
**Likely cause:** Same root — FakeSession missing `execute`.
**Fix difficulty:** trivial.

---

### tests/test_permission_lockdown.py::test_production_delete_batch_requires_explicit_permission

**Category:** A — Stale mock signature
**Error:** `AttributeError: 'FakeSession' object has no attribute 'execute'` at `app/routers/production.py:285`
**Failing line:** 403 check never reached
**Likely cause:** Same root.
**Fix difficulty:** trivial.

---

### tests/test_permission_lockdown.py::test_production_log_spoilage_requires_explicit_permission

**Category:** A — Stale mock signature
**Error:** `AttributeError: 'FakeSession' object has no attribute 'execute'` at `app/routers/production.py:347`
**Failing line:** 403 check never reached
**Likely cause:** Same root.
**Fix difficulty:** trivial.

---

### tests/test_pos_price_edit.py::test_audit_log_contains_all_edited_lines

**Category:** D — Drifted business logic
**Error:** `fastapi.exceptions.HTTPException: 404: Product not found: SKU-B`
**Failing line:** `tests/test_pos_price_edit.py:241` → `pos_service.py:85`
**Likely cause:** `pos_service.create_invoice` was refactored from per-item product lookups (one `db.execute` call per SKU) to a single bulk load (one `db.execute` for all active products, then a dict lookup). The test was written for the old per-item pattern: `extra_results=[[prod_a], [prod_b], [prod_c]]` — three separate single-element responses. Under the bulk-load code, only the first `execute()` fires and returns `FakeScalarResult([prod_a])`. The `product_map` contains only SKU-A. Lookup for SKU-B returns `None` → 404.
**Fix difficulty:** trivial — change `extra_results=[[prod_a], [prod_b], [prod_c]]` to `extra_results=[[prod_a, prod_b, prod_c]]` (all three products in a single list, matching the bulk-load response shape).

---

### tests/test_receive_products.py::test_create_receipt_with_cost_creates_expense_and_journal

**Category:** A — Stale mock signature
**Error:** `TypeError: unsupported operand type(s) for +: 'Account' and 'int'` at `receive_service.py:148` (inside `_next_expense_ref`)
**Failing line:** `tests/test_receive_products.py:273`
**Likely cause:** `_create_receipt_core` now calls `ensure_default_stock_location(db)` and `get_or_create_location_stock(db, ...)` for the new multi-location storage feature. These new calls appear before `_post_receipt_expense` in the code, consuming extra slots from `FakeReceiveSession`'s response queue. By the time `_next_expense_ref` fires its `select(max(Expense.id))` query, the queue has shifted and returns `FakeScalarResult(exp_acc)` (an Account object) instead of `FakeScalarResult(None)`. Since `exp_acc` is truthy, `scalar() or 0` evaluates to `exp_acc`, and then `exp_acc + 1` → TypeError.
**Fix difficulty:** small — trace through the current `_create_receipt_core` and all helpers to count all `execute()` calls in order, then insert appropriate `FakeScalarResult` objects for the new location queries at the correct queue positions.

---

### tests/test_receive_products.py::test_create_receipt_links_expense_to_receipt

**Category:** A — Stale mock signature
**Error:** `TypeError: unsupported operand type(s) for +: 'Account' and 'int'` at `receive_service.py:148`
**Failing line:** `tests/test_receive_products.py:318`
**Likely cause:** Identical root cause as the test above — misaligned response queue due to new location-management execute calls.
**Fix difficulty:** small.

---

### tests/test_receive_products.py::test_batch_receive_two_products

**Category:** A — Stale mock signature
**Error:** `AttributeError: 'int' object has no attribute 'qty'` at `receive_service.py:664` (`loc_stock.qty = ...`)
**Failing line:** `tests/test_receive_products.py:388`
**Likely cause:** `get_or_create_location_stock` is consuming an out-of-order queue slot and returning a raw `int` value. Then `loc_stock.qty` attribute access fails because `int` has no `qty`. Same queue-offset root cause as the preceding tests.
**Fix difficulty:** small.

---

### tests/test_receive_products.py::test_batch_receive_single_commit_even_with_cost

**Category:** A — Stale mock signature
**Error:** `TypeError: unsupported operand type(s) for +: 'Account' and 'int'` at `receive_service.py:148`
**Failing line:** `tests/test_receive_products.py:419`
**Likely cause:** Same queue-offset root cause.
**Fix difficulty:** small.

---

### tests/test_receive_products.py::test_update_receipt_updates_stock_move_and_receipt_fields

**Category:** A — Stale mock signature (secondary D issue)
**Error:** `fastapi.exceptions.HTTPException: 422: Product Type is required` at `receive_service.py:462`
**Failing line:** `tests/test_receive_products.py:464`
**Likely cause:** `_sync_receipt_expense` now requires a non-null `product_type` when creating a new expense for an updated receipt (the `receipt.expense_id is None` code path, line ~461–462). The test's `ReceiptUpdate` payload doesn't include `product_type`, so the new validation guard fires before the test's main assertion logic is reached.
**Fix difficulty:** small — add `product_type="produce"` (or whichever value is valid) to the `ReceiptUpdate(...)` call in the test.

---

### tests/test_users_auth_flow.py::test_log_action_uses_optional_shared_auth_dependency

**Category:** F — Test references deleted/renamed code
**Error:** `AssertionError: assert 404 == 200` (HTTP status)
**Failing line:** `tests/test_users_auth_flow.py:246`
**Likely cause:** The test POSTs to `/users/api/log` (singular). That endpoint does not exist in `app/routers/users.py` — the only matching route is `GET /users/api/logs` (plural, read-only). The POST endpoint for client-side activity logging was removed or relocated to a different router or path.
**Fix difficulty:** small — investigate whether the endpoint was intentionally removed (in which case the test covers dead functionality and the entire test may need replacing), renamed, or moved to another router. If it was deleted and no replacement exists, this may indicate a gap in production activity-logging coverage worth flagging.

---

## Recommended fix order

### Easiest first (trivial — 5–20 min per item)

1. **`test_permission_lockdown.py` (5 tests)** — Add `async def execute(self, *args, **kwargs): return None` and `async def flush(self): return None` to the shared `FakeSession` class in that file. One edit fixes all five.

2. **`test_expense_service.py::test_list_expenses_returns_expenses_with_no_filters`** — Add three missing fields to the `SimpleNamespace` stub and to the expected-result dict. One-function edit.

3. **`test_dashboard_summary_shape.py::test_recent_activity_sorted_desc`** — Add a third parameter to the `fake_panels` inner function.

4. **`test_expenses_frontend.py::test_expenses_loaders_check_status_and_log_debug_shapes`** — Change one assertion string.

5. **`test_pos_price_edit.py::test_audit_log_contains_all_edited_lines`** — Change `extra_results=[[prod_a], [prod_b], [prod_c]]` to `extra_results=[[prod_a, prod_b, prod_c]]`.

### Fix next (small — 30–90 min each)

6. **`test_b2b_sales_import.py` (all 12 tests)** — One-line production-code fix in `app/services/b2b_shared.py:51`: change `account_type="liability"` → `type="liability"`. Verify no other `Account(account_type=` usages exist before merging. Fixes 12 tests at once with zero test-file changes.

7. **`test_hr_farm_salary_expense_integration.py`** — Add `Attendance.__table__` to the `create_all` call inside `make_session()`.

8. **`test_hr_clear_data.py::test_clear_hr_data_deletes_only_hr_scope`** — Update expected dict to include `loans: 0, loan_repayments: 0, payroll_deductions: 0`.

9. **`test_auth_endpoints.py::test_login_page_renders_valid_escaped_newline_checks`** — Restore the `url.indexOf("\\r")` guard to the login template, or update the assertion.

10. **`test_accounting_journal_filters.py` (2 tests)** — Pin timezone or update the expected UTC strings.

### Most painful (2–4 hours, careful review needed)

11. **`test_receive_products.py` (5 tests)** — Each test's `FakeReceiveSession` response queue needs to be re-mapped against the current `_create_receipt_core` call sequence. New calls for `ensure_default_stock_location` and `get_or_create_location_stock` must be inserted. Also requires a separate `product_type` fix for the update test.

12. **`test_dashboard_summary_shape.py::test_numbers_contains_expected_additive_entries`** — Two independent issues: fix `FakeDB.execute` signature, then audit which keys are always/conditionally present in `numbers` before updating the assertion.

13. **`test_users_auth_flow.py::test_log_action_uses_optional_shared_auth_dependency`** — Requires investigation to determine whether the `/users/api/log` POST was intentionally removed or relocated. May reveal a real production gap in activity logging.
