"""Payslips — the same figures as the payroll run, printed or sent on WhatsApp."""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import unquote

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.database import Base
from app.models.farm import Farm
from app.models.hr import (
    Employee, EmployeeAllowanceAdvance, EmployeeLoan, EmployeeLoanRepayment, EmployeePayrollDeduction, Payroll,
)
from app.routers.hr import _load_payrolls, _payslip, _whatsapp_number, print_payslips


class AsyncSessionAdapter:
    def __init__(self, session):
        self.s = session

    async def execute(self, statement, params=None):
        return self.s.execute(statement, params or {})


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_db(*, paid=False, phone="01012345678"):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all([
        Farm(id=1, name="Organic Farm", is_active=1),
        Employee(id=1, name="Mahmoud Ali", phone=phone, position="Harvester", department="Field",
                 farm_id=1, base_salary=Decimal("6000"), food_allowance=Decimal("900"),
                 transportation_allowance=Decimal("300")),
        # September: 28 of 30 days worked → 5 600 earned, food 30 × 28 = 840.
        Payroll(id=10, employee_id=1, period="2026-09", base_salary=Decimal("5600"), bonuses=Decimal("250"),
                days_worked=28, working_days=30, day_deduction_days=Decimal("1"), day_deductions=Decimal("200"),
                manual_deductions=Decimal("100"), loan_deductions=Decimal("500"), deductions=Decimal("800"),
                net_salary=Decimal("6090"), paid=paid,
                paid_amount=Decimal("5690") if paid else None,
                days_off_credited=Decimal("2") if paid else 0,
                paid_at=datetime(2026, 10, 1, 10, tzinfo=timezone.utc) if paid else None),
        EmployeePayrollDeduction(employee_id=1, payroll_id=10, period="2026-09", type="day_deduction",
                                 days=Decimal("1"), amount=Decimal("200"), note="Absent without leave 14 Sep"),
        EmployeePayrollDeduction(employee_id=1, payroll_id=10, period="2026-09", type="manual",
                                 amount=Decimal("100"), note="Broken crate"),
        EmployeeLoan(id=1, employee_id=1, loan_date=date(2026, 8, 1), amount=Decimal("2000")),
        EmployeeLoanRepayment(loan_id=1, employee_id=1, payroll_id=10, repayment_date=date(2026, 9, 30),
                              amount=Decimal("500")),
        # Food advance paid early, recovered on this run.
        EmployeeAllowanceAdvance(employee_id=1, advance_date=date(2026, 9, 10), amount=Decimal("100"),
                                 status="deducted", payroll_id=10),
    ])
    session.commit()
    return AsyncSessionAdapter(session)


def slip(db):
    return run(_payslip(db, run(_load_payrolls(db, payroll_id=10))[0]))


def test_payslip_breaks_the_net_down_into_what_was_earned_and_deducted():
    s = slip(make_db())

    assert [(e["label_ar"], e["amount"]) for e in s["earnings"]] == [
        ("الراتب عن أيام العمل", 5600.0), ("بدل طعام", 840.0), ("بدل مواصلات", 300.0), ("مكافأة", 250.0),
    ]
    assert [(d["label"], d["amount"], d["note"]) for d in s["deductions"]] == [
        ("Allowance advance recovered", 100.0, ""),
        ("Day deductions (1 day)", 200.0, "Absent without leave 14 Sep"),
        ("Other deductions", 100.0, "Broken crate"),
        ("Loan repayment", 500.0, ""),
    ]
    assert s["gross"] - s["total_deductions"] == s["net"] == 6090.0
    assert s["loan_balance"] == 1500.0           # 2 000 lent − 500 repaid by end of September
    assert s["period_ar"] == "سبتمبر 2026"
    assert s["paid"] is False


def test_a_partly_paid_slip_shows_cash_paid_and_the_days_off_given_for_the_rest():
    s = slip(make_db(paid=True))
    assert s["net"] == 6090.0
    assert (s["paid_amount"], s["unpaid_remainder"], s["days_off_credited"]) == (5690.0, 400.0, 2.0)
    assert s["paid_on"] == "2026-10-01"
    assert "تم الصرف: 5,690.00" in s["whatsapp_text"]


def test_whatsapp_link_goes_to_the_employees_phone_with_the_payslip_in_arabic():
    s = slip(make_db())
    assert s["whatsapp_url"].startswith("https://wa.me/201012345678?text=")
    text = unquote(s["whatsapp_url"].split("text=", 1)[1])
    assert text.startswith("قسيمة راتب — سبتمبر 2026")
    assert "صافي الراتب: 6,090.00 ج.م" in text
    assert "خصم أيام (1 يوم) (Absent without leave 14 Sep): -200.00" in text
    assert "رصيد السلفة المتبقي: 1,500.00 ج.م" in text


def test_no_usable_phone_means_no_link_but_the_text_is_still_there():
    s = slip(make_db(phone=""))
    assert s["whatsapp_url"] is None
    assert s["whatsapp_text"]


def test_egyptian_numbers_are_turned_into_whatsapp_format():
    assert _whatsapp_number("010 1234 5678") == "201012345678"
    assert _whatsapp_number("+20 101 234 5678") == "201012345678"
    assert _whatsapp_number("00201012345678") == "201012345678"
    assert _whatsapp_number("1012345678") == "201012345678"
    assert _whatsapp_number("123") is None
    assert _whatsapp_number(None) is None


def test_the_printed_page_has_one_payslip_per_employee():
    db = make_db()
    request = SimpleNamespace(scope={"type": "http"}, url=SimpleNamespace(path="/"))
    from starlette.requests import Request
    response = run(print_payslips(Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                                           "query_string": b""}), period="2026-09", db=db))
    html = response.body.decode()
    assert html.count('class="slip"') == 1
    assert "Mahmoud Ali" in html and "قسيمة راتب" in html
    assert "6,090.00 EGP" in html
    assert "Broken crate" in html
