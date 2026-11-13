"""Exercise the browser-facing routes over a real HTTP connection."""

import json
import tempfile
import unittest
from datetime import timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from hotel.app import DEMO_PASSWORD, Application, seed
from hotel.domain import current_time
from hotel.web import HotelServer


class WebTests(unittest.TestCase):
    def test_booking_and_staff_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "web.db"
            with Application(database) as app:
                seed(app)
            with HotelServer(("127.0.0.1", 0), database) as server:
                worker = Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    self.exercise(server.server_port)
                finally:
                    server.shutdown()
                    worker.join()

    def exercise(self, port):
        root = f"http://127.0.0.1:{port}"
        guest = build_opener(HTTPCookieProcessor(CookieJar()))
        staff = build_opener(HTTPCookieProcessor(CookieJar()))
        admin = build_opener(HTTPCookieProcessor(CookieJar()))

        def api(client, command, *args, header=True):
            headers = {"Content-Type": "application/json"}
            if header:
                headers["X-Hotel-Web"] = "1"
            request = Request(
                root + "/api",
                json.dumps({"command": command, "args": list(map(str, args))}).encode(),
                headers,
            )
            try:
                response = client.open(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        with guest.open(root, timeout=5) as page:
            self.assertIn("栖程酒店", page.read().decode())
        for asset in ("/app.js", "/style.css"):
            with guest.open(root + asset, timeout=5) as page:
                self.assertTrue(page.read())
        self.assertEqual(api(guest, "orders")[0], 401)
        self.assertEqual(
            api(guest, "login", "customer", DEMO_PASSWORD, header=False)[0], 400
        )
        self.assertEqual(api(guest, "login", "customer", DEMO_PASSWORD)[0], 200)
        self.assertEqual(api(guest, "session")[1]["result"]["role"], "guest")
        self.assertEqual(api(guest, "hotels")[0], 403)
        today = current_time().date()
        stay = (today + timedelta(days=1), today + timedelta(days=2), 2, 1)
        quote = api(guest, "search", "南京", *stay)[1]["result"][0]
        order = api(guest, "book", quote["room_id"], *stay, quote["token"])[1]
        self.assertEqual(order["status"], "pending")
        order_id = order["order_id"]
        self.assertEqual(api(guest, "confirm", order_id, order["version"])[0], 403)
        self.assertEqual(api(staff, "login", "frontdesk", DEMO_PASSWORD)[0], 200)
        self.assertEqual(
            api(staff, "confirm", order_id, order["version"])[1]["status"],
            "confirmed",
        )
        self.assertEqual(
            api(guest, "order", order_id)[1]["result"]["status"], "confirmed"
        )
        self.assertEqual(api(admin, "login", "admin", DEMO_PASSWORD)[0], 200)
        self.assertEqual(api(admin, "accounts")[0], 200)
        self.assertEqual(api(staff, "accounts")[0], 403)
        self.assertEqual(api(guest, "logout")[0], 200)
        self.assertEqual(api(guest, "orders")[0], 401)
