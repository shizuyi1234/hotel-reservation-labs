"""SQLite transactions and common persistence operations."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


class Store:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(SCHEMA.read_text(encoding="utf-8"))

    def close(self):
        self.connection.close()

    def execute(self, sql, parameters=()):
        return self.connection.execute(sql, parameters)

    def many(self, sql, parameters):
        return self.connection.executemany(sql, parameters)

    def one(self, sql, parameters=()):
        return self.execute(sql, parameters).fetchone()

    def all(self, sql, parameters=()):
        return self.execute(sql, parameters).fetchall()

    @contextmanager
    def transaction(self, write=True):
        self.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    @contextmanager
    def savepoint(self):
        self.execute("SAVEPOINT business_action")
        try:
            yield
        except BaseException:
            self.execute("ROLLBACK TO business_action")
            raise
        finally:
            self.execute("RELEASE business_action")

    def log(
        self, actor, hotel, at, action, target, result="ok",
        detail="", before=None, after=None,
    ):
        self.execute(
            """INSERT INTO audit(actor_id,hotel_id,at,action,target,
                                  before_status,after_status,result,detail)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (actor, hotel, at, action, target, before, after, result, detail),
        )
