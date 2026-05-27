import logging

from app.routers import (
    accounting,
    animals,
    audit_log,
    auth,
    b2b,
    carbon,
    customers,
    dashboard,
    drying,
    expenses_refactored,
    farm,
    home,
    hr,
    import_data,
    inventory,
    pos,
    production,
    products,
    receive,
    refunds,
    reports,
    suppliers,
    users,
)

_log = logging.getLogger(__name__)

# farm_dashboard is loaded defensively. If the file is missing on a partial
# deployment, the app should still boot — the rest of the system stays usable
# and only the Farm dashboard surface goes dark until the file lands.
try:
    from app.routers import farm_dashboard  # type: ignore
    _farm_dashboard_router = farm_dashboard.router
except Exception as exc:  # noqa: BLE001
    _log.warning(
        "Farm dashboard router could not be loaded (%s: %s). "
        "App will start without it. Ensure app/routers/farm_dashboard.py is deployed.",
        type(exc).__name__, exc,
    )
    _farm_dashboard_router = None

_base_routers = (
    auth.router,
    home.router,
    pos.router,
    import_data.router,
    dashboard.router,
    products.router,
    customers.router,
    receive.router,
    suppliers.router,
    inventory.router,
    hr.router,
    accounting.router,
    production.router,
    drying.router,
    b2b.router,
    farm.router,
    animals.router,
    carbon.router,
    reports.router,
    users.router,
    refunds.router,
    expenses_refactored.router,
    audit_log.router,
)

if _farm_dashboard_router is not None:
    # Slot the farm dashboard right after the sales dashboard for nav parity.
    _ordered = []
    for r in _base_routers:
        _ordered.append(r)
        if r is dashboard.router:
            _ordered.append(_farm_dashboard_router)
    ROUTERS = tuple(_ordered)
else:
    ROUTERS = _base_routers

__all__ = ["ROUTERS"]