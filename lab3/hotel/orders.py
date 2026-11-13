"""Reservation lifecycle, atomic writes, and idempotent commands."""

import hmac
import json
from dataclasses import asdict
from datetime import timedelta

from .domain import (
    ACTIVE_BOOKINGS, TRANSITIONS, BusinessError, boundary, fingerprint,
    integer, required, timestamp,
)


class Orders:
    def __init__(self, store, accounts, catalog, search, clock):
        self.store = store
        self.accounts = accounts
        self.catalog = catalog
        self.search = search
        self.clock = clock

    def _order(self, actor, order_id):
        order = self.store.one("SELECT * FROM orders WHERE id=?", (order_id,))
        allowed = order is not None
        if allowed and actor["role"] == "guest":
            allowed = order["customer_id"] == actor["id"]
        if allowed and actor["role"] == "staff":
            try:
                self.accounts.require_hotel(actor, order["hotel_id"])
            except BusinessError:
                allowed = False
        if not allowed:
            raise BusinessError("not_found", "未找到可访问的订单")
        return order

    def _due(self, now):
        at = timestamp(now)
        due = self.store.all(
            """SELECT * FROM orders WHERE (status='pending' AND deadline<=?)
               OR (status='confirmed' AND departure || 'T12:00:00.000000+08:00'<=?)
               ORDER BY id""",
            (at, at),
        )
        for order in due:
            if order["status"] == "pending":
                action, status, reason = "expire", "expired", "超过酒店确认截止"
            else:
                action, status, reason = "no_show", "cancelled", "计划退房日 12:00 仍未到店"
            self._release(order)
            self._transition(order, None, at, action, status, reason)
        return len(due)

    def tick(self, token):
        with self.store.transaction():
            self.accounts.require(token)
            return self._due(self.clock())

    def _event(self, order_id, actor, hotel, room, at, action, before, after, reason):
        self.store.execute(
            """INSERT INTO events(order_id,actor_id,at,action,before_status,
                                  after_status,reason,room_id) VALUES(?,?,?,?,?,?,?,?)""",
            (order_id, actor, at, action, before, after, reason, room),
        )
        self.store.log(
            actor, hotel, at, action, str(order_id),
            detail=reason, before=before, after=after,
        )

    def _transition(self, order, actor, at, action, after, reason=""):
        before = order["status"]
        if after not in TRANSITIONS.get(before, ()):
            raise BusinessError("state", f"当前状态 {before} 不允许 {action}")
        self.store.execute(
            "UPDATE orders SET status=?,version=version+1 WHERE id=?",
            (after, order["id"]),
        )
        self._event(
            order["id"], actor, order["hotel_id"], order["room_id"],
            at, action, before, after, reason,
        )
        if after in ("expired", "rejected", "cancelled"):
            self.store.execute(
                """UPDATE cases SET resolved_at=?,decision=?
                   WHERE order_id=? AND resolved_at IS NULL""",
                (at, reason or "订单已结束", order["id"]),
            )

    def _release(self, order):
        self.store.execute(
            """UPDATE nights SET usage='released'
               WHERE order_id=? AND revision=? AND usage='reserved'""",
            (order["id"], order["revision"]),
        )

    def _save_nights(self, order_id, revision, quote):
        self.store.many(
            """INSERT INTO nights(order_id,revision,room_id,day,quantity,price,usage)
               VALUES(?,?,?,?,?,?,'reserved')""",
            [
                (order_id, revision, quote.room_id, day, quote.stay.quantity, price)
                for day, price in quote.nights
            ],
        )

    def _existing(self, actor, quote, skip=None):
        row = self.store.one(
            """SELECT id FROM orders WHERE customer_id=? AND hotel_id=? AND room_id=?
               AND arrival=? AND departure=? AND status IN ('pending','confirmed')
               AND (? IS NULL OR id<>?) ORDER BY id LIMIT 1""",
            (actor, quote.hotel_id, quote.room_id, quote.stay.arrival,
             quote.stay.departure, skip, skip),
        )
        if row:
            raise BusinessError("duplicate_order", "存在相同预订", {"order_id": row["id"]})

    @staticmethod
    def _matches_quote(quote, quoted):
        if not isinstance(quoted, str) or not hmac.compare_digest(
            quote.data()["token"].encode(), quoted.encode()
        ):
            raise BusinessError("requote", "报价已变化，请重新确认", quote.data())

    def _command(self, token, role, operation, request_id, payload, perform):
        request_id = required(request_id, "请求标识", 128)
        digest = fingerprint(payload)
        with self.store.transaction():
            actor = self.accounts.require(token, role)
            now = self.clock()
            at = timestamp(now)
            key = (actor["id"], operation, request_id)
            old = self.store.one(
                "SELECT digest,response FROM receipts WHERE actor_id=? AND operation=? AND request_id=?",
                key,
            )
            if old:
                if old["digest"] != digest:
                    raise BusinessError("request_conflict", "同一请求标识已用于不同内容")
                return json.loads(old["response"])
            self._due(now)
            hotel, target = None, str(payload.get("order_id", payload.get("room_id", "")))
            order = None
            try:
                with self.store.savepoint():
                    if "order_id" in payload:
                        order = self._order(actor, payload["order_id"])
                        hotel = order["hotel_id"]
                    elif "room_id" in payload:
                        hotel = self.catalog.room(payload["room_id"])["hotel_id"]
                    value = perform(actor, now, order)
                response = {"ok": True, **value}
            except BusinessError as exc:
                response = exc.result()
                if operation == "modify" and exc.code == "requote":
                    response["details"]["difference"] = response["details"]["total"] - order["total"]
                    response["details"]["version"] = order["version"]
                status = order["status"] if order else None
                self.store.log(
                    actor["id"], hotel, at, operation, target, result=exc.code,
                    detail=str(exc), before=status, after=status,
                )
            self.store.execute(
                "INSERT INTO receipts(actor_id,operation,request_id,digest,response) VALUES(?,?,?,?,?)",
                (*key, digest, json.dumps(response, ensure_ascii=False)),
            )
            return response

    def quote(self, token, room_id, stay, order_id=None):
        with self.store.transaction(write=False):
            actor = self.accounts.require(token, "guest")
            now = self.clock()
            order = self._order(actor, order_id) if order_id is not None else None
            if order:
                self._customer_window(order, now)
                if stay.people != order["people"]:
                    raise BusinessError("invalid", "改订须保留原入住人数")
            result = self.search.quote(room_id, stay, now, order).data()
            if order:
                result["difference"] = result["total"] - order["total"]
                result["version"] = order["version"]
            return result

    @staticmethod
    def _summary(order):
        return {
            "order_id": order["id"],
            "status": order["status"],
            "version": order["version"],
            "total": order["total"],
            "deadline": order["deadline"],
        }

    def _updated(self, order_id):
        return self._summary(self.store.one("SELECT * FROM orders WHERE id=?", (order_id,)))

    def book(self, token, room_id, stay, quoted, request_id):
        def perform(actor, now, _order):
            quote = self.search.quote(room_id, stay, now)
            self._existing(actor["id"], quote)
            self._matches_quote(quote, quoted)
            at = timestamp(now)
            deadline = timestamp(now + timedelta(minutes=30))
            order_id = self.store.execute(
                """INSERT INTO orders(customer_id,hotel_id,room_id,hotel_name,room_name,
                        arrival,departure,people,quantity,total,status,created_at,deadline)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (actor["id"], quote.hotel_id, room_id, quote.hotel_name, quote.room_name,
                 stay.arrival, stay.departure, stay.people, stay.quantity,
                 quote.total, at, deadline),
            ).lastrowid
            self._save_nights(order_id, 1, quote)
            self._event(
                order_id, actor["id"], quote.hotel_id, room_id,
                at, "book", None, "pending", "",
            )
            return self._updated(order_id)

        payload = {"room_id": room_id, "stay": asdict(stay), "quote": quoted}
        return self._command(token, "guest", "book", request_id, payload, perform)

    @staticmethod
    def _version(order, version):
        integer(version, "订单版本")
        if order["version"] != version:
            raise BusinessError(
                "version", "订单版本已变化，请刷新订单",
                {"version": order["version"], "status": order["status"]},
            )

    @staticmethod
    def _customer_window(order, now):
        if order["status"] not in ACTIVE_BOOKINGS:
            raise BusinessError("state", "只有待确认或已确认订单可以改退")
        if order["status"] == "pending" and timestamp(now) >= order["deadline"]:
            raise BusinessError("expired", "订单已超过确认截止")
        if now >= boundary(order["arrival"]):
            raise BusinessError("cutoff", "入住日 00:00 起不可自行改退")

    def modify(self, token, order_id, version, room_id, stay, quoted, request_id):
        def perform(actor, now, order):
            self._version(order, version)
            self._customer_window(order, now)
            if stay.people != order["people"]:
                raise BusinessError("invalid", "改订须保留原入住人数")
            quote = self.search.quote(room_id, stay, now, order)
            self._existing(actor["id"], quote, order_id)
            self._matches_quote(quote, quoted)
            self._release(order)
            revision = order["revision"] + 1
            at = timestamp(now)
            self._save_nights(order_id, revision, quote)
            self.store.execute(
                """UPDATE orders SET room_id=?,hotel_name=?,room_name=?,arrival=?,
                       departure=?,people=?,quantity=?,total=?,revision=?,deadline=?
                   WHERE id=?""",
                (room_id, quote.hotel_name, quote.room_name, stay.arrival,
                 stay.departure, stay.people, stay.quantity, quote.total,
                 revision, timestamp(now + timedelta(minutes=30)), order_id),
            )
            self.store.execute(
                """UPDATE cases SET resolved_at=?,decision='已按新安排改订'
                   WHERE order_id=? AND resolved_at IS NULL""",
                (at, order_id),
            )
            updated = dict(order)
            updated["room_id"] = room_id
            self._transition(updated, actor["id"], at, "modify", "pending")
            return self._updated(order_id)

        payload = {
            "order_id": order_id, "version": version, "room_id": room_id,
            "stay": asdict(stay), "quote": quoted,
        }
        return self._command(token, "guest", "modify", request_id, payload, perform)

    def cancel(self, token, order_id, version, request_id):
        def perform(actor, now, order):
            self._version(order, version)
            self._customer_window(order, now)
            self._release(order)
            self._transition(order, actor["id"], timestamp(now), "cancel", "cancelled", "顾客取消")
            return self._updated(order_id)

        payload = {"order_id": order_id, "version": version}
        return self._command(token, "guest", "cancel", request_id, payload, perform)

    def _blocked(self, order):
        if self.catalog.blocked(
            order["hotel_id"], order["room_id"],
            order["arrival"], order["departure"], for_existing=True,
        ):
            raise BusinessError("closed", "订单涉及停售或停业，请先联系顾客处理")

    def staff_action(self, token, order_id, version, action, request_id, reason=""):
        states = {
            "confirm": ("pending", "confirmed"),
            "reject": ("pending", "rejected"),
            "check_in": ("confirmed", "staying"),
            "check_out": ("staying", "completed"),
        }
        if action not in states:
            raise BusinessError("invalid", "未知订单操作")

        def perform(actor, now, order):
            self._version(order, version)
            before, after = states[action]
            if order["status"] != before:
                raise BusinessError("state", "订单当前状态不允许此操作")
            at = timestamp(now)
            detail = ""
            if action in ("confirm", "check_in"):
                self._blocked(order)
            if action == "reject":
                detail = required(reason, "拒单原因", 300)
                self._release(order)
            if action == "check_in":
                if not boundary(order["arrival"], 14) <= now < boundary(order["departure"], 12):
                    raise BusinessError("window", "当前不在入住办理时间内")
                self.store.execute(
                    "UPDATE orders SET checked_in_at=?,checkin_staff_id=? WHERE id=?",
                    (at, actor["id"], order_id),
                )
            if action == "check_out":
                self._checkout(order, actor["id"], now, at)
            self._transition(order, actor["id"], at, action, after, detail)
            return self._updated(order_id)

        payload = {"order_id": order_id, "version": version, "reason": reason}
        return self._command(token, "staff", action, request_id, payload, perform)

    def _checkout(self, order, actor, now, at):
        if at < order["checked_in_at"]:
            raise BusinessError("window", "退房时间早于实际入住")
        last_used = now.date()
        if now.hour < 14:
            last_used -= timedelta(days=1)
        self.store.execute(
            """UPDATE nights SET usage=CASE WHEN day<=? THEN 'used' ELSE 'released' END
               WHERE order_id=? AND revision=? AND usage='reserved'""",
            (last_used.isoformat(), order["id"], order["revision"]),
        )
        self.store.execute(
            "UPDATE orders SET checked_out_at=?,checkout_staff_id=? WHERE id=?",
            (at, actor, order["id"]),
        )

    def detail(self, token, order_id):
        self.tick(token)
        with self.store.transaction(write=False):
            actor = self.accounts.require(token)
            order = self._order(actor, order_id)
            result = dict(order)
            result["nights"] = [dict(row) for row in self.store.all(
                "SELECT * FROM nights WHERE order_id=? ORDER BY revision,day", (order_id,)
            )]
            result["events"] = [dict(row) for row in self.store.all(
                "SELECT * FROM events WHERE order_id=? ORDER BY id", (order_id,)
            )]
            return result
