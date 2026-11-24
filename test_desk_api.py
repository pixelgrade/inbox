"""The agent and operator see the same review queue, including its empty states."""
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
import unittest

import agent_ledger
import store
import app
import operator_ui


class DeskApi(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        env = patch.dict(os.environ, {
            "INBOX_DB": str(Path(scratch.name) / "desk.db"), "INBOX_BASE_URL": "https://inbox.test",
            "INBOX_CLIENT_ID": "reader", "INBOX_CLIENT_SECRET": "fixture",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app.app, base_url="https://inbox.test").__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = store.connect()
        self.addCleanup(self.db.close)
        customer = {"id": 1, "first": "Robin", "last": "Reed", "email": "reader@example.test"}
        with store.transaction(self.db):
            store.save_mailbox(self.db, {"id": 1, "email": "help@example.test"})
            for cid in (7, 9):
                store.import_conversation(self.db, {
                    "id": cid, "number": cid, "mailboxId": 1, "state": "published", "status": "active",
                    "type": "email", "subject": "Plain support", "primaryCustomer": customer,
                    "createdAt": "2026-01-01T10:00:00Z",
                }, [{"id": cid * 10, "type": "customer", "state": "published", "body": "A question",
                     "createdAt": "2026-01-01T10:01:00Z", "createdBy": customer}])
            session = operator_ui.issue(self.db, "session", 600)
        self.client.cookies.set(operator_ui.COOKIE, session)

    def token(self):
        result = self.client.post("/v2/oauth2/token", data={
            "grant_type": "client_credentials", "client_id": "reader", "client_secret": "fixture",
        })
        return {"Authorization": "Bearer " + result.json()["access_token"]}

    def test_page_and_gateway_show_the_same_order_and_counts(self):
        with store.transaction(self.db):
            self.db.execute("UPDATE conversations SET data=json_set(data,'$.number',909,'$.customerWaitingSince.time',?) WHERE id=9",
                            ("2026-01-01T10:01:00Z",))
            for cid, mode in ((9, "manual_review"), (7, "auto_send")):
                agent_ledger.record(self.db, cid, dict(kind="decision", actor="agent", title="Recorded decision",
                    data=dict(mode=mode, why="Check the attachment", confidence=7, blockers=["missing detail"])))
        desk = self.client.get("/v2/desk", headers=self.token()).json()
        page = self.client.get("/").text
        for group, css in (("held", "held-row"), ("handled", "receipt")):
            links = re.findall(fr'class="row {css}" href="/conversation/(\d+)"', page)
            self.assertEqual([str(row["conversation"]) for row in desk[group]], links)
            self.assertEqual(desk["counts"][group], 1)
            self.assertFalse(desk[group + "_more"])
        held, = desk["held"]
        self.assertEqual((held["number"], held["customer"], held["subject"]), (909, "Robin Reed", "Plain support"))
        self.assertEqual((held["kind"], held["reason"], held["confidence"], held["blockers"]),
                         ("manual_review", "Check the attachment", 7, ["missing detail"]))
        self.assertEqual(held["url"], "https://inbox.test/conversation/9")
        self.assertTrue(held["waiting"])
        handled, = desk["handled"]
        self.assertIn(handled["actor"] + " · " + handled["action"], page)
        self.assertEqual(handled["at"][:10], desk["day"])
        self.assertEqual(desk["day"], datetime.now(timezone.utc).date().isoformat())
        self.assertEqual(self.client.get("/v2/desk?p=2", headers=self.token()).json()["held"], [])

    def test_missing_ledger_is_distinct_from_an_empty_desk_and_requires_gateway(self):
        self.assertIn(self.client.get("/v2/desk").status_code, (401, 403))
        headers = self.token()
        self.assertIsNone(self.client.get("/v2/desk", headers=headers).json()["unavailable_reason"])
        with store.transaction(self.db):
            self.db.execute("DROP TABLE agent_events")
        desk = self.client.get("/v2/desk", headers=headers).json()
        self.assertEqual((desk["held"], desk["handled"], desk["unavailable_reason"]), ([], [], "ledger_unavailable"))
