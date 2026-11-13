"""Behavioral tests against a real SQLite database and controlled clock."""

import io
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

from hotel.app import DEMO_PASSWORD, Application, seed
from hotel.cli import Console
from hotel.domain import BEIJING, BusinessError, Stay


class Reservations(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.db"
        self.now = datetime(2026, 11, 13, 10, tzinfo=BEIJING)
        self.app = Application(self.path, lambda: self.now)
        self.addCleanup(self.app.close)
        seed(self.app)
        self.guest = self.login("customer")
        self.staff = self.login("frontdesk")
        self.admin = self.login("admin")
        self.stay = Stay("2026-11-14", "2026-11-16", 2, 1)

    def login(self, username):
        return self.app.accounts.login(username, DEMO_PASSWORD)

    def stock(self, room=1):
        return self.app.catalog.inventory(
            self.staff, room, self.stay.arrival, self.stay.departure
        )

    def quote(self):
        return self.app.orders.quote(self.guest, 1, self.stay)

    def book(self, key="first"):
        return self.app.orders.book(
            self.guest, 1, self.stay, self.quote()["token"], key
        )

    def staff_action(self, order, action, reason=""):
        return self.app.orders.staff_action(
            self.staff, order["order_id"], order["version"], action, action, reason
        )

    def test_full_flow_and_early_checkout(self):
        booked = self.book()
        self.assertEqual([row["occupied"] for row in self.stock()], [1, 1])
        confirmed = self.staff_action(booked, "confirm")
        self.assertEqual([row["occupied"] for row in self.stock()], [1, 1])
        self.now = datetime(2026, 11, 14, 14, tzinfo=BEIJING)
        staying = self.staff_action(confirmed, "check_in")
        self.assertEqual([row["occupied"] for row in self.stock()], [1, 1])
        self.now = datetime(2026, 11, 15, 11, tzinfo=BEIJING)
        completed = self.staff_action(staying, "check_out")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual([row["occupied"] for row in self.stock()], [1, 0])
        detail = self.app.orders.detail(self.guest, completed["order_id"])
        self.assertEqual(
            [row["usage"] for row in detail["nights"]], ["used", "released"]
        )
        report = self.app.queries.statistics(self.staff, "2026-11-13", "2026-11-17")
        self.assertEqual((report["new_orders"], report["used_nights"]), (1, 1))

    def test_requote_snapshot_and_receipt(self):
        quote = self.quote()
        self.app.catalog.save_inventory(self.staff, 1, self.stay.arrival, 5, 33333)
        request = self.app.orders.book(self.guest, 1, self.stay, quote["token"], "old")
        self.assertEqual(request["code"], "requote")
        self.assertEqual([row["occupied"] for row in self.stock()], [0, 0])
        fresh = request["details"]
        booked = self.app.orders.book(self.guest, 1, self.stay, fresh["token"], "fresh")
        self.assertTrue(booked["ok"])
        self.app.catalog.save_inventory(self.staff, 1, self.stay.arrival, 5, 99999)
        replay = self.app.orders.book(self.guest, 1, self.stay, fresh["token"], "fresh")
        self.assertEqual(replay, booked)
        detail = self.app.orders.detail(self.guest, booked["order_id"])
        self.assertEqual(detail["nights"][0]["price"], 33333)
        self.assertEqual(self.book("duplicate")["code"], "duplicate_order")
        with self.assertRaisesRegex(BusinessError, "不同内容"):
            self.app.orders.book(self.guest, 1, self.stay, quote["token"], "fresh")

    def test_overlapping_modification_credits_existing_stock(self):
        for row in self.stock():
            self.app.catalog.save_inventory(self.staff, 1, row["day"], 1, row["price"])
        booked = self.book()
        confirmed = self.staff_action(booked, "confirm")
        revised = Stay("2026-11-14", "2026-11-15", 2, 1)
        order_id = confirmed["order_id"]
        quote = self.app.orders.quote(self.guest, 1, revised, order_id)
        result = self.app.orders.modify(
            self.guest,
            order_id,
            confirmed["version"],
            1,
            revised,
            quote["token"],
            "edit",
        )
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["version"], 3)
        self.assertEqual([row["occupied"] for row in self.stock()], [1, 0])
        detail = self.app.orders.detail(self.guest, order_id)
        self.assertEqual(
            [row["usage"] for row in detail["nights"]],
            ["released", "released", "reserved"],
        )

    def test_modify_rolls_back_after_write_failure(self):
        booked = self.book()
        order_id = booked["order_id"]
        quote = self.app.orders.quote(self.guest, 2, self.stay, order_id)
        self.app.store.execute(
            """CREATE TRIGGER test_failure BEFORE INSERT ON nights
               WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT,'injected'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.app.orders.modify(
                self.guest, order_id, 1, 2, self.stay, quote["token"], "modify"
            )
        self.assertEqual([row["occupied"] for row in self.stock()], [1, 1])
        self.assertEqual([row["occupied"] for row in self.stock(2)], [0, 0])
        self.assertEqual(self.app.orders.detail(self.guest, order_id)["version"], 1)
        self.assertIsNone(
            self.app.store.one("SELECT 1 FROM receipts WHERE request_id='modify'")
        )

    def test_expiry_at_exact_deadline(self):
        booked = self.book()
        self.now += timedelta(minutes=30)
        refused = self.staff_action(booked, "confirm")
        self.assertEqual(refused["code"], "expired")
        detail = self.app.orders.detail(self.guest, booked["order_id"])
        self.assertEqual(detail["status"], "expired")
        self.assertEqual([e["action"] for e in detail["events"]], ["book", "expire"])
        self.assertEqual([row["occupied"] for row in self.stock()], [0, 0])

    def test_check_in_and_no_show_boundaries(self):
        confirmed = self.staff_action(self.book(), "confirm")
        self.now = datetime(2026, 11, 14, 13, 59, 59, tzinfo=BEIJING)
        self.assertEqual(self.staff_action(confirmed, "check_in")["code"], "window")
        self.now = datetime(2026, 11, 16, 12, tzinfo=BEIJING)
        detail = self.app.orders.detail(self.guest, confirmed["order_id"])
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(detail["events"][-1]["action"], "no_show")

    def test_customer_cancellation_cutoff_and_replay(self):
        booked = self.book()
        confirmed = self.staff_action(booked, "confirm")
        order_id = booked["order_id"]
        stale = self.app.orders.cancel(self.guest, order_id, 1, "stale")
        self.assertEqual(stale["code"], "version")
        self.now = datetime(2026, 11, 14, 0, tzinfo=BEIJING)
        stopped = self.app.orders.cancel(
            self.guest, order_id, confirmed["version"], "cancel"
        )
        self.assertEqual(stopped["code"], "cutoff")
        self.assertEqual(
            stopped, self.app.orders.cancel(self.guest, order_id, 2, "cancel")
        )

    def test_scope_and_disabled_sessions(self):
        other_id = self.app.accounts.register("other", DEMO_PASSWORD)
        other = self.login("other")
        booked = self.book()
        with self.assertRaises(BusinessError):
            self.app.orders.detail(other, booked["order_id"])
        self.assertEqual(self.app.queries.list_orders(other), [])
        other_hotel = self.app.catalog.save_hotel(
            self.admin, "分店", "南京", "示例路", "000"
        )
        with self.assertRaises(BusinessError):
            self.app.catalog.rooms(self.staff, other_hotel)
        with self.assertRaises(BusinessError):
            self.app.orders.staff_action(
                self.admin, booked["order_id"], 1, "confirm", "x"
            )
        self.app.accounts.set_enabled(self.admin, other_id, False)
        with self.assertRaises(BusinessError):
            self.app.queries.list_orders(other)
        row = self.app.store.one(
            "SELECT salt,digest FROM accounts WHERE id=?", (other_id,)
        )
        self.assertNotEqual(row["digest"], DEMO_PASSWORD)
        self.assertEqual(len(row["salt"]), 32)

    def test_closure_case_retains_status_and_blocks_confirmation(self):
        booked = self.book()
        closure = self.app.catalog.close_hotel(
            self.admin, 1, self.stay.arrival, self.stay.departure, "设备检修"
        )
        cases = self.app.catalog.cases(self.staff, 1)
        self.assertEqual(cases[0]["status"], "pending")
        self.assertEqual(self.staff_action(booked, "confirm")["code"], "closed")
        self.app.catalog.resolve_case(
            self.staff, cases[0]["id"], "电话", "客户等恢复营业"
        )
        self.app.catalog.reopen_hotel(self.admin, closure)
        restored = self.app.orders.staff_action(
            self.staff, booked["order_id"], 1, "confirm", "after-reopen"
        )
        self.assertEqual(restored["status"], "confirmed")
        logs = self.app.queries.audit(self.staff, hotel=1, result="closed")
        self.assertEqual(logs[0]["action"], "confirm")

    def test_combination_filters_and_input_errors(self):
        hits = self.app.search.find("南京", self.stay, maximum=30000)
        self.assertEqual([hit["room_id"] for hit in hits], [1])
        self.assertEqual(self.app.search.find("南京", self.stay, maximum=29999), [])
        with self.assertRaises(BusinessError) as ctx:
            self.app.search.find(
                "", Stay("2026-11-14", "2026-11-14", 0, 0), minimum=2, maximum=1
            )
        self.assertEqual(
            set(ctx.exception.details), {"城市", "退房日", "人数", "间数", "最低价"}
        )

    def test_concurrent_last_room(self):
        for row in self.stock():
            self.app.catalog.save_inventory(self.staff, 1, row["day"], 1, row["price"])
        self.app.accounts.register("racer", DEMO_PASSWORD)
        gate = Barrier(2)
        quote = self.quote()

        def race(name):
            with Application(self.path, lambda: self.now) as app:
                token = app.accounts.login(name, DEMO_PASSWORD)
                gate.wait(timeout=5)
                return app.orders.book(token, 1, self.stay, quote["token"], "race")

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(race, ("customer", "racer")))
        self.assertEqual(sum(outcome["ok"] for outcome in outcomes), 1)
        self.assertEqual([row["occupied"] for row in self.stock()], [1, 1])

    def test_console_batch_and_inline_help(self):
        console = Console(self.app, interactive=False)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(console.execute(f"login customer {DEMO_PASSWORD}"))
            self.assertTrue(console.execute("orders --help"))
            self.assertTrue(console.execute("orders"))
            self.assertFalse(console.execute("inventory 1 2026-11-14 2026-11-16"))
        self.assertIn("forbidden", output.getvalue())


if __name__ == "__main__":
    unittest.main()
