from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
import sqlite3

import mailbox_settings
import store
import transport


def db_at(tmp_path, monkeypatch):
    monkeypatch.setattr(transport.provider_feedback, "SES_MESSAGE_DOMAIN", "email.fixture.test")
    monkeypatch.setattr(transport.provider_feedback, "SES_SIMULATOR_DOMAIN", "simulator.fixture.test")
    monkeypatch.setenv("INBOX_BASE_URL", "https://inbox.woodland.example")
    db = store.connect(tmp_path / "inbox.db")
    store.initialize(db)
    transport.schema(db)
    with store.transaction(db):
        store.save_mailbox(db, {"id": 1, "email": transport.ADDRESS})
        db.execute(
            "INSERT INTO mailbox_settings VALUES('autoReply',?)",
            (
                store.encode(
                    {
                        "enabled": True,
                        "subject": "Re: {%subject%}",
                        "message": "Hi {%customer.firstName,fallback=there%}",
                    }
                ),
            ),
        )
        db.execute(
            "INSERT INTO mailbox_settings VALUES('ratings',?)",
            (
                store.encode(
                    {
                        "enabled": "on",
                        "placement": "below",
                        "ratingsText": '<a href="{%ratings.great%}">Great</a>',
                    }
                ),
            ),
        )
    return db


def mime(mid, author="someone@example.org", reply=None, attach=False):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = author
    msg["To"] = transport.ADDRESS
    msg["Sender"] = transport.ADDRESS
    msg["List-ID"] = "<hello.woodland.example>"
    msg["Message-ID"] = mid
    msg["Subject"] = "My support request" if not reply else "Re: My support request"
    if reply:
        msg["In-Reply-To"] = reply
    msg.set_content("Here is the attachment.")
    if attach:
        msg.add_attachment(
            b"attachment",
            maintype="application",
            subtype="octet-stream",
            filename="example.txt",
        )
    return msg.as_bytes()


