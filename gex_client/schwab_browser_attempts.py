"""Durable backup-local attempt state for the Schwab operator bridge."""

import hashlib
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager

from .schwab_browser_grant import (
    GrantRejected, callback_code, exchange_and_validate, prepare_grant,
)
from . import schwab

TTL_SECONDS = 300


class Attempts:
    """Store state only. The authenticated relay supplies an opaque session binding."""

    def __init__(self, path):
        self.path = path
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError("unsafe_attempt_store")
        finally:
            os.close(fd)
        with closing(sqlite3.connect(path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempt (
                slot INTEGER PRIMARY KEY CHECK (slot = 1),
                owner TEXT NOT NULL, state TEXT NOT NULL, expires REAL NOT NULL,
                status TEXT NOT NULL
            )""")

    @contextmanager
    def _write(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _owner(binding):
        if not isinstance(binding, str) or not re.fullmatch(r"[0-9a-f]{64}", binding):
            raise GrantRejected("invalid_owner_binding")
        return hashlib.sha256(binding.encode("ascii")).hexdigest()

    def begin(self, binding):
        owner = self._owner(binding)
        with self._write() as db:
            prior = db.execute("SELECT expires,status FROM attempt").fetchone()
            now = time.time()
            if prior and (prior[1] == "exchanging" or prior[1] == "pending" and prior[0] > now):
                raise GrantRejected("attempt_busy")
            state, url = prepare_grant()
            db.execute("INSERT OR REPLACE INTO attempt VALUES (1,?,?,?,?)",
                       (owner, state, now + TTL_SECONDS, "pending"))
        return url

    def complete(self, binding, callback):
        owner = self._owner(binding)
        with self._write() as db:
            row = db.execute("SELECT state,expires,status FROM attempt WHERE owner=?", (owner,)).fetchone()
            if row is None or row[1] <= time.time() or row[2] != "pending":
                raise GrantRejected("attempt_unavailable")
            _, _, redirect = schwab._credentials()
            code = callback_code(callback, redirect, row[0])
            db.execute("UPDATE attempt SET status='exchanging',state='' WHERE slot=1")
        # A process crash leaves the slot blocked for operator reconciliation.
        try:
            exchange_and_validate(code)
        except GrantRejected as exc:
            if str(exc) == "provider_response_unconfirmed":
                raise
            with self._write() as db:
                db.execute("UPDATE attempt SET status='failed' WHERE slot=1")
            raise
        else:
            with self._write() as db:
                db.execute("UPDATE attempt SET status='succeeded' WHERE slot=1")
