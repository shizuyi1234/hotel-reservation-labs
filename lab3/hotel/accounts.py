"""Account registration, authentication and hotel permissions."""

import hashlib
import hmac
import re
import secrets

from .domain import BusinessError, flag, integer, required, timestamp

USER_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,40}")
PBKDF2_ROUNDS = 600_000


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()


class Accounts:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock
        self.sessions = {}

    def _create(self, username, password, role):
        username = required(username, "账号", 40)
        if not USER_PATTERN.fullmatch(username):
            raise BusinessError("invalid", "账号需由 3 至 40 个字母、数字或 _.- 组成")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise BusinessError("invalid", "密码长度须为 8 至 128")
        username = username.lower()
        if self.store.one("SELECT id FROM accounts WHERE username=?", (username,)):
            raise BusinessError("duplicate_account", "该账号已存在")
        salt = secrets.token_hex(16)
        return self.store.execute(
            "INSERT INTO accounts(username,salt,digest,role) VALUES(?,?,?,?)",
            (username, salt, password_digest(password, salt), role),
        ).lastrowid

    def bootstrap(self, username, password):
        """Create the first administrator on a newly initialized database."""
        with self.store.transaction():
            if self.store.one("SELECT 1 FROM accounts WHERE role='admin'"):
                raise BusinessError("initialized", "管理员已初始化")
            actor = self._create(username, password, "admin")
            self.store.log(
                actor, None, timestamp(self.clock()), "bootstrap", str(actor)
            )
            return actor

    def register(self, username, password):
        with self.store.transaction():
            actor = self._create(username, password, "guest")
            self.store.log(actor, None, timestamp(self.clock()), "register", str(actor))
            return actor

    def login(self, username, password):
        username = required(username, "账号", 40).lower()
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise BusinessError("login", "登录失败")
        account = self.store.one("SELECT * FROM accounts WHERE username=?", (username,))
        salt = account["salt"] if account else "0" * 32
        digest = password_digest(password, salt)
        expected = account["digest"] if account else "0" * 64
        valid = hmac.compare_digest(digest, expected)
        if not account or not valid or not account["enabled"]:
            raise BusinessError("login", "账号或密码错误，或账号已停用")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = account["id"]
        return token

    def logout(self, token):
        self.sessions.pop(token, None)

    def require(self, token, *roles):
        actor = self.sessions.get(token)
        account = self.store.one(
            "SELECT id,username,role,enabled FROM accounts WHERE id=?", (actor,)
        )
        if not account or not account["enabled"]:
            raise BusinessError("auth", "请使用有效账号登录")
        if roles and account["role"] not in roles:
            raise BusinessError("forbidden", "该角色无权操作")
        return account

    def require_hotel(self, actor, hotel):
        if actor["role"] == "admin":
            return
        granted = actor["role"] == "staff" and self.store.one(
            "SELECT 1 FROM grants WHERE user_id=? AND hotel_id=?",
            (actor["id"], hotel),
        )
        if not granted:
            raise BusinessError("forbidden", "无权操作该酒店")

    def admin_list(self, token):
        self.require(token, "admin")
        return [
            dict(row)
            for row in self.store.all(
                """SELECT a.id,a.username,a.role,a.enabled,
                      GROUP_CONCAT(g.hotel_id) AS hotels
               FROM accounts a LEFT JOIN grants g ON g.user_id=a.id
               GROUP BY a.id ORDER BY a.id"""
            )
        ]

    def assign(self, token, username, role, hotel=None):
        with self.store.transaction():
            actor = self.require(token, "admin")
            target = self.store.one(
                "SELECT id,role,enabled FROM accounts WHERE username=?",
                (required(username, "账号").lower(),),
            )
            if not target or not target["enabled"]:
                raise BusinessError("not_found", "未找到可用账号")
            if role not in ("staff", "admin"):
                raise BusinessError("invalid", "只能授予工作人员或管理员身份")
            if target["role"] == "admin":
                raise BusinessError("invalid", "该账号已有管理员权限")
            if role == "staff":
                integer(hotel, "酒店编号")
                if not self.store.one("SELECT id FROM hotels WHERE id=?", (hotel,)):
                    raise BusinessError("not_found", "酒店不存在")
                self.store.execute(
                    "INSERT OR IGNORE INTO grants(user_id,hotel_id) VALUES(?,?)",
                    (target["id"], hotel),
                )
            else:
                hotel = None
                self.store.execute(
                    "DELETE FROM grants WHERE user_id=?", (target["id"],)
                )
            self.store.execute(
                "UPDATE accounts SET role=? WHERE id=?", (role, target["id"])
            )
            self.store.log(
                actor["id"],
                hotel,
                timestamp(self.clock()),
                "assign",
                str(target["id"]),
                before=target["role"],
                after=role,
            )

    def revoke(self, token, user_id, hotel):
        with self.store.transaction():
            actor = self.require(token, "admin")
            removed = self.store.execute(
                "DELETE FROM grants WHERE user_id=? AND hotel_id=?", (user_id, hotel)
            )
            if not removed.rowcount:
                raise BusinessError("not_found", "该授权不存在")
            self.store.log(
                actor["id"],
                hotel,
                timestamp(self.clock()),
                "revoke",
                str(user_id),
                before="granted",
                after="revoked",
            )

    def set_enabled(self, token, user_id, enabled):
        with self.store.transaction():
            actor = self.require(token, "admin")
            enabled = flag(enabled, "账号状态")
            target = self.store.one(
                "SELECT enabled FROM accounts WHERE id=?", (user_id,)
            )
            if not target:
                raise BusinessError("not_found", "账号不存在")
            if actor["id"] == user_id and not enabled:
                raise BusinessError("invalid", "不能停用当前管理员")
            self.store.execute(
                "UPDATE accounts SET enabled=? WHERE id=?", (enabled, user_id)
            )
            self.store.log(
                actor["id"],
                None,
                timestamp(self.clock()),
                "account_status",
                str(user_id),
                before=str(target["enabled"]),
                after=str(enabled),
            )
