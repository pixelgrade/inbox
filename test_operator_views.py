"""Operator pages: richer list rows, the draft review queue, change checks and cached assets."""

import base64
from contextlib import closing
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import intake_triage
import operator_ui
import store

ANA = {"id": 1, "first": "Ana", "last": "Albu", "email": "ana@example.test"}
AGENT = {"id": 19, "type": "user", "first": "Sam"}


class OperatorViewTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        env = patch.dict(os.environ, {
            "INBOX_DB": str(Path(self.scratch.name) / "mail.db"), "INBOX_BASE_URL": "https://inbox.test",
            "INBOX_OPERATOR_WRITES": "1", "INBOX_SEND_ENABLED": "1", "INBOX_AGENT_ID": "19"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app.app, base_url="https://inbox.test").__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = store.connect()
        self.addCleanup(self.db.close)
        with store.transaction(self.db):
            store.save_mailbox(self.db, {"id": 1, "email": "help@example.test"})
            store.save_customer(self.db, {"id": 1, "firstName": "Ana", "_embedded": {"emails": [{"value": "ana@example.test"}]}})
            self.db.execute("UPDATE writer_authority SET mode='live'")
            store.import_conversation(self.db, self.conversation(5, "Old question", "closed"), [
                self.thread(51, "customer", "<p>Earlier.</p>"), self.thread(52, "message", "<p>Answered.</p>")])
            store.import_conversation(self.db, self.conversation(
                7, "Header overlaps logo", "active", tags=[{"id": 1, "tag": "maple"}], threads=4,
                preview="The header overlaps my logo", customerWaitingSince={"time": "2026-01-01T10:00:00Z"}), [
                self.thread(71, "customer", "<p>The header overlaps my logo.</p><script>alert(1)</script>"),
                self.thread(72, "lineitem", "", action={"type": "automatic-workflow", "text": "Tagged maple"}),
                self.thread(73, "note", "<p>Confidence 8 of 10.</p>"),
                self.thread(74, "message", "<p>Set the logo height to 60px.</p>", state="draft")])
            # A draft left on a closed conversation is history, not work to review.
            store.import_conversation(self.db, self.conversation(8, "Closed with a draft", "closed"), [
                self.thread(81, "customer", "<p>Never mind.</p>"), self.thread(82, "message", "<p>Abandoned.</p>", state="draft")])
            session = operator_ui.issue(self.db, "session", 600)
        self.client.cookies.set(operator_ui.COOKIE, session)
        self.origin = {"Origin": "https://inbox.test"}

    def conversation(self, cid, subject, status, **extra):
        return {"id": cid, "number": cid, "mailboxId": 1, "type": "email", "state": "published", "status": status,
                "subject": subject, "createdAt": "2026-01-01T10:00:00Z", "modifiedAt": f"2026-01-0{cid}T10:00:00Z",
                "primaryCustomer": ANA, "assignee": AGENT, "tags": [], **extra}

    def thread(self, tid, kind, body, **extra):
        return {"id": tid, "type": kind, "state": "published", "createdAt": f"2026-01-01T10:{tid % 60:02}:00Z",
                "body": body, "createdBy": ANA if kind == "customer" else AGENT, **extra}

    def test_list_rows_show_who_what_and_state(self):
        page = self.client.get("/?status=active").text
        self.assertIn("Ana Albu", page)
        self.assertIn("The header overlaps my logo", page)
        self.assertIn("Draft ready", page)
        self.assertRegex(page, r"Waiting \d+d")
        self.assertIn('<span class="chip">maple</span>', page)
        self.assertIn("4 messages", page)
        self.assertIn('<time datetime="2026-01-07T10:00:00Z">', page)
        closed = self.client.get("/?status=closed").text
        self.assertNotIn("Draft ready", closed)
        self.assertNotIn("Waiting", closed)

    def test_rail_counts_match_visible_pending_and_snoozed_conversations(self):
        with store.transaction(self.db):
            self.db.execute("UPDATE conversations SET data=json_set(data,'$.status','pending') WHERE id=7")
            self.db.execute("INSERT INTO snoozes VALUES(5,'2100-01-01',0)")
        counts = operator_ui.rail_counts(self.db)
        self.assertEqual((counts["active"], counts["pending"], counts["mine"], counts["drafts"]), (0, 1, 1, 1))
        self.assertEqual((counts["closed"], counts["later"]), (1, 1))

    def test_conversation_page_adds_history_and_compact_activity(self):
        page = self.client.get("/conversation/7").text
        self.assertIn('<p class="activity" id="thread-72">', page)
        self.assertIn("Tagged maple", page)
        self.assertIn('href="/conversation/5"', page)  # Earlier conversation of the same customer.
        self.assertIn('data-version="', page)
        self.assertIn('id="similar"', page)
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("style=", page)

    def test_ticket_sequence_and_landing(self):
        page = self.client.get("/conversation/7").text
        markers = ['id="thread-71"', 'id="thread-72"', 'id="thread-73"', 'id="thread-74"', 'id="instruction"']
        self.assertEqual(sorted(markers, key=page.index), markers)
        self.assertIn('data-landing="thread-74"', page)

    def test_api_authorship_survives_an_offline_agent(self):
        self.db.execute("UPDATE threads SET data=json_set(data,'$.source.type','api') WHERE id=73")
        with patch.dict(os.environ, {"INBOX_PIPELINE_NAME": "Field Guide"}):
            page = self.client.get("/conversation/7").text
        note = page[page.index('id="thread-73"'):].split("</article>")[0]
        self.assertIn("Field Guide", note)
        self.assertNotIn("Sam", note)

    def test_ordinary_draft_text_edit_and_stale_guard(self):
        page = self.client.get("/conversation/7").text
        self.assertRegex(page, r'name="draft_text"[^>]*>Set the logo height to 60px\.</textarea>')
        data = dict(version=operator_ui.version(self.db, 7), action="edit-draft", thread="74",
                    draft_text="Hi Ana,\n\nUse <small> & save.")
        result = self.client.post("/conversation/7/edit", headers=self.origin, data=data)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(store.thread(self.db, 7, 74)["body"], "<p>Hi Ana,</p><p>Use &lt;small&gt; &amp; save.</p>")
        self.assertEqual(self.client.post("/conversation/7/edit", headers=self.origin, data=data).status_code, 409)

    def test_formatted_draft_preserves_markup_and_requires_source_edit(self):
        rich = '<p>Read <a href="https://example.org/guide">the guide</a>.</p>'
        self.db.execute("UPDATE threads SET data=json_set(data,'$.body',?) WHERE id=74", (rich,))
        page = self.client.get("/conversation/7").text
        self.assertIn("the guide</a>", page)
        self.assertNotIn('name="draft_text"', page)
        result = self.client.post("/conversation/7/edit", headers=self.origin, data={
            "version": operator_ui.version(self.db, 7), "action": "edit-draft", "thread": 74,
            "draft_text": "Read the guide."})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(store.thread(self.db, 7, 74)["body"], rich)

    def test_rich_draft_editor_preserves_links_and_emphasis(self):
        rich = '<p>Read <a href="https://example.org/guide">the <em>new</em> guide</a>.</p>'
        self.db.execute("UPDATE threads SET data=json_set(data,'$.body',?) WHERE id=74", (rich,))
        page = self.client.get("/conversation/7").text
        self.assertIn('data-rich-editor', page)
        edited = rich.replace("new", "updated")
        data = {"version": operator_ui.version(self.db, 7), "action": "edit-draft", "thread": 74, "draft_rich": edited}
        self.assertEqual(self.client.post("/conversation/7/edit", headers=self.origin, data=data).status_code, 200)
        self.assertEqual(store.thread(self.db, 7, 74)["body"], edited)
        # Mail with an unsupported structure must go through its source editor.
        self.db.execute("UPDATE threads SET data=json_set(data,'$.body',?) WHERE id=74", ("<table><tr><td>Details</td></tr></table>",))
        self.assertNotIn('data-rich-editor', self.client.get("/conversation/7").text)
        data["version"] = operator_ui.version(self.db, 7)
        self.assertEqual(self.client.post("/conversation/7/edit", headers=self.origin, data=data).status_code, 409)

    def test_draft_shortcuts_follow_send_permission_and_message_state(self):
        path = "/?status=active"
        self.assertIn('data-row-key="a"', self.client.get(path).text)
        with patch.dict(os.environ, {"INBOX_SEND_ENABLED": "0"}):
            page = self.client.get(path).text
            self.assertNotIn('data-row-key="a"', page)
            self.assertIn('data-row-key="e"', page)
        self.db.execute("UPDATE threads SET data=json_set(data,'$.type','note') WHERE id=74")
        self.assertNotIn("data-draft", self.client.get(path).text)

    def test_waiting_selector_uses_the_installed_ledgers_owner_key(self):
        import agent_ledger

        original = agent_ledger.view
        for owner in ("caseworker", "owner"):
            def waiting(*args):
                view = original(*args)
                view["state"].update(campaign="example", waiting_on=owner)
                return view

            with self.subTest(owner=owner), patch.object(agent_ledger, "view", waiting), patch.object(
                agent_ledger, "SETTABLE_WAITING", ("customer", owner, "blocked")
            ), patch.dict(os.environ, {"INBOX_AGENT_NAME": "Desk lead"}):
                page = self.client.get("/conversation/7").text
                self.assertIn(f'<option value="{owner}" selected>Me · Desk lead</option>', page)

    def test_archived_image_attachment_is_a_thumbnail(self):
        self.db.execute("""UPDATE threads SET data=json_set(data,'$.attachments',
            json('[{"id":543,"fileName":"capture.png","mimeType":"image/png"}]')) WHERE id=71""")
        page = self.client.get("/conversation/7").text
        self.assertIn('src="/attachment/7/543?image=1"', page)
        self.assertIn('href="/attachment/7/543"', page)

    def test_generic_archived_png_has_a_verified_inline_type(self):
        # A one-pixel grayscale PNG.
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg==")
        self.db.execute("""UPDATE threads SET data=json_set(data,'$.attachments',
            json('[{"id":543,"fileName":"capture.PNG","mimeType":"application/octet-stream"}]')) WHERE id=71""")
        self.db.execute("INSERT INTO attachments(id,thread_id,mime,content) VALUES(543,71,?,?)",
                        ("application/octet-stream", png))
        self.assertIn('src="/attachment/7/543?image=1"', self.client.get("/conversation/7").text)
        preview = self.client.get("/attachment/7/543?image=1")
        self.assertEqual(preview.headers["content-type"], "image/png")
        self.assertTrue(preview.headers["content-disposition"].startswith("inline"))
        self.assertEqual(preview.content, png)
        self.assertTrue(self.client.get("/attachment/7/543").headers["content-disposition"].startswith("attachment"))

    def test_plex_fonts_are_same_origin_and_versioned(self):
        import hashlib
        css = self.client.get("/assets/inbox.css").text
        urls = re.findall(r'url\("(/assets/[^"]+\.woff2?\?v=[0-9a-f]+)"\)', css)
        self.assertGreaterEqual(len(urls), 3)
        for url in urls:
            response = self.client.get(url)
            self.assertIn("immutable", response.headers["cache-control"])
            self.assertEqual(hashlib.sha256(response.content).hexdigest()[:16], url.split("=")[-1])
        self.assertIn("font-src 'self'", self.client.get("/").headers["Content-Security-Policy"])

    def test_version_endpoint_tracks_changes(self):
        first = self.client.get("/conversation-version/7").json()["version"]
        self.assertIn(f'data-version="{first}"', self.client.get("/conversation/7").text)
        with store.transaction(self.db):
            store.append_thread(self.db, 7, "note", {"text": "<p>Agent note</p>"})
        self.assertNotEqual(self.client.get("/conversation-version/7").json()["version"], first)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/conversation-version/7").status_code, 401)

    def test_review_queue_pairs_question_and_draft_and_sends(self):
        page = self.client.get("/review").text
        self.assertIn("Header overlaps logo", page)
        self.assertIn("The header overlaps my logo.", page)
        self.assertIn("Set the logo height to 60px.", page)
        self.assertIn("Confidence 8 of 10.", page)
        self.assertNotIn("Old question", page)
        self.assertNotIn("Closed with a draft", page)
        self.assertNotIn("alert(1)", page)
        version = re.search(r'name="version" value="([0-9a-f]+)"', page)[1]
        sent = self.client.post("/conversation/7/edit", headers=self.origin, follow_redirects=False, data={
            "version": version, "thread": "74", "next": "review", "action": "send-draft"})
        self.assertEqual((sent.status_code, sent.headers["location"]), (303, "/review"))
        self.assertEqual(self.db.execute("SELECT state FROM mail_outbox WHERE thread_id=74").fetchone()[0], "queued")
        self.assertIn("No drafts are waiting", self.client.get("/review").text)

    def test_review_queue_is_read_only_before_handover(self):
        with store.transaction(self.db):
            self.db.execute("UPDATE writer_authority SET mode='standby'")
        page = self.client.get("/review").text
        self.assertIn("Migration preview", page)
        self.assertNotIn('value="send-draft"', page)
        self.assertNotIn('value="discard-draft"', page)

    def test_mailbox_reader_follows_replaced_database_and_new_commits(self):
        import sqlite3
        path = Path(self.scratch.name) / "reader.db"
        replacement = path.with_suffix(".new")
        for file, value in ((path,1), (replacement,3)):
            with closing(sqlite3.connect(file)) as writer:
                writer.execute("CREATE TABLE sample(n)")
                writer.execute("INSERT INTO sample VALUES(?)", (value,))
                writer.commit()
        with patch.dict(os.environ, {"INBOX_DB": str(path)}):
            def read():
                with operator_ui.mailbox_reader() as reader:
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.execute("UPDATE sample SET n=0")
                    return reader.execute("SELECT n FROM sample").fetchone()[0]
            self.assertEqual(read(),1)
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("UPDATE sample SET n=2")
                writer.commit()
            self.assertEqual(read(),2)
            os.replace(replacement,path)
            self.assertEqual(read(),3)

    def test_assets_are_cached_only_under_their_fingerprint(self):
        page = self.client.get("/").text
        css = re.search(r'href="(/assets/inbox\.css\?v=[0-9a-f]{16})"', page)[1]
        self.assertIn("immutable", self.client.get(css).headers["cache-control"])
        self.assertEqual(self.client.get("/assets/inbox.css").headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.get("/assets/inbox.css?v=stale").headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.get("/").headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.get("/health").headers["cache-control"], "private, no-store")

    def test_new_pages_need_a_signed_in_operator(self):
        self.client.cookies.clear()
        for path in ("/review", "/triage", "/insights"):
            page = self.client.get(path)
            self.assertIn("Sign in to your inbox", page.text, path)
            self.assertNotIn("Header overlaps logo", page.text, path)
        for path in ("/similar/7", "/conversation-version/7"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_sign_in_returns_to_the_requested_section(self):
        for path in ("/review", "/insights", "/triage"):
            self.assertEqual(operator_ui.login_target(path), path)
        self.assertEqual(operator_ui.login_target("/insights?x=//evil.example"), "/")

    def test_intake_page_and_conversation_notice(self):
        verdict = {"category": "wrong_address", "confidence": "low", "score": 3,
                   "reasons": ["first message from this sender, and it names none of our products"],
                   "evidence": {"headers": ["x-mailer"], "mailer": "", "links": 0, "images": 0, "product_words": []}}
        with store.transaction(self.db):
            intake_triage.record(self.db, "raw/1", 7, verdict, False, store.now())
            intake_triage.record(self.db, "raw/2", None, dict(verdict, category="promotion", confidence="high"), False, store.now())
        page = self.client.get("/triage").text
        self.assertIn("Possibly meant for another business", page)
        self.assertIn("#7 · Header overlaps logo", page)
        self.assertIn("Observed before handover", page)
        self.assertIn("Verdicts are recorded only", page)
        self.assertEqual(self.client.get("/triage?category=nonsense").status_code, 400)
        self.assertNotIn("#7", self.client.get("/triage?category=promotion").text)
        self.assertIn("possibly meant for another business", self.client.get("/conversation/7").text)
        self.assertNotIn('class="intake-event"', self.client.get("/conversation/5").text)


if __name__ == "__main__":
    unittest.main()
