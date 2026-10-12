"""SQLite-backed DB-API double: real constraints/transactions, no external MySQL."""

import sqlite3
import threading
from contextlib import suppress
from datetime import datetime

import pymysql


class Cursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, parameters=()):
        sql = sql.replace("%s", "?").replace("FOR UPDATE", "")
        try:
            self.cursor.execute(
                sql,
                tuple(
                    value.isoformat(sep=" ") if isinstance(value, datetime) else value
                    for value in parameters
                ),
            )
        except sqlite3.IntegrityError as error:
            raise pymysql.IntegrityError(1062, "offline constraint") from error
        except sqlite3.OperationalError as error:
            raise pymysql.OperationalError(1146, "offline schema") from error
        return self.cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def lastrowid(self):
        return self.cursor.lastrowid


class Connection:
    def __init__(self, database):
        self.db = database
        database.lock.acquire()

    def cursor(self):
        return Cursor(self.db.connection)

    def begin(self):
        self.db.connection.execute("BEGIN")

    def commit(self):
        self.db.connection.commit()

    def rollback(self):
        self.db.connection.rollback()

    def close(self):
        self.db.lock.release()


class FakeDatabase:
    def __init__(self):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE agent_bot_ai_provider (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE, protocol TEXT, base_url TEXT,
            credential_ref TEXT, enabled INTEGER, description TEXT, revision INTEGER,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE agent_bot_ai_model (
            id INTEGER PRIMARY KEY, alias TEXT UNIQUE COLLATE BINARY, upstream_model TEXT,
            provider_id INTEGER REFERENCES agent_bot_ai_provider(id), enabled INTEGER,
            revision INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE agent_bot_ai_session (
            execution_id TEXT PRIMARY KEY, session_id TEXT, request_id TEXT,
            configured_model TEXT, model TEXT, status TEXT, duration_ms INTEGER,
            usage TEXT, created_at TEXT, error_code TEXT, prompt TEXT, content TEXT);
        """)

    def connect(self, **kwargs):
        return Connection(self)

    def close(self):
        with suppress(Exception):
            self.connection.close()
