import base64
from contextlib import closing
from email import policy
from email.parser import BytesParser

from fastapi.testclient import TestClient
import pytest

import app
import asset_library
import operator_actions as actions
import operator_ui
import store
import transport


@pytest.fixture
def mailbox(monkeypatch, tmp_path):
    for name, value in dict(
        INBOX_DB=str(tmp_path / "inbox.db"),
        INBOX_BASE_URL="https://inbox.test",
        INBOX_AGENT_ID="15",
        INBOX_OPERATOR_WRITES="1",
        INBOX_SEND_ENABLED="1",
    ).items():
        monkeypatch.setenv(name, value)
    with (
        TestClient(app.app, base_url="https://inbox.test") as client,
        closing(store.connect()) as db,
    ):
        transport.schema(db)
        with store.transaction(db):
            store.save_mailbox(db, {"id": 1, "name": "Test"})
            code = operator_ui.issue(db, "login", 300)
        client.post(
            "/session", headers={"Origin": "https://inbox.test"}, data={"code": code}
        )
        yield db, client


def make(db, email, subject, text="Example"):
    return store.create_conversation(
        db,
        dict(
            mailboxId=1,
            subject=subject,
            customer={"email": email},
            threads=[dict(type="customer", text=text)],
            imported=True,
        ),
    )


def post(client, cid, form):
    return client.post(
        f"/manage/{cid}",
        data=form,
        headers={"Origin": "https://inbox.test"},
        follow_redirects=False,
    )


def test_repeated_merge_retains_original_owners_numbers_receipts_and_full_text(
    mailbox, monkeypatch, tmp_path
):
    db, client = mailbox
    with store.transaction(db):
        dest = make(db, "one@example.test", "First")
        other = make(db, "one@example.test", "Second")
        source = make(db, "one@example.test", "Third", "archaeology")
        src = store.conversation(db, source)
        tid = store.thread_list(db, source)[0]["id"]
        aid = store.allocate(db, "attachment")
        thread = store.thread(db, source, tid)
        thread["_embedded"]["attachments"] = [
            dict(id=aid, filename="linked.txt", mimeType="text/plain")
        ]
        db.execute("UPDATE threads SET data=? WHERE id=?", (store.encode(thread), tid))
        db.execute(
            "INSERT INTO mail_ids VALUES(?,?,?)", ("<rfc@fixture.test>", source, tid)
        )
        db.execute(
            "INSERT INTO inbound_objects VALUES(?,?,?,?,?,?,?)",
            (
                "raw/a",
                "hash",
                "<rfc@fixture.test>",
                "imported",
                source,
                tid,
                store.now(),
            ),
        )
        db.execute(
            "INSERT INTO acknowledgments(cid,address,queued,state,receipt) VALUES(?,?,1,'sent','ack-id')",
            (source, "one@example.test"),
        )
        db.execute(
            "INSERT INTO contact_receipts VALUES(?,?,?,?,?,?,?)",
            ("nonce", "digest", source, src["number"], "ip", "hash", 4),
        )
        db.execute("INSERT INTO native_folders VALUES(1,1,'Saved')")
        db.execute("INSERT INTO native_folder_members VALUES(1,?)", (source,))
        db.execute(
            "INSERT INTO webhooks(data) VALUES(?)",
            (store.encode({"state": "disabled"}),),
        )
        db.execute(
            "INSERT INTO deliveries(webhook_id,event,conversation_id,payload,state) VALUES(1,'convo.created',?,'{}','sent')",
            (source,),
        )
        first = store.append_thread(db, source, "reply", {"text": "sent reply"})
        store.publish(db, source, first)
    path = tmp_path / "library.db"
    with closing(asset_library.writer(path)) as assets, assets:
        assets.execute(
            "INSERT INTO asset VALUES('attachment',?,?,?,? ,?)",
            (source, str(aid), "text/plain", "hash", b"verified original"),
        )
    monkeypatch.setenv("INBOX_ASSET_DB", str(path))
    fields = dict(
        action="merge",
        target=other,
        version=operator_ui.version(db, source),
        other_version=operator_ui.version(db, other),
    )
    assert (
        post(client, source, {**fields, "other_version": "outdated"}).status_code == 409
    )
    assert post(client, source, fields).headers["location"] == f"/conversation/{other}"
    assert (
        post(
            client,
            other,
            dict(
                action="merge",
                target=dest,
                version=operator_ui.version(db, other),
                other_version=operator_ui.version(db, dest),
            ),
        ).status_code
        == 303
    )
    assert actions.canonical(db, source) == actions.canonical(db, other) == dest
    assert (
        client.get(f"/conversation/{source}", follow_redirects=False).status_code == 301
    )
    assert store.attachment(db, dest, aid) == (b"verified original", "text/plain")
    assert store.thread(db, dest, tid)["id"] == tid
    assert (
        db.execute(
            "SELECT conversation_id FROM mail_outbox WHERE thread_id=?", (first,)
        ).fetchone()[0]
        == dest
    )
    assert (
        db.execute(
            "SELECT conversation FROM inbound_objects WHERE object_key='raw/a'"
        ).fetchone()[0]
        == source
    )
    assert (
        db.execute("SELECT cid FROM acknowledgments WHERE receipt='ack-id'").fetchone()[
            0
        ]
        == source
    )
    assert (
        db.execute("SELECT cid FROM contact_receipts WHERE nonce='nonce'").fetchone()[0]
        == source
    )
    assert (
        db.execute(
            "SELECT conversation_id FROM deliveries WHERE state='sent'"
        ).fetchone()[0]
        == source
    )
    assert (
        transport.known_conversation(db, "one@example.test", ["<rfc@fixture.test>"])
        == dest
    )
    assert (
        db.execute("SELECT conversation FROM native_folder_members").fetchone()[0]
        == dest
    )
    for q in ("archaeology", str(src["number"]), f"#{src['number']}"):
        assert "First" in client.get("/", params={"q": q, "status": "all"}).text
    assert sorted(
        tuple(r) for r in db.execute("SELECT source,target FROM operator_merges")
    ) == sorted([(source, other), (other, dest)])
    with store.transaction(db):
        new = make(db, "new@example.test", "Next")
    assert store.conversation(db, new)["number"] > src["number"]


