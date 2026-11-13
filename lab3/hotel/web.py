"""Same-origin HTTP interface for the hotel application."""

import argparse
import json
import sqlite3
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

from .app import Application
from .cli import Console
from .domain import BusinessError

ASSETS = Path(__file__).with_name("static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
SESSION_SECONDS = 8 * 60 * 60


class HotelServer(ThreadingHTTPServer):
    def __init__(self, address, database, secure_cookie=False):
        self.database = database
        self.secure_cookie = secure_cookie
        self.sessions = {}
        self.lock = Lock()
        super().__init__(address, Handler)

    def account_id(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session and session[1] > time.monotonic():
                return session[0]
            self.sessions.pop(token, None)
            return None

    def login(self, token, account_id):
        with self.lock:
            now = time.monotonic()
            self.sessions = {
                key: value for key, value in self.sessions.items() if value[1] > now
            }
            self.sessions[token] = (account_id, now + SESSION_SECONDS)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data, media, cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, value, cookie=None):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.reply(status, data, "application/json; charset=utf-8", cookie)

    def cookie(self, token):
        age = SESSION_SECONDS if token else 0
        secure = "; Secure" if self.server.secure_cookie else ""
        return (
            f"hotel_session={token or ''}; Max-Age={age}; Path=/; "
            f"HttpOnly; SameSite=Strict{secure}"
        )

    def session(self):
        try:
            item = SimpleCookie(self.headers.get("Cookie", "")).get("hotel_session")
        except CookieError:
            return None
        return item.value if item else None

    def do_GET(self):
        asset = FILES.get(self.path)
        if asset is None:
            self.json(404, BusinessError("not_found", "页面不存在").result())
            return
        name, media = asset
        self.reply(200, (ASSETS / name).read_bytes(), media)

    def read_command(self):
        if (
            self.headers.get("X-Hotel-Web") != "1"
            or self.headers.get_content_type() != "application/json"
        ):
            raise BusinessError("request", "请求格式不正确")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 16_384:
            raise BusinessError("request", "请求长度不正确")
        body = json.loads(self.rfile.read(size))
        if not isinstance(body, dict):
            raise BusinessError("request", "请求必须是 JSON 对象")
        name, args = body.get("command"), body.get("args", [])
        if not isinstance(name, str) or not isinstance(args, list):
            raise BusinessError("request", "命令或参数格式不正确")
        if len(args) > 32 or not all(
            isinstance(arg, str) and len(arg) <= 600 for arg in args
        ):
            raise BusinessError("request", "参数长度或类型不正确")
        return name, args

    def run_command(self, name, args):
        token = self.session()
        user_id = self.server.account_id(token)
        with Application(self.server.database) as app:
            console = Console(app, interactive=False)
            if user_id is not None:
                app.accounts.sessions[token] = user_id
                console.token = token
            if name == "session":
                actor = (
                    dict(app.accounts.require(token)) if user_id is not None else None
                )
                return {"ok": True, "result": actor}, None
            if name == "logout":
                self.server.logout(token)
                return {"ok": True, "result": None}, self.cookie(None)
            command = console.commands.get(name)
            if command is None:
                raise BusinessError("not_found", "命令不存在")
            if command.roles:
                app.accounts.require(console.token, *command.roles)
            if any(arg in ("-h", "--help") for arg in args):
                raise BusinessError("arguments", "表单字段不能使用帮助参数")
            result = command.run(command.parser.parse_args(args))
            if name == "login":
                self.server.logout(token)
                self.server.login(console.token, result["id"])
                return {"ok": True, "result": result}, self.cookie(console.token)
            if isinstance(result, dict) and "ok" in result:
                return result, None
            return {"ok": True, "result": result}, None

    def do_POST(self):
        if self.path != "/api":
            self.json(404, BusinessError("not_found", "接口不存在").result())
            return
        try:
            result, cookie = self.run_command(*self.read_command())
            self.json(200, result, cookie)
        except BusinessError as exc:
            status = {"auth": 401, "login": 401, "forbidden": 403}.get(exc.code, 400)
            self.json(status, exc.result())
        except ValueError:
            self.json(400, BusinessError("request", "请求内容无法解析").result())
        except sqlite3.Error:
            self.json(503, BusinessError("database", "数据库暂不可用，请重试").result())


def main():
    parser = argparse.ArgumentParser(description="栖程酒店预订网站")
    parser.add_argument("--db", default="hotel.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--secure-cookie", action="store_true")
    args = parser.parse_args()
    with HotelServer((args.host, args.port), args.db, args.secure_cookie) as server:
        print(f"栖程酒店预订 http://{args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
