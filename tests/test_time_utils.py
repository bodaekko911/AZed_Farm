from datetime import datetime, timezone

from app.core.time_utils import format_app_datetime


def test_format_app_datetime_converts_utc_to_configured_app_timezone() -> None:
    value = datetime(2026, 5, 16, 10, 30, tzinfo=timezone.utc)

    assert format_app_datetime(value) == "2026-05-16 13:30:00"


def test_format_app_datetime_treats_naive_database_values_as_utc() -> None:
    value = datetime(2026, 5, 16, 10, 30)

    assert format_app_datetime(value) == "2026-05-16 13:30:00"


def test_format_app_datetime_handles_missing_values() -> None:
    assert format_app_datetime(None) == "—"
