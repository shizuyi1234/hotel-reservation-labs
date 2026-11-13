"""Run a complete reservation on a disposable SQLite database."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from hotel.app import DEMO_PASSWORD, Application, seed
from hotel.domain import BEIJING, Stay


def show(label, result):
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(result)
    print(label, result)


def main():
    now = datetime(2026, 11, 13, 10, tzinfo=BEIJING)
    with TemporaryDirectory() as temp:
        with Application(Path(temp) / "demo.db", lambda: now) as app:
            sample = seed(app)
            customer = app.accounts.login("customer", DEMO_PASSWORD)
            staff = app.accounts.login("frontdesk", DEMO_PASSWORD)
            stay = Stay("2026-11-14", "2026-11-16", 3, 2)
            quotes = app.search.find("南京", stay)
            show("搜索结果", [(q["room_name"], q["total"]) for q in quotes])
            room = sample["room_ids"][0]
            quote = app.orders.quote(customer, room, stay)
            show("逐晚报价（分）", quote["nights"])
            order = app.orders.book(customer, room, stay, quote["token"], "demo-book")
            show("提交", order)
            retry = app.orders.book(customer, room, stay, quote["token"], "demo-book")
            assert order == retry
            order_id = order["order_id"]
            confirmed = app.orders.staff_action(
                staff, order_id, order["version"], "confirm", "demo-confirm"
            )
            show("确认", confirmed)
            now = datetime(2026, 11, 14, 14, tzinfo=BEIJING)
            checked_in = app.orders.staff_action(
                staff, order_id, confirmed["version"], "check_in", "demo-checkin"
            )
            show("入住", checked_in)
            now = datetime(2026, 11, 15, 10, tzinfo=BEIJING)
            checked_out = app.orders.staff_action(
                staff, order_id, checked_in["version"], "check_out", "demo-checkout"
            )
            show("提前退房", checked_out)
            detail = app.orders.detail(customer, order_id)
            show("库存明细", [(row["day"], row["usage"]) for row in detail["nights"]])
            show("统计", app.queries.statistics(staff, "2026-11-13", "2026-11-17"))


if __name__ == "__main__":
    main()
