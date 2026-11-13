"""Availability and immutable quote calculation."""

from .domain import BusinessError, Quote, integer, required, timestamp


class Search:
    def __init__(self, store, catalog, clock):
        self.store = store
        self.catalog = catalog
        self.clock = clock

    def quote(self, room_id, stay, now, prior=None):
        days = stay.days(now.date())
        room = self.catalog.room(room_id)
        if not room["enabled"] or not room["hotel_enabled"]:
            raise BusinessError("unavailable", "酒店或房型已停用")
        if self.catalog.blocked(
            room["hotel_id"], room_id, stay.arrival, stay.departure
        ):
            raise BusinessError("closed", "住宿日期包含停业时段")
        if stay.people > room["capacity"] * stay.quantity:
            raise BusinessError("capacity", "人数超过预订房间总容量")
        if prior and room["hotel_id"] != prior["hotel_id"]:
            raise BusinessError("invalid", "改订只能选择原酒店的房型")
        rows = self.catalog.stock(room_id, stay.arrival, stay.departure, timestamp(now))
        if len(rows) != len(days):
            raise BusinessError("unavailable", "至少一晚尚未配置库存")
        for row in rows:
            credit = 0
            if (
                prior
                and prior["room_id"] == room_id
                and prior["arrival"] <= row["day"] < prior["departure"]
            ):
                credit = prior["quantity"]
            if (
                row["stopped"]
                or row["total"] - row["occupied"] + credit < stay.quantity
            ):
                raise BusinessError("unavailable", f"{row['day']} 停售或库存不足")
        return Quote(
            room["hotel_id"],
            room_id,
            room["hotel_name"],
            room["name"],
            stay,
            tuple((row["day"], row["price"]) for row in rows),
        )

    def find(self, city, stay, room_name=None, minimum=None, maximum=None):
        now = self.clock()
        errors = {}
        try:
            stay.days(now.date())
        except BusinessError as exc:
            errors.update(exc.details or {})
        try:
            city = required(city, "城市")
        except BusinessError as exc:
            errors["城市"] = str(exc)
        for field, value in (("最低价", minimum), ("最高价", maximum)):
            if value is not None:
                try:
                    integer(value, field, 0)
                except BusinessError as exc:
                    errors[field] = str(exc)
        if not errors.keys() & {"最低价", "最高价"}:
            if minimum is not None and maximum is not None and minimum > maximum:
                errors["最低价"] = "最低价不能高于最高价"
        if errors:
            raise BusinessError("invalid", "请修正筛选条件", errors)
        room_name = room_name.strip() if room_name else None
        with self.store.transaction(write=False):
            rooms = self.store.all(
                """SELECT r.id FROM rooms r JOIN hotels h ON h.id=r.hotel_id
                   WHERE h.city=? AND h.enabled=1 AND r.enabled=1
                     AND (? IS NULL OR r.name=? COLLATE NOCASE)""",
                (city, room_name, room_name),
            )
            result = []
            for room in rooms:
                try:
                    quote = self.quote(room["id"], stay, now)
                except BusinessError:
                    continue
                if all(
                    (minimum is None or price >= minimum)
                    and (maximum is None or price <= maximum)
                    for _, price in quote.nights
                ):
                    result.append(quote)
        result.sort(key=lambda q: (q.total, q.hotel_id, q.room_id))
        return [quote.data() for quote in result]
