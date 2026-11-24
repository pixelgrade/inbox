"""Insight figures are checked against a small mailbox whose answers are worked out by hand."""

from contextlib import closing
from datetime import datetime, timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import insights
import operator_ui
import store

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
ANA = {"id": 1, "first": "Ana", "last": "Albu", "email": "ana@example.test"}
BEN = {"id": 2, "first": "Ben", "last": "Berg", "email": "ben@example.test"}
AGENT = {"id": 19, "type": "user"}


def thread(tid, kind, at, **extra):
    item = {"id": tid, "type": kind, "state": "published", "createdAt": at,
            "body": "<p>Text</p>", "source": {"type": "email", "via": "customer"}}
    if kind == "message":
        item["source"] = {"type": "api", "via": "user"}
        item["createdBy"] = AGENT
    item.update(extra)
    return item


def conversation(cid, at, customer=ANA, **extra):
    item = {"id": cid, "number": cid, "mailboxId": 1, "type": "email", "state": "published",
            "status": "closed", "subject": f"Question {cid}", "createdAt": at, "modifiedAt": at,
            "primaryCustomer": customer, "source": {"type": "email", "via": "customer"}, "tags": []}
    item.update(extra)
    return item


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        env = patch.dict(os.environ, {
            "INBOX_DB": str(Path(self.scratch.name) / "mail.db"), "INBOX_BASE_URL": "https://inbox.test",
            "INBOX_CLIENT_ID": "agent", "INBOX_CLIENT_SECRET": "private", "INBOX_AGENT_ID": "19"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app.app, base_url="https://inbox.test").__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = store.connect()
        self.addCleanup(self.db.close)
        insights._cache.clear()
        maple, willow = {"id": 1, "tag": "maple"}, {"id": 2, "tag": "Willow"}
        with store.transaction(self.db):
            store.save_mailbox(self.db, {"id": 1, "email": "help@example.test"})
            add = lambda conv, threads: store.import_conversation(self.db, conv, threads)
            # Answered in 2 hours, one reply, closed after 5 hours, rated great.
            add(conversation(1, "2026-11-02T08:00:00Z", closedAt="2026-11-02T13:00:00Z", tags=[maple]), [
                thread(11, "customer", "2026-11-02T08:00:00Z"),
                thread(12, "message", "2026-11-02T10:00:00Z", rating={"rating": "great", "comments": ""}),
                thread(13, "lineitem", "2026-11-02T13:00:00Z", action={"type": "automatic-workflow"})])
            # Answered in 30 hours, two replies (one written in the inbox), rated not_good.
            add(conversation(2, "2026-11-03T09:00:00Z", closedAt="2026-11-06T09:00:00Z", tags=[maple, willow]), [
                thread(21, "customer", "2026-11-03T09:00:00Z"),
                thread(22, "message", "2026-11-04T15:00:00Z", source={"type": "web", "via": "user"},
                       rating={"rating": "not_good", "comments": "Too slow"}),
                thread(23, "customer", "2026-11-05T09:00:00Z"),
                thread(24, "message", "2026-11-06T09:00:00Z")])
            # Closed without any reply.
            add(conversation(3, "2026-10-10T22:00:00Z", customer=BEN, closedAt="2026-10-12T22:00:00Z"), [
                thread(31, "customer", "2026-10-10T22:00:00Z")])
            # Still waiting for four days, with an agent draft and no assignee.
            add(conversation(4, "2026-11-20T12:00:00Z", customer=BEN, status="active",
                             customerWaitingSince={"time": "2026-11-20T12:00:00Z"}), [
                thread(41, "customer", "2026-11-20T12:00:00Z"),
                thread(42, "message", "2026-11-21T12:00:00Z", state="draft")])
            # Outreach: two sent, one opened, one answered by the customer.
            add(conversation(5, "2026-11-10T10:00:00Z", source={"type": "api", "via": "user"}), [
                thread(51, "message", "2026-11-10T10:00:00Z", openedAt="2026-11-10T11:00:00Z"),
                thread(52, "customer", "2026-11-11T10:00:00Z")])
            add(conversation(6, "2026-11-11T10:00:00Z", customer=BEN, source={"type": "api", "via": "user"}), [
                thread(61, "message", "2026-11-11T10:00:00Z")])
            # The preceding period: Ana already wrote in, about Willow.
            add(conversation(7, "2026-07-15T10:00:00Z", closedAt="2026-07-15T12:00:00Z", tags=[willow]), [
                thread(71, "customer", "2026-07-15T10:00:00Z"), thread(72, "message", "2026-07-15T11:00:00Z")])
            # Mail a workflow tagged as not support is counted apart and feeds no support figure.
            add(conversation(10, "2026-11-13T10:00:00Z", customer=BEN, closedAt="2026-11-13T10:00:05Z",
                             tags=[{"id": 3, "tag": "Spammy"}]), [thread(101, "customer", "2026-11-13T10:00:00Z")])
            # Spam and whole unsent drafts stay out of every figure.
            add(conversation(8, "2026-11-12T10:00:00Z", status="spam"), [thread(81, "customer", "2026-11-12T10:00:00Z")])
            add(conversation(9, "2026-11-12T10:00:00Z", state="draft", status="active"), [
                thread(91, "message", "2026-11-12T10:00:00Z", state="draft")])

    def test_period_figures_match_the_hand_count(self):
        r = insights.report(self.db, 3, NOW)
        self.assertEqual(r["period"]["from"], "2026-09-01T00:00:00Z")
        self.assertEqual((r["volume"]["inbound"], r["volume"]["outbound"], r["volume"]["other"]), (4, 2, 1))
        self.assertEqual(r["volume"]["other_share"], 20)
        # The comparison span is as long as the elapsed part of this period: 9 June to 31 August.
        self.assertEqual(r["period"]["compared_from"], "2026-06-08T12:00:00Z")
        self.assertEqual(r["volume"]["inbound_change"], 300)  # One conversation in that span.
        self.assertEqual([(m["month"], m["inbound"], m["outbound"], m["other"]) for m in r["volume"]["months"]],
                         [("2026-09", 0, 0, 0), ("2026-10", 1, 0, 0), ("2026-11", 3, 2, 1)])
        self.assertEqual(r["volume"]["months"][2]["first_response_hours"], 16.0)
        response = r["response"]
        self.assertEqual((response["answered"], response["median_hours"], response["p90_hours"]), (2, 16.0, 27.2))
        self.assertEqual(response["within_day"], 50)
        self.assertEqual(response["median_resolution_hours"], 38.5)
        self.assertEqual((response["one_reply"], response["one_reply_share"]), (1, 50))
        self.assertEqual(response["closed_without_reply"], 1)
        self.assertEqual(r["replies"]["total"], 5)
        self.assertEqual({c["channel"]: c["count"] for c in r["replies"]["channels"]}, {"api": 4, "web": 1})
        self.assertEqual(r["replies"]["workflow_actions"], 1)
        self.assertEqual(r["outreach"], {"sent": 2, "opened": 1, "replied": 1, "open_share": 50, "reply_share": 50})
        self.assertEqual((r["ratings"]["great"], r["ratings"]["bad"], r["ratings"]["great_share"]), (1, 1, 50))
        self.assertEqual([(u["id"], u["thread"], u["comment"]) for u in r["ratings"]["unhappy"]], [(2, 22, "Too slow")])
        self.assertEqual((r["customers"]["total"], r["customers"]["returning"], r["customers"]["new"]), (2, 1, 1))
        self.assertEqual([(t["tag"], t["count"], t["before"]) for t in r["topics"]], [("maple", 2, 0), ("Willow", 1, 1)])
        self.assertEqual(r["rhythm"]["grid"][0][8], 1)  # Monday 2 November, 08:00 UTC.
        self.assertEqual(sum(map(sum, r["rhythm"]["grid"])), 4)

    def test_queue_and_history(self):
        r = insights.report(self.db, 3, NOW)
        queue = r["queue"]
        self.assertEqual((queue["active"], queue["drafts"], queue["unassigned"], queue["pending"]), (1, 1, 1, 0))
        self.assertEqual([w["count"] for w in queue["waiting"]], [0, 0, 1, 0])
        self.assertEqual((queue["oldest"]["id"], queue["oldest"]["days"]), (4, 4.0))
        self.assertEqual({h["month"]: h["count"] for h in r["history"]},
                         {"2026-07": 1, "2026-10": 1, "2026-11": 7})

    def test_report_never_reads_the_whole_mailbox(self):
        statements = []
        self.db.set_trace_callback(statements.append)
        insights.report(self.db, 3, NOW)
        self.db.set_trace_callback(None)
        reads = [q for q in statements if q.lstrip().upper().startswith("SELECT")]
        self.assertGreater(len(reads), 5)
        for query in reads:
            plan = " | ".join(row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + query))
            for table in ("c", "t", "conversations", "threads"):
                self.assertNotRegex(plan, rf"SCAN {table}(?! USING)(\b|$)", query[:90])

    def test_comparison_uses_equal_spans(self):
        # One customer conversation a day: the first days of a month must not read as a collapse.
        with store.transaction(self.db):
            for day in range(200):
                when = (NOW - __import__("datetime").timedelta(days=day, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                store.import_conversation(self.db, conversation(1000 + day, when, customer={"id": 50, "email": "daily@example.test"}),
                                          [thread(10000 + day, "customer", when)])
        early = datetime(2026, 11, 2, 0, 5, tzinfo=timezone.utc)
        r = insights.report(self.db, 3, early)
        self.assertLessEqual(abs(r["volume"]["inbound_change"]), 12)

    def test_conversations_created_here_are_read_from_their_first_message(self):
        with store.transaction(self.db):
            self.db.execute("UPDATE writer_authority SET mode='live'")
            store.save_customer(self.db, {"id": 60, "firstName": "Dee", "_embedded": {"emails": [{"value": "dee@example.test"}]}})
            ours = store.create_conversation(self.db, {"mailboxId": 1, "subject": "A note from us", "customer": {"id": 60},
                "createdAt": "2026-11-15T09:00:00Z", "threads": [{"type": "reply", "text": "<p>Hello</p>", "imported": True, "createdAt": "2026-11-15T09:00:00Z"}],
                "imported": True}, source="api")
            theirs = store.create_conversation(self.db, {"mailboxId": 1, "subject": "Maple question", "customer": {"id": 60},
                "createdAt": "2026-11-16T09:00:00Z", "threads": [{"type": "customer", "text": "<p>Help</p>", "createdAt": "2026-11-16T09:00:00Z"}],
                "imported": True}, source="email")
            store.append_thread(self.db, theirs, "reply", {"text": "<p>Answer</p>", "createdAt": "2026-11-16T19:00:00Z"}, imported=True)
        self.assertIsNone(store.conversation(self.db, ours).get("source"))
        r = insights.report(self.db, 3, NOW)
        self.assertEqual((r["volume"]["inbound"], r["volume"]["outbound"]), (5, 3))
        self.assertEqual({c["channel"]: c["count"] for c in r["volume"]["channels"]}.get("email"), 5)
        self.assertEqual(r["response"]["answered"], 3)
        self.assertEqual(r["response"]["median_hours"], 10.0)  # 2 h, 10 h and 30 h.

    def test_offset_timestamps_are_read_as_utc(self):
        with store.transaction(self.db):
            store.import_conversation(self.db, conversation(900, "2026-10-31T23:30:00-05:00", customer={"id": 70, "email": "tz@example.test"}),
                                      [thread(9001, "customer", "2026-10-31T23:30:00-05:00")])
        r = insights.report(self.db, 3, NOW)
        months = {m["month"]: m["inbound"] for m in r["volume"]["months"]}
        self.assertEqual(sum(months.values()), r["volume"]["inbound"])
        self.assertEqual(months["2026-11"], 4)  # 04:30 UTC on 1 November.
        self.assertEqual(r["rhythm"]["grid"][6][4], 1)  # Sunday, 04:00 UTC.

    def test_operator_page_and_agent_endpoint(self):
        self.assertIn("Sign in", self.client.get("/insights").text)
        self.assertEqual(self.client.get("/v2/insights").status_code, 401)
        with closing(store.connect()) as db, store.transaction(db):
            session = operator_ui.issue(db, "session", 600)
        self.client.cookies.set(operator_ui.COOKIE, session)
        page = self.client.get("/insights?months=3")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("First response", page.text)
        self.assertIn("<svg", page.text)
        self.assertNotIn("style=", page.text)  # The content policy allows no inline styles.
        self.assertEqual(self.client.get("/insights?months=5").status_code, 400)
        self.client.cookies.clear()
        token = self.client.post("/v2/oauth2/token", data={"grant_type": "client_credentials",
                                 "client_id": "agent", "client_secret": "private"}).json()["access_token"]
        data = self.client.get("/v2/insights?months=3", headers={"Authorization": "Bearer " + token})
        self.assertEqual(data.status_code, 200, data.text)
        self.assertEqual(data.json()["period"]["months"], 3)
        self.assertIn("queue", data.json())
        # "Right now" is read on every request, not served from the cached report.
        with store.transaction(self.db):
            store.import_conversation(self.db, conversation(950, "2026-11-23T10:00:00Z", status="active"), [thread(9501, "customer", "2026-11-23T10:00:00Z")])
        again = self.client.get("/v2/insights?months=3", headers={"Authorization": "Bearer " + token}).json()
        self.assertEqual(again["queue"]["active"], data.json()["queue"]["active"] + 1)
        self.assertEqual(again["generated"], data.json()["generated"])

    def test_a_stale_report_is_served_at_once_and_refreshed_behind(self):
        import time

        first = insights.cached(self.db, 3)
        with store.transaction(self.db):
            self.db.execute("UPDATE insight_reports SET computed=computed-3600")
            store.import_conversation(self.db, conversation(960, "2026-11-22T10:00:00Z"), [thread(9601, "customer", "2026-11-22T10:00:00Z")])
        stale = insights.cached(self.db, 3)
        self.assertEqual(stale["generated"], first["generated"])  # Served without waiting.
        for _ in range(100):
            fresh = insights.cached(self.db, 3)
            if fresh["generated"] != first["generated"] or fresh["volume"]["inbound"] != first["volume"]["inbound"]:
                break
            time.sleep(0.05)
        self.assertEqual(fresh["volume"]["inbound"], first["volume"]["inbound"] + 1)
        # A second worker reads the same saved report instead of computing its own.
        other = store.connect()
        self.addCleanup(other.close)
        with patch.object(insights, "report", side_effect=AssertionError("must not recompute")):
            self.assertEqual(insights.cached(other, 3)["volume"]["inbound"], fresh["volume"]["inbound"])


if __name__ == "__main__":
    unittest.main()
