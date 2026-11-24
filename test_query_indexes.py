"""Indexed tag, sender and time-window filters return the same rows as the original scans."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import store


def conversation(cid, **fields):
    item = {
        "id": cid,
        "number": cid,
        "mailboxId": 1,
        "status": "closed",
        "type": "email",
        "state": "published",
        "subject": f"Question {cid}",
        "primaryCustomer": {"id": 7, "email": "reader@example.test"},
        "createdAt": "2026-01-01T10:00:00Z",
        "modifiedAt": "2026-01-01T10:00:00Z",
        "tags": [],
    }
    item.update(fields)
    return item


class QueryIndexTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        env = patch.dict(
            os.environ,
            {
                "INBOX_DB": str(Path(self.scratch.name) / "mail.db"),
                "INBOX_CLIENT_ID": "agent",
                "INBOX_CLIENT_SECRET": "private",
                "INBOX_BASE_URL": "https://inbox.test",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app.app, base_url="https://inbox.test").__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = store.connect()
        self.addCleanup(self.db.close)
        with store.transaction(self.db):
            store.save_mailbox(self.db, {"id": 1, "email": "help@example.test"})
        token = self.client.post(
            "/v2/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "agent",
                "client_secret": "private",
            },
        ).json()["access_token"]
        self.client.headers["Authorization"] = "Bearer " + token

    def test_index_command_loads_configured_extensions(self):
        directory = Path(self.scratch.name)
        marker = directory / "loaded"
        (directory / "fixture_index_extension.py").write_text(
            "from pathlib import Path\nPath(__file__).with_name('loaded').touch()\n"
        )
        env = dict(os.environ, PYTHONPATH=str(directory), INBOX_EXTENSIONS="fixture_index_extension")
        command = [sys.executable, "store.py", "indexes"]
        cwd = Path(__file__).resolve().parent
        subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True)
        self.assertTrue(marker.exists())
        env["INBOX_EXTENSIONS"] = "missing_index_extension"
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing_index_extension", result.stderr)

    def add(self, cid, **fields):
        with store.transaction(self.db):
            store.import_conversation(self.db, conversation(cid, **fields), [])

    def ids(self, **params):
        result = self.client.get(
            "/v2/conversations", params={"status": "all", **params}
        )
        self.assertEqual(result.status_code, 200, result.text)
        return sorted(c["id"] for c in result.json()["_embedded"]["conversations"])

    def plan(self, sql, params):
        return " ".join(
            row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + sql, params)
        )

    def test_tag_filter_follows_imports_edits_and_deletes(self):
        self.add(1, tags=[{"id": 4, "tag": "Maple"}, {"id": 5, "tag": "billing"}])
        self.add(2, tags=[{"id": 6, "tag": "willow"}])
        self.add(3, tags="maple")  # A malformed source value must not break the write.
        self.add(4, tags=[{"id": 7, "tag": 12}, "maple", {"id": 8}])
        self.assertEqual(self.ids(tag="maple"), [1])
        self.assertEqual(self.ids(query="tag:MAPLE"), [1])
        self.assertEqual(self.ids(query="tags:billing"), [1])

        self.add(1, tags=[{"id": 5, "tag": "billing"}])  # A source refresh drops a tag.
        self.assertEqual(self.ids(tag="maple"), [])
        replaced = self.client.put(
            "/v2/conversations/2/tags", json={"tags": ["willow", "maple"]}
        )
        # Tag edits require the live writer; the index must follow them when it is.
        if replaced.status_code == 503:
            with store.transaction(self.db):
                self.db.execute("UPDATE writer_authority SET mode='live'")
            replaced = self.client.put(
                "/v2/conversations/2/tags", json={"tags": ["willow", "maple"]}
            )
        self.assertEqual(replaced.status_code, 204, replaced.text)
        self.assertEqual(self.ids(tag="Maple"), [2])

        with store.transaction(self.db):
            self.db.execute("DELETE FROM conversations WHERE id=2")
        self.assertEqual(self.ids(tag="maple"), [])
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM conversation_tags WHERE conversation=2"
            ).fetchone()[0],
            0,
        )

    def test_tag_filter_reads_the_tag_index(self):
        clauses, params = [], []
        app.add_query("tag:maple", clauses, params)
        plan = self.plan(
            "SELECT id FROM conversations c WHERE " + " AND ".join(clauses), params
        )
        self.assertIn("conversation_tags", plan)
        self.assertNotIn("SCAN c", plan)

    def test_existing_mailbox_gains_the_tag_index_on_start(self):
        path = str(Path(self.scratch.name) / "older.db")
        with closing(sqlite3.connect(path, isolation_level=None)) as old:
            old.execute(
                "CREATE TABLE conversations(id INTEGER PRIMARY KEY, data TEXT NOT NULL, native INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '')"
            )
            old.execute(
                "INSERT INTO conversations(id,data) VALUES(9,?)",
                (store.encode(conversation(9, tags=[{"id": 1, "tag": "Cedar"}])),),
            )
        with closing(store.connect(path)) as db:
            # The pre-existing table lacks generated columns, so only the tag index is exercised.
            with store.transaction(db):
                store.index_tags(db)
            self.assertEqual(
                db.execute("SELECT tag,conversation FROM conversation_tags").fetchall()[
                    0
                ][:],
                ("Cedar", 9),
            )
            with store.transaction(db):
                store.index_tags(db)  # A second start leaves the index as it is.
            self.assertEqual(
                db.execute("SELECT count(*) FROM conversation_tags").fetchone()[0], 1
            )

    def test_time_windows_keep_exact_bounds_across_stored_formats(self):
        self.add(1, modifiedAt="2026-03-10T09:59:59Z")
        self.add(2, modifiedAt="2026-03-10T10:00:00Z")
        self.add(3, modifiedAt="2026-03-10T10:00:00.250000Z")
        self.add(4, modifiedAt="2026-03-10T12:30:00+02:00")  # 10:30 UTC
        self.add(5, modifiedAt="2026-03-11T00:00:00Z")
        self.add(6, modifiedAt="2026-03-09T23:00:00-03:00")  # 02:00 UTC on the 10th
        since = "2026-03-10T10:00:00Z"
        self.assertEqual(self.ids(modifiedSince=since), [2, 3, 4, 5])
        self.assertEqual(
            self.ids(query=f"(modifiedAt:[{since} TO *])"), [2, 3, 4, 5]
        )
        self.assertEqual(
            self.ids(query="(modifiedAt:[2026-03-10T02:00:00Z TO 2026-03-10T10:00:00Z])"),
            [1, 2, 6],
        )
        self.assertEqual(self.ids(query="(modifiedAt:[* TO 2026-03-10])"), [])
        self.assertEqual(self.ids(query="(modifiedAt:[2026-03-11 TO *])"), [5])
        self.assertEqual(self.ids(query="(createdAt:[2026-01-01 TO 2026-01-02])"), [1, 2, 3, 4, 5, 6])
        # A value SQLite cannot read as a date keeps matching nothing.
        self.assertEqual(self.ids(modifiedSince="yesterday"), [])
        # Dates at the edge of the calendar keep the exact comparison and never fail.
        self.assertEqual(self.ids(modifiedSince="0001-01-01T00:00:00Z"), [1, 2, 3, 4, 5, 6])
        self.assertEqual(self.ids(modifiedSince="0001-01-01"), [1, 2, 3, 4, 5, 6])
        self.assertEqual(self.ids(query="(modifiedAt:[0001-01-01 TO *])"), [1, 2, 3, 4, 5, 6])
        self.assertEqual(self.ids(query="(modifiedAt:[* TO 9999-12-31])"), [1, 2, 3, 4, 5, 6])
        self.assertEqual(self.ids(modifiedSince="9999-12-31T23:59:59+00:00"), [])

    def test_sorting_is_the_same_for_short_and_long_results(self):
        for cid in range(1, 8):
            self.add(cid, createdAt=f"2026-01-0{8 - cid}T10:00:00Z", modifiedAt=f"2026-02-0{cid}T10:00:00Z")

        def ordered(**params):
            result = self.client.get("/v2/conversations", params={"status": "all", **params})
            return [c["id"] for c in result.json()["_embedded"]["conversations"]]

        cases = [dict(sortField=f, sortOrder=o) for f in ("createdAt", "modifiedAt", "number", "id") for o in ("asc", "desc")]
        short = [ordered(**case) for case in cases]
        with patch.object(app, "SORT_AFTER_FILTER", 3):
            long = [ordered(**case) for case in cases]
        self.assertEqual(short, long)
        self.assertEqual(short[1], [1, 2, 3, 4, 5, 6, 7])  # createdAt descending.

    def test_large_mailbox_waits_for_the_index_command_and_scans_meanwhile(self):
        path = str(Path(self.scratch.name) / "large.db")
        with patch.object(store, "LARGE_MAILBOX", 2), patch.dict(os.environ, {"INBOX_DB": path}):
            with closing(sqlite3.connect(path, isolation_level=None)) as old:
                # A mailbox created before the tag table existed.
                old.execute("CREATE TABLE conversations(id INTEGER PRIMARY KEY, data TEXT NOT NULL, native INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '')")
                old.execute("CREATE TABLE threads(id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, data TEXT NOT NULL)")
                old.execute("CREATE INDEX thread_conversation ON threads(conversation_id,id)")
                for cid in (1, 2, 3):
                    old.execute("INSERT INTO conversations(id,data) VALUES(?,?)", (cid, store.encode(
                        conversation(cid, tags=[{"id": 1, "tag": "Maple"}] if cid != 2 else [],
                                     primaryCustomer={"id": 7, "email": f"reader{cid}@example.test"}))))
            with closing(store.connect(path)) as db:
                with store.transaction(db):
                    self.assertFalse(store.build_indexes(db))  # Start-up leaves a large mailbox alone.
                self.assertEqual(store.tag_filter(db), store.SCANNED_TAG)
                self.assertFalse(store.owners_ready(db))
                found = lambda: [r[0] for r in db.execute("SELECT c.id FROM conversations c WHERE " + store.tag_filter(db), ("maple",))]
                self.assertEqual(found(), [1, 3])
                with store.transaction(db):
                    self.assertTrue(store.build_indexes(db, force=True))  # `python store.py indexes`
                self.assertEqual(store.tag_filter(db), store.TAGGED)
                self.assertEqual(found(), [1, 3])
                with store.transaction(db):
                    self.assertTrue(store.build_indexes(db))  # Later starts find it ready.

    def test_time_window_reads_the_modified_index(self):
        clauses, params = [], []
        app.time_bound(clauses, params, "c.modified", ">=", "2026-03-10T10:00:00Z")
        plan = self.plan(
            "SELECT id FROM conversations c WHERE " + " AND ".join(clauses), params
        )
        self.assertIn("conversation_modified", plan)

    def test_word_search_finds_the_same_conversations_with_and_without_the_owner_table(self):
        def message(cid, tid, body):
            return {"id": tid, "type": "customer", "createdAt": "2026-01-01T10:00:00Z", "body": f"<p>{body}</p>"}

        with store.transaction(self.db):
            for cid, body in ((1, "The parallax hero stopped"), (2, "Invoice copy please"), (3, "My parallax is fine")):
                store.import_conversation(self.db, conversation(cid), [message(cid, cid * 10, body)])
        self.assertTrue(store.owners_ready(self.db))  # A small mailbox builds it at start-up.
        searches = lambda: (self.ids(query="parallax"), self.ids(query="2"), self.ids(query="invoice copy"))
        indexed = searches()
        self.assertEqual(indexed, ([1, 3], [2], [2]))
        clauses, params = [], []
        app.add_query("parallax", clauses, params, self.db)
        plan = self.plan("SELECT id FROM conversations c WHERE " + " AND ".join(clauses), params)
        self.assertIn("SEARCH o USING PRIMARY KEY", plan)
        self.assertNotIn("SEARCH t ", plan)
        # The table follows a re-import that drops a message, a move and a delete.
        with store.transaction(self.db):
            store.import_conversation(self.db, conversation(3), [message(3, 31, "Nothing to see")])
            self.db.execute("UPDATE threads SET conversation_id=2 WHERE id=10")
        self.assertEqual(self.ids(query="parallax"), [2])
        with store.transaction(self.db):
            self.db.execute("DELETE FROM conversations WHERE id=2")
        self.assertEqual(self.ids(query="parallax"), [])
        self.assertEqual(self.db.execute("SELECT thread,conversation FROM message_owner ORDER BY 1").fetchall()[0][:], (31, 3))
        # Without the ready mark the search reads the messages table and answers the same.
        with store.transaction(self.db):
            self.db.execute("DELETE FROM index_state")
        self.assertIn("JOIN threads t", store.word_matches(self.db))
        self.assertEqual(self.ids(query="nothing"), [3])

    def test_owner_table_fills_in_batches_while_messages_keep_arriving(self):
        with store.transaction(self.db):
            # Back to a mailbox that predates the owner table.
            for name in ("insert", "update", "delete"):
                self.db.execute(f"DROP TRIGGER message_owner_{name}")
            self.db.execute("DROP TABLE message_owner")
            self.db.execute("DELETE FROM index_state")
            for cid in range(1, 8):
                store.import_conversation(self.db, conversation(cid), [
                    {"id": cid * 10 + n, "type": "customer", "createdAt": "2026-01-01T10:00:00Z", "body": f"<p>word{cid}</p>"} for n in (1, 2)])
            store.index_owners(self.db)
        self.assertFalse(store.owners_ready(self.db))
        with store.transaction(self.db):
            position = store.fill_owners(self.db, 0, batch=3)
        self.assertEqual(position, 3)
        self.assertFalse(store.owners_ready(self.db))
        with store.transaction(self.db):
            # A message that arrives between batches is recorded by the trigger.
            store.import_conversation(self.db, conversation(9), [{"id": 91, "type": "customer", "createdAt": "2026-01-01T10:00:00Z", "body": "<p>late</p>"}])
        while position is not None:
            with store.transaction(self.db):
                position = store.fill_owners(self.db, position, batch=3)
        self.assertTrue(store.owners_ready(self.db))
        self.assertEqual(self.db.execute("SELECT count(*) FROM message_owner").fetchone()[0], 15)
        self.assertEqual(self.db.execute("SELECT count(*) FROM threads").fetchone()[0], 15)
        self.assertEqual(self.ids(query="late"), [9])

    def test_sender_filter_reads_indexes_and_keeps_both_matches(self):
        with store.transaction(self.db):
            store.save_customer(
                self.db,
                {
                    "id": 7,
                    "_embedded": {"emails": [{"value": "reader@example.test"}]},
                },
            )
        self.add(1)
        # A conversation can carry an address the profile no longer lists.
        self.add(2, primaryCustomer={"id": 8, "email": "Former@Example.test"})
        self.assertEqual(self.ids(query="email:reader@example.test"), [1])
        self.assertEqual(self.ids(query="email:former@example.test"), [2])
        clauses, params = [], []
        app.add_query("email:reader@example.test", clauses, params)
        plan = self.plan(
            "SELECT id FROM conversations c WHERE " + " AND ".join(clauses), params
        )
        self.assertIn("conversation_sender", plan)
        self.assertIn("conversation_customer", plan)
        self.assertNotIn("SCAN conversations", plan)


if __name__ == "__main__":
    unittest.main()
