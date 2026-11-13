"""Domain values shared by the command line and application services."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
ACTIVE_BOOKINGS = ("pending", "confirmed")
STATUS_LABELS = {
    "pending": "待确认",
    "confirmed": "已确认",
    "staying": "已入住",
    "completed": "已完成",
    "cancelled": "已取消",
    "rejected": "已拒绝",
    "expired": "已超时",
}
TRANSITIONS = {
    "pending": {"pending", "confirmed", "rejected", "expired", "cancelled"},
    "confirmed": {"pending", "staying", "cancelled"},
    "staying": {"completed"},
}


class BusinessError(Exception):
    """Expected business rejection with a stable code and optional details."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details

    def result(self):
        return {
            "ok": False,
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


def current_time():
    return datetime.now(BEIJING)


def timestamp(value):
    if value.tzinfo is None:
        raise ValueError("clock must supply an aware datetime")
    return value.astimezone(BEIJING).isoformat(timespec="microseconds")


def parse_day(value):
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise BusinessError("date", "日期格式应为 YYYY-MM-DD")


def boundary(day, hour=0):
    return datetime.combine(parse_day(day), time(hour), BEIJING)


def required(value, field, maximum=200):
    if not isinstance(value, str) or not value.strip():
        raise BusinessError("invalid", f"{field}不能为空")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise BusinessError("invalid", f"{field}长度不能超过 {maximum}")
    return cleaned


def integer(value, field, minimum=1, maximum=100_000_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BusinessError("invalid", f"{field}应为 {minimum} 至 {maximum} 的整数")
    return value


def flag(value, field):
    if type(value) is not bool:
        raise BusinessError("invalid", f"{field}必须为 true 或 false")
    return int(value)


def fingerprint(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class Stay:
    arrival: str
    departure: str
    people: int
    quantity: int

    def days(self, earliest=None, limit=90):
        errors = {}
        bounds = {}
        for key, label in (("arrival", "入住日"), ("departure", "退房日")):
            try:
                bounds[key] = parse_day(getattr(self, key))
            except BusinessError as exc:
                errors[label] = str(exc)
        for key, label in (("people", "人数"), ("quantity", "间数")):
            try:
                integer(getattr(self, key), label, 1, 1000)
            except BusinessError as exc:
                errors[label] = str(exc)
        first, last = bounds.get("arrival"), bounds.get("departure")
        if first and earliest and first < earliest:
            errors["入住日"] = "入住日不得早于今天"
        if first and last and not 1 <= (last - first).days <= limit:
            errors["退房日"] = f"退房须晚于入住，最多预订 {limit} 晚"
        if errors:
            raise BusinessError("invalid", "请修正住宿条件", errors)
        return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days)]


@dataclass(frozen=True)
class Quote:
    hotel_id: int
    room_id: int
    hotel_name: str
    room_name: str
    stay: Stay
    nights: tuple

    @property
    def total(self):
        return self.stay.quantity * sum(price for _, price in self.nights)

    def data(self):
        data = asdict(self)
        data["nights"] = [
            {
                "day": day,
                "price": price,
                "quantity": self.stay.quantity,
                "subtotal": price * self.stay.quantity,
            }
            for day, price in self.nights
        ]
        data["total"] = self.total
        data["token"] = fingerprint(data)
        return data
