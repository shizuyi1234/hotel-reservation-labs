"""Interactive and scriptable console application."""

import argparse
import getpass
import json
import shlex
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .app import Application, seed
from .domain import BusinessError, Stay


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise BusinessError("arguments", message)


@dataclass
class Command:
    parser: Arguments
    run: object
    roles: tuple


def switch(value):
    values = {"yes": True, "no": False}
    if value not in values:
        raise argparse.ArgumentTypeError("请填 yes 或 no")
    return values[value]


def add_stay(parser):
    parser.add_argument("arrival", metavar="入住日")
    parser.add_argument("departure", metavar="退房日")
    parser.add_argument("people", type=int, metavar="人数")
    parser.add_argument("quantity", type=int, metavar="间数")


def stay_from(args):
    return Stay(args.arrival, args.departure, args.people, args.quantity)


def order_reference(parser):
    parser.add_argument("order_id", type=int)
    parser.add_argument("version", type=int)
    parser.add_argument("--request-id")


def submission_id(args):
    return args.request_id or uuid4().hex


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


class Console:
    def __init__(self, app, interactive=True):
        self.app = app
        self.interactive = interactive
        self.token = None
        self.commands = {}
        self._account_commands()
        self._catalog_commands()
        self._order_commands()
        self._report_commands()

    def _add(self, name, summary, run, *roles):
        parser = Arguments(prog=name, description=summary)
        self.commands[name] = Command(parser, run, roles)
        return parser

    def _password(self, args):
        if self.interactive:
            return getpass.getpass("密码：")
        if not args.password:
            raise BusinessError("arguments", "批处理登录或注册须提供演示密码")
        return args.password

    def _login(self, args):
        token = self.app.accounts.login(args.username, self._password(args))
        self.app.accounts.logout(self.token)
        self.token = token
        return dict(self.app.accounts.require(token))

    def _logout(self, _args):
        self.app.accounts.logout(self.token)
        self.token = None
        return {"logged_out": True}

    def _account_commands(self):
        accounts = self.app.accounts
        parser = self._add("login", "登录", self._login)
        parser.add_argument("username")
        parser.add_argument("password", nargs="?", help="仅用于批处理演示")
        parser = self._add(
            "register", "注册顾客",
            lambda a: {"user_id": accounts.register(a.username, self._password(a))},
        )
        parser.add_argument("username")
        parser.add_argument("password", nargs="?", help="仅用于批处理演示")
        self._add("logout", "退出登录", self._logout)
        self._add("accounts", "查看账号及授权", lambda a: accounts.admin_list(self.token), "admin")
        parser = self._add(
            "grant", "授予员工或管理员身份",
            lambda a: accounts.assign(self.token, a.username, a.role, a.hotel), "admin",
        )
        parser.add_argument("username")
        parser.add_argument("role", choices=("staff", "admin"))
        parser.add_argument("--hotel", type=int)
        parser = self._add(
            "revoke", "撤销酒店授权",
            lambda a: accounts.revoke(self.token, a.user_id, a.hotel), "admin",
        )
        parser.add_argument("user_id", type=int)
        parser.add_argument("hotel", type=int)
        parser = self._add(
            "account-state", "启用或停用账号",
            lambda a: accounts.set_enabled(self.token, a.user_id, a.enabled), "admin",
        )
        parser.add_argument("user_id", type=int)
        parser.add_argument("enabled", type=switch)

    def _catalog_commands(self):
        catalog = self.app.catalog
        self._add("hotels", "查看授权酒店", lambda a: catalog.hotels(self.token), "staff", "admin")
        parser = self._add(
            "hotel-save", "登记或修改酒店资料",
            lambda a: {"hotel_id": catalog.save_hotel(
                self.token, a.name, a.city, a.address, a.contact, a.hotel
            )}, "staff", "admin",
        )
        for field in ("name", "city", "address", "contact"):
            parser.add_argument(field)
        parser.add_argument("--hotel", type=int)
        parser = self._add(
            "rooms", "查看酒店房型", lambda a: catalog.rooms(self.token, a.hotel),
            "staff", "admin",
        )
        parser.add_argument("hotel", type=int)
        parser = self._add(
            "room-save", "新增或修改房型",
            lambda a: {"room_id": catalog.save_room(
                self.token, a.hotel, a.name, a.description, a.capacity, a.room
            )}, "staff",
        )
        parser.add_argument("hotel", type=int)
        parser.add_argument("name")
        parser.add_argument("description")
        parser.add_argument("capacity", type=int)
        parser.add_argument("--room", type=int)
        parser = self._add(
            "room-state", "启用或停用房型",
            lambda a: catalog.set_room_enabled(self.token, a.room, a.enabled), "staff",
        )
        parser.add_argument("room", type=int)
        parser.add_argument("enabled", type=switch)
        parser = self._add(
            "inventory", "查询日期区间内的库存",
            lambda a: catalog.inventory(self.token, a.room, a.first, a.end), "staff", "admin",
        )
        parser.add_argument("room", type=int)
        parser.add_argument("first")
        parser.add_argument("end")
        parser = self._add(
            "inventory-save", "维护单日库存、分价与停售",
            lambda a: catalog.save_inventory(
                self.token, a.room, a.day, a.total, a.price, a.stopped
            ), "staff",
        )
        parser.add_argument("room", type=int)
        parser.add_argument("day")
        parser.add_argument("total", type=int)
        parser.add_argument("price", type=int, help="每间每晚价格，单位分")
        parser.add_argument("--stopped", type=switch, default=False)
        self._restriction_commands(catalog)

    def _restriction_commands(self, catalog):
        parser = self._add(
            "closures", "查看酒店停业安排",
            lambda a: catalog.closures(self.token, a.hotel), "staff", "admin",
        )
        parser.add_argument("hotel", type=int)
        parser = self._add(
            "closure-add", "登记停业区间",
            lambda a: {"closure_id": catalog.close_hotel(
                self.token, a.hotel, a.first, a.end, a.reason
            )}, "admin",
        )
        parser.add_argument("hotel", type=int)
        parser.add_argument("first")
        parser.add_argument("end")
        parser.add_argument("reason")
        parser = self._add(
            "closure-end", "恢复营业",
            lambda a: catalog.reopen_hotel(self.token, a.closure), "admin",
        )
        parser.add_argument("closure", type=int)
        parser = self._add(
            "cases", "查看停业停售处置清单",
            lambda a: catalog.cases(self.token, a.hotel, not a.all), "staff", "admin",
        )
        parser.add_argument("hotel", type=int)
        parser.add_argument("--all", action="store_true")
        parser = self._add(
            "case-resolve", "登记联系记录和处置决定",
            lambda a: catalog.resolve_case(self.token, a.case, a.contact, a.decision),
            "staff",
        )
        parser.add_argument("case", type=int)
        parser.add_argument("contact")
        parser.add_argument("decision")

    def _order_commands(self):
        orders = self.app.orders
        parser = self._add(
            "search", "组合查询可订酒店",
            lambda a: self.app.search.find(
                a.city, stay_from(a), a.room_name, a.minimum, a.maximum
            ),
        )
        parser.add_argument("city")
        add_stay(parser)
        parser.add_argument("--room-name")
        parser.add_argument("--minimum", type=int, help="每间每晚最低价，单位分")
        parser.add_argument("--maximum", type=int, help="每间每晚最高价，单位分")
        parser = self._add(
            "quote", "取得预订或改订报价",
            lambda a: orders.quote(self.token, a.room, stay_from(a), a.order), "guest",
        )
        parser.add_argument("room", type=int)
        add_stay(parser)
        parser.add_argument("--order", type=int)
        parser = self._add(
            "book", "确认报价并创建订单",
            lambda a: orders.book(
                self.token, a.room, stay_from(a), a.quote_token, submission_id(a)
            ), "guest",
        )
        parser.add_argument("room", type=int)
        add_stay(parser)
        parser.add_argument("quote_token")
        parser.add_argument("--request-id")
        parser = self._add(
            "modify", "按当前版本和已确认报价改订",
            lambda a: orders.modify(
                self.token, a.order_id, a.version, a.room, stay_from(a),
                a.quote_token, submission_id(a),
            ), "guest",
        )
        order_reference(parser)
        parser.add_argument("room", type=int)
        add_stay(parser)
        parser.add_argument("quote_token")
        parser = self._add(
            "cancel", "取消本人订单",
            lambda a: orders.cancel(self.token, a.order_id, a.version, submission_id(a)),
            "guest",
        )
        order_reference(parser)
        parser = self._add(
            "order", "查看订单、历史快照和事件",
            lambda a: orders.detail(self.token, a.order_id), "guest", "staff", "admin",
        )
        parser.add_argument("order_id", type=int)
        self._add(
            "tick", "处理到期及未到店订单",
            lambda a: {"processed": orders.tick(self.token)}, "guest", "staff", "admin",
        )
        for name, action, summary in (
            ("confirm", "confirm", "确认待确认订单"),
            ("reject", "reject", "填写原因拒绝订单"),
            ("check-in", "check_in", "办理整单入住"),
            ("check-out", "check_out", "办理整单退房"),
        ):
            parser = self._add(name, summary, self._staff_handler(action), "staff")
            order_reference(parser)
            if action == "reject":
                parser.add_argument("reason")

    def _staff_handler(self, action):
        def handle(args):
            return self.app.orders.staff_action(
                self.token, args.order_id, args.version, action,
                submission_id(args), getattr(args, "reason", ""),
            )
        return handle

    def _report_commands(self):
        query = self.app.queries
        parser = self._add(
            "orders", "按条件查询授权范围内的订单",
            lambda a: query.list_orders(self.token, **vars(a)), "guest", "staff", "admin",
        )
        for name in ("hotel", "order-id", "room"):
            parser.add_argument("--" + name, type=int)
        for name in ("status", "created-from", "created-to", "stay-from", "stay-to"):
            parser.add_argument("--" + name)
        parser = self._add(
            "statistics", "统计订单、间夜和预计房费",
            lambda a: query.statistics(self.token, **vars(a)), "staff", "admin",
        )
        parser.add_argument("first")
        parser.add_argument("end")
        parser.add_argument("--hotel", type=int)
        parser.add_argument("--room", type=int)
        parser.add_argument("--include-rows", action="store_true")
        parser = self._add(
            "audit", "查看权限范围内的关键操作日志",
            lambda a: query.audit(self.token, **vars(a)), "staff", "admin",
        )
        parser.add_argument("--hotel", type=int)
        parser.add_argument("--actor-id", type=int)
        for name in ("first", "end", "target", "result"):
            parser.add_argument("--" + name)

    def help(self):
        role = None
        if self.token:
            try:
                role = self.app.accounts.require(self.token)["role"]
            except BusinessError:
                self.token = None
        print("命令后加 --help 查看参数。exit 退出。")
        for name, command in self.commands.items():
            if not command.roles or role in command.roles:
                print(f"  {name:16} {command.parser.description}")

    def execute(self, line):
        try:
            words = shlex.split(line, comments=True)
            if not words:
                return True
            name, *values = words
            if name == "help":
                self.help()
                return True
            command = self.commands.get(name)
            if not command:
                raise BusinessError("arguments", "未知命令，请输入 help")
            if values == ["--help"] or values == ["-h"]:
                command.parser.print_help()
                return True
            if command.roles:
                self.app.accounts.require(self.token, *command.roles)
            args = command.parser.parse_args(values)
            result = command.run(args)
            if isinstance(result, dict) and "ok" in result:
                emit(result)
                return result["ok"]
            emit({"ok": True, "result": result})
            return True
        except BusinessError as exc:
            emit(exc.result())
        except ValueError as exc:
            emit(BusinessError("arguments", str(exc)).result())
        except sqlite3.Error:
            emit(BusinessError("database", "数据库操作失败，请检查文件或稍后重试").result())
        return False

    def loop(self):
        print("栖程酒店预订系统 | 日期使用北京时间，金额单位为分")
        self.help()
        while True:
            try:
                line = input("hotel> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line.strip() in ("exit", "quit"):
                break
            self.execute(line)
        self.app.accounts.logout(self.token)

    def batch(self, filename):
        lines = Path(filename).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if not self.execute(line):
                print(f"批处理在第 {number} 行停止")
                return 1
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="酒店预订系统控制台")
    parser.add_argument("--db", default="hotel.db", help="SQLite 数据文件")
    parser.add_argument("--seed-demo", action="store_true", help="初始化演示数据并退出")
    parser.add_argument("--init-admin", metavar="USERNAME", help="初始化自定义管理员")
    parser.add_argument("--batch", metavar="FILE", help="执行命令文件")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.seed_demo, args.init_admin, args.batch)) > 1:
        parser.error("初始化、演示填充和批处理选项不能同时使用")
    try:
        with Application(args.db) as app:
            if args.seed_demo:
                emit(seed(app))
            elif args.init_admin:
                password = getpass.getpass("初始管理员密码：")
                if password != getpass.getpass("再次输入密码："):
                    raise BusinessError("invalid", "两次输入的密码不一致")
                emit({"user_id": app.accounts.bootstrap(args.init_admin, password)})
            elif args.batch:
                return Console(app, interactive=False).batch(args.batch)
            else:
                Console(app).loop()
    except (BusinessError, OSError, sqlite3.Error) as exc:
        error = exc if isinstance(exc, BusinessError) else BusinessError("io", str(exc))
        emit(error.result())
        return 1
    return 0
