from types import SimpleNamespace

from tests.env_defaults import apply_test_environment_defaults

apply_test_environment_defaults()

from app.routers.receive import receive_ui


def test_receive_page_labels_recent_received_products_and_gates_edit_actions() -> None:
    user = SimpleNamespace(
        id=1,
        name="Manager",
        email="manager@example.com",
        role="manager",
        permissions="",
        is_active=True,
    )

    html = receive_ui(user)

    assert "Recent Received Products" in html
    assert "action_receive_products_update" in html
    assert "openEditModal" in html
    assert "No action permission" in html
