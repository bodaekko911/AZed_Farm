import re
from types import SimpleNamespace

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.routers.suppliers import suppliers_ui


def test_suppliers_page_keeps_backslash_escape_regex_valid_for_browser_js() -> None:
    user = SimpleNamespace(
        id=1,
        name="Admin",
        email="admin@example.com",
        role="admin",
        permissions="*",
        is_active=True,
    )

    html = suppliers_ui(user)
    scripts = "\n".join(
        re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL)
    )

    assert r'.replace(/\\/g, "\\\\")' in scripts
    assert ".replace(/\\/g" not in scripts
