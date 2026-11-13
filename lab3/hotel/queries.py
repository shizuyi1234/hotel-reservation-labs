"""Scoped order browsing, management statistics and audit queries."""

from datetime import timedelta

from .domain import BusinessError, STATUS_LABELS, parse_day, timestamp


class Queries:
    def __init__(self, store, accounts, orders, clock):
        self.store = store
        self.accounts = accounts
        self.orders = orders
        self.clock = clock

    def _scope(self, actor, hotel, customer=False):
        if actor["role"] == "guest":
            if customer:
                return "o.customer_id=?", [actor["id"]]
            raise BusinessError("forbidden", "该角色无权查看经营数据")
        if hotel is not None:
            self.accounts.require_hotel(actor, hotel)
            return "o.hotel_id=?", [hotel]
        if actor["role"] == "staff":
            return (
                "EXISTS(SELECT 1 FROM grants g WHERE g.user_id=? AND g.hotel_id=o.hotel_id)",
                [actor["id"]],
            )
        return "1=1", []

    @staticmethod
    def _date_range(first, end, label):
        if first is not None:
            parse_day(first)
        if end is not None:
            parse_day(end)
        if first is not None and end is not None and first >= end:
            raise BusinessError("date", f"{label}的结束日期须晚于开始日期")

    @staticmethod
    def _add(clauses, values, condition, value):
        if value is not None:
            clauses.append(condition)
            values.append(value)

    def list_orders(
        self, token, hotel=None, order_id=None, room=None, status=None,
        created_from=None, created_to=None, stay_from=None, stay_to=None,
    ):
        self._date_range(created_from, created_to, "创建时间")
        self._date_range(stay_from, stay_to, "住宿日期")
        if status is not None and status not in STATUS_LABELS:
            raise BusinessError("invalid", "订单状态无效")
        self.orders.tick(token)
        with self.store.transaction(write=False):
            actor = self.accounts.require(token)
            scope, values = self._scope(actor, hotel, customer=True)
            clauses = [scope]
            if actor["role"] == "guest":
                self._add(clauses, values, "o.hotel_id=?", hotel)
            filters = (
                ("o.id=?", order_id),
                ("o.room_id=?", room),
                ("o.status=?", status),
                ("o.created_at>=?", created_from),
                ("o.created_at<?", created_to),
                ("o.departure>?", stay_from),
                ("o.arrival<?", stay_to),
            )
            for condition, value in filters:
                self._add(clauses, values, condition, value)
            where = " AND ".join(clauses)
            return [dict(row) for row in self.store.all(
                f"""SELECT o.id,o.hotel_id,o.hotel_name,o.room_id,o.room_name,
                           o.arrival,o.departure,o.quantity,o.status,o.version,
                           o.total,o.created_at,o.deadline
                    FROM orders o WHERE {where} ORDER BY o.created_at DESC,o.id DESC""",
                values,
            )]

    def statistics(self, token, first, end, hotel=None, room=None, include_rows=False):
        self._date_range(first, end, "统计日期")
        self.accounts.require(token, "admin", "staff")
        self.orders.tick(token)
        with self.store.transaction(write=False):
            actor = self.accounts.require(token, "admin", "staff")
            scope, values = self._scope(actor, hotel)
            event_rows = self.store.all(
                f"""SELECT e.order_id,e.room_id,e.at,e.action FROM events e
                    JOIN orders o ON o.id=e.order_id
                    WHERE {scope} AND (? IS NULL OR e.room_id=?)
                      AND e.at>=? AND e.at<? AND e.action IN ('book','cancel','no_show')
                    ORDER BY e.at,e.id""",
                (*values, room, room, first, end),
            )
            night_rows = self.store.all(
                f"""SELECT n.order_id,n.room_id,n.day,n.quantity,n.price,n.usage
                    FROM nights n JOIN orders o ON o.id=n.order_id
                    WHERE {scope} AND (? IS NULL OR n.room_id=?)
                      AND n.day>=? AND n.day<? AND n.usage IN ('reserved','used')
                    ORDER BY n.day,n.order_id""",
                (*values, room, room, first, end),
            )
            report = {
                "new_orders": sum(row["action"] == "book" for row in event_rows),
                "cancelled": sum(row["action"] != "book" for row in event_rows),
                "reserved_nights": sum(
                    row["quantity"] for row in night_rows if row["usage"] == "reserved"
                ),
                "used_nights": sum(
                    row["quantity"] for row in night_rows if row["usage"] == "used"
                ),
                "estimated_revenue": sum(
                    row["quantity"] * row["price"] for row in night_rows
                ),
            }
            if include_rows:
                report["events"] = [dict(row) for row in event_rows]
                report["nights"] = [dict(row) for row in night_rows]
            return report

    def audit(
        self, token, hotel=None, actor_id=None, target=None, result=None,
        first=None, end=None,
    ):
        self._date_range(first, end, "日志日期")
        with self.store.transaction():
            actor = self.accounts.require(token, "admin", "staff")
            scope, values = self._scope(actor, hotel)
            self.prune()
            filters = (
                ("o.actor_id=?", actor_id),
                ("o.target=?", target),
                ("o.result=?", result),
                ("o.at>=?", first),
                ("o.at<?", end),
            )
            clauses = [scope]
            for condition, value in filters:
                self._add(clauses, values, condition, value)
            where = " AND ".join(clauses)
            return [dict(row) for row in self.store.all(
                f"SELECT o.* FROM audit o WHERE {where} ORDER BY o.at DESC,o.id DESC",
                values,
            )]

    def prune(self):
        cutoff = timestamp(self.clock() - timedelta(days=180))
        self.store.execute("DELETE FROM audit WHERE at<?", (cutoff,))
