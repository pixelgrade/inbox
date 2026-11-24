import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import store
import native_workflows as workflows


def rule(rid, groups, actions, manual=False, order=0):
    return {
        "id": rid,
        "mailboxId": 1,
        "name": f"Rule {rid}",
        "statusLabel": "Active",
        "typeLabel": "Manual" if manual else "Automatic",
        "order": order,
        "conditions": [
            {
                "facets": [
                    {"type": str(t), "operator": o, "value": v} for t, o, v in group
                ]
            }
            for group in groups
        ],
        "actions": [{"type": t, "value": v} for t, v in actions],
    }


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")
        store.initialize(self.db)
        workflows.initialize(self.db)
        store.save_mailbox(self.db, {"id": 1})
        self.cid = store.create_conversation(
            self.db,
            {
                "mailboxId": 1,
                "subject": "A Starlight issue",
                "customer": {"email": "a@example.org"},
                "threads": [{"type": "customer", "text": "Need assistance"}],
            },
        )
        self.addCleanup(self.db.close)

    def install(self, *rules):
        workflows.install(
            self.db,
            {
                "version": 1,
                "mailboxId": 1,
                "rules": [{"definition": r} for r in rules],
            },
        )

    def test_order_matching_once_and_membership(self):
        self.install(
            rule(
                2,
                [[(80, 1, "starlight")]],
                [(10, {"id": 4, "name": "Billing"}), (50, 2)],
                order=2,
            ),
            rule(
                1,
                [[(150, 18, "other"), (60, 3, "starlight")]],
                [(70, ["starlight"])],
                order=1,
            ),
        )
        with store.transaction(self.db):
            workflows.observe(self.db, self.cid, "convo.created")
            workflows.observe(self.db, self.cid, "convo.tags")
        self.assertEqual(store.conversation(self.db, self.cid)["status"], "pending")
        self.assertEqual(workflows.folders(self.db, self.cid), [4])
        self.assertEqual(
            [
                t["action"]["associatedEntities"]["workflow"]
                for t in store.thread_list(self.db, self.cid)
                if "action" in t
            ],
            [2, 1],
        )

    def test_imported_receipts_and_word_boundaries(self):
        self.install(
            rule(7, [[(60, 3, "light")]], [(50, 4)]),
            rule(8, [[(60, 3, "Starlight")]], [(50, 3)]),
        )
        self.db.execute(
            "INSERT INTO threads(id,conversation_id,data) VALUES(?,?,?)",
            (
                9,
                self.cid,
                store.encode(
                    {
                        "id": 9,
                        "type": "lineitem",
                        "action": {
                            "type": "automatic-workflow",
                            "associatedEntities": {"workflow": 8},
                        },
                    }
                ),
            ),
        )
        workflows.observe(self.db, self.cid, "convo.created")
        self.assertEqual(store.conversation(self.db, self.cid)["status"], "active")

    def test_time_and_manual_send(self):
        self.install(
            rule(3, [[(40, 1, 2)], [(110, 8, "2:d")]], [(50, 3)]),
            rule(
                4,
                [],
                [
                    (
                        40,
                        {
                            "type": "email",
                            "from": 15,
                            "cc": [],
                            "bcc": [],
                            "body": "Hey {%customer.firstName,fallback=friend%}",
                        },
                    ),
                    (50, 3),
                ],
                manual=True,
            ),
        )
        store.update_conversation(self.db, self.cid, {"status": "pending"})
        workflows.tick(self.db, datetime(2040, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(store.conversation(self.db, self.cid)["status"], "pending")
        with (
            patch.dict(os.environ, {"INBOX_SEND_ENABLED": "0"}),
            self.assertRaises(store.Conflict),
        ):
            with store.transaction(self.db):
                workflows.run_manual(self.db, self.cid, 4, "sample")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0], 0
        )
        with (
            patch.dict(os.environ, {"INBOX_SEND_ENABLED": "1"}),
            store.transaction(self.db),
        ):
            a = workflows.run_manual(self.db, self.cid, 4, "sample")
            b = workflows.run_manual(self.db, self.cid, 4, "sample")
            self.assertEqual(a, b)
            self.assertEqual(
                store.thread(self.db, self.cid, a["replyThreadId"])["body"],
                "Hey friend",
            )
            store.publish(self.db, self.cid, a["replyThreadId"])
            self.assertEqual(store.conversation(self.db, self.cid)["status"], "closed")
            store.update_conversation(self.db, self.cid, {"status": "pending"})
            workflows.tick(self.db, datetime(2040, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(store.conversation(self.db, self.cid)["status"], "closed")

    def test_unknown_rule_operator_fails_import_without_replacing_config(self):
        self.install(rule(1, [[(40, 1, 2)]], [(50, 3)]))
        with self.assertRaises(store.Invalid), store.transaction(self.db):
            self.install(rule(2, [[(900, 1, "bogus")]], [(50, 3)]))
        self.assertEqual(
            self.db.execute("SELECT id FROM native_rules").fetchone()[0], 1
        )
