"""Hotel configuration, inventory, search, and restrictions."""

from .domain import BusinessError, flag, integer, parse_day, required, timestamp


class Catalog:
    def __init__(self, store, accounts, clock):
        self.store = store
        self.accounts = accounts
        self.clock = clock

    def _hotel(self, hotel_id):
        hotel = self.store.one("SELECT * FROM hotels WHERE id=?", (hotel_id,))
        if not hotel:
            raise BusinessError("not_found", "酒店不存在")
        return hotel

    def room(self, room_id):
        room = self.store.one(
            """SELECT r.*,h.name AS hotel_name,h.enabled AS hotel_enabled
               FROM rooms r JOIN hotels h ON h.id=r.hotel_id WHERE r.id=?""",
            (room_id,),
        )
        if not room:
            raise BusinessError("not_found", "房型不存在")
        return room

    def hotels(self, token):
        actor = self.accounts.require(token, "admin", "staff")
        return [dict(row) for row in self.store.all(
            """SELECT h.* FROM hotels h WHERE ?='admin' OR EXISTS(
                   SELECT 1 FROM grants g WHERE g.hotel_id=h.id AND g.user_id=?)
               ORDER BY h.id""",
            (actor["role"], actor["id"]),
        )]

    def rooms(self, token, hotel_id):
        actor = self.accounts.require(token, "admin", "staff")
        self.accounts.require_hotel(actor, hotel_id)
        return [dict(row) for row in self.store.all(
            "SELECT * FROM rooms WHERE hotel_id=? ORDER BY id", (hotel_id,)
        )]

    def save_hotel(self, token, name, city, address, contact, hotel_id=None):
        with self.store.transaction():
            actor = self.accounts.require(token, "admin", "staff")
            values = tuple(
                required(value, field)
                for value, field in zip(
                    (name, city, address, contact), ("名称", "城市", "地址", "联系方式")
                )
            )
            if hotel_id is None:
                self.accounts.require(token, "admin")
                hotel_id = self.store.execute(
                    "INSERT INTO hotels(name,city,address,contact) VALUES(?,?,?,?)",
                    values,
                ).lastrowid
                action = "create_hotel"
            else:
                self.accounts.require_hotel(actor, hotel_id)
                self._hotel(hotel_id)
                self.store.execute(
                    "UPDATE hotels SET name=?,city=?,address=?,contact=? WHERE id=?",
                    (*values, hotel_id),
                )
                action = "edit_hotel"
            self.store.log(
                actor["id"], hotel_id, timestamp(self.clock()), action, str(hotel_id)
            )
            return hotel_id

    def save_room(self, token, hotel_id, name, description, capacity, room_id=None):
        with self.store.transaction():
            actor = self.accounts.require(token, "staff")
            self.accounts.require_hotel(actor, hotel_id)
            values = (
                required(name, "房型名称"),
                required(description, "房型说明", 500),
                integer(capacity, "每间容量", 1, 1000),
            )
            if room_id is None:
                room_id = self.store.execute(
                    """INSERT INTO rooms(name,description,capacity,hotel_id)
                       VALUES(?,?,?,?)""",
                    (*values, hotel_id),
                ).lastrowid
            else:
                self.accounts.require_hotel(actor, self.room(room_id)["hotel_id"])
                if self.room(room_id)["hotel_id"] != hotel_id:
                    raise BusinessError("invalid", "房型不能迁移酒店")
                self.store.execute(
                    "UPDATE rooms SET name=?,description=?,capacity=? WHERE id=?",
                    (*values, room_id),
                )
            self.store.log(
                actor["id"], hotel_id, timestamp(self.clock()), "save_room", str(room_id)
            )
            return room_id

    def set_room_enabled(self, token, room_id, enabled):
        with self.store.transaction():
            actor = self.accounts.require(token, "staff")
            room = self.room(room_id)
            self.accounts.require_hotel(actor, room["hotel_id"])
            enabled = flag(enabled, "房型状态")
            self.store.execute("UPDATE rooms SET enabled=? WHERE id=?", (enabled, room_id))
            self.store.log(
                actor["id"], room["hotel_id"], timestamp(self.clock()), "room_status",
                str(room_id), before=str(room["enabled"]), after=str(enabled),
            )

    def stock(self, room_id, first, end, at):
        """Read effective occupancy, disregarding pending holds at their deadline."""
        return self.store.all(
            """SELECT i.*,
                  COALESCE((SELECT SUM(n.quantity) FROM nights n
                    JOIN orders o ON o.id=n.order_id
                    WHERE n.room_id=i.room_id AND n.day=i.day AND
                      (n.usage='used' OR (n.usage='reserved'
                        AND (o.status='staying'
                          OR (o.status='pending' AND o.deadline>?)
                          OR (o.status='confirmed' AND o.departure || 'T12:00:00.000000+08:00'>?))))),
                  0) AS occupied
               FROM inventory i WHERE i.room_id=? AND i.day>=? AND i.day<?
               ORDER BY i.day""",
            (at, at, room_id, first, end),
        )

    def inventory(self, token, room_id, first, end):
        actor = self.accounts.require(token, "admin", "staff")
        self.accounts.require_hotel(actor, self.room(room_id)["hotel_id"])
        self._interval(first, end)
        return [
            {**dict(row), "available": row["total"] - row["occupied"]}
            for row in self.stock(room_id, first, end, timestamp(self.clock()))
        ]

    @staticmethod
    def _interval(first, end):
        if parse_day(first) >= parse_day(end):
            raise BusinessError("date", "结束日期必须晚于开始日期")

    def save_inventory(self, token, room_id, day, total, price, stopped=False):
        from datetime import timedelta

        with self.store.transaction():
            actor = self.accounts.require(token, "staff")
            room = self.room(room_id)
            hotel = room["hotel_id"]
            self.accounts.require_hotel(actor, hotel)
            day_value = parse_day(day)
            now = self.clock()
            if day_value < now.date() or day_value.year >= 9999:
                raise BusinessError("date", "仅支持维护今天或未来的有效库存日期")
            end = (day_value + timedelta(days=1)).isoformat()
            total = integer(total, "库存总量", 0)
            price = integer(price, "每晚价格（分）", 0)
            stopped = flag(stopped, "停售状态")
            at = timestamp(now)
            rows = self.stock(room_id, day, end, at)
            old = rows[0] if rows else None
            if old and total < old["occupied"]:
                raise BusinessError("capacity", "库存总量不能低于有效占用")
            self.store.execute(
                """INSERT INTO inventory(room_id,day,total,price,stopped) VALUES(?,?,?,?,?)
                   ON CONFLICT(room_id,day) DO UPDATE SET total=excluded.total,
                     price=excluded.price,stopped=excluded.stopped""",
                (room_id, day, total, price, stopped),
            )
            if stopped:
                self._open_cases(hotel, day, end, f"stock:{room_id}:{day}", at, room_id)
            before = self._stock_summary(old) if old else None
            after = self._stock_summary({"total": total, "price": price, "stopped": stopped})
            self.store.log(
                actor["id"], hotel, at, "save_inventory", f"{room_id}:{day}",
                before=before, after=after,
            )

    @staticmethod
    def _stock_summary(row):
        return f"total={row['total']}, price={row['price']}, stopped={row['stopped']}"

    def _open_cases(self, hotel, first, end, source, at, room=None):
        self.store.execute(
            """INSERT OR IGNORE INTO cases(order_id,source,opened_at)
               SELECT id,?,? FROM orders WHERE hotel_id=? AND arrival<? AND departure>?
                 AND ((status='pending' AND deadline>?) OR
                      (status='confirmed' AND departure || 'T12:00:00.000000+08:00'>?))
                 AND (? IS NULL OR room_id=?)""",
            (source, at, hotel, end, first, at, at, room, room),
        )

    def blocked(self, hotel, room, first, end, for_existing=False):
        hotel_closed = self.store.one(
            """SELECT 1 FROM closures WHERE hotel_id=? AND active=1
               AND first_day<? AND end_day>? LIMIT 1""",
            (hotel, end, first),
        )
        if hotel_closed:
            return True
        return for_existing and self.store.one(
            """SELECT 1 FROM inventory WHERE room_id=? AND day>=? AND day<?
               AND stopped=1 LIMIT 1""",
            (room, first, end),
        ) is not None

    def close_hotel(self, token, hotel, first, end, reason):
        with self.store.transaction():
            actor = self.accounts.require(token, "admin")
            self._hotel(hotel)
            self._interval(first, end)
            reason = required(reason, "停业原因", 300)
            now = self.clock()
            if parse_day(first) < now.date():
                raise BusinessError("date", "停业开始日期不得早于今天")
            at = timestamp(now)
            closure = self.store.execute(
                """INSERT INTO closures(hotel_id,first_day,end_day,reason)
                   VALUES(?,?,?,?)""",
                (hotel, first, end, reason),
            ).lastrowid
            self._open_cases(hotel, first, end, f"closure:{closure}", at)
            self.store.log(
                actor["id"], hotel, at, "close_hotel", str(closure),
                detail=reason, after="closed",
            )
            return closure

    def closures(self, token, hotel):
        actor = self.accounts.require(token, "admin", "staff")
        self.accounts.require_hotel(actor, hotel)
        return [dict(row) for row in self.store.all(
            "SELECT * FROM closures WHERE hotel_id=? ORDER BY id DESC", (hotel,)
        )]

    def reopen_hotel(self, token, closure):
        with self.store.transaction():
            actor = self.accounts.require(token, "admin")
            row = self.store.one("SELECT * FROM closures WHERE id=? AND active=1", (closure,))
            if not row:
                raise BusinessError("not_found", "没有该项有效停业限制")
            self.store.execute("UPDATE closures SET active=0 WHERE id=?", (closure,))
            self.store.log(
                actor["id"], row["hotel_id"], timestamp(self.clock()), "reopen_hotel",
                str(closure), before="closed", after="reopened",
            )

    def cases(self, token, hotel, open_only=True):
        actor = self.accounts.require(token, "staff", "admin")
        self.accounts.require_hotel(actor, hotel)
        return [dict(row) for row in self.store.all(
            """SELECT c.*,o.status,o.version,o.arrival,o.departure
               FROM cases c JOIN orders o ON o.id=c.order_id
               WHERE o.hotel_id=? AND (?=0 OR c.resolved_at IS NULL) ORDER BY c.id""",
            (hotel, int(open_only)),
        )]

    def resolve_case(self, token, case, contact, decision):
        with self.store.transaction():
            actor = self.accounts.require(token, "staff")
            row = self.store.one(
                """SELECT o.hotel_id,c.resolved_at FROM cases c
                   JOIN orders o ON o.id=c.order_id WHERE c.id=?""",
                (case,),
            )
            if not row:
                raise BusinessError("not_found", "处置事项不存在")
            self.accounts.require_hotel(actor, row["hotel_id"])
            if row["resolved_at"]:
                raise BusinessError("conflict", "处置事项已登记结果")
            contact = required(contact, "联系记录", 500)
            decision = required(decision, "处置决定", 500)
            at = timestamp(self.clock())
            self.store.execute(
                """UPDATE cases SET resolved_at=?,staff_id=?,contact_note=?,decision=?
                   WHERE id=?""",
                (at, actor["id"], contact, decision, case),
            )
            self.store.log(
                actor["id"], row["hotel_id"], at, "resolve_case", str(case),
                before="open", after="resolved",
            )
