"""Reconcile authenticated SES feedback with exact sender, wire recipients and acceptance receipts."""

from email.utils import getaddresses
import hashlib
import json
import secrets

import store
import company


SES_MESSAGE_DOMAIN = "email.amazonses.com"
SES_SIMULATOR_DOMAIN = "simulator.amazonses.com"


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS provider_events(id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,
            kind TEXT NOT NULL, disposition TEXT NOT NULL, recipients TEXT NOT NULL, received TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS submission_intents(token TEXT PRIMARY KEY,kind TEXT NOT NULL,
            operation INTEGER NOT NULL,destinations TEXT NOT NULL,UNIQUE(kind,operation));
        CREATE TABLE IF NOT EXISTS unmatched_feedback(id TEXT PRIMARY KEY,event TEXT NOT NULL,
            received INTEGER NOT NULL,next_check INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS unmatched_due ON unmatched_feedback(next_check);
        CREATE TABLE IF NOT EXISTS reviewed_feedback(digest TEXT PRIMARY KEY, event TEXT NOT NULL,
            reason TEXT NOT NULL, decided TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback_quarantine(id TEXT PRIMARY KEY,reason TEXT NOT NULL,
            body TEXT NOT NULL,received TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS address_suppression(address TEXT PRIMARY KEY COLLATE NOCASE,
            reason TEXT NOT NULL, event TEXT NOT NULL);
    """)


def verify_recipients(db, addresses):
    for address in addresses:
        if db.execute(
            "SELECT 1 FROM address_suppression WHERE address=?", (address,)
        ).fetchone():
            raise store.Invalid(
                "A recipient has a permanent delivery failure or complaint"
            )


def begin_submission(db, kind, operation, recipients):
    token = secrets.token_hex(24)
    db.execute(
        "INSERT INTO submission_intents VALUES(?,?,?,?)",
        (token, kind, operation, store.encode(sorted({a.lower() for a in recipients}))),
    )
    return token


def record_acceptance(db, kind, operation, provider_id):
    if kind == "reply":
        row = db.execute(
            "SELECT conversation_id,state FROM mail_outbox WHERE thread_id=?",
            (operation,),
        ).fetchone()
        if not row or row[1] not in ("submitting", "uncertain"):
            return
        cid, tid = row[0], operation
        db.execute(
            "INSERT OR REPLACE INTO transport_receipts VALUES(?,?,NULL)",
            (tid, provider_id),
        )
        db.execute(
            "INSERT OR IGNORE INTO mail_ids VALUES(?,?,?)",
            (company.message_id(cid,tid), cid, tid),
        )
        store.publish(db, cid, tid)
    else:
        cid = operation
        if not db.execute(
            "UPDATE acknowledgments SET state='sent',receipt=? WHERE cid=? AND state IN ('uncertain','submitting')",
            (provider_id, cid),
        ).rowcount:
            return
        tid = db.execute(
            "SELECT id FROM threads WHERE conversation_id=? ORDER BY created,id LIMIT 1",
            (cid,),
        ).fetchone()[0]
    db.execute(
        "INSERT OR IGNORE INTO mail_ids VALUES(?,?,?)",
        (f"<{provider_id}@{SES_MESSAGE_DOMAIN}>", cid, tid),
    )


def from_inbox(mail):
    sender = [a.lower() for _, a in getaddresses([mail.get("source", "")])]
    fields = (mail.get("commonHeaders") or {}).get("from")
    visible = (
        [a.lower() for _, a in getaddresses(fields)] if isinstance(fields, list) else []
    )
    if fields and visible != [company.ADDRESS]:
        return False
    return sender == [company.ADDRESS] or (
        sender == [company.RETURN_PATH]
        and visible == [company.ADDRESS]
    )


def reconcile_tag(db, mail):
    tokens = (mail.get("tags") or {}).get("pxg-inbox-receipt", [])
    if len(tokens) != 1 or not from_inbox(mail):
        return
    row = db.execute(
        "SELECT kind,operation,destinations FROM submission_intents WHERE token=?",
        (tokens[0],),
    ).fetchone()
    if row and set(json.loads(row[2])) == {
        a.lower() for a in mail.get("destination", [])
    }:
        record_acceptance(db, row[0], row[1], mail["messageId"])


def event_digest(event):
    return hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def review_simulator(db, key, expected, reason):
    """Archive one proved pre-launch simulator event without modifying any mail or suppression."""
    with store.transaction(db):
        row = db.execute(
            "SELECT event FROM unmatched_feedback WHERE id=?", (key,)
        ).fetchone()
        if not row:
            raise store.Missing("Unknown pending provider receipt")
        event = json.loads(row[0])
        mail = event["mail"]
        targets = set(mail.get("destination", []))
        simulator = {
            f"{x}@{SES_SIMULATOR_DOMAIN}" for x in ("success", "bounce", "complaint")
        }
        if (
            event_digest(event) != expected
            or not targets
            or not targets <= simulator
            or not from_inbox(mail)
        ):
            raise store.Invalid(
                "Receipt does not match the verified simulator submission"
            )
        if (
            db.execute("SELECT mode FROM writer_authority WHERE id=1").fetchone()[0]
            != "standby"
            or db.execute(
                "SELECT 1 FROM mail_outbox UNION ALL SELECT 1 FROM acknowledgments UNION ALL SELECT 1 FROM submission_intents LIMIT 1"
            ).fetchone()
        ):
            raise store.Conflict("This mailbox has a live delivery history")
        db.execute(
            "INSERT OR IGNORE INTO reviewed_feedback VALUES(?,?,?,?)",
            (expected, row[0], store.text(reason, "reason", 500), store.now()),
        )
        db.execute("DELETE FROM unmatched_feedback WHERE id=?", (key,))


def feedback(db, event_id, event):
    kind = event.get("eventType") or event.get("notificationType")
    if kind not in ("Bounce", "Complaint", "Delivery", "DeliveryDelay"):
        return "ignored"
    mail = event["mail"]
    provider_id = mail["messageId"]
    body = event[
        {
            "Bounce": "bounce",
            "Complaint": "complaint",
            "Delivery": "delivery",
            "DeliveryDelay": "deliveryDelay",
        }[kind]
    ]
    if kind == "Delivery":
        addresses = body.get("recipients") or []
    else:
        recipients = body.get(
            {
                "Bounce": "bouncedRecipients",
                "Complaint": "complainedRecipients",
                "DeliveryDelay": "delayedRecipients",
            }[kind],
            [],
        )
        addresses = [r["emailAddress"] for r in recipients if r.get("emailAddress")]
    addresses = sorted({str(a).lower() for a in addresses})
    with store.transaction(db):
        # SNS can redeliver the same SES event with a different SQS message ID.
        event_id = hashlib.sha256(
            store.encode(
                [
                    provider_id,
                    kind,
                    body.get("feedbackId"),
                    body.get("timestamp"),
                    addresses,
                ]
            ).encode()
        ).hexdigest()
        prior = db.execute(
            "SELECT disposition FROM provider_events WHERE id=?", (event_id,)
        ).fetchone()
        if prior:
            return prior[0]
        if db.execute(
            "SELECT 1 FROM reviewed_feedback WHERE digest=?", (event_digest(event),)
        ).fetchone():
            return "reviewed_simulator"
        reconcile_tag(db, mail)
        receipt = db.execute(
            """SELECT o.conversation_id,o.thread_id,t.data FROM transport_receipts r
                   JOIN mail_outbox o ON o.thread_id=r.thread JOIN threads t ON t.id=r.thread
                   WHERE r.provider_id=?""",
            (provider_id,),
        ).fetchone()
        ack = db.execute(
            "SELECT cid,address FROM acknowledgments WHERE receipt=?", (provider_id,)
        ).fetchone()
        envelope = {s.lower() for s in mail.get("destination", [])}
        outcome = "applied"
        if not from_inbox(mail):
            outcome = "other_sender"
        elif not receipt and not ack:
            # The queue can deliver an event before send_raw_email returns to the caller.
            return "awaiting_receipt"
        else:
            from operator_actions import canonical

            cid = receipt[0] if receipt else canonical(db, ack[0])
            original = json.loads(receipt[2]) if receipt else {}
            actual = (
                {
                    str(a).lower()
                    for k in ("to", "cc", "bcc")
                    for a in original.get(k, [])
                }
                if receipt
                else {ack[1].lower()}
            )
            if (
                not addresses
                or not set(addresses) <= actual
                or not set(addresses) <= envelope
            ):
                outcome = "held_recipient_mismatch"
            else:
                when = body.get("timestamp") or store.now()
                if kind == "Bounce" and receipt and actual == set(addresses):
                    original["state"] = "bounced"
                    db.execute(
                        "UPDATE threads SET data=? WHERE id=?",
                        (store.encode(original), receipt[1]),
                    )
                elif kind in ("Bounce", "Complaint", "DeliveryDelay"):
                    tid = store.allocate(db, "thread")
                    line = {
                        "id": tid,
                        "type": "lineitem",
                        "state": "published",
                        "createdAt": when,
                        "to": addresses,
                        "source": {"type": "email", "via": "system"},
                        "createdBy": {"type": "system_user"},
                        "action": {
                            "type": {
                                "Bounce": "recipient-bounced",
                                "Complaint": "delivery-complaint",
                                "DeliveryDelay": "delivery-delay",
                            }[kind],
                            "text": kind,
                            "associatedEntities": {
                                "thread": receipt[1] if receipt else None
                            },
                        },
                    }
                    db.execute(
                        "INSERT INTO threads(id,conversation_id,data) VALUES(?,?,?)",
                        (tid, cid, store.encode(line)),
                    )
                if kind == "Complaint" or (
                    kind == "Bounce" and body.get("bounceType") == "Permanent"
                ):
                    db.executemany(
                        "INSERT OR REPLACE INTO address_suppression VALUES(?,?,?)",
                        [(a, kind, event_id) for a in addresses],
                    )
                if kind in ("Bounce", "Complaint"):
                    conv = store.conversation(db, cid)
                    tag = "delivery-" + kind.lower()
                    if all(t.get("tag") != tag for t in conv.get("tags", [])):
                        store.update_conversation(
                            db, cid, {"tags": conv.get("tags", []) + [{"tag": tag}]}
                        )
                        store.emit(db, cid, "convo.tags")
        db.execute(
            "INSERT INTO provider_events VALUES(?,?,?,?,?,?)",
            (
                event_id,
                provider_id,
                kind,
                outcome,
                store.encode(addresses),
                store.now(),
            ),
        )
        return outcome


def retry_unmatched(db):
    waiting = db.execute("""
        SELECT id,event FROM unmatched_feedback WHERE next_check<=unixepoch()
        ORDER BY next_check,id LIMIT 25""").fetchall()
    for key, raw in waiting:
        disposition = feedback(db, key, json.loads(raw))
        with store.transaction(db):
            if disposition == "awaiting_receipt":
                db.execute(
                    "UPDATE unmatched_feedback SET attempts=attempts+1, next_check=unixepoch()+min(3600,30*(1<<min(attempts,7))) WHERE id=?",
                    (key,),
                )
            else:
                db.execute("DELETE FROM unmatched_feedback WHERE id=?", (key,))


def poll(db, sqs, queue, topic=None):
    retry_unmatched(db)
    result = sqs.receive_message(
        QueueUrl=queue, WaitTimeSeconds=20, MaxNumberOfMessages=10
    )
    for item in result.get("Messages", []):
        raw = item["Body"]
        key = item["MessageId"]
        if (
            hashlib.sha256(raw.encode()).hexdigest()
            == "e473876b2984f3d502b8f66c442dbba7d9398084fd1c570c6d15a7cbb34e29d6"
        ):
            disposition = "ses_topic_validation"
        else:
            try:
                envelope = json.loads(raw)
                if envelope.get("Type") == "Notification":
                    if not topic or envelope.get("TopicArn") != topic:
                        raise ValueError("Unexpected feedback publisher")
                    event = json.loads(envelope["Message"])
                else:
                    event = envelope
                disposition = feedback(db, key, event)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                # Retain the original private receipt for repair, then release the queue message.
                with store.transaction(db):
                    db.execute(
                        "INSERT OR IGNORE INTO feedback_quarantine VALUES(?,?,?,?)",
                        (key, type(error).__name__, raw, store.now()),
                    )
                disposition = "quarantined"
        if disposition == "awaiting_receipt":
            with store.transaction(db):
                db.execute(
                    """INSERT OR IGNORE INTO unmatched_feedback(id,event,received,next_check)
                              VALUES(?,?,unixepoch(),unixepoch()+15)""",
                    (key, store.encode(event)),
                )
        sqs.delete_message(QueueUrl=queue, ReceiptHandle=item["ReceiptHandle"])
