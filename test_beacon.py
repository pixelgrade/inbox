import os
import re
import tempfile
from contextlib import closing
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import beacon
import store


class ContactTest(TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        ctx = patch.dict(
            os.environ,
            {
                "INBOX_DB": root.name + "/mail.db",
                "INBOX_PUBLIC_CONTACT_ENABLED": "1",
                "INBOX_BASE_URL": "https://inbox.example.test",
            },
        )
        ctx.start()
        self.addCleanup(ctx.stop)
        with closing(store.connect()) as db:
            store.initialize(db)
            beacon.initialize(db)
            store.save_mailbox(db, {"id": 1})
        app = FastAPI()
        app.include_router(beacon.router)
        self.client = TestClient(app, base_url="https://inbox.example.test")
        self.token = self.issue()
        self.fields = {
            "token": self.token,
            "email": "customer@example.org",
            "name": "Dana",
            "subject": "Maple support",
            "message": "<b>Need help</b>",
        }

    def issue(self):
        return re.search(
            'name="token" value="([^"]+)"', self.client.get("/contact").text
        ).group(1)

    def post(self, fields=None, files=None):
        return self.client.post(
            "/contact",
            data=fields or self.fields,
            files=files or {},
            headers={
                "Origin": "https://inbox.example.test",
                "Accept": "application/json",
            },
        )

    def test_form_submission_and_replay_with_attachment(self):
        f = {"attachment": ("screenshot.png", b"\x89PNG\r\n\x1a\ncontent", "image/png")}
        result = self.post(files=f)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json(), self.post(files=f).json())
        self.assertEqual(
            self.post(self.fields | {"message": "changed"}, f).status_code, 409
        )
        with closing(store.connect()) as db:
            cid = db.execute("select id from conversations").fetchone()[0]
            t = store.thread_list(db, cid)[0]
            self.assertEqual(t["source"]["type"], "beacon-v2")
            self.assertIn("&lt;b&gt;", t["body"])
            a = t["_embedded"]["attachments"][0]
            self.assertEqual(
                store.attachment(db, cid, a["id"]), (f["attachment"][1], "image/png")
            )
            self.assertEqual(
                db.execute("select count(*) from mail_outbox").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("select count(*) from conversations").fetchone()[0], 1
            )

    def test_bounds_origin_and_disabled(self):
        self.assertEqual(self.post(self.fields | {"email": "bad"}).status_code, 400)
        self.assertEqual(self.post(self.fields | {"token": "bad"}).status_code, 403)
        r = self.client.post(
            "/contact", data=self.fields, headers={"Origin": "https://example.invalid"}
        )
        self.assertEqual(r.status_code, 403)
        with patch.dict(os.environ, {"INBOX_PUBLIC_CONTACT_ENABLED": "0"}):
            self.assertIn("hello@woodland.example", self.client.get("/contact").text)
            self.assertEqual(self.post().status_code, 503)
        with patch.object(beacon, "FILE_LIMIT", 5):
            self.assertEqual(
                self.post(
                    files={"attachment": ("a.txt", b"longtext", "text/plain")}
                ).status_code,
                413,
            )

    def test_limits_do_not_block_retry(self):
        with patch.object(beacon, "IP_LIMIT", 1):
            self.assertEqual(self.post().status_code, 201)
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(
                self.post(self.fields | {"token": self.issue()}).status_code, 429
            )
