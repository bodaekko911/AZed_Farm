"""Every tab on the Animals page is its own permission.

A user can be given the feeding log without the mortality log, and so on. A
tab without permission has no button, and its data API refuses — hiding the
button alone would leave the data one URL away.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from app.core.permission_catalog import get_role_permissions, is_known_permission
from tests.test_permission_enforcement import _make_client

TAB_KEYS = ("tab_animals_groups", "tab_animals_feedings",
            "tab_animals_deaths", "tab_animals_receive")


def _user(perms: str, role: str = "viewer"):
    return SimpleNamespace(id=5, name="Farm Hand", email="f@x.com", role=role,
                           permissions=perms, is_active=True)


def _tabs_shown(html: str) -> list[str]:
    return [t for t in ("groups", "feedings", "deaths", "receive", "analyze")
            if f'id="tab-{t}"' in html]


def test_the_tab_permissions_exist_and_the_manager_has_them():
    for key in TAB_KEYS:
        assert is_known_permission(key)
        assert key in get_role_permissions("manager")


def test_only_the_granted_tabs_are_shown():
    client, _ = _make_client(_user("page_animals,tab_animals_feedings,tab_animals_receive"))
    resp = client.get("/animals/")
    assert resp.status_code == 200
    assert _tabs_shown(resp.text) == ["feedings", "receive"]
    # The first tab the user has is where the page opens.
    assert 'const FIRST_TAB = "feedings";' in resp.text


def test_analyze_follows_the_cost_analysis_permission():
    client, _ = _make_client(_user("page_animals,action_animals_analyze"))
    assert _tabs_shown(client.get("/animals/").text) == ["analyze"]


def test_no_tabs_says_so():
    client, _ = _make_client(_user("page_animals"))
    html = client.get("/animals/").text
    assert _tabs_shown(html) == []
    assert "not been given any Animals tab" in html


def test_admin_sees_every_tab():
    client, _ = _make_client(_user("", role="admin"))
    assert _tabs_shown(client.get("/animals/").text) == [
        "groups", "feedings", "deaths", "receive", "analyze"]


def test_a_hidden_tab_s_data_is_refused():
    client, _ = _make_client(_user("page_animals,tab_animals_groups"))
    for path in ("/animals/api/feedings", "/animals/api/deaths", "/animals/api/intakes"):
        assert client.get(path).status_code == 403, path


def _migration():
    path = next(Path("alembic/versions").glob("*_0057_animal_tab_permissions.py"))
    spec = importlib.util.spec_from_file_location("m0057", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_keeps_existing_animals_users_on_every_tab():
    m = _migration()
    out = set(m._with_tabs("page_animals,action_animal_feeding_create").split(","))
    assert set(TAB_KEYS) <= out
    # A tab somebody already took away stays taken away.
    out = set(m._with_tabs("page_animals,-tab_animals_deaths").split(","))
    assert "tab_animals_deaths" not in out and "-tab_animals_deaths" in out
    # Users without Animals are left exactly as they were.
    assert m._with_tabs("page_pos,action_pos_create_sale") == "page_pos,action_pos_create_sale"
    assert m._with_tabs(None) is None