def test_customer_reassignment_and_pending_delivery_fences(mailbox):
    db, client = mailbox
    with store.transaction(db):
        cid = make(db, "old@example.test", "Reassign")
        dest = make(db, "another@example.test", "Other")
        tid = store.append_thread(db, cid, "reply", {"text": "queued"})
        db.execute(
            "INSERT INTO mail_ids VALUES(?,?,?)", ("<identity@fixture.test>", cid, tid)
        )
    form = dict(
        action="customer",
        version=operator_ui.version(db, cid),
        email="new@example.test",
    )
    assert post(client, cid, form).status_code == 409
    with store.transaction(db):
        store.publish(db, cid, tid)
        draft = store.append_thread(db, cid, "reply", {"text": "draft", "draft": True})
    assert (
        post(client, cid, {**form, "version": operator_ui.version(db, cid)}).status_code
        == 409
    )
    with store.transaction(db):
        db.execute("DELETE FROM threads WHERE id=?", (draft,))
    assert (
        post(client, cid, {**form, "version": operator_ui.version(db, cid)}).status_code
        == 303
    )
    assert store.conversation(db, cid)["primaryCustomer"]["email"] == "new@example.test"
    assert store.thread(db, cid, tid)["to"] == ["old@example.test"]
    assert store.thread(db, cid, tid)["customer"]["email"] == "old@example.test"
    assert (
        transport.known_conversation(
            db, "old@example.test", ["<identity@fixture.test>"]
        )
        is None
    )
    assert (
        db.execute(
            "SELECT count(*) FROM mail_ids WHERE conversation=?", (dest,)
        ).fetchone()[0]
        == 0
    )
    with store.transaction(db):
        db.execute("UPDATE writer_authority SET mode='paused'")
    assert (
        post(client, cid, {**form, "version": operator_ui.version(db, cid)}).status_code
        == 503
    )


