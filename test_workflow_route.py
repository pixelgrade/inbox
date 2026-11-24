"""A manual workflow run over the agent route answers only once its reply has left."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app
import native_workflows as workflows
import store
from test_native_workflows import rule


class WorkflowRouteTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        env = patch.dict(os.environ, {
            "INBOX_DB": str(Path(self.scratch.name) / "mail.db"), "INBOX_BASE_URL": "https://inbox.test",
            "INBOX_CLIENT_ID": "agent", "INBOX_CLIENT_SECRET": "private", "INBOX_SEND_ENABLED": "1",
            "INBOX_AGENT_ID": "19", "INBOX_INITIAL_AUTHORITY": "live"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app.app, base_url="https://inbox.test").__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = store.connect()
        self.addCleanup(self.db.close)
        with store.transaction(self.db):
            store.save_mailbox(self.db, {"id": 1, "email": "help@example.test"})
            self.cid = store.create_conversation(self.db, {
                "mailboxId": 1, "subject": "Table for four", "customer": {"email": "guest@example.org"},
                "threads": [{"type": "customer", "text": "Could we book for Friday?"}]})
            workflows.install(self.db, {"version": 1, "mailboxId": 1, "rules": [{"definition": r} for r in (
                rule(18, [], [(40, {"type": "email", "from": 19, "cc": [], "bcc": [],
                                        "body": "Hello {%customer.firstName,fallback=%}! This did not reach the venue."}),
                                  (50, 3), (10, {"id": 24, "name": "Wrong Way"})], manual=True),
                rule(7, [], [(70, ["checked"])], manual=True))]})
        token = self.client.post("/v2/oauth2/token", data={
            "grant_type": "client_credentials", "client_id": "agent", "client_secret": "private"}).json()["access_token"]
        self.headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "agent-wrong-address-1"}

    def run_rule(self, rule_id, **headers):
        return self.client.post(f"/v2/conversations/{self.cid}/workflows/{rule_id}", headers={**self.headers, **headers})

    def test_reply_is_awaited_on_the_first_and_on_a_repeated_request(self):
        with patch.object(app, "await_delivery", AsyncMock()) as delivered:
            first = self.run_rule(18)
            self.assertEqual(first.status_code, 200, first.text)
            reply = first.json()["replyThreadId"]
            delivered.assert_awaited_once_with([reply])
            again = self.run_rule(18)
            self.assertEqual(again.json(), first.json())
            self.assertEqual(delivered.await_count, 2)
        conv = store.conversation(self.db, self.cid)
        self.assertEqual(conv["status"], "closed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0], 1)  # One email, ever.
        self.assertEqual(workflows.folders(self.db, self.cid), [24])

    def test_undelivered_reply_is_reported_to_the_caller(self):
        with patch.object(app, "await_delivery", AsyncMock(side_effect=HTTPException(502, "Mail transport rejected delivery"))):
            self.assertEqual(self.run_rule(18).status_code, 502)

    def test_a_workflow_without_a_reply_answers_at_once(self):
        with patch.object(app, "await_delivery", AsyncMock()) as delivered:
            done = self.run_rule(7, **{"Idempotency-Key": "tag-only"})
            self.assertEqual(done.status_code, 200, done.text)
            delivered.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
