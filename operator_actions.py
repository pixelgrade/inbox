"""Owner-only conversation organization and linked, separately addressed forwards."""

import base64
from contextlib import closing
import html
import json
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
import nh3
import store


router = APIRouter()


def canonical(db, cid):
    row = db.execute(
        "SELECT target_id FROM conversation_aliases WHERE source_id=?", (cid,)
    ).fetchone()
    return row[0] if row else cid


def activity(db, cid, kind, text, entities, *, system=False):
    tid = store.allocate(db, "thread")
    entry = dict(
        id=tid,
        type="lineitem",
        state="published",
        createdAt=store.now(),
        createdBy={"type":"system_user"} if system else {"type":"user","id":int(os.environ.get("INBOX_AGENT_ID","1"))},
        source={"type":"system","via":"system"} if system else {"type":"web","via":"user"},
        action={"type": kind, "text": text, "associatedEntities": entities},
    )
    db.execute(
        "INSERT INTO threads(id,conversation_id,data) VALUES(?,?,?)",
        (tid, cid, store.encode(entry)),
    )
    return tid


def table(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()


def require_settled(db, cid):
    # Preserve accepted external work with its original address, payload, and receipt identity.
    checks = [
        ("mail_outbox", "conversation_id", "('queued','submitting','uncertain')"),
        ("deliveries", "conversation_id", "('queued','held')"),
        ("acknowledgments", "cid", "('queued','submitting','uncertain')"),
    ]
    for name, column, states in checks:
        if (
            table(db, name)
            and db.execute(
                f"SELECT 1 FROM {name} WHERE {column}=? AND state IN {states} LIMIT 1",
                (cid,),
            ).fetchone()
        ):
            raise store.Conflict("This conversation has a delivery awaiting resolution")


def preserve_draft_recipients(db, cid, customer_id):
    if db.execute(
        """SELECT 1 FROM threads WHERE conversation_id=? AND json_extract(data,'$.state')='draft'
        AND coalesce(json_extract(data,'$.customer.id'),0) != ? LIMIT 1""",
        (cid, customer_id),
    ).fetchone():
        raise store.Conflict(
            "Review the draft addressed to the previous customer before changing ownership"
        )


def merge(db, source, target):
    if (
        source == target
        or source != canonical(db, source)
        or target != canonical(db, target)
    ):
        raise store.Conflict("Choose two distinct current conversations")
    src = store.conversation(db, source)
    dst = store.conversation(db, target)
    if src["mailboxId"] != dst["mailboxId"]:
        raise store.Conflict("Choose a conversation in the same mailbox")
    require_settled(db, source)
    require_settled(db, target)
    preserve_draft_recipients(db, source, dst["primaryCustomer"]["id"])
    drafts = db.execute(
        """SELECT count(DISTINCT conversation_id) FROM threads
                        WHERE conversation_id IN (?,?) AND json_extract(data,'$.state')='draft'""",
        (source, target),
    ).fetchone()[0]
    if drafts > 1:
        raise store.Conflict(
            "Review both drafts and keep the intended reply before merging"
        )
    db.execute(
        """INSERT INTO operator_merges SELECT id,?,number,data,owner,? FROM conversations WHERE id=?""",
        (target, store.now(), source),
    )
    db.execute(
        """INSERT OR IGNORE INTO operator_thread_origin SELECT id,conversation_id FROM threads
               WHERE conversation_id=?""",
        (source,),
    )
    db.execute(
        "UPDATE threads SET conversation_id=? WHERE conversation_id=?", (target, source)
    )
    db.execute(
        "UPDATE mail_outbox SET conversation_id=? WHERE conversation_id=?",
        (target, source),
    )
    if table(db, "mail_ids"):
        db.execute(
            """INSERT OR IGNORE INTO mail_ids SELECT mid,?,thread FROM mail_ids WHERE conversation=?
                      AND thread IN (SELECT id FROM threads WHERE conversation_id=?)""",
            (target, source, target),
        )
    db.execute(
        "INSERT OR IGNORE INTO native_folder_members SELECT folder,? FROM native_folder_members WHERE conversation=?",
        (target, source),
    )
    db.execute("DELETE FROM native_folder_members WHERE conversation=?", (source,))
    db.execute(
        "UPDATE conversation_aliases SET target_id=? WHERE target_id=?",
        (target, source),
    )
    if db.execute("SELECT 1 FROM snoozes WHERE conversation=?", (source,)).fetchone():
        if db.execute("SELECT 1 FROM snoozes WHERE conversation=?", (target,)).fetchone():
            raise store.Conflict("Resolve the two snooze reminders before merging")
        db.execute("UPDATE snoozes SET conversation=? WHERE conversation=?", (target,source))
    import agent_ledger
    agent_ledger.move(db, source, target)  # Before the delete, which would take the records with it.
    db.execute("DELETE FROM conversations WHERE id=?", (source,))
    store.save_alias(db, source, target)
    activity(
        db,
        target,
        "merged",
        f"Merged conversation #{src['number']}",
        {"originalConversation": source},
    )
    tags = list(
        dict.fromkeys(t["tag"] for record in (dst, src) for t in record.get("tags", []))
    )
    count = db.execute(
        "SELECT count(*) FROM threads WHERE conversation_id=?", (target,)
    ).fetchone()[0]
    store.update_conversation(
        db, target, {"threads": count, "tags": store.tag_list(tags)}
    )
    return target


def change_customer(db, cid, addr):
    current = store.conversation(db, cid)
    addr = store.email(addr)
    customer = store.resolve_customer(db, {"email": addr})
    previous = current["primaryCustomer"]
    if (previous["id"], previous.get("email", "").lower()) == (
        customer["id"],
        addr.lower(),
    ):
        return cid
    require_settled(db, cid)
    preserve_draft_recipients(db, cid, customer["id"])
    if (
        current.get("primaryCustomer", {}).get("email", "").casefold()
        != addr.casefold()
        and db.execute(
            "SELECT 1 FROM threads WHERE conversation_id=? AND json_extract(data,'$.state')='draft'",
            (cid,),
        ).fetchone()
    ):
        raise store.Conflict(
            "Review the draft addressed to the previous customer first"
        )
    activity(
        db,
        cid,
        "change-customer",
        "Changed conversation customer",
        {"previousCustomer": previous, "customer": customer},
    )
    store.update_conversation(
        db,
        cid,
        {"primaryCustomer": customer, "threads": int(current.get("threads", 0)) + 1},
    )
    return cid


def edit_profile(db, cid, form):
    from operator_ui import fingerprint

    customer_id = store.conversation(db, cid)["primaryCustomer"]["id"]
    profile = store.load(db, "SELECT data FROM customers WHERE id=?", (customer_id,))
    if form.get("profile_version") != fingerprint(store.encode(profile)):
        raise store.Conflict("Customer profile changed. Reload before editing.")
    previous = db.execute(
        "SELECT fields,emails FROM operator_customer_edits WHERE customer=?",
        (customer_id,),
    ).fetchone()
    fields, emails = (
        (json.loads(previous[0]), json.loads(previous[1])) if previous else ({}, [])
    )
    for field, limit in (
        ("firstName", 150),
        ("lastName", 150),
        ("organization", 255),
        ("jobTitle", 255),
        ("background", 12000),
    ):
        if field not in form:
            continue
        value = str(form[field]).strip()
        if len(value) > limit or "\x00" in value:
            raise store.Invalid("Invalid customer profile field")
        fallback = {"firstName": "first", "lastName": "last"}.get(field, field)
        if value != str(profile.get(field, profile.get(fallback)) or ""):
            fields[field] = value
    address = str(form.get("additional_email") or "").strip()
    if address:
        address = store.email(address)
        owners = {
            r[0]
            for r in db.execute(
                "SELECT customer_id FROM customer_emails WHERE email=?", (address,)
            )
        }
        if owners - {customer_id}:
            raise store.Conflict("Address already belongs to another customer")
        if customer_id not in owners:
            emails.append(address)
    db.execute(
        "INSERT INTO operator_customer_edits VALUES(?,?,?,?) ON CONFLICT(customer) DO UPDATE SET fields=excluded.fields,emails=excluded.emails,changed=excluded.changed",
        (customer_id, store.encode(fields), store.encode(emails), store.now()),
    )
    store.save_customer(db, profile)
    return cid


def addresses(raw):
    return list(
        dict.fromkeys(
            store.email(a.strip()) for a in str(raw or "").split(",") if a.strip()
        )
    )


def forward(db, cid, tid, form):
    if os.getenv("INBOX_SEND_ENABLED") != "1":
        raise store.Conflict("Mail delivery is disabled")
    original = store.thread(db, cid, tid)
    if original.get("state") != "published" or original.get("type") not in {
        "customer",
        "message",
    }:
        raise store.Invalid(
            "Select a published customer message or sent reply to forward"
        )
    to = addresses(form.get("to"))
    if not to:
        raise store.Invalid("Enter a forward recipient")
    conv = store.conversation(db, cid)
    note = str(form.get("message") or "").strip()
    if len(note) > 100000:
        raise store.Invalid("Forward message is too long")
    quote = nh3.clean(
        str(original.get("body") or ""),
        tags={
            "p",
            "div",
            "br",
            "b",
            "strong",
            "i",
            "em",
            "a",
            "ul",
            "li",
            "ol",
            "blockquote",
            "pre",
            "code",
        },
        attributes={"a": {"href"}},
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
    )
    payload = dict(
        type="reply",
        to=to,
        cc=addresses(form.get("cc")),
        bcc=addresses(form.get("bcc")),
        text="<p>"
        + html.escape(note).replace("\n", "<br>")
        + "</p><blockquote>"
        + quote
        + "</blockquote>",
    )
    if form.get("files") == "yes":
        total = 0
        payload["attachments"] = []
        for item in (original.get("_embedded") or {}).get("attachments") or []:
            blob, mime = store.attachment(db, cid, item["id"])
            total += len(blob)
            if total > 24 * 1024**2:
                raise store.Invalid("Forward attachments exceed 24 MB")
            payload["attachments"].append(
                dict(
                    fileName=item.get("filename")
                    or item.get("fileName")
                    or "attachment",
                    mimeType=mime,
                    data=base64.b64encode(blob).decode(),
                )
            )
    child = store.create_conversation(
        db,
        dict(
            subject=("Fwd: " + conv["subject"])[:2000],
            mailboxId=conv["mailboxId"],
            customer={"email": to[0]},
            status="pending",
            threads=[payload],
        ),
        source="web",
    )
    sent = db.execute(
        "SELECT id,data FROM threads WHERE conversation_id=?", (child,)
    ).fetchone()
    parent_id = store.allocate(db, "thread")
    sent_doc = json.loads(sent[1])
    sent_doc.update(
        type="forwardchild", linkedConversationId=cid, linkedThreadId=parent_id
    )
    db.execute(
        "UPDATE threads SET data=? WHERE id=?", (store.encode(sent_doc), sent[0])
    )
    # The original mailbox view holds a link and delivery state; only the child can send or receive replies.
    parent = {
        **sent_doc,
        "id": parent_id,
        "type": "forwardparent",
        "linkedConversationId": child,
        "linkedThreadId": sent[0],
        "body": html.escape(note),
        "_embedded": {"attachments": []},
    }
    db.execute(
        "INSERT INTO threads(id,conversation_id,data) VALUES(?,?,?)",
        (parent_id, cid, store.encode(parent)),
    )
    store.update_conversation(db, cid, {"threads": int(conv.get("threads", 0)) + 1})
    return child


def ticket_number(db, raw):
    number = store.identifier(str(raw).strip().lstrip("#"))
    targets = {
        row[0]
        for row in db.execute(
            """SELECT id FROM conversations WHERE number=? UNION
              SELECT a.target_id FROM operator_merges m JOIN conversation_aliases a ON a.source_id=m.source WHERE m.number=?""",
            (number, number),
        )
    }
    if len(targets) != 1:
        raise store.Invalid("Enter one existing, unambiguous ticket number")
    return targets.pop()


@router.get("/manage/{cid}")
def management(request: Request, cid: int, target: str = ""):
    from operator_ui import fingerprint, operator, page, version

    with closing(store.connect()) as db, store.transaction(db, False):
        operator(request, db)
        current = canonical(db, cid)
        if current != cid:
            return RedirectResponse(f"/manage/{current}", status_code=303)
        conv = store.conversation(db, cid)
        dst = store.conversation(db, ticket_number(db, target)) if target else None
        profile = store.load(
            db,
            "SELECT data FROM customers WHERE id=?",
            (conv["primaryCustomer"]["id"],),
        )
        return page(
            "operator_actions.html",
            conv=conv,
            target=dst,
            profile=profile,
            profile_version=fingerprint(store.encode(profile)),
            v=version(db, cid),
            other=version(db, dst["id"]) if dst else None,
            threads=[
                t
                for t in store.thread_list(db, cid)
                if t.get("state") == "published"
                and t.get("type") in ("customer", "message")
            ],
            writes=store.writer_live(db) and os.getenv("INBOX_OPERATOR_WRITES") == "1",
            send=os.getenv("INBOX_SEND_ENABLED") == "1",
        )


@router.post("/manage/{cid}")
async def apply(request: Request, cid: int):
    from operator_ui import operator, same_origin, version

    same_origin(request)
    if os.getenv("INBOX_OPERATOR_WRITES") != "1":
        raise HTTPException(409, "The mailbox is in migration preview")
    form = await request.form()
    with closing(store.connect()) as db, store.transaction(db):
        operator(request, db)
        store.require_writer(db)
        if form.get("version") != version(db, cid):
            raise store.Conflict("Conversation changed. Reload before continuing.")
        action = form.get("action")
        if action == "merge":
            target = store.identifier(form.get("target"))
            if form.get("other_version") != version(db, target):
                raise store.Conflict("The destination conversation changed")
            result = merge(db, cid, target)
        elif action == "profile":
            result = edit_profile(db, cid, form)
        elif action == "customer":
            result = change_customer(db, cid, form.get("email"))
        elif action == "forward":
            result = forward(db, cid, store.identifier(form.get("thread")), form)
        else:
            raise store.Invalid("Unknown conversation action")
    return RedirectResponse(f"/conversation/{result}", status_code=303)
