"""The key store: hashes only, show-once keys, and the seed rules."""

import os
import sqlite3
import tempfile
import unittest

from hindsight_gateway.store import Store, hash_key


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "gw.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store._db.close()
        self.tmp.cleanup()

    def reopen(self):
        self.store.flush_usage()
        self.store._db.close()
        self.store = Store(self.path)

    def by_name(self, name):
        return next((k for k in self.store.keys() if k.name == name), None)

    def test_only_hashes_are_stored(self):
        key = self.store.create("laptop", "hermes-alice")
        self.assertTrue(key.startswith("hgw_") and len(key) > 40)
        dump = "\n".join(sqlite3.connect(self.path).iterdump())
        self.assertNotIn(key, dump)
        self.assertIn(hash_key(key), dump)
        self.assertEqual(self.store.lookup(key), ("laptop", "hermes-alice"))

    def test_create_rotate_disable_delete(self):
        key = self.store.create("laptop", "hermes-alice")
        k = self.by_name("laptop")
        new = self.store.rotate(k.id)
        self.assertIsNone(self.store.lookup(key))
        self.assertEqual(self.store.lookup(new), ("laptop", "hermes-alice"))
        self.store.set_enabled(k.id, False)
        self.assertIsNone(self.store.lookup(new))
        self.store.set_enabled(k.id, True)
        self.store.update(k.id, "laptop2", "hermes-bob")
        self.assertEqual(self.store.lookup(new), ("laptop2", "hermes-bob"))
        self.store.delete(k.id)
        self.assertIsNone(self.store.lookup(new))
        actions = [e[2] for e in self.store.events()]
        for a in ("created", "rotated", "disabled", "enabled", "edited", "deleted"):
            self.assertIn(a, actions)

    def test_seed_imported_once_and_survives_restart(self):
        self.assertEqual(self.store.seed([("alice", "hermes-alice", "seed-a")]), ["seed alice: imported"])
        self.assertEqual(self.store.seed([("alice", "hermes-alice", "seed-a")]), [])
        self.reopen()
        self.assertEqual(self.store.lookup("seed-a"), ("alice", "hermes-alice"))
        self.assertEqual(self.by_name("alice").source, "seed")

    def test_ui_edits_to_seed_bank_and_status_stick(self):
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        k = self.by_name("alice")
        self.store.update(k.id, "alice", "hermes-shared")
        self.store.set_enabled(k.id, False)
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        k = self.by_name("alice")
        self.assertEqual((k.bank, k.enabled), ("hermes-shared", False))

    def test_secret_change_updates_seed_key(self):
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        notes = self.store.seed([("alice", "hermes-alice", "seed-a2")])
        self.assertEqual(notes, ["seed alice: key changed in its Secret, updated"])
        self.assertIsNone(self.store.lookup("seed-a"))
        self.assertEqual(self.store.lookup("seed-a2"), ("alice", "hermes-alice"))

    def test_deleted_seed_never_comes_back(self):
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        self.store.delete(self.by_name("alice").id)
        self.reopen()
        self.assertEqual(self.store.seed([("alice", "hermes-alice", "seed-a")]), [])
        self.assertIsNone(self.store.lookup("seed-a"))

    def test_rotated_seed_is_taken_over(self):
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        new = self.store.rotate(self.by_name("alice").id)
        self.assertEqual(self.by_name("alice").source, "ui")
        self.store.seed([("alice", "hermes-alice", "seed-a")])  # the Secret still has the old value
        self.assertIsNone(self.store.lookup("seed-a"))
        self.assertEqual(self.store.lookup(new), ("alice", "hermes-alice"))

    def test_renamed_seed_is_taken_over(self):
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        self.store.update(self.by_name("alice").id, "alice-main", "hermes-alice")
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        self.assertEqual([k.name for k in self.store.keys()], ["alice-main"])

    def test_seed_without_key_or_duplicate_value_is_skipped(self):
        notes = self.store.seed([("a", "hermes-a", ""), ("b", "hermes-b", "same"), ("c", "hermes-c", "same")])
        self.assertEqual(notes, ["seed a: no key in its Secret, skipped", "seed b: imported",
                                 "seed c: same key as b, skipped"])

    def test_usage_survives_restart(self):
        self.store.seed([("alice", "hermes-alice", "seed-a")])
        self.store.lookup("seed-a")
        self.reopen()
        self.assertIsNotNone(self.by_name("alice").last_used_at)

    def test_session_secret_is_stable(self):
        s1 = self.store.secret("session-signing-key")
        self.reopen()
        self.assertEqual(self.store.secret("session-signing-key"), s1)
        self.assertEqual(len(s1), 32)


if __name__ == "__main__":
    unittest.main()
