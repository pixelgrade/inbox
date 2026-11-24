import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import store
from app import app


class MailboxContract(unittest.IsolatedAsyncioTestCase):
    async def test_agent_collection_uses_source_visibility_order_and_page_size(self):
        for cid in range(1, 29):
            store.import_conversation(self.db, {
                "id": cid, "mailboxId": 1, "status": "pending", "type": "email",
                "state": "draft" if cid == 28 else "published",
                "primaryCustomer": {"id": 1, "email": "reader@example.test"},
                "createdAt": f"2026-01-{cid:02}T12:00:00Z",
                "modifiedAt": f"2026-02-{29-cid:02}T12:00:00Z",
            }, [])
        async def listing(**params):
            result = await self.http.get("conversations", params={"status": "all", **params})
            result.raise_for_status()
            data = result.json()
            return [row["id"] for row in data["_embedded"]["conversations"]], data["page"]

        ids, page = await listing(pageSize=100)
        self.assertEqual(page, {"number": 1, "size": 25, "totalElements": 27, "totalPages": 2})
        self.assertEqual(ids, list(range(27, 2, -1)))
        self.assertEqual((await listing(page=2))[0], [2, 1])
        self.assertEqual((await listing(sortField="modifiedAt"))[0], list(range(1, 26)))
        self.assertEqual((await listing(status="spam"))[1]["number"], 1)
        self.assertEqual((await self.http.get("conversations/28")).json()["state"], "draft")

    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "INBOX_DB": str(Path(self.scratch.name) / "tickets.db"),
                "INBOX_CLIENT_ID": "agent",
                "INBOX_CLIENT_SECRET": "private",
                "INBOX_INTAKE_CLIENT_ID": "shop",
                "INBOX_INTAKE_CLIENT_SECRET": "shop-secret",
                "INBOX_BASE_URL": "https://inbox.example.test",
                "INBOX_AGENT_ID": "19",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = store.connect()
        store.initialize(self.db)
        self.addCleanup(self.db.close)
        with store.transaction(self.db):
            store.save_mailbox(
                self.db,
                {"id": 1, "email": "support@example.test", "name": "Support"},
            )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://inbox.example.test/v2/",
        )
        self.addAsyncCleanup(self.http.aclose)
        self.http.headers["Authorization"] = await self.auth("agent", "private")

    async def auth(self, name, secret):
        response = await self.http.post(
            "oauth2/token",
            data={
                "client_id": name,
                "client_secret": secret,
                "grant_type": "client_credentials",
            },
        )
        self.assertEqual(response.status_code, 200)
        return "Bearer " + response.json()["access_token"]

    async def create(self, **overrides):
        data = {
            "mailboxId": 1,
            "type": "email",
            "subject": "Footer",
            "customer": {"email": "person@example.test"},
            "tags": ["maple"],
            "threads": [{"type": "customer", "text": "<p>Grid issue</p>"}],
        }
        r = await self.http.post("conversations", json={**data, **overrides})
        self.assertEqual(r.status_code, 201, r.text)
        return int(r.headers["resource-id"])

    async def test_agent_pages_search_and_thread_state(self):
        cid = await self.create()
        path = f"conversations/{cid}"
        draft = await self.http.post(
            path + "/reply", json={"text": "draft", "draft": True}
        )
        self.assertEqual(draft.status_code, 201)
        draft_id = draft.headers["Resource-Id"]
        r = await self.http.patch(
            path + f"/threads/{draft_id}",
            json={"op": "replace", "path": "/text", "value": "revised"},
        )
        self.assertEqual(r.status_code, 204)
        r = await self.http.post(
            path + "/notes", json={"text": "for the team", "status": "pending"}
        )
        self.assertEqual(r.status_code, 201)
        r = (await self.http.get(path, params={"embed": "threads"})).json()
        self.assertEqual(
            [t["type"] for t in r["_embedded"]["threads"]],
            ["note", "message", "customer"],
        )
        self.assertEqual(r["_embedded"]["threads"][1]["state"], "draft")
        self.assertEqual(r["_embedded"]["threads"][1]["body"], "revised")
        self.assertEqual(r["status"], "pending")
        self.assertEqual(
            self.db.execute("select count(*) from mail_outbox").fetchone()[0], 0
        )
        for query in [
            '(email:"person@example.test")',
            "(customerIds:%d)" % r["primaryCustomer"]["id"],
            "tag:maple grid",
            "(modifiedAt:[2020-01-01T00:00:00Z TO *])",
        ]:
            result = await self.http.get(
                "conversations", params={"query": query, "status": "all"}
            )
            self.assertEqual(
                [c["id"] for c in result.json()["_embedded"]["conversations"]],
                [cid],
                query,
            )
        self.assertEqual((await self.http.delete(path)).status_code, 409)

    async def test_inbound_attachment_and_import_replay(self):
        blob = b"test attachment"
        cid = await self.create(
            imported=True,
            threads=[
                {
                    "type": "customer",
                    "text": "file",
                    "attachments": [
                        {
                            "fileName": "test.txt",
                            "mimeType": "text/plain",
                            "data": base64.b64encode(blob).decode(),
                        }
                    ],
                }
            ],
        )
        conv = store.conversation(self.db, cid, embed=True)
        threads = conv["_embedded"]["threads"]
        aid = threads[0]["_embedded"]["attachments"][0]["id"]
        data = (
            await self.http.get(f"conversations/{cid}/attachments/{aid}/data")
        ).json()
        self.assertEqual(base64.b64decode(data["data"]), blob)
        conv["id"] = 220
        threads = [{**threads[0], "id": 444, "_embedded": {"attachments": []}}]
        for i in range(2):
            with store.transaction(self.db):
                store.import_conversation(self.db, conv, threads)
        self.assertEqual(
            [
                t["id"]
                for t in store.conversation(self.db, 220, True)["_embedded"]["threads"]
            ],
            [444],
        )
        self.assertEqual(
            self.db.execute("select count(*) from deliveries").fetchone()[0], 0
        )

    async def test_scoped_intake_no_read_or_reply(self):
        old = await self.create()
        self.http.headers["Authorization"] = await self.auth("shop", "shop-secret")
        own = await self.create()
        self.assertEqual((await self.http.get("conversations")).status_code, 403)
        self.assertEqual(
            (
                await self.http.post(
                    f"conversations/{old}/notes", json={"text": "private"}
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            (
                await self.http.post(
                    f"conversations/{own}/notes", json={"text": "reference"}
                )
            ).status_code,
            201,
        )
        self.assertEqual(
            (
                await self.http.post(
                    f"conversations/{own}/reply", json={"text": "reply"}
                )
            ).status_code,
            403,
        )
        r = await self.http.post(
            "conversations",
            json={
                "mailboxId": 1,
                "subject": "x",
                "customer": {"email": "someone@example.test"},
                "threads": [{"type": "reply", "text": "x"}],
            },
        )
        self.assertEqual(r.status_code, 403)

    async def test_publication_and_events_are_transactional(self):
        with store.transaction(self.db):
            self.db.execute(
                "insert into webhooks(data) values(?)",
                (
                    '{"url":"https://example.test/events","events":["convo.created","convo.agent.reply.created"],"state":"enabled","secret":"abc"}',
                ),
            )
            cid = store.create_conversation(
                self.db,
                {
                    "subject": "Mail",
                    "mailboxId": 1,
                    "customer": {"email": "one@example.test"},
                    "threads": [{"type": "customer", "text": "hi"}],
                },
            )
            tid = store.append_thread(
                self.db, cid, "reply", {"text": "hello", "status": "pending"}
            )
        self.assertNotEqual(store.thread(self.db, cid, tid)["state"], "published")
        with store.transaction(self.db):
            store.publish(self.db, cid, tid)
            store.publish(self.db, cid, tid)
        self.assertEqual(store.thread(self.db, cid, tid)["state"], "published")
        self.assertEqual(
            self.db.execute("select state from mail_outbox").fetchone()[0], "sent"
        )
        self.assertEqual(
            [r[0] for r in self.db.execute("select event from deliveries order by id")],
            ["convo.created", "convo.agent.reply.created"],
        )

    async def test_search_index_tracks_edit_and_discard(self):
        cid = await self.create(
            threads=[
                {"type": "reply", "text": "Searchable testing draft", "draft": True}
            ]
        )

        def results(needle):
            return self.db.execute(
                "SELECT rowid FROM thread_search WHERE thread_search MATCH ?", (needle,)
            ).fetchall()

        self.assertEqual(len(results("searchable")), 1)
        tid = store.thread_list(self.db, cid)[0]["id"]
        r = await self.http.patch(
            f"conversations/{cid}/threads/{tid}",
            json={"op": "replace", "path": "/text", "value": "Updated"},
        )
        self.assertEqual(r.status_code, 204)
        self.assertFalse(results("searchable"))
        self.assertTrue(results("updated"))
        self.assertEqual(
            (await self.http.delete(f"conversations/{cid}")).status_code, 204
        )
        self.assertFalse(results("updated"))

    async def test_retired_id_redirects_to_canonical_id_and_preserves_draft(self):
        cid = await self.create()
        with store.transaction(self.db):
            store.save_alias(self.db, 775, cid)
            store.save_alias(self.db, 776, 775)
        result = await self.http.get("conversations/776?embed=threads")
        self.assertEqual(result.status_code, 301)
        self.assertEqual(
            result.headers["Location"],
            f"https://inbox.example.test/v2/conversations/{cid}",
        )
        self.assertEqual((await self.http.delete("conversations/776")).status_code, 404)
        with self.assertRaises(store.Conflict), store.transaction(self.db):
            store.save_alias(self.db, cid, 775)

    async def test_bad_payload_rolls_back_all_writes(self):
        r = await self.http.post(
            "conversations",
            json={
                "customer": {"email": "new@example.test"},
                "mailboxId": 1,
                "subject": "bad",
                "threads": [
                    {"type": "customer", "text": "first"},
                    {"type": "wrong", "text": "bad"},
                ],
            },
        )
        self.assertEqual(r.status_code, 400)
        for table in (
            "conversations",
            "threads",
            "customers",
            "mail_outbox",
            "deliveries",
        ):
            self.assertEqual(
                self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
            )

    async def test_writer_pause_is_atomic_across_agent_and_intake(self):
        cid = await self.create()
        with store.transaction(self.db):
            self.db.execute("UPDATE writer_authority SET mode='standby'")
        self.assertEqual((await self.http.get(f"conversations/{cid}")).status_code, 200)
        response = await self.http.post(f"conversations/{cid}/notes", json={"text":"Private note"})
        self.assertEqual(response.status_code, 503)
        self.http.headers["Authorization"] = await self.auth("shop", "shop-secret")
        response = await self.http.post("conversations", json={
            "mailboxId":1, "subject":"Suspended intake","customer":{"email":"person@example.test"},
            "threads":[{"type":"customer","text":"Body"}],
        })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.db.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)
        self.assertEqual(len(store.thread_list(self.db,cid)), 1)
