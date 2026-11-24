"""Receive SES mailbox objects and deliver durable mailbox operations."""

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
import hashlib
import hmac
import html
import http.client
import json
import logging
import os
import re
import ssl
import time
from urllib.parse import urlsplit

import store
import company
import extensions
import provider_feedback
import inbound_queue
import intake_triage
import mailbox_settings

LOG = logging.getLogger("mailbox")
MESSAGE_ID = re.compile(r"<[^<>\s]{1,996}>")
MAILBOX_ID = company.MAILBOX
ADDRESS = company.ADDRESS
RETURN_PATH = company.RETURN_PATH
MAX_RAW = 40 * 1024 * 1024


def schema(db):
    mailbox_settings.initialize(db)
    provider_feedback.initialize(db)
    inbound_queue.initialize(db)
    intake_triage.schema(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mail_ids(mid TEXT, conversation INTEGER, thread INTEGER,
            PRIMARY KEY(mid,conversation,thread));
        CREATE INDEX IF NOT EXISTS mail_thread_refs ON mail_ids(conversation,thread);
        CREATE TABLE IF NOT EXISTS inbound_objects(object_key TEXT PRIMARY KEY, sha TEXT,
            mid TEXT, disposition TEXT NOT NULL, conversation INTEGER, thread INTEGER, observed TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS inbound_mid ON inbound_objects(mid);
        CREATE INDEX IF NOT EXISTS inbound_disposition ON inbound_objects(disposition);
        CREATE TABLE IF NOT EXISTS transport_receipts(thread INTEGER PRIMARY KEY, provider_id TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS inbound_fingerprints(mid TEXT PRIMARY KEY,identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS intake_decisions(object_key TEXT PRIMARY KEY,sha TEXT,previous TEXT,
            outcome TEXT,conversation INTEGER,reason TEXT,decided TEXT);
        CREATE TABLE IF NOT EXISTS hook_attempts(id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, next INTEGER DEFAULT 0);
    """)


def addresses(msg, field):
    return [
        addr for _, addr in getaddresses(msg.get_all(field, [])) if addr and "@" in addr
    ]


def normalize_subject(subject):
    return (
        re.sub(r"^(?:(?:re|fwd?|aw):\s*)+", "", str(subject).strip(), flags=re.I)
        .strip()
        .casefold()
    )


def timestamp(msg):
    try:
        when = parsedate_to_datetime(str(msg["Date"]))
        if not when.tzinfo:
            raise ValueError()
        return (
            when.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OverflowError):
        return store.now()


def extract_message(msg):
    # The unquoted Reply-To can be the true author when Google rewrites a DMARC From as the group.
    sender = addresses(msg, "From")
    if len(sender) != 1:
        raise ValueError("invalid_from")
    reply_to = addresses(msg, "Reply-To")
    if (
        sender[0].lower() == ADDRESS
        and len(reply_to) == 1
        and reply_to[0].lower() != ADDRESS
    ):
        sender = reply_to
    author = store.email(sender[0]).lower()
    selected = msg.get_body(preferencelist=("html", "plain"))
    body = ""
    if selected:
        body = selected.get_content()
        if selected.get_content_type() == "text/plain":
            body = "<pre>" + html.escape(body) + "</pre>"
    attachments = []
    for part in msg.walk():
        if part is selected or part.is_multipart():
            continue
        name = part.get_filename()
        cid = part.get("Content-ID")
        if not name and not cid and part.get_content_disposition() != "attachment":
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        attachments.append(
            dict(
                fileName=(name or "attachment")[:255],
                mimeType=part.get_content_type(),
                data=base64.b64encode(data).decode(),
                contentId=str(cid or "").strip("<>"),
            )
        )
    thread = dict(
        customer={"email": author},
        text=body.strip() or "<p>[No message body]</p>",
        createdAt=timestamp(msg),
        attachments=attachments,
        to=addresses(msg, "To"),
        cc=addresses(msg, "Cc"),
    )
    return author, thread


def known_conversation(db, author, refs):
    # An unrelated sender may quote another customer's message identifier or ticket number.
    for ref in reversed(refs):
        matches = db.execute(
            """
            SELECT DISTINCT c.id FROM mail_ids m JOIN conversations c ON c.id=m.conversation
             WHERE m.mid=? AND (lower(json_extract(c.data,'$.primaryCustomer.email'))=? OR
                   EXISTS (SELECT 1 FROM customer_emails e WHERE e.customer_id=c.customer_id AND e.email=?) OR
                   EXISTS (SELECT 1 FROM threads t, json_each(t.data,'$.cc') cc WHERE t.conversation_id=c.id
                       AND json_extract(t.data,'$.type') IN ('message','forwardchild') AND json_extract(t.data,'$.state')='published'
                       AND lower(COALESCE(json_extract(CASE WHEN cc.type='object' THEN cc.value ELSE '{}' END,'$.email'),cc.value))=?) OR
                   EXISTS (SELECT 1 FROM threads f,json_each(f.data,'$.to') recipient
                       WHERE f.conversation_id=c.id AND json_extract(f.data,'$.type')='forwardchild'
                       AND json_extract(f.data,'$.state')='published' AND lower(recipient.value)=?))
        """,
            (ref, author, author, author, author),
        ).fetchall()
        if len(matches) == 1:
            return matches[0][0]


class GroupSignature(frozenset):
    """Header names bound by the verified Google Group redistribution signature."""


def original_sender_matches(msg, author, signed):
    if "x-original-authentication-results" not in signed:
        return False
    results = msg.get_all("X-Original-Authentication-Results", [])
    if len(results) != 1:
        return False
    clauses = str(results[0]).lower().split(";")
    if clauses[0].strip() != "mx.google.com":
        return False
    auth = [re.sub(r"\([^()]*\)", "", part).strip() for part in clauses[1:]]
    domain = author.rsplit("@", 1)[-1]

    def valid(part, method, prop):
        if not part.startswith(method + "=pass "):
            return False
        values = re.findall(r"(?<!\S)" + re.escape(prop) + r"=([^\s;]+)", part)
        return len(values) == 1 and values[0].rsplit("@", 1)[-1] == domain

    dmarc = [part for part in auth if part.startswith("dmarc=")]
    aligned = len(dmarc) == 1 and valid(dmarc[0], "dmarc", "header.from")
    # Some legitimate senders have no DMARC policy. Accept an exact-domain DKIM
    # or SPF pass recorded by Google only when DMARC did not report failure.
    if not aligned and (
        not dmarc or len(dmarc) == 1 and dmarc[0].split()[0] == "dmarc=none"
    ):
        aligned = any(
            valid(part, "dkim", "header.i")
            or valid(part, "dkim", "header.d")
            or valid(part, "spf", "smtp.mailfrom")
            for part in auth
        )
    if not aligned:
        return False
    if [a.lower() for a in addresses(msg, "From")] == [author]:
        return True
    return (
        "x-original-sender" in signed
        and len(msg.get_all("X-Original-Sender", [])) == 1
        and [a.lower() for a in addresses(msg, "X-Original-Sender")] == [author]
    )


def group_signature(raw, msg):
    """Verify the redistribution signature, including the exact group identity headers."""
    import dkim

    for header in ("List-ID", "From", "Message-ID"):
        if len(msg.get_all(header, [])) != 1:
            return False
    if len(msg.get_all("Reply-To", [])) > 1 or len(msg.get_all("Sender", [])) > 1:
        return False
    if not re.fullmatch(
        r"(?:[^<>]*\s)?<"+re.escape(company.GROUP_ID)+">", str(msg["List-ID"]).strip(), re.I
    ):
        return False
    required = {"list-id", "from", "message-id"}
    if msg.get("Sender"):
        if [a.lower() for a in addresses(msg, "Sender")] != [ADDRESS]:
            return False
        required.add("sender")
    else:
        # For DMARC-rewritten mail Google Groups can make the list itself From and omit Sender.
        if (
            [a.lower() for a in addresses(msg, "From")] != [ADDRESS]
            or len(msg.get_all("X-Original-Sender", [])) != 1
            or len(msg.get_all("X-Original-Authentication-Results", [])) != 1
        ):
            return False
        required.update(("x-original-sender", "x-original-authentication-results"))
    if msg.get("Reply-To"):
        required.add("reply-to")
    for index, value in enumerate(msg.get_all("DKIM-Signature", [])):
        tags = {}
        for part in str(value).split(";"):
            name, sep, field = part.strip().partition("=")
            if sep:
                tags[name.lower()] = field.strip()
        if tags.get("d", "").lower() != company.GROUP_DOMAIN or "l" in tags:
            continue
        covered = {name.strip().lower() for name in tags.get("h", "").split(":")}
        if required.issubset(covered) and dkim.DKIM(raw, timeout=8).verify(idx=index):
            return GroupSignature(covered)
    return False


def report_parent(db, message):
    mids = set()
    for part in message.walk():
        if part is message:
            continue
        mid = str(
            part.get("Original-Message-ID") or part.get("Message-ID") or ""
        ).strip()
        if MESSAGE_ID.fullmatch(mid):
            mids.add(mid)
        if part.get_content_type() == "text/rfc822-headers":
            headers = BytesParser(policy=policy.default).parsebytes(
                part.get_payload(decode=True) or b"", headersonly=True
            )
            mid = str(headers.get("Message-ID") or "").strip()
            if MESSAGE_ID.fullmatch(mid):
                mids.add(mid)
    parents = set()
    for mid in mids:
        parents.update(
            tuple(row)
            for row in db.execute(
                """SELECT m.conversation,m.thread FROM mail_ids m JOIN transport_receipts r ON r.thread=m.thread
               WHERE m.mid=? AND r.provider_id IS NOT NULL""",
                (mid,),
            )
        )
    return parents.pop() if len(parents) == 1 else (None, None)


def triage_verdict(db, msg, subject, body, author, refs):
    """Assess a message without letting a fault in the check stop the mail."""
    db.execute("SAVEPOINT intake_triage")
    try:
        return intake_triage.judge(db, msg, subject, body, author, refs)
    except Exception:  # The check is advisory; intake continues without a verdict.
        logging.exception("Intake check failed; the message is admitted without a verdict")
        db.execute("ROLLBACK TO intake_triage")
        return None
    finally:
        db.execute("RELEASE intake_triage")


def triage_record(db, key, conversation, verdict, filed):
    db.execute("SAVEPOINT intake_triage")
    try:
        intake_triage.record(db, key, conversation, verdict, filed, store.now())
    except Exception:
        logging.exception("Intake verdict could not be recorded")
        db.execute("ROLLBACK TO intake_triage")
    finally:
        db.execute("RELEASE intake_triage")


def admit(
    db, key, raw, active=True, verify=group_signature, promotion=None, decision=None
):
    """Called only for objects from the dedicated SES receipt rule's restricted S3 prefix."""
    if len(raw) > MAX_RAW:
        raise ValueError("oversize")
    digest = hashlib.sha256(raw).hexdigest()
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    mid = str(msg.get("Message-ID") or "")
    refs = MESSAGE_ID.findall(
        " ".join(str(msg.get(h, "")) for h in ("References", "In-Reply-To"))
    )
    trusted = verify(raw, msg)
    with store.transaction(db):
        active = active and store.writer_live(db)
        previous = db.execute(
            "SELECT disposition,sha FROM inbound_objects WHERE object_key=?", (key,)
        ).fetchone()
        if decision is not None:
            store.require_writer(db)
            if (
                not previous
                or previous[0] != decision["expected"]
                or digest != decision["sha"]
            ):
                raise store.Conflict("Review receipt changed")
            if previous[0] not in (
                "held_cutover",
                "held_non_group",
                "held_parse",
                "held_sender_verification",
                "held_threading",
                "held_source_identity",
            ):
                raise store.Invalid("This disposition requires a separate review")
            if not trusted or msg.get_content_type() == "multipart/report":
                raise store.Invalid("Only verified group messages can be released")
        if previous:
            if decision is None and (
                not active
                or previous[0] not in ("shadow", "held_cutover", "awaiting_parent")
                or promotion is None
            ):
                return previous[0]
            if digest != previous[1]:
                raise store.Conflict("Stored intake content changed")
            db.execute("DELETE FROM inbound_objects WHERE object_key=?", (key,))
            db.execute("DELETE FROM intake_parent_wait WHERE object_key=?", (key,))
        duplicate = db.execute(
            "SELECT sha FROM inbound_objects WHERE (sha=? OR (mid=? AND mid!='')) "
            "AND disposition IN ('imported','shadow','represented_in_source') LIMIT 1",
            (digest, mid),
        ).fetchone()
        kind = "duplicate" if duplicate else "imported" if active else "shadow"
        fingerprint = None
        if (
            trusted
            and MESSAGE_ID.fullmatch(mid)
            and msg.get_content_type() != "multipart/report"
        ):
            author, content = extract_message(msg)
            fingerprint = hashlib.sha256(
                store.encode(
                    [
                        author,
                        str(msg.get("Subject") or ""),
                        content["text"],
                        content["attachments"],
                        sorted(content["to"]),
                        sorted(content["cc"]),
                    ]
                ).encode()
            ).hexdigest()
            if duplicate and duplicate[0] != digest:
                prior_identity = db.execute(
                    "SELECT identity FROM inbound_fingerprints WHERE mid=?", (mid,)
                ).fetchone()
                if not prior_identity or prior_identity[0] != fingerprint:
                    kind = "held_duplicate_identity"
        cid = tid = None
        if not trusted:
            kind = "held_non_group"
        if not duplicate and any(
            str(msg.get(header, "")).strip().upper() in ("FAIL", "GRAY")
            for header in ("X-SES-Spam-Verdict", "X-SES-Virus-Verdict")
        ):
            kind = "held_filter"
        if (
            kind in ("held_non_group", "imported")
            and msg.get_content_type() == "multipart/report"
        ):
            cid, tid = report_parent(db, msg)
            kind = "provider_report" if cid is not None else "held_delivery_report"
        if trusted and active and kind == "imported":
            # An email delivered to both providers can already be in the final source delta.
            matches = (
                list(
                    db.execute(
                        """
                SELECT t.conversation_id,t.id,t.data FROM mail_ids m
                  JOIN threads t ON t.id=m.thread AND t.conversation_id=m.conversation
                 WHERE m.mid=? AND t.id<? AND json_extract(t.data,'$.type')='customer'
                 LIMIT 2""",
                        (mid, 1_000_000_000_000),
                    )
                )
                if MESSAGE_ID.fullmatch(mid)
                else []
            )
            if matches:
                author, _ = extract_message(msg)
                if len(matches) == 1:
                    doc = json.loads(matches[0][2])
                    identity = (doc.get("customer") or {}).get("email") or (
                        doc.get("createdBy") or {}
                    ).get("email")
                    if identity and identity.casefold() == author:
                        kind = "represented_in_source"
                        cid, tid = matches[0][:2]
                    else:
                        kind = "held_source_identity"
                else:
                    kind = "held_source_identity"
            elif promotion == "before_cutover" and decision is None:
                kind = "held_cutover"
        if kind == "imported":
            author, thread = extract_message(msg)
            subject = str(msg.get("Subject") or "(no subject)")[:2000]
            auto = str(msg.get("Auto-Submitted", "no")).lower() != "no"
            is_report = msg.get_content_type() == "multipart/report"
            cid = None if auto or is_report else known_conversation(db, author, refs)
            if decision is not None:
                cid = decision.get("conversation")
                if cid:
                    store.conversation(db, cid)
            else:
                hold = None
                if (
                    cid
                    and isinstance(trusted, GroupSignature)
                    and not original_sender_matches(msg, author, trusted)
                ):
                    hold = "held_sender_verification"
                elif not cid and refs and not auto and not is_report:
                    # SES can notify us of a reply before its parent. Keep the original
                    # durable and retry once a reference arrives or the grace period ends.
                    if (
                        promotion == "after_cutover"
                        and not (previous and previous[0] == "awaiting_parent")
                        and not db.execute(
                            "SELECT 1 FROM json_each(?) ref JOIN mail_ids m ON m.mid=ref.value LIMIT 1",
                            (store.encode(refs),),
                        ).fetchone()
                    ):
                        db.execute(
                            "INSERT INTO intake_parent_wait VALUES(?,?,?)",
                            (
                                key,
                                store.encode(refs),
                                int(time.time()) + inbound_queue.PARENT_WAIT_SECONDS,
                            ),
                        )
                        db.execute(
                            "INSERT INTO inbound_objects VALUES(?,?,?,?,NULL,NULL,?)",
                            (key, digest, mid, "awaiting_parent", store.now()),
                        )
                        return "awaiting_parent"
                    # A reply to an old message may have a surviving subject but no RFC mapping.
                    # Surface a candidate privately. Only a human may attach it based on this evidence.
                    rows = db.execute(
                        """
                        SELECT id,json_extract(data,'$.subject') FROM conversations WHERE customer_id IN
                           (SELECT customer_id FROM customer_emails WHERE email=?)
                        ORDER BY modified DESC LIMIT 150""",
                        (author,),
                    )
                    candidates = [
                        row[0]
                        for row in rows
                        if normalize_subject(row[1]) == normalize_subject(subject)
                    ]
                    if candidates:
                        cid = candidates[0] if len(candidates) == 1 else None
                        hold = "held_threading"
                if hold:
                    db.execute(
                        "INSERT INTO inbound_objects VALUES(?,?,?,?,?,?,?)",
                        (key, digest, mid, hold, cid, None, store.now()),
                    )
                    return hold
            if cid:
                tid = store.append_thread(db, cid, "customer", thread, source="email")
            else:
                tags = (
                    [company.IGNORE_TAG, "delivery-report" if is_report else "auto-reply"]
                    if auto or is_report
                    else []
                )
                # A message a person released by hand is support by their decision.
                # Mail its sender marks as automatic is read by the check as well: without a verdict
                # it stayed open with the agents off, for a person to close by hand.
                verdict = None
                if not is_report and decision is None and intake_triage.mode() != "off":
                    verdict = triage_verdict(db, msg, subject, thread["text"], author, refs)
                acting = verdict is not None and intake_triage.mode() == "act"
                filed = acting and intake_triage.acts(verdict)
                # Mail the receiver itself treats as automatic is never answered as a wrong address.
                suspect = acting and not auto and verdict["category"] == "wrong_address"
                unacknowledged = suspect and intake_triage.holds_acknowledgment(verdict)
                if filed:
                    # Confident machine mail is kept, closed and out of the agents' queue.
                    tags = [company.IGNORE_TAG, "non-support", intake_triage.FILED_TAGS[verdict["category"]]]
                elif suspect:
                    tags = [intake_triage.SUSPECT_TAG]
                cid = store.create_conversation(
                    db,
                    dict(
                        mailboxId=MAILBOX_ID,
                        subject=subject,
                        customer=thread["customer"],
                        tags=tags,
                        createdAt=thread["createdAt"],
                        threads=[dict(thread, type="customer")],
                        **({"status": "closed"} if filed else {}),
                    ),
                    source="email",
                )
                tid = db.execute(
                    "SELECT id FROM threads WHERE conversation_id=?", (cid,)
                ).fetchone()[0]
                if verdict is not None:
                    triage_record(db, key, cid, verdict, filed)
                # No automatic acknowledgment goes to mail that was filed as machine mail.
                if (
                    not auto
                    and not is_report
                    and author != ADDRESS
                    and decision is None
                    and not filed
                    and not unacknowledged
                ):
                    mailbox_settings.first_inbound(db, cid, author, msg)
            if MESSAGE_ID.fullmatch(mid):
                db.execute(
                    "INSERT OR IGNORE INTO mail_ids VALUES(?,?,?)", (mid, cid, tid)
                )
        db.execute(
            "INSERT INTO inbound_objects VALUES(?,?,?,?,?,?,?)",
            (key, digest, mid, kind, cid, tid, store.now()),
        )
        if (
            kind == "shadow"
            and trusted
            and msg.get_content_type() != "multipart/report"
            and intake_triage.mode() != "off"
        ):
            # Before handover, verdicts on real mail are recorded for review and change nothing.
            try:
                author, content = extract_message(msg)
            except (ValueError, store.Invalid):
                author = None
            if (
                author
                and str(msg.get("Auto-Submitted", "no")).lower() == "no"
                and not known_conversation(db, author, refs)
            ):
                verdict = triage_verdict(
                    db, msg, str(msg.get("Subject") or ""), content["text"], author, refs
                )
                if verdict is not None:
                    triage_record(db, key, None, verdict, False)
        if fingerprint and kind in ("shadow", "imported", "represented_in_source"):
            db.execute(
                "INSERT OR IGNORE INTO inbound_fingerprints VALUES(?,?)",
                (mid, fingerprint),
            )
        if decision is not None:
            if kind not in ("imported", "duplicate", "represented_in_source"):
                raise store.Conflict(
                    "Underlying receipt still conflicts with the source"
                )
            db.execute(
                "INSERT INTO intake_decisions VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    digest,
                    decision["expected"],
                    kind,
                    cid,
                    decision["reason"],
                    store.now(),
                ),
            )
    return kind


def render(db, cid, tid):
    conv = store.conversation(db, cid)
    thread = store.thread(db, cid, tid)
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = mailbox_settings.sender_name(db) + " <" + ADDRESS + ">"
    msg["Reply-To"] = ADDRESS
    msg["Return-Path"] = "<" + RETURN_PATH + ">"
    to = thread.get("to") or [thread["customer"]["email"]]
    cc, bcc = thread.get("cc", []), thread.get("bcc", [])
    recipients = list(dict.fromkeys(store.email(addr) for addr in [*to, *cc, *bcc]))
    provider_feedback.verify_recipients(db, recipients)
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    subject = str(conv["subject"])
    refs = [
        row[0]
        for row in db.execute(
            "SELECT mid FROM mail_ids WHERE conversation=? ORDER BY thread DESC LIMIT 12",
            (cid,),
        )
    ]
    msg["Subject"] = (
        "Re: " if refs and not subject.lower().startswith("re:") else ""
    ) + subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = company.message_id(cid,tid)
    if refs:
        msg["In-Reply-To"] = refs[0]
        msg["References"] = " ".join(reversed(refs))
    body = mailbox_settings.outbound_body(
        db, tid, thread["body"], os.environ["INBOX_BASE_URL"].rstrip("/")
    )
    msg.set_content(body, subtype="html")
    for att in thread["_embedded"].get("attachments", []):
        data, mime = store.attachment(db, cid, att["id"])
        major, _, minor = mime.partition("/")
        msg.add_attachment(
            data,
            maintype=major or "application",
            subtype=minor or "octet-stream",
            filename=att.get("filename") or att.get("fileName") or "attachment",
        )
    return recipients, msg


def submit(ses, recipients, wire, token):
    options = {}
    if os.getenv("INBOX_SES_CONFIGURATION_SET"):
        options["ConfigurationSetName"] = os.environ["INBOX_SES_CONFIGURATION_SET"]
        options["Tags"] = [{"Name": "pxg-inbox-receipt", "Value": token}]
    return ses.send_raw_email(
        Destinations=recipients, RawMessage={"Data": wire}, **options
    )


def send_one(db, ses):
    row = db.execute(
        "SELECT conversation_id,thread_id FROM mail_outbox WHERE state='queued' ORDER BY thread_id LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    cid, tid = row
    try:
        recipients, msg = render(db, cid, tid)
        wire = msg.as_bytes()
    except (ValueError, KeyError, TypeError, UnicodeError, store.Missing) as exc:
        with store.transaction(db):
            db.execute(
                "UPDATE mail_outbox SET state='failed' WHERE thread_id=? AND state='queued'",
                (tid,),
            )
            db.execute(
                "INSERT OR REPLACE INTO transport_receipts VALUES (?,NULL,?)",
                (tid, type(exc).__name__),
            )
        return True
    with store.transaction(db):
        if not store.writer_live(db):
            return False
        if not db.execute(
            "UPDATE mail_outbox SET state='submitting' WHERE thread_id=? AND state='queued'",
            (tid,),
        ).rowcount:
            return False
        token = provider_feedback.begin_submission(db, "reply", tid, recipients)
    try:
        # SDK is configured with exactly one attempt for a non-idempotent provider operation.
        result = submit(ses, recipients, wire, token)
        receipt = result["MessageId"]
    except Exception as exc:
        status = (
            getattr(exc, "response", {})
            .get("ResponseMetadata", {})
            .get("HTTPStatusCode")
        )
        state = "failed" if status in (400, 401, 403, 404, 413) else "uncertain"
        with store.transaction(db):
            if db.execute(
                "UPDATE mail_outbox SET state=? WHERE thread_id=? AND state='submitting'",
                (state, tid),
            ).rowcount:
                db.execute(
                    "INSERT OR REPLACE INTO transport_receipts VALUES (?,NULL,?)",
                    (tid, type(exc).__name__),
                )
        return True
    with store.transaction(db):
        provider_feedback.record_acceptance(db, "reply", tid, receipt)
    return True


def send_ack(db, ses):
    with store.transaction(db):
        if not store.writer_live(db):
            return False
        mailbox_settings.release_deferred(db, int(time.time()))
        record = db.execute(
            "SELECT cid,address FROM acknowledgments WHERE state='queued' ORDER BY queued LIMIT 1"
        ).fetchone()
        if not record:
            return False
        cid, address = record
        try:
            provider_feedback.verify_recipients(db, [address])
        except store.Invalid:
            db.execute(
                "UPDATE acknowledgments SET state='suppressed' WHERE cid=?", (cid,)
            )
            return True
        subject, body = mailbox_settings.ack_text(db, cid)
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = company.NAME+" <" + ADDRESS + ">"
        msg["Reply-To"] = ADDRESS
        msg["Return-Path"] = "<" + RETURN_PATH + ">"
        msg["To"] = store.email(address)
        msg["Subject"] = subject
        msg["Auto-Submitted"] = "auto-replied"
        msg["X-Auto-Response-Suppress"] = "All"
        reference = db.execute(
            "SELECT mid FROM mail_ids WHERE conversation=? ORDER BY thread LIMIT 1",
            (cid,),
        ).fetchone()
        if reference:
            msg["In-Reply-To"] = reference[0]
            msg["References"] = reference[0]
        msg.set_content(body, subtype="html")
        wire = msg.as_bytes()
        db.execute("UPDATE acknowledgments SET state='submitting' WHERE cid=?", (cid,))
        token = provider_feedback.begin_submission(db, "ack", cid, [address])
    try:
        result = submit(ses, [address], wire, token)
        state, receipt = "sent", result["MessageId"]
    except Exception as exc:
        status = (
            getattr(exc, "response", {})
            .get("ResponseMetadata", {})
            .get("HTTPStatusCode")
        )
        state = "failed" if status in (400, 401, 403, 404, 413) else "uncertain"
        receipt = type(exc).__name__
    with store.transaction(db):
        if state == "sent":
            provider_feedback.record_acceptance(db, "ack", cid, receipt)
        else:
            db.execute(
                "UPDATE acknowledgments SET state=?,receipt=? WHERE cid=? AND state='submitting'",
                (state, receipt, cid),
            )
    return True


def claim_hook(db):
    with store.transaction(db):
        if not store.writer_live(db):
            return None
        now = int(time.time())
        row = db.execute(
            """SELECT d.id,d.event,d.payload,w.data,coalesce(a.count,0)+1
               FROM deliveries d JOIN webhooks w ON w.id=d.webhook_id
               LEFT JOIN hook_attempts a ON a.id=d.id
               WHERE d.state IN ('queued','submitting') AND coalesce(a.next,0)<=?
               ORDER BY d.id LIMIT 1""",
            (now,),
        ).fetchone()
        if row:
            db.execute("UPDATE deliveries SET state='submitting' WHERE id=?", (row[0],))
            db.execute(
                """INSERT INTO hook_attempts(id,count,next) VALUES(?,?,?)
                   ON CONFLICT(id) DO UPDATE SET count=excluded.count,next=excluded.next""",
                (row[0], row[4], now + 60),
            )
        return row


def deliver_hook(db, allowed_url):
    row = claim_hook(db)
    if not row:
        return False
    key, event, payload, config, attempt = row
    hook = json.loads(config)
    url = urlsplit(allowed_url)
    if (
        hook["url"] != allowed_url
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
    ):
        state = "held"
    elif hook.get("state") != "enabled":
        state = "cancelled"
    else:
        raw = payload.encode()
        signature = base64.b64encode(
            hmac.digest(hook["secret"].encode(), raw, "sha1")
        ).decode()
        try:
            with closing(
                http.client.HTTPSConnection(
                    url.hostname,
                    url.port or 443,
                    timeout=10,
                    context=ssl.create_default_context(),
                )
            ) as client:
                client.request(
                    "POST",
                    url.path + ("?" + url.query if url.query else ""),
                    raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-HelpScout-Event": event,
                        "X-HelpScout-Signature": signature,
                        "X-Inbox-Delivery": str(key),
                        "X-Inbox-Signature": hmac.new(
                            hook["secret"].encode(),
                            f"{key}\n{event}\n".encode() + raw,
                            hashlib.sha256,
                        ).hexdigest(),
                    },
                )
                state = "sent" if 200 <= client.getresponse().status < 300 else "queued"
        except (OSError, http.client.HTTPException):
            state = "queued"
    with store.transaction(db):
        # A slow HTTP response may arrive after a later attempt obtained the lease.
        updated = db.execute(
            """UPDATE deliveries SET state=? WHERE id=? AND state='submitting'
               AND EXISTS (SELECT 1 FROM hook_attempts WHERE id=? AND count=?)""",
            (state, key, key, attempt),
        )
        if updated.rowcount:
            retry_at = int(time.time()) + 10 * (1 << min(attempt - 1, 8))
            db.execute("UPDATE hook_attempts SET next=? WHERE id=?", (retry_at, key))
    return True


def receive(db, s3, bucket, prefix, active, sqs=None, queue=None):
    active = active and store.writer_live(db)
    cutoff = None
    if active:
        cutoff = datetime.fromisoformat(
            os.environ["INBOX_RECEIVE_CUTOVER"].replace("Z", "+00:00")
        )
        if cutoff.tzinfo is None:
            raise ValueError("Cutover must have an explicit timezone")
    if queue:
        inbound_queue.run(db, sqs, queue, s3, bucket, prefix, active, cutoff)
    else:
        inbound_queue.sweep(db, s3, bucket, prefix, active, cutoff)
        if active:
            inbound_queue.retry_receipts(db, s3, bucket, cutoff)


def notify_ready():
    """Publish this worker's completed startup to its service manager, when present."""
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return
    import socket
    status = json.dumps({
        "pid": os.getpid(), "invocation": os.getenv("INVOCATION_ID"), "extensions": extensions.loaded(),
    })
    # Linux represents an abstract Unix socket address with a leading NUL.
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
        notification.sendto(("READY=1\nSTATUS=" + status).encode(), address)


def main():
    extensions.load()
    import boto3
    from botocore.config import Config

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "worker", choices=["outbound", "inbound", "events", "feedback", "workflows"]
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with closing(store.connect()) as db:
        store.initialize(db)
        schema(db)
        region = os.getenv("AWS_REGION", "us-east-1")
        ses = (
            boto3.client(
                "ses",
                region_name=region,
                config=Config(retries={"total_max_attempts": 1}),
            )
            if args.worker == "outbound"
            else None
        )
        s3 = (
            boto3.client("s3", region_name=region) if args.worker == "inbound" else None
        )
        sqs = (
            boto3.client("sqs", region_name=region)
            if args.worker in ("feedback", "inbound")
            else None
        )
        notify_ready()
        while True:
            if args.worker == "feedback":
                provider_feedback.poll(
                    db,
                    sqs,
                    os.environ["INBOX_FEEDBACK_QUEUE_URL"],
                    os.getenv("INBOX_FEEDBACK_TOPIC_ARN"),
                )
            elif args.worker == "inbound":
                queue = os.getenv("INBOX_RECEIVE_QUEUE_URL")
                receive(
                    db,
                    s3,
                    os.environ["INBOX_BUCKET"],
                    os.environ["INBOX_PREFIX"],
                    os.getenv("INBOX_RECEIVE_MODE") == "live",
                    sqs,
                    queue,
                )
                if not queue:
                    time.sleep(25)
            elif args.worker == "workflows":
                if os.environ.get("INBOX_RECEIVE_MODE") == "live":
                    import native_workflows

                    with store.transaction(db):
                        if store.writer_live(db):
                            native_workflows.tick(db)
                time.sleep(60)
            elif args.worker == "outbound":
                if os.getenv("INBOX_SEND_ENABLED") != "1" or not (
                    send_one(db, ses) or send_ack(db, ses)
                ):
                    time.sleep(0.5)
            elif not store.writer_live(db) or not deliver_hook(
                db, os.environ["INBOX_WEBHOOK_URL"]
            ):
                time.sleep(0.5)


if __name__ == "__main__":
    main()