def test_forward_is_one_distinct_delivery_excluding_notes_and_original_recipients(
    mailbox, monkeypatch,
):
    monkeypatch.setattr(transport.provider_feedback, "SES_MESSAGE_DOMAIN", "feedback.example.test")
    db, client = mailbox
    with store.transaction(db):
        cid = make(db, "original@example.test", "Question", "Only selected original")
        orig = store.thread_list(db, cid)[0]
        note = store.append_thread(
            db, cid, "note", {"text": "Internal detail"}, imported=True
        )
        db.execute(
            "INSERT INTO mail_ids VALUES(?,?,?)",
            ("<private@fixture.test>", cid, orig["id"]),
        )
    fields = dict(
        action="forward",
        version=operator_ui.version(db, cid),
        thread=orig["id"],
        to="forward@example.test",
        cc="copied@example.test",
        bcc="blind@example.test",
        message="Please check.",
    )
    assert post(client, cid, {**fields, "thread": note}).status_code == 400
    response = post(client, cid, fields)
    assert response.status_code == 303
    child = int(response.headers["location"].split("/")[-1])
    assert post(client, cid, fields).status_code == 409
    outgoing = store.thread_list(db, child)[0]
    assert (
        outgoing["type"] == "forwardchild" and outgoing["linkedConversationId"] == cid
    )
    parent = store.thread(db, cid, outgoing["linkedThreadId"])
    assert (
        parent["type"] == "forwardparent"
        and parent["linkedConversationId"] == child
        and parent["state"] == "pending"
    )
    recipients, message = transport.render(db, child, outgoing["id"])
    assert recipients == [
        "forward@example.test",
        "copied@example.test",
        "blind@example.test",
    ]
    assert message["In-Reply-To"] is None and message["Bcc"] is None
    assert "Only selected original" in message.get_content()
    assert (
        "Internal detail" not in message.get_content()
        and "original@example.test" not in message.as_string()
    )

    class SES:
        def send_raw_email(self, Destinations, RawMessage):
            assert Destinations == recipients and "Bcc" not in BytesParser(
                policy=policy.default
            ).parsebytes(RawMessage["Data"])
            return {"MessageId": "feedback-receipt"}

    assert transport.send_one(db, SES())
    assert (
        store.thread(db, child, outgoing["id"])["state"]
        == store.thread(db, cid, parent["id"])["state"]
        == "published"
    )
    assert db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0] == 1
    assert (
        transport.known_conversation(
            db, "copied@example.test", ["<feedback-receipt@feedback.example.test>"]
        )
        == child
    )
    assert (
        transport.known_conversation(
            db, "forward@example.test", ["<feedback-receipt@feedback.example.test>"]
        )
        == child
    )


def test_shared_profile_keeps_unedited_archive_fields_and_refuses_cross_customer_alias(
    mailbox,
):
    db, client = mailbox
    with store.transaction(db):
        cid = make(db, "one@example.test", "Customer")
        make(db, "two@example.test", "Other")
    key = store.conversation(db, cid)["primaryCustomer"]["id"]
    profile = store.load(db, "SELECT data FROM customers WHERE id=?", (key,))
    form = dict(
        action="profile",
        version=operator_ui.version(db, cid),
        profile_version=operator_ui.fingerprint(store.encode(profile)),
        firstName="Updated",
        background="Keeps a private note",
        additional_email="two@example.test",
    )
    assert post(client, cid, form).status_code == 409
    assert (
        post(
            client, cid, {**form, "additional_email": "alias@example.test"}
        ).status_code
        == 303
    )
    assert post(client, cid, {**form, "additional_email": ""}).status_code == 409
    with store.transaction(db):
        store.save_customer(db, {**profile, "organization": "From next archive pass"})
    actual = store.load(db, "SELECT data FROM customers WHERE id=?", (key,))
    assert (
        actual["organization"] == "From next archive pass"
        and actual["firstName"] == "Updated"
    )
    assert actual["background"] == "Keeps a private note"
    assert store.conversation(db, cid)["primaryCustomer"]["first"] == "Updated"
    assert "Updated" in client.get(f"/conversation/{cid}").text
    assert store.resolve_customer(db, {"email": "alias@example.test"})["id"] == key
    assert store.conversation(db, cid)["primaryCustomer"]["email"] == "one@example.test"


def test_historical_aliases_require_one_documented_destination_and_absent_original(
    mailbox,
):
    from historical_aliases import reconcile

    db, _ = mailbox
    with store.transaction(db):
        a = make(db, "old@example.test", "First")
        b = make(db, "new@example.test", "Second")
        for target, source in ((a, 11), (a, 12), (b, 12), (a, b), (b, 0)):
            actions.activity(
                db,
                target,
                "merged",
                "Historical merge",
                {"originalConversation": source},
            )
        assert reconcile(db) == dict(
            eligible=1, existing=0, ambiguous=1, live_id=1, invalid=0, inserted=0
        )
        assert reconcile(db, apply=True)["inserted"] == 1
        assert reconcile(db, apply=True)["existing"] == 1
        assert (
            actions.canonical(db, 11) == a
            and actions.canonical(db, 12) == 12
            and actions.canonical(db, b) == b
        )
