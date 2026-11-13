"""Wire the application services and seed a fresh demonstration database."""

from datetime import timedelta

from .accounts import Accounts
from .catalog import Catalog
from .domain import current_time
from .orders import Orders
from .queries import Queries
from .search import Search
from .storage import Store

DEMO_PASSWORD = "demo-hotel-2026"


class Application:
    def __init__(self, path, clock=current_time):
        self.clock = clock
        self.store = Store(path)
        self.accounts = Accounts(self.store, clock)
        self.catalog = Catalog(self.store, self.accounts, clock)
        self.search = Search(self.store, self.catalog, clock)
        self.orders = Orders(self.store, self.accounts, self.catalog, self.search, clock)
        self.queries = Queries(self.store, self.accounts, self.orders, clock)
        with self.store.transaction():
            self.queries.prune()

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()


def seed(app):
    """Seed documented demo users, hotel rooms and fourteen bookable days."""
    app.accounts.bootstrap("admin", DEMO_PASSWORD)
    app.accounts.register("frontdesk", DEMO_PASSWORD)
    app.accounts.register("customer", DEMO_PASSWORD)
    admin = app.accounts.login("admin", DEMO_PASSWORD)
    hotel = app.catalog.save_hotel(
        admin, "栖程酒店", "南京", "示例路 18 号", "025-00000000"
    )
    app.accounts.assign(admin, "frontdesk", "staff", hotel)
    staff = app.accounts.login("frontdesk", DEMO_PASSWORD)
    rooms = [
        app.catalog.save_room(staff, hotel, "双床房", "两张单人床", 2),
        app.catalog.save_room(staff, hotel, "家庭房", "一张双人床和一张单人床", 3),
    ]
    first = app.clock().date()
    for offset in range(14):
        day = (first + timedelta(days=offset)).isoformat()
        for room, price, total in zip(rooms, (26000, 42000), (5, 3)):
            surcharge = 4000 if (first.weekday() + offset) % 7 in (4, 5) else 0
            app.catalog.save_inventory(staff, room, day, total, price + surcharge)
    app.accounts.logout(admin)
    app.accounts.logout(staff)
    return {"hotel_id": hotel, "room_ids": rooms, "first_day": first.isoformat()}
