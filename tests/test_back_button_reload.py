"""Going Back shows the page as it is now, not a stored copy.

Every HTML page is marked no-store, so Back fetches it again; a page the browser restores from its back/forward
memory anyway reloads itself (theme-init.js, loaded first on every working page). Scripts, styles and images keep
their caching, and a route that sets its own policy keeps it.
"""
from types import SimpleNamespace

from tests.test_permission_enforcement import _make_client

ADMIN = SimpleNamespace(id=1, name="Admin", email="a@x", role="admin", permissions="", is_active=True)


def test_pages_are_never_reused_from_the_cache():
    client, _ = _make_client(ADMIN)
    page = client.get("/animals/")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"


def test_static_files_keep_their_caching():
    client, _ = _make_client(ADMIN)
    js = client.get("/static/theme-init.js")
    assert js.status_code == 200 and "no-store" not in (js.headers.get("cache-control") or "")


def test_a_page_with_its_own_policy_keeps_it():
    client, _ = _make_client(ADMIN)
    pos = client.get("/pos")
    assert pos.headers["cache-control"].startswith("no-store, no-cache, must-revalidate")


def test_a_page_restored_from_back_forward_memory_reloads():
    src = open("app/static/theme-init.js", encoding="utf-8").read()
    head = src[:src.index("(function () {")]
    assert 'addEventListener("pageshow"' in head and "e.persisted" in head and "location.reload()" in head
