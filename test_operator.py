import os
import base64
from contextlib import closing
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import app
import asset_library
import standard_folders
import operator_ui
import store


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env=patch.dict(os.environ,{"INBOX_DB":self.directory.name+"/mail.db","INBOX_BASE_URL":"https://inbox.test",
            "INBOX_OPERATOR_WRITES":"1","INBOX_SEND_ENABLED":"1","INBOX_AGENT_ID":"15"})
        env.start()
        self.addCleanup(env.stop)
        self.client=TestClient(app.app,base_url="https://inbox.test").__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.db=store.connect()
        self.addCleanup(self.db.close)
        with store.transaction(self.db):
            store.save_mailbox(self.db,{"id":1,"email":"help@example.org"})
            self.code=operator_ui.issue(self.db,"login",300)
        self.origin={"Origin":"https://inbox.test"}

    def test_ticket_revision_reaches_agent_and_preserves_original_until_accepted(self):
        from test_workbench import install
        from workbench import assignments, core
        self.client.post("/session", headers=self.origin, data={"code": self.code})
        with store.transaction(self.db):
            install(self.db, "journal")
            assignments.claim(self.db, "journal", "Woodland agent")
            cid = store.create_conversation(self.db, {
                "mailboxId": 1, "subject": "Refresh", "customer": {"email": "reader@example.org"},
                "threads": [{"type": "customer", "text": "How do I refresh my journal?"}],
            })
            tid = store.append_thread(self.db, cid, "reply", {"text": "First attempt", "draft": True})
        path = f"/conversation/{cid}"
        data = {"version": operator_ui.version(self.db, cid), "product": "journal", "thread": tid,
                "body": "Mention the Sync command.", "action": "request-draft"}
        self.assertIn("Request changes", self.client.get(path).text)
        self.assertNotIn("Insert a saved reply", self.client.get(path).text)
        res = self.client.post(path+"/edit", headers=self.origin, data=data)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("Queued", res.text)
        self.assertEqual(self.client.post(path+"/edit", headers=self.origin, data=data).status_code, 409)
        with store.transaction(self.db):
            job = assignments.claim(self.db, "journal", "Woodland agent")
            case = core.case_details(self.db, "journal", job["id"])
            for expected in ("Mention the Sync", "First attempt", "How do I refresh"):
                self.assertIn(expected, case["question"])
            source = case["context"]["guidance"][0]["key"]
            core.propose(self.db, "journal", job["id"], "Choose Sync to refresh your journal.", [source])
        self.assertEqual(store.thread(self.db, cid, tid)["body"], "First attempt")
        self.assertIn("Use this proposal as draft", self.client.get(path).text)
        result = self.client.post(f"/investigations/journal/{job['id']}", headers=self.origin,
                                  data={"action": "draft", "next": "ticket"})
        self.assertEqual(result.status_code, 200, result.text)
        revised = store.thread(self.db, cid, tid)
        self.assertIn("Choose Sync", revised["body"])
        self.assertEqual(revised["state"], "draft")
        self.assertEqual(self.db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0], 0)

    def test_company_operator_and_sign_in_instructions(self):
        with patch.object(operator_ui.company, "LOGIN_COMMAND", "woodland-inbox login"):
            response = self.client.post("/session", headers=self.origin, data={"code": "expired"})
            self.assertEqual(response.status_code, 401)
            self.assertIn("woodland-inbox login", response.text)
        self.client.post("/session", headers=self.origin, data={"code": self.code})
        with store.transaction(self.db):
            cid = store.create_conversation(self.db, {
                "mailboxId": 1, "subject": "Field entry", "customer": {"email": "reader@example.org"},
                "threads": [{"type": "customer", "text": "Sync question"}],
            })
        with patch.dict(os.environ, {"INBOX_AGENT_NAME": "Woodland support"}):
            page = self.client.get(f"/conversation/{cid}")
            self.assertEqual(page.status_code, 200)
            self.assertRegex(page.text, r'<option value="me"[^>]*>Woodland support</option>')

    def test_archived_inline_images_render_from_original_bytes(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aLXkAAAAASUVORK5CYII="
        )
        path = self.directory.name + "/library.db"
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="3"><rect width="2" height="3"/></svg>'
        with closing(asset_library.writer(path)) as library, library:
            for key, mime, body in (
                ("generic", "application/octet-stream", png),
                ("binary", "binary/octet-stream", png),
                ("unknown", "application/octet-stream", b"Unrecognized file"),
                ("vector", "application/octet-stream", svg),
            ):
                library.execute("INSERT INTO asset VALUES('inline',0,?,?,?,?)",
                                (key, mime, hashlib.sha256(body).hexdigest(), body))
        with patch.dict(os.environ, {"INBOX_ASSET_DB": path}):
            self.assertEqual(self.client.get("/v2/inline/generic").status_code, 401)
            self.client.post("/session", headers=self.origin, data={"code": self.code}, follow_redirects=False)
            for key in ("generic", "binary"):
                response = self.client.get("/v2/inline/" + key)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Content-Type"], "image/png")
                self.assertEqual(response.content, png)
            self.assertEqual(self.client.get("/v2/inline/unknown").status_code, 415)
            original = self.client.get("/v2/inline/unknown?download=1")
            self.assertEqual(original.status_code, 200)
            self.assertEqual(original.content, b"Unrecognized file")
            self.assertTrue(original.headers["Content-Disposition"].startswith("attachment"))
            vector = self.client.get("/v2/inline/vector")
            self.assertEqual(vector.status_code, 200)
            self.assertEqual(vector.content, svg)
            self.assertEqual(vector.headers["Content-Type"], "image/svg+xml")
            self.assertTrue(vector.headers["Content-Disposition"].startswith("attachment"))
            self.assertIn("sandbox", vector.headers["Content-Security-Policy"])

    def test_login_private_threads_draft_publish_and_stale_version(self):
        self.assertIn('Sign in',self.client.get('/').text)
        login=self.client.post('/session',headers=self.origin,data={"code":self.code},follow_redirects=False)
        self.assertEqual(login.status_code,303)
        self.assertEqual(self.client.post('/session',headers=self.origin,data={"code":self.code}).status_code,401)
        compose={"operation":"fixture-1","email":"fixture@example.org","subject":"Demo thread","body":"First draft","action":"draft"}
        created=self.client.post('/compose',headers=self.origin,data=compose,follow_redirects=False)
        self.assertEqual(created.status_code,303)
        location=created.headers["location"]
        self.assertEqual(self.client.post('/compose',headers=self.origin,data=compose,follow_redirects=False).headers["location"],location)
        page=self.client.get(location)
        self.assertEqual(page.status_code,200)
        self.assertIn('First draft',page.text)
        cid=int(location.split("/")[-1])
        self.assertEqual(store.conversation(self.db,cid)["state"], "draft")
        self.assertIn("Demo thread", self.client.get("/?status=drafts").text)
        self.assertNotIn("Demo thread", self.client.get("/?status=unassigned").text)
        tid=store.thread_list(self.db,cid)[0]["id"]
        version=operator_ui.version(self.db,cid)
        fields={"version":version,"thread":tid,"action":"send-draft"}
        self.assertEqual(self.client.post(location+"/edit",data=fields).status_code,403)
        self.assertEqual(self.client.post(location+"/edit",data=fields,headers=self.origin,follow_redirects=False).status_code,303)
        self.assertEqual(self.client.post(location+"/edit",data=fields,headers=self.origin).status_code,409)
        self.assertEqual(store.thread(self.db,cid,tid)["state"],"pending")
        self.assertEqual(self.db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0],1)
        with store.transaction(self.db):
            store.publish(self.db,cid,tid)
        self.assertEqual(store.conversation(self.db,cid)["state"],"published")
        self.assertNotIn("Demo thread",self.client.get("/?status=drafts").text)
        self.assertEqual(self.client.get('/?status=all&q=Demo').status_code,200)
        self.assertIn("Demo thread",self.client.get('/?status=all&q=Demo').text)
        self.client.post('/logout',headers=self.origin)
        self.assertNotIn("First draft",self.client.get(location).text)

    def test_message_html_preserves_links_without_scripts_or_tracking_images(self):
        with patch.dict(os.environ, {"INBOX_ARCHIVED_TICKET_HOST": "archive.example"}):
            clean=str(operator_ui.body_html({"body":'<script>alert(1)</script><img src="https://example.org/pixel"><a href="https://archive.example/conversation/123/66#thread-99">See</a>'},123))
        self.assertNotIn("alert",clean)
        self.assertNotIn("example.org",clean)
        self.assertIn('/conversation/123#thread-99',clean)

    def test_archived_drafts_and_primary_owner_folders(self):
        with store.transaction(self.db):
            for fid,kind in enumerate(["mytickets","assigned","drafts","open"],start=1):
                self.db.execute("INSERT INTO source_profile VALUES('folders',?,?)",(fid,store.encode({"id":fid,"type":kind,"name":kind})))
            for cid,state,owner in [(100,"active",None),(101,"closed",None),(102,"pending",15),(103,"pending",99)]:
                store.import_conversation(self.db,{"id":cid,"number":cid,"status":state,"state":"published","mailboxId":1,
                    "assignee":{"id":owner} if owner else None},[{"id":cid+100,"type":"message","state":"draft"}])
        counts={f["type"]:(f["activeCount"],f["totalCount"]) for f in standard_folders.listing(self.db,1)}
        self.assertEqual(counts,{"mytickets":(0,1),"assigned":(0,1),"drafts":(1,3),"open":(1,1)})

    def test_customer_rating_visible_on_the_rated_reply_and_api_thread(self):
        with store.transaction(self.db):
            store.import_conversation(self.db,{"id":2,"number":3,"mailboxId":1,"status":"closed","primaryCustomer":{"id":4,"email":"fixture@example.org"}},
                [{"id":9,"type":"message","createdAt":"2026-11-01","rating":{"rating":"great","comments":"Historical comment"}},
                 {"id":10,"type":"message","createdAt":"2026-11-02","customer":{"id":4},"bcc":["team@example.org"]}])
            self.db.execute("INSERT INTO rating_links(tid,token) VALUES(10,'private-token')")
        self.client.post('/session',headers=self.origin,data={"code":self.code})
        page=self.client.get('/conversation/2')
        self.assertIn("Historical comment",page.text)
        self.assertIn("team@example.org",page.text)
        response=self.client.post('/rating/private-token/okay',headers=self.origin,data={"comment":"Clear answer"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(store.thread(self.db,2,10)["rating"],{"rating":"okay","comments":"Clear answer","customerId":4})
        self.assertIn("Clear answer",self.client.get('/conversation/2').text)

    def test_snooze_visibility_reply_and_due_reminder(self):
        import native_workflows
        from datetime import datetime,timezone
        base={"id":400,"number":7,"status":"pending","state":"published","mailboxId":1,
              "assignee":{"id":15},"primaryCustomer":{"id":1,"email":"fixture@example.org"},"subject":"Wake fixture"}
        with store.transaction(self.db):
            store.import_conversation(self.db,base,[{"id":2,"type":"customer","state":"published","createdAt":"2026-11-23"}])
        self.client.post('/session',headers=self.origin,data={"code":self.code})
        def snooze(regardless=False):
            data={"version":operator_ui.version(self.db,400),"action":"snooze","until":"2099-03-03T09:00"}
            if regardless:
                data["regardless"]="yes"
            self.assertEqual(self.client.post('/conversation/400/edit',data=data,headers=self.origin,follow_redirects=False).status_code,303)
        snooze()
        self.assertNotIn("Wake fixture",self.client.get("/?status=mine").text)
        self.assertIn("Wake fixture",self.client.get("/?status=later").text)
        with store.transaction(self.db):
            store.append_thread(self.db,400,"customer",{"text":"Reply"})
        self.assertNotIn("snooze",store.conversation(self.db,400))
        self.assertEqual(store.conversation(self.db,400)["status"],"active")
        snooze(True)
        self.assertIn("Wake fixture",self.client.get("/?status=mine").text)
        with store.transaction(self.db):
            store.append_thread(self.db,400,"customer",{"text":"Another reply"})
            store.update_conversation(self.db,400,{"status":"closed"})
            native_workflows.tick(self.db,datetime(2099,3,3,9,0,tzinfo=timezone.utc))
        self.assertNotIn("snooze",store.conversation(self.db,400))
        self.assertEqual(store.conversation(self.db,400)["status"],"active")
        self.assertEqual(store.conversation(self.db,400)["assignee"]["id"],15)
        self.assertEqual(self.db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0],0)

    def test_delivered_operator_reply_uses_defaults_and_preserves_concurrent_reply(self):
        with store.transaction(self.db):
            self.db.execute("INSERT INTO mailbox_settings VALUES('general',?)",(store.encode({"convoStatus":2,"convoAssignee":3}),))
            cid=store.create_conversation(self.db,{"mailboxId":1,"subject":"Defaults","customer":{"email":"fixture@example.org"},"threads":[{"type":"customer","text":"Question"}]})
            tid=store.append_thread(self.db,cid,"reply",{"text":"Response"},source="web")
            self.assertNotIn("assignee",store.conversation(self.db,cid))
            store.publish(self.db,cid,tid)
            conv=store.conversation(self.db,cid)
            self.assertEqual(conv["status"],"pending")
            self.assertEqual(conv["assignee"]["id"],15)
            tid=store.append_thread(self.db,cid,"reply",{"text":"Next response","status":"closed"},source="web")
            store.append_thread(self.db,cid,"customer",{"text":"New question","createdAt":"2010-01-01T00:00:00Z"})
            store.publish(self.db,cid,tid)
            self.assertEqual(store.conversation(self.db,cid)["status"],"active")
            self.assertTrue(store.conversation(self.db,cid)["customerWaitingSince"])
