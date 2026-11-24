"""Agent reading tools: a bounded plain-text brief and ranked earlier answers."""

from contextlib import closing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_context
import app
import operator_ui
import store

AGENT = {"id": 19, "type": "user", "first": "Sam"}


def customer(key, name):
    return {"id": key, "first": name, "last": "Reader", "email": f"{name.lower()}@example.test"}


def thread(tid, kind, at, body, **extra):
    item = {"id": tid, "type": kind, "state": "published", "createdAt": at, "body": body,
            "createdBy": AGENT if kind in ("message", "note") else None}
    item.update(extra)
    return item


class AgentContextTests(unittest.TestCase):
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
        with store.transaction(self.db):
            store.save_mailbox(self.db, {"id": 1, "email": "help@example.test"})
            for key, name in ((1, "Ana"), (2, "Ben"), (3, "Cleo")):
                store.save_customer(self.db, {"id": key, "firstName": name, "_embedded": {"emails": [{"value": f"{name.lower()}@example.test"}]}})
            self.add(10, 1, "Parallax hero stopped after update", "closed", ["maple"], [
                thread(101, "customer", "2025-03-01T10:00:00Z", "<p>The parallax hero stopped scrolling after the update.</p><p>Ana</p>"),
                thread(102, "message", "2025-03-01T12:00:00Z", "<p>Please update Maple Blocks; the parallax hero needs version 2.</p>",
                       rating={"rating": "great"})])
            self.add(11, 2, "Parallax question", "closed", ["willow"], [
                thread(111, "customer", "2025-04-01T10:00:00Z", "<p>How do I enable parallax on the hero?</p>"),
                thread(112, "message", "2025-04-01T12:00:00Z", "<p>Open the hero block and switch on parallax scrolling.</p>")])
            # Never offered as precedents: unanswered, still open, and started by us.
            self.add(12, 2, "Parallax hero again", "closed", ["maple"], [
                thread(121, "customer", "2025-05-01T10:00:00Z", "<p>The parallax hero is broken.</p>")])
            self.add(13, 2, "Parallax hero open", "active", ["maple"], [
                thread(131, "customer", "2025-05-02T10:00:00Z", "<p>My parallax hero flickers.</p>"),
                thread(132, "message", "2025-05-02T11:00:00Z", "<p>Looking into the parallax hero.</p>")])
            self.add(14, 3, "About your parallax hero", "closed", ["maple"], [
                thread(141, "message", "2025-05-03T10:00:00Z", "<p>We noticed your parallax hero.</p>"),
                thread(142, "customer", "2025-05-03T11:00:00Z", "<p>Thanks.</p>")], source={"type": "api", "via": "user"})
            self.add(15, 3, "Invoice copy", "closed", ["billing"], [
                thread(151, "customer", "2025-06-01T10:00:00Z", "<p>Please send my invoice copy.</p>"),
                thread(152, "message", "2025-06-01T11:00:00Z", "<p>The invoice is attached.</p>")])
            self.add(20, 1, "Hero parallax effect is not moving", "active", ["maple"], [
                thread(201, "customer", "2026-11-20T10:00:00Z",
                       "<p>Hi,</p><p>The parallax hero does not move on my homepage.<br>Ana</p>"
                       "<blockquote>On Monday support wrote: an old quoted reply about invoices</blockquote>",
                       _embedded={"attachments": [{"id": 5, "filename": "hero.png", "mimeType": "image/png", "size": 10}]}),
                thread(202, "note", "2026-11-20T10:05:00Z", "<p>Internal: looks like the known parallax issue.</p>"),
                thread(203, "lineitem", "2026-11-20T10:06:00Z", "", action={"type": "automatic-workflow", "text": "Tagged maple"}),
                thread(204, "message", "2026-11-20T10:10:00Z", "<p>Draft answer about parallax.</p>", state="draft")],
                customerWaitingSince={"time": "2026-11-20T10:00:00Z"})

    def add(self, cid, who, subject, status, tags, threads, **extra):
        names = {1: "Ana", 2: "Ben", 3: "Cleo"}
        store.import_conversation(self.db, {
            "id": cid, "number": cid, "mailboxId": 1, "type": "email", "state": "published", "status": status,
            "subject": subject, "createdAt": threads[0]["createdAt"], "modifiedAt": threads[-1]["createdAt"],
            "primaryCustomer": customer(who, names[who]), "source": {"type": "email", "via": "customer"},
            "tags": [{"id": i, "tag": t} for i, t in enumerate(tags, 1)], **extra}, threads)

    def test_plain_text_keeps_paragraphs_and_drops_quoted_mail(self):
        html = "<p>First&nbsp;line<br>second &amp; third</p><style>p{}</style><blockquote>old</blockquote><p>&gt; quoted line</p>"
        self.assertEqual(agent_context.plain(html), "First line\nsecond & third")
        self.assertIn("old", agent_context.plain(html, quotes=True))
        trailing = "<p>Thanks, that worked.</p><p>On 3 May 2026, Support wrote:</p>"
        self.assertEqual(agent_context.plain(trailing), "Thanks, that worked.")
        self.assertEqual(agent_context.plain("<script>alert(1)</script><p>Safe</p>"), "Safe")

    def test_brief_is_chronological_bounded_and_carries_history(self):
        b = agent_context.brief(self.db, 20)
        self.assertEqual([m["from"] for m in b["messages"]], ["customer", "note", "activity", "agent"])
        first = b["messages"][0]
        self.assertEqual(first["text"], "Hi,\n\nThe parallax hero does not move on my homepage.\nAna")
        self.assertEqual(first["attachments"][0]["filename"], "hero.png")
        self.assertEqual(b["messages"][2]["text"], "Tagged maple")
        self.assertEqual(b["draft"], {"id": 204, "text": "Draft answer about parallax."})
        self.assertEqual((b["status"], b["tags"], b["waiting_since"]), ("active", ["maple"], "2026-11-20T10:00:00Z"))
        self.assertEqual((b["customer"]["name"], b["customer"]["conversations"]), ("Ana Reader", 1))
        self.assertEqual([h["id"] for h in b["customer"]["recent"]], [10])
        self.assertEqual(b["links"]["web"], "https://inbox.test/conversation/20")
        self.assertIn("invoices", agent_context.brief(self.db, 20, quotes=True)["messages"][0]["text"])

    def test_long_histories_keep_the_opening_and_newest_messages(self):
        messages = [{"id": i, "text": "x" * 1000} for i in range(10)]
        kept, omitted = agent_context.fit(messages, 4500)
        self.assertEqual(kept[0]["id"], 0)
        self.assertEqual([m["id"] for m in kept[1:]], [7, 8, 9])
        self.assertEqual(omitted, 6)
        self.assertLessEqual(sum(len(m["text"]) for m in kept), 4500)
        short, none = agent_context.fit([{"id": 1, "text": "y" * 9000}], 1000)
        self.assertTrue(short[0]["truncated"] and len(short[0]["text"]) <= 1000 and none == 0)
        pair, _ = agent_context.fit([{"id": 1, "text": "a" * 20000}, {"id": 2, "text": "b" * 20000}], 24000)
        self.assertLessEqual(sum(len(m["text"]) for m in pair), 24000)

    def test_plain_text_is_linear_on_hostile_bodies_and_reads_every_message_kind(self):
        import time

        started = time.perf_counter()
        agent_context.plain("<br>" * 60000 + "On Monday someone wrote:")
        agent_context.plain("\n" * 200000 + "> quoted")
        self.assertLess(time.perf_counter() - started, 2)
        chat = agent_context.message({"id": 1, "type": "chat", "body": "<p>Typed in the chat</p>", "createdBy": {"type": "customer"}})
        self.assertEqual((chat["from"], chat["text"]), ("customer", "Typed in the chat"))
        call = agent_context.message({"id": 2, "type": "phone", "body": "<p>Call summary</p>", "createdBy": {"type": "user"}})
        self.assertEqual((call["from"], call["text"]), ("agent", "Call summary"))
        # Accented words are looked up the way the search index stores them.
        self.assertEqual(agent_context.key_terms(self.db, "Parállax hero"), agent_context.key_terms(self.db, "parallax hero"))

    def test_precedents_are_answered_closed_customer_conversations(self):
        found = agent_context.precedents(self.db, cid=20)
        self.assertIn("parallax", found["terms"])
        self.assertNotIn("ana", found["terms"])  # The customer's own name is no topic.
        self.assertEqual([p["id"] for p in found["precedents"]], [10, 11])  # The shared tag leads.
        top = found["precedents"][0]
        self.assertEqual((top["shared_tags"], top["rating"], top["replies"]), (["maple"], "great", 1))
        self.assertEqual(top["answer"], "Please update Maple Blocks; the parallax hero needs version 2.")
        self.assertEqual(top["web"], "https://inbox.test/conversation/10#thread-102")
        by_text = agent_context.precedents(self.db, "a copy of my invoice please", limit=3)
        self.assertEqual([p["id"] for p in by_text["precedents"]], [15])
        self.assertEqual(agent_context.precedents(self.db, "the and for")["precedents"], [])

    def test_fixed_wording_exchanges_are_no_precedent(self):
        with store.transaction(self.db):
            self.add(16, 3, "Parallax hero table booking", "closed", ["wrong-address"], [
                thread(161, "customer", "2025-07-01T10:00:00Z", "<p>Can I book the parallax hero table?</p>"),
                thread(162, "message", "2025-07-01T11:00:00Z", "<p>Your message did not reach the venue.</p>")])
            self.add(17, 3, "Parallax hero deals", "closed", ["spammy"], [
                thread(171, "customer", "2025-07-02T10:00:00Z", "<p>Parallax hero deals this week.</p>"),
                thread(172, "message", "2025-07-02T11:00:00Z", "<p>No thanks.</p>")])
        self.assertEqual([p["id"] for p in agent_context.precedents(self.db, cid=20)["precedents"]], [10, 11])

    def test_gateway_endpoints(self):
        self.assertEqual(self.client.get("/v2/conversations/20/brief").status_code, 401)
        token = self.client.post("/v2/oauth2/token", data={"grant_type": "client_credentials",
                                 "client_id": "agent", "client_secret": "private"}).json()["access_token"]
        auth = {"Authorization": "Bearer " + token}
        brief = self.client.get("/v2/conversations/20/brief?precedents=2", headers=auth)
        self.assertEqual(brief.status_code, 200, brief.text)
        self.assertEqual([p["id"] for p in brief.json()["precedents"]["precedents"]], [10, 11])
        self.assertEqual(self.client.get("/v2/conversations/20/brief?max_chars=5", headers=auth).status_code, 400)
        self.assertEqual(self.client.get("/v2/conversations/999/brief", headers=auth).status_code, 404)
        found = self.client.get("/v2/precedents", params={"q": "parallax hero stopped", "tag": "willow"}, headers=auth)
        self.assertEqual(found.json()["precedents"][0]["id"], 11)
        private = self.client.post("/v2/precedents", json={"q": "parallax hero stopped", "tag": "willow"}, headers=auth)
        self.assertEqual(private.json(), found.json())
        self.assertIn(self.client.post("/v2/precedents", json={"q": "hello"}).status_code, (401, 403))
        self.assertEqual(self.client.get("/v2/precedents", headers=auth).status_code, 400)

    def test_operator_similar_panel(self):
        self.assertEqual(self.client.get("/similar/20").status_code, 401)
        with closing(store.connect()) as db, store.transaction(db):
            session = operator_ui.issue(db, "session", 600)
        self.client.cookies.set(operator_ui.COOKIE, session)
        found = self.client.get("/similar/20").json()["precedents"]
        self.assertEqual([(p["id"], p["web"]) for p in found], [(10, "/conversation/10"), (11, "/conversation/11")])


if __name__ == "__main__":
    unittest.main()