def test_receive_receive_again_send_and_customer_reply(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    raw = mime("<original@example.org>", attach=True)
    assert transport.admit(db, "raw/1", raw, verify=lambda *_: True) == "imported"
    assert transport.admit(db, "raw/2", raw, verify=lambda *_: True) == "duplicate"
    cid, tid = db.execute(
        "SELECT conversation,thread FROM inbound_objects WHERE disposition='imported'"
    ).fetchone()
    conv = store.conversation(db, cid, True)
    att = conv["_embedded"]["threads"][0]["_embedded"]["attachments"][0]
    assert store.attachment(db, cid, att["id"])[0] == b"attachment"
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1
    with store.transaction(db):
        sent = store.append_thread(
            db, cid, "reply", {"text": "<p>Reply</p>", "status": "pending"}
        )

    class Ses:
        messages = []

        def send_raw_email(self, Destinations, RawMessage):
            assert Destinations == ["someone@example.org"]
            self.messages.append(
                BytesParser(policy=policy.default).parsebytes(RawMessage["Data"])
            )
            return {"MessageId": "transport-" + str(len(self.messages))}

    ses = Ses()
    assert transport.send_one(db, ses)
    assert ses.messages[0]["Return-Path"] == "<inbox@intake.woodland.example>"
    assert ses.messages[0]["In-Reply-To"] == "<original@example.org>"
    assert "https://inbox.woodland.example/rating/" in ses.messages[0].get_content()
    assert store.thread(db, cid, sent)["state"] == "published"
    assert transport.send_ack(db, ses) and not transport.send_ack(db, ses)
    assert ses.messages[1]["Auto-Submitted"] == "auto-replied"
    assert len(store.thread_list(db, cid)) == 2
    assert (
        transport.admit(
            db,
            "raw/3",
            mime("<next@example.org>", reply="<transport-1@email.fixture.test>"),
            verify=lambda *_: True,
        )
        == "imported"
    )
    assert len(store.thread_list(db, cid)) == 3
    assert store.conversation(db, cid)["status"] == "active"
    transport.admit(
        db,
        "raw/4",
        mime(
            "<stranger@example.org>",
            "other@example.org",
            reply="<transport-1@email.fixture.test>",
        ),
        verify=lambda *_: True,
    )
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
    assert len(store.thread_list(db, cid)) == 3


def test_shadow_forged_headers_uncertain_delivery(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    assert (
        transport.admit(db, "raw/forged", mime("<fake@example.org>"))
        == "held_non_group"
    )
    assert (
        transport.admit(
            db,
            "raw/shadow",
            mime("<shadow@example.org>"),
            active=False,
            verify=lambda *_: True,
        )
        == "shadow"
    )
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0
    transport.admit(db, "raw/real", mime("<real@example.org>"), verify=lambda *_: True)
    cid = db.execute("SELECT id FROM conversations").fetchone()[0]
    with store.transaction(db):
        tid = store.append_thread(db, cid, "reply", {"text": "Hello"})

    class Uncertain:
        def send_raw_email(self, **kwargs):
            raise TimeoutError()

    assert transport.send_one(db, Uncertain())
    assert db.execute("SELECT state FROM mail_outbox").fetchone()[0] == "uncertain"
    assert store.thread(db, cid, tid)["state"] == "pending"
    assert not transport.send_one(db, Uncertain())


def test_ses_feedback_exact_partial_cc_and_suppression(tmp_path, monkeypatch):
    import provider_feedback

    db = db_at(tmp_path, monkeypatch)
    transport.admit(db, "raw/a", mime("<mail@example.org>"), verify=lambda *_: True)
    cid = db.execute("SELECT id FROM conversations").fetchone()[0]
    with store.transaction(db):
        tid = store.append_thread(
            db, cid, "reply", {"text": "Reply", "cc": ["copy@example.org"]}
        )

    class Ses:
        def send_raw_email(self, **kwargs):
            return {"MessageId": "accepted"}

    assert transport.send_one(db, Ses())
    event = {
        "eventType": "Bounce",
        "mail": {
            "source": transport.ADDRESS,
            "messageId": "accepted",
            "destination": ["someone@example.org", "copy@example.org"],
        },
        "bounce": {
            "bounceType": "Permanent",
            "bouncedRecipients": [{"emailAddress": "copy@example.org"}],
            "timestamp": "2026-11-23T19:00:00Z",
        },
    }
    assert provider_feedback.feedback(db, "evt", event) == "applied"
    assert provider_feedback.feedback(db, "sqs-redelivery", event) == "applied"
    assert store.thread(db, cid, tid)["state"] == "published"
    bounced = [
        t
        for t in store.thread_list(db, cid)
        if "bounce" in t.get("action", {}).get("type", "")
    ]
    assert len(bounced) == 1 and bounced[0]["to"] == ["copy@example.org"]
    assert (
        db.execute("SELECT address FROM address_suppression").fetchone()[0]
        == "copy@example.org"
    )
    event["bounce"]["bouncedRecipients"] = [{"emailAddress": "outsider@example.org"}]
    assert (
        provider_feedback.feedback(db, "unrelated", event) == "held_recipient_mismatch"
    )
    event["mail"]["messageId"] = "newer"
    assert (
        provider_feedback.feedback(db, "ahead-of-receipt", event) == "awaiting_receipt"
    )


def test_simulator_feedback_before_acceptance_is_retried(tmp_path, monkeypatch):
    import provider_feedback

    db = db_at(tmp_path, monkeypatch)
    address = "bounce@simulator.fixture.test"
    event = {
        "eventType": "Bounce",
        "mail": {
            "messageId": "pending",
            "source": transport.ADDRESS,
            "destination": [address],
        },
        "bounce": {
            "bounceType": "Permanent",
            "bouncedRecipients": [{"emailAddress": address}],
        },
    }
    assert provider_feedback.feedback(db, "sqs-id", event) == "awaiting_receipt"
    assert db.execute("SELECT count(*) FROM provider_events").fetchone()[0] == 0


def test_existing_case_join_requires_group_signed_original_sender(
    tmp_path, monkeypatch
):
    db = db_at(tmp_path, monkeypatch)
    initial = mime("<initial@example.org>")
    assert transport.admit(db, "initial", initial, verify=lambda *_: True) == "imported"
    unverified = mime("<next@example.org>", reply="<initial@example.org>")
    signed = transport.GroupSignature({"from", "x-original-authentication-results"})
    assert (
        transport.admit(db, "unsigned", unverified, verify=lambda *_: signed)
        == "held_sender_verification"
    )
    verified = (
        b"X-Original-Authentication-Results: mx.google.com; dmarc=pass (p=none) header.from=example.org\r\n"
        + unverified
    )
    assert (
        transport.admit(db, "verified", verified, verify=lambda *_: signed)
        == "imported"
    )
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1


def test_shadow_boundary_and_source_identity(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    raw = mime("<cutover@example.org>")
    trust = lambda *_: True
    assert transport.admit(db, "group/raw", raw, False, trust) == "shadow"
    assert (
        transport.admit(db, "group/raw", raw, True, trust, "before_cutover")
        == "held_cutover"
    )
    assert not db.execute("SELECT 1 FROM conversations").fetchone()
    with store.transaction(db):
        store.import_conversation(
            db,
            {
                "id": 8,
                "number": 8,
                "mailboxId": 1,
                "status": "active",
                "subject": "Prior request",
                "primaryCustomer": {"id": 4, "email": "someone@example.org"},
            },
            [
                {
                    "id": 9,
                    "type": "customer",
                    "customer": {"email": "someone@example.org"},
                    "body": "Prior request",
                }
            ],
        )
        db.execute(
            "INSERT INTO mail_ids VALUES(?,?,?)", ("<cutover@example.org>", 8, 9)
        )
    assert (
        transport.admit(db, "group/raw", raw, True, trust, "before_cutover")
        == "represented_in_source"
    )
    assert not db.execute("SELECT 1 FROM acknowledgments").fetchone()
    fresh = mime("<second@example.org>")
    assert transport.admit(db, "staged", fresh, False, trust) == "shadow"
    assert (
        transport.admit(db, "staged", fresh, True, trust, "after_cutover") == "imported"
    )
    assert (
        transport.admit(db, "staged", fresh, True, trust, "after_cutover") == "imported"
    )
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1


def test_feedback_recovers_uncertain_submission_once(tmp_path, monkeypatch):
    import provider_feedback

    db = db_at(tmp_path, monkeypatch)
    transport.admit(db, "inbound", mime("<one@example.org>"), verify=lambda *_: True)
    cid = db.execute("SELECT id FROM conversations").fetchone()[0]
    with store.transaction(db):
        tid = store.append_thread(db, cid, "reply", {"text": "Hello"})

    class Timeout:
        def send_raw_email(self, **kwargs):
            raise TimeoutError()

    transport.send_one(db, Timeout())
    token = db.execute("SELECT token FROM submission_intents").fetchone()[0]
    event = {
        "eventType": "Delivery",
        "mail": {
            "source": transport.ADDRESS,
            "destination": ["someone@example.org"],
            "tags": {"pxg-inbox-receipt": [token]},
            "messageId": "accepted-after-timeout",
        },
        "delivery": {"recipients": ["someone@example.org"]},
    }
    assert provider_feedback.feedback(db, "q", event) == "applied"
    assert provider_feedback.feedback(db, "q-again", event) == "applied"
    assert db.execute("SELECT state FROM mail_outbox").fetchone()[0] == "sent"
    assert store.thread(db, cid, tid)["state"] == "published"
    assert not transport.send_one(db, Timeout())


def test_signed_provider_report_does_not_create_a_support_ticket(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = transport.ADDRESS
    msg["Message-ID"] = "<report@example.org>"
    msg.set_type("multipart/report")
    original = EmailMessage()
    original["Message-ID"] = "<unmatched@email.fixture.test>"
    original.set_content("Fixture")
    msg.attach(original)
    assert (
        transport.admit(db, "report", msg.as_bytes(), verify=lambda *_: True)
        == "held_delivery_report"
    )
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 0


def test_feedback_visible_and_envelope_sender():
    import provider_feedback

    mail = {
        "source": "Robin from Woodland <hello@woodland.example>",
        "commonHeaders": {"from": ["Robin <hello@woodland.example>"]},
    }
    assert provider_feedback.from_inbox(mail)
    mail["source"] = transport.RETURN_PATH
    assert provider_feedback.from_inbox(mail)
    mail["commonHeaders"]["from"] = ["someone@example.org"]
    assert not provider_feedback.from_inbox(mail)


def test_original_sender_without_dmarc_can_have_exact_aligned_dkim():
    msg = EmailMessage()
    msg["From"] = "person@example.org"
    msg["X-Original-Authentication-Results"] = (
        "mx.google.com; dkim=pass header.i=@example.org"
    )
    signed = {"x-original-authentication-results"}
    assert transport.original_sender_matches(msg, "person@example.org", signed)
    msg.replace_header(
        "X-Original-Authentication-Results",
        "mx.google.com; dkim=pass header.i=@unrelated.example",
    )
    assert not transport.original_sender_matches(msg, "person@example.org", signed)
    msg.replace_header(
        "X-Original-Authentication-Results",
        "mx.google.com; dmarc=fail header.from=example.org; dkim=pass header.i=@example.org",
    )
    assert not transport.original_sender_matches(msg, "person@example.org", signed)


def test_reviewable_legacy_reply_has_one_audited_resolution(tmp_path, monkeypatch):
    import hashlib

    db = db_at(tmp_path, monkeypatch)
    trust = lambda *_: True
    transport.admit(db, "a", mime("<first@example.org>"), verify=trust)
    cid = db.execute("SELECT id FROM conversations").fetchone()[0]
    raw = mime("<next@example.org>", reply="<unavailable@example.org>")
    assert transport.admit(db, "legacy", raw, verify=trust) == "held_threading"
    assert len(store.thread_list(db, cid)) == 1
    decision = {
        "expected": "held_threading",
        "sha": hashlib.sha256(raw).hexdigest(),
        "conversation": cid,
        "reason": "Verified original sender and prior correspondence",
    }
    assert (
        transport.admit(db, "legacy", raw, verify=trust, decision=decision)
        == "imported"
    )
    assert len(store.thread_list(db, cid)) == 2
    assert db.execute("SELECT count(*) FROM intake_decisions").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1


def test_duplicate_rfc_id_checks_sender_and_body(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    trust = lambda *_: True
    raw = mime("<shared@example.org>")
    assert transport.admit(db, "one", raw, verify=trust) == "imported"
    assert (
        transport.admit(
            db, "routed", b"Received: from receipt.example.org\r\n" + raw, verify=trust
        )
        == "duplicate"
    )
    assert (
        transport.admit(
            db,
            "different",
            mime("<shared@example.org>", author="unrelated@example.org"),
            verify=trust,
        )
        == "held_duplicate_identity"
    )
    assert db.execute("SELECT count(*) FROM threads").fetchone()[0] == 1


def test_unpaired_feedback_survives_queue_deletion_and_late_acceptance(
    tmp_path, monkeypatch
):
    import provider_feedback, json

    db = db_at(tmp_path, monkeypatch)
    event = {
        "eventType": "Delivery",
        "mail": {
            "source": transport.ADDRESS,
            "destination": ["someone@example.org"],
            "messageId": "acceptance",
        },
        "delivery": {"recipients": ["someone@example.org"]},
    }

    class Queue:
        removed = []

        def receive_message(self, **unused):
            return {
                "Messages": [
                    {
                        "MessageId": "event-id",
                        "Body": json.dumps(event),
                        "ReceiptHandle": "opaque",
                    }
                ]
            }

        def delete_message(self, **kwargs):
            assert (
                db.execute("SELECT count(*) FROM unmatched_feedback").fetchone()[0] == 1
            )
            self.removed.append(kwargs["ReceiptHandle"])

    queue = Queue()
    provider_feedback.poll(db, queue, "fixture")
    assert queue.removed == ["opaque"]
    transport.admit(db, "mail", mime("<a@example.org>"), verify=lambda *_: True)
    cid = db.execute("select id from conversations").fetchone()[0]
    with store.transaction(db):
        store.append_thread(db, cid, "reply", {"text": "Fixture response"})

    class SES:
        def send_raw_email(self, **unused):
            return {"MessageId": "acceptance"}

    transport.send_one(db, SES())
    db.execute("UPDATE unmatched_feedback SET next_check=0")
    provider_feedback.retry_unmatched(db)
    assert db.execute("SELECT count(*) FROM unmatched_feedback").fetchone()[0] == 0
    assert (
        db.execute("SELECT disposition FROM provider_events").fetchone()[0] == "applied"
    )


def test_simulator_review_is_exact_preserved_and_replay_safe(tmp_path, monkeypatch):
    import provider_feedback, pytest

    db = db_at(tmp_path, monkeypatch)
    event = {
        "eventType": "Delivery",
        "mail": {
            "source": transport.ADDRESS,
            "messageId": "fixture",
            "destination": ["success@simulator.fixture.test"],
        },
        "delivery": {"recipients": ["success@simulator.fixture.test"]},
    }
    db.execute(
        "INSERT INTO unmatched_feedback VALUES('test',?,1,1,0)", (store.encode(event),)
    )
    db.execute("UPDATE writer_authority SET mode='standby'")
    with pytest.raises(store.Invalid):
        provider_feedback.review_simulator(db, "test", "wrong", "isolated simulator")
    digest = provider_feedback.event_digest(event)
    provider_feedback.review_simulator(db, "test", digest, "isolated simulator")
    assert db.execute("SELECT count(*) FROM unmatched_feedback").fetchone()[0] == 0
    assert db.execute("SELECT event FROM reviewed_feedback").fetchone()[
        0
    ] == store.encode(event)
    assert provider_feedback.feedback(db, "redelivery", event) == "reviewed_simulator"
    event["mail"]["destination"] = ["outside@example.org"]
    db.execute(
        "INSERT INTO unmatched_feedback VALUES('test2',?,1,1,0)", (store.encode(event),)
    )
    with pytest.raises(store.Invalid):
        provider_feedback.review_simulator(
            db, "test2", provider_feedback.event_digest(event), "isolated simulator"
        )


def test_source_ack_window_only_defers_recent_possible_recipients(
    tmp_path, monkeypatch
):
    import time

    db = db_at(tmp_path, monkeypatch)
    now = int(time.time())
    store.import_conversation(
        db,
        {
            "id": 39,
            "mailboxId": 1,
            "type": "email",
            "status": "active",
            "createdAt": store.now(),
            "modifiedAt": store.now(),
            "primaryCustomer": {"email": "returning@example.org", "id": 100},
        },
        [
            {
                "id": 311,
                "type": "customer",
                "state": "published",
                "createdAt": store.now(),
                "customer": {"email": "returning@example.org"},
                "source": {"type": "email"},
            }
        ],
    )
    assert mailbox_settings.capture_source_window(db, now) == 1
    assert (
        transport.admit(
            db,
            "first",
            mime("<new-one@fixture.example>", "returning@example.org"),
            verify=lambda *_: True,
        )
        == "imported"
    )
    assert (
        db.execute(
            "SELECT state FROM acknowledgments WHERE address='returning@example.org'"
        ).fetchone()[0]
        == "deferred_cutover"
    )
    assert (
        transport.admit(
            db,
            "second",
            mime("<new-two@fixture.example>", "new@example.org"),
            verify=lambda *_: True,
        )
        == "imported"
    )
    assert (
        db.execute(
            "SELECT state FROM acknowledgments WHERE address='new@example.org'"
        ).fetchone()[0]
        == "queued"
    )
    assert (
        transport.admit(
            db,
            "third",
            mime("<second-from-same-sender@example.org>", "returning@example.org"),
            verify=lambda *_: True,
        )
        == "imported"
    )
    assert (
        db.execute(
            "SELECT count(*) FROM acknowledgments WHERE address='returning@example.org'"
        ).fetchone()[0]
        == 1
    )
    assert mailbox_settings.release_deferred(db, now + 86399) == 0
    assert mailbox_settings.release_deferred(db, now + 86400) == 1
    assert db.execute(
        "SELECT state,queued FROM acknowledgments WHERE address='returning@example.org'"
    ).fetchone()[:] == ("queued", now + 86400)


def test_ack_loop_indicators_keep_customer_mail_and_skip_automatic_response(
    tmp_path, monkeypatch
):
    db = db_at(tmp_path, monkeypatch)
    for idx, (author, subject, body) in enumerate(
        (
            ("alert@example.org", "Alert", "Ready"),
            ("person@example.org", "Automatic reply", "Message"),
            ("other@example.org", "Vacation", "This is my auto-reply."),
        )
    ):
        msg = EmailMessage()
        msg["From"] = author
        msg["To"] = transport.ADDRESS
        msg["Message-ID"] = f"<auto{idx}@fixture.example>"
        msg["Subject"] = subject
        msg.set_content(body)
        assert (
            transport.admit(db, str(idx), msg.as_bytes(), verify=lambda *_: True)
            == "imported"
        )
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 0


def test_deferred_acknowledgment_skips_answered_ticket_after_boundary(
    tmp_path, monkeypatch
):
    db = db_at(tmp_path, monkeypatch)
    db.execute(
        "INSERT INTO source_ack_window VALUES('person@example.org',unixepoch()+100)"
    )
    assert (
        transport.admit(
            db,
            "customer",
            mime("<answered@test.example>", "person@example.org"),
            verify=lambda *_: True,
        )
        == "imported"
    )
    cid = db.execute("SELECT cid FROM acknowledgments").fetchone()[0]
    db.execute("UPDATE source_ack_window SET expires=0")
    # The original customer still got a normal reply while the automated acknowledgment waited.
    with store.transaction(db):
        tid = store.append_thread(db, cid, "reply", {"text": "Here is your answer."})
        store.publish(db, cid, tid)

    class NoMail:
        def send_raw_email(self, **kwargs):
            raise AssertionError("Already answered")

    assert not transport.send_ack(db, NoMail())
    assert (
        db.execute("SELECT state FROM acknowledgments").fetchone()[0]
        == "resolved_before_ack"
    )


def hook_fixture(tmp_path, monkeypatch):
    import json

    db = db_at(tmp_path, monkeypatch)
    url = "https://agent.example.org/events"
    config = {"url": url, "state": "enabled", "secret": "test-secret"}
    db.execute("INSERT INTO webhooks VALUES(1,?)", (json.dumps(config),))
    db.execute("INSERT INTO deliveries VALUES(1,1,'convo.created',8,'{}','queued')")
    return db, url


def test_callback_survives_pause_between_worker_poll_and_claim(tmp_path, monkeypatch):
    import writer_control

    monkeypatch.setenv("INBOX_WEBHOOK_IDS", "11,12,13,14")
    db, url = hook_fixture(tmp_path, monkeypatch)
    for key in writer_control.expected_hooks():
        db.execute("INSERT INTO webhooks VALUES(?,(SELECT data FROM webhooks WHERE id=1))", (key,))
    key = min(writer_control.expected_hooks())
    db.execute("UPDATE deliveries SET webhook_id=?", (key,))
    db.execute("DELETE FROM webhooks WHERE id=1")
    assert store.writer_live(db)
    writer_control.change(db, "paused", "live")
    assert not transport.deliver_hook(db, url)
    assert db.execute("SELECT state FROM deliveries").fetchone()[0] == "queued"
    assert not db.execute("SELECT 1 FROM hook_attempts").fetchone()


def test_callback_has_one_claim_and_preserves_newer_success(tmp_path, monkeypatch):
    from contextlib import closing
    from types import SimpleNamespace

    db, url = hook_fixture(tmp_path, monkeypatch)
    seen = []
    clock = [1000]
    monkeypatch.setattr(transport.time, "time", lambda: clock[0])

    class Receiver:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

        def request(self, method, path, body, headers):
            seen.append((method, path, body, dict(headers)))

        def getresponse(self):
            if len(seen) == 1:
                with closing(store.connect(tmp_path / "inbox.db")) as other:
                    assert not transport.deliver_hook(other, url)
                    clock[0] += 300
                    assert transport.deliver_hook(other, url)
                return SimpleNamespace(status=503)
            return SimpleNamespace(status=200)

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Receiver)
    assert transport.deliver_hook(db, url)
    assert len(seen) == 2 and seen[0] == seen[1]
    assert seen[0][-1]["X-Inbox-Delivery"] == "1"
    assert db.execute("SELECT state FROM deliveries").fetchone()[0] == "sent"
    assert db.execute("SELECT count FROM hook_attempts").fetchone()[0] == 2
    assert not transport.deliver_hook(db, url)
