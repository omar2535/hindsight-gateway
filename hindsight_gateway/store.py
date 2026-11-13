"""The key store: API keys (as hashes), the bank each one opens, and an activity log.

SQLite, one file. Only SHA-256 hashes of keys are stored; a key's full value is shown
once, when it's created or rotated. Safe to use from several threads.

Seed keys come from the environment (typically a Kubernetes Secret per agent). They are
imported once. After that the database owns them: a seed key that's rotated or deleted
here is never brought back by a restart.
"""

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

KEY_PREFIX = "hgw_"

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    bank         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    key_hint     TEXT NOT NULL,
    source       TEXT NOT NULL,          -- 'seed' (managed by its Secret) or 'ui'
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    last_used_at INTEGER
);
-- Seed names the UI took over (deleted or rotated): never imported again.
CREATE TABLE IF NOT EXISTS seed_tombstones (name TEXT PRIMARY KEY, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY,
    at     INTEGER NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def hint(key: str) -> str:
    """What the UI shows instead of the key: enough to tell keys apart, useless to an attacker."""
    return f"{key[:8]}…{key[-4:]}" if len(key) >= 16 else "…"


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Key:
    id: int
    name: str
    bank: str
    key_hint: str
    source: str
    enabled: bool
    created_at: int
    updated_at: int
    last_used_at: int | None


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._by_hash: dict[str, tuple[int, str, str, bool]] = {}  # hash -> (id, name, bank, enabled)
        self._used: dict[int, int] = {}  # id -> last use, flushed in batches
        self._reload()

    # -- lookups (the hot path: every proxied request) --------------------------------

    def _reload(self):
        rows = self._db.execute("SELECT key_hash, id, name, bank, enabled FROM keys").fetchall()
        self._by_hash = {h: (i, n, b, bool(e)) for h, i, n, b, e in rows}

    def lookup(self, key: str) -> tuple[str, str] | None:
        """(name, bank) for a valid, enabled key, else None. Records the use."""
        if not key:
            return None
        with self._lock:
            hit = self._by_hash.get(hash_key(key))
            if not hit or not hit[3]:
                return None
            self._used[hit[0]] = int(time.time())
            return hit[1], hit[2]

    def flush_usage(self):
        with self._lock:
            used, self._used = self._used, {}
            for key_id, at in used.items():
                self._db.execute("UPDATE keys SET last_used_at=? WHERE id=?", (at, key_id))

    # -- listing ------------------------------------------------------------------------

    def keys(self) -> list[Key]:
        self.flush_usage()
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, bank, key_hint, source, enabled, created_at, updated_at, last_used_at "
                "FROM keys ORDER BY bank, name").fetchall()
        return [Key(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6], r[7], r[8]) for r in rows]

    def get(self, key_id: int) -> Key | None:
        return next((k for k in self.keys() if k.id == key_id), None)

    def banks_in_use(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT bank FROM keys ORDER BY bank")]

    def events(self, limit: int = 50) -> list[tuple[int, str, str, str]]:
        with self._lock:
            return self._db.execute(
                "SELECT at, actor, action, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # -- changes ------------------------------------------------------------------------

    def _event(self, actor: str, action: str, detail: str):
        self._db.execute("INSERT INTO events (at, actor, action, detail) VALUES (?, ?, ?, ?)",
                         (int(time.time()), actor, action, detail))

    def create(self, name: str, bank: str, actor: str = "admin") -> str:
        """New key for `bank`. Returns the full key: the only time it's ever available."""
        key = new_key()
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "INSERT INTO keys (name, bank, key_hash, key_hint, source, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'ui', 1, ?, ?)", (name, bank, hash_key(key), hint(key), now, now))
            self._event(actor, "created", f"{name} → {bank}")
            self._reload()
        return key

    def rotate(self, key_id: int, actor: str = "admin") -> str:
        """Replace a key's value; the old one stops working at once. Returns the new key."""
        key = new_key()
        with self._lock:
            k = self._get_row(key_id)
            self._db.execute("UPDATE keys SET key_hash=?, key_hint=?, source='ui', updated_at=? WHERE id=?",
                             (hash_key(key), hint(key), int(time.time()), key_id))
            if k["source"] == "seed":
                self._tombstone(k["name"])
            self._event(actor, "rotated", k["name"])
            self._reload()
        return key

    def set_enabled(self, key_id: int, enabled: bool, actor: str = "admin"):
        with self._lock:
            k = self._get_row(key_id)
            self._db.execute("UPDATE keys SET enabled=?, updated_at=? WHERE id=?",
                             (int(enabled), int(time.time()), key_id))
            self._event(actor, "enabled" if enabled else "disabled", k["name"])
            self._reload()

    def update(self, key_id: int, name: str, bank: str, actor: str = "admin"):
        with self._lock:
            k = self._get_row(key_id)
            self._db.execute("UPDATE keys SET name=?, bank=?, updated_at=? WHERE id=?",
                             (name, bank, int(time.time()), key_id))
            if k["source"] == "seed" and k["name"] != name:
                # The seed would come back under its old name otherwise.
                self._tombstone(k["name"])
                self._db.execute("UPDATE keys SET source='ui' WHERE id=?", (key_id,))
            changes = []
            if k["name"] != name:
                changes.append(f"name {k['name']} → {name}")
            if k["bank"] != bank:
                changes.append(f"bank {k['bank']} → {bank}")
            if changes:
                self._event(actor, "edited", f"{name}: " + ", ".join(changes))
            self._reload()

    def delete(self, key_id: int, actor: str = "admin"):
        with self._lock:
            k = self._get_row(key_id)
            self._db.execute("DELETE FROM keys WHERE id=?", (key_id,))
            if k["source"] == "seed":
                self._tombstone(k["name"])
            self._event(actor, "deleted", f"{k['name']} ({k['bank']})")
            self._reload()

    def _tombstone(self, name: str):
        self._db.execute("INSERT OR REPLACE INTO seed_tombstones (name, at) VALUES (?, ?)",
                         (name, int(time.time())))

    def _get_row(self, key_id: int) -> dict:
        row = self._db.execute("SELECT name, bank, source FROM keys WHERE id=?", (key_id,)).fetchone()
        if not row:
            raise KeyError(key_id)
        return {"name": row[0], "bank": row[1], "source": row[2]}

    # -- seeds ----------------------------------------------------------------------------

    def seed(self, seeds: list[tuple[str, str, str]]) -> list[str]:
        """Import (name, bank, key) seeds. Returns what happened, for the log.

        - new name: imported
        - name the UI took over (deleted, rotated or renamed there): skipped
        - existing seed key with a different value (its Secret was changed): value updated
        - otherwise: left alone (bank and enabled state stay as set in the UI)
        """
        notes = []
        now = int(time.time())
        with self._lock:
            tomb = {r[0] for r in self._db.execute("SELECT name FROM seed_tombstones")}
            for name, bank, key in seeds:
                if not key:
                    notes.append(f"seed {name}: no key in its Secret, skipped")
                    continue
                if name in tomb:
                    continue
                h = hash_key(key)
                row = self._db.execute("SELECT id, source, key_hash FROM keys WHERE name=?", (name,)).fetchone()
                other = self._db.execute("SELECT name FROM keys WHERE key_hash=?", (h,)).fetchone()
                if row is None:
                    if other:
                        notes.append(f"seed {name}: same key as {other[0]}, skipped")
                        continue
                    self._db.execute(
                        "INSERT INTO keys (name, bank, key_hash, key_hint, source, enabled, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'seed', 1, ?, ?)", (name, bank, h, hint(key), now, now))
                    self._event("seed", "imported", f"{name} → {bank}")
                    notes.append(f"seed {name}: imported")
                elif row[1] == "seed" and row[2] != h:
                    if other:
                        notes.append(f"seed {name}: same key as {other[0]}, skipped")
                        continue
                    self._db.execute("UPDATE keys SET key_hash=?, key_hint=?, updated_at=? WHERE id=?",
                                     (h, hint(key), now, row[0]))
                    self._event("seed", "rotated", f"{name} (its Secret changed)")
                    notes.append(f"seed {name}: key changed in its Secret, updated")
            self._reload()
        return notes

    # -- settings -------------------------------------------------------------------------

    def secret(self, name: str) -> bytes:
        """A random value kept in the database (e.g. the session signing key)."""
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (name,)).fetchone()
            if row:
                return bytes.fromhex(row[0])
            value = secrets.token_bytes(32)
            self._db.execute("INSERT INTO meta (k, v) VALUES (?, ?)", (name, value.hex()))
            return value

    def log(self, actor: str, action: str, detail: str):
        with self._lock:
            self._event(actor, action, detail)
