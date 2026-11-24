"""Mailbox-level acknowledgments and private, reply-scoped customer ratings."""
import hashlib
from datetime import datetime, timezone
import html
from pathlib import Path
import json
import os
import re
import secrets
import time
from urllib.parse import quote

import store
import company

OPTIONS = ("great","okay","bad")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mailbox_settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS acknowledgments(cid INTEGER PRIMARY KEY,address TEXT COLLATE NOCASE,
            queued INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued',receipt TEXT);
        CREATE INDEX IF NOT EXISTS ack_history ON acknowledgments(address,queued);
        CREATE TABLE IF NOT EXISTS source_ack_window(address TEXT PRIMARY KEY COLLATE NOCASE, expires INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ack_deferred ON acknowledgments(state,queued) WHERE state='deferred_cutover';
        CREATE TABLE IF NOT EXISTS open_receipts(tid INTEGER PRIMARY KEY,token TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS rating_links(tid INTEGER PRIMARY KEY,token TEXT NOT NULL UNIQUE,
            choice TEXT,comment TEXT,changed TEXT);
    """)


def get(db,key):
    row=db.execute("SELECT value FROM mailbox_settings WHERE name=?",(key,)).fetchone()
    return json.loads(row[0]) if row else {}


def install(db,path):
    settings=json.loads(Path(path).read_text())
    for key in ("autoReply", "ratings", "general"):
        db.execute(
            "INSERT OR REPLACE INTO mailbox_settings VALUES(?,?)",
            (key, store.encode(settings[key])),
        )


AUTO_REPLY = re.compile(r"\bauto(?:matic)?[\s_-]*repl(?:y|ies)\b", re.I)


def capture_source_window(db, now):
    """Preserve possible former-provider cooldowns, without inventing a send receipt."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='mailbox_settings'"
    ).fetchone():
        return 0
    if not get(db, "autoReply").get("enabled"):
        return 0
    cutoff = datetime.fromtimestamp(now - 86400, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    addresses = set()
    db.execute("DELETE FROM source_ack_window")
    for cid, header in db.execute(
        "SELECT id,data FROM conversations WHERE native=0 AND modified>=?", (cutoff,)
    ):
        conv = json.loads(header)
        if conv.get("type") != "email":
            continue
        # A source merge can bring a new customer conversation into a much older one.
        rows = db.execute(
            "SELECT data FROM threads WHERE conversation_id=? AND created>=? ORDER BY created,id",
            (cid, cutoff),
        )
        first = (conv.get("createdAt") or "") >= cutoff
        for row in rows:
            thread = json.loads(row[0])
            if (
                thread.get("type") in ("lineitem", "note")
                or thread.get("state") == "draft"
            ):
                continue
            merged = (thread.get("action") or {}).get("type") == "merged"
            if (
                (first or merged)
                and thread.get("type") == "customer"
                and (thread.get("source") or {}).get("type") == "email"
            ):
                sender = (thread.get("customer") or {}).get("email") or (
                    thread.get("createdBy") or {}
                ).get("email")
                if not sender and not merged:
                    sender = (conv.get("primaryCustomer") or {}).get("email")
                addresses.add(sender.lower() if sender else "*")
            first = False
    db.executemany(
        "INSERT INTO source_ack_window VALUES(?,?)",
        ((address, now + 86400) for address in addresses),
    )
    return len(addresses)


def release_deferred(db, now):
    """Release one due acknowledgment only if the question still awaits a reply."""
    row = db.execute(
        """SELECT a.cid,a.address FROM acknowledgments a WHERE a.state='deferred_cutover'
        AND NOT EXISTS(SELECT 1 FROM source_ack_window w WHERE w.address IN (a.address,'*') AND w.expires>?)
        ORDER BY a.queued,a.cid LIMIT 1""",
        (now,),
    ).fetchone()
    if not row:
        return 0
    cid, address = row
    eligible = db.execute(
        """SELECT 1 FROM conversations c WHERE id=? AND status IN ('active','pending')
        AND lower(json_extract(data,'$.primaryCustomer.email'))=lower(?)
        AND NOT EXISTS(SELECT 1 FROM threads t WHERE t.conversation_id=c.id
                       AND json_extract(t.data,'$.type')='message' AND json_extract(t.data,'$.state')='published')
        AND NOT EXISTS(SELECT 1 FROM acknowledgments a WHERE a.address=? AND a.cid!=c.id AND a.queued>?
                       AND a.state IN ('queued','submitting','uncertain','sent'))""",
        (cid, address, address, now - 86400),
    ).fetchone()
    if eligible and get(db, "autoReply").get("enabled"):
        db.execute(
            "UPDATE acknowledgments SET state='queued',queued=? WHERE cid=?", (now, cid)
        )
    else:
        db.execute(
            "UPDATE acknowledgments SET state='resolved_before_ack' WHERE cid=?", (cid,)
        )
    return 1


def first_inbound(db, cid, author, message):
    config = get(db, "autoReply")
    if not config.get("enabled") or config.get("considerOfficeHours"):
        return
    if (
        str(message.get("Auto-Submitted", "no")).lower() != "no"
        or message.get("X-Auto-Response-Suppress")
        or str(message.get("Precedence", "")).lower() in ("bulk", "junk", "auto_reply")
        or message.get_content_type() == "multipart/report"
        or str(message.get("Return-Path", "")) == "<>"
        or re.search(
            r"^(?:alerts?|mailer-daemon|postmaster|no-?reply|do-?not-?reply)(?:[+.@_-])",
            author,
            re.I,
        )
        or AUTO_REPLY.search(str(message.get("Subject", "")))
    ):
        return
    part = message.get_body(preferencelist=("plain", "html"))
    if part is not None and part.get_content_maintype() == "text":
        body = part.get_content()
        if part.get_content_subtype() == "html":
            import nh3

            body = html.unescape(nh3.clean(body, tags=set()))
        if AUTO_REPLY.search(body):
            return
    now = int(time.time())
    if not db.execute(
        "SELECT 1 FROM acknowledgments WHERE address=? AND queued>? AND state!='resolved_before_ack'",
        (author, now - 86400),
    ).fetchone():
        deferred = db.execute(
            "SELECT 1 FROM source_ack_window WHERE address IN (?, '*') AND expires>?",
            (author, now),
        ).fetchone()
        state = "deferred_cutover" if deferred else "queued"
        db.execute(
            "INSERT OR IGNORE INTO acknowledgments(cid,address,queued,state) VALUES(?,?,?,?)",
            (cid, author, now, state),
        )


def ack_text(db, cid):
    conv = store.conversation(db, cid)
    customer = conv["primaryCustomer"]
    cfg = get(db, "autoReply")
    first=customer.get("first") or "there"
    body=cfg["message"].replace("{%customer.firstName,fallback=there%}",html.escape(first))
    subject=cfg["subject"].replace("{%subject%}",conv["subject"])
    return subject,body


def sender_name(db):
    config=get(db,"general")
    if config.get("fromName")==3 and config.get("fromNameCustom"):
        return config["fromNameCustom"].replace("{%user.firstName%}",os.environ.get("INBOX_AGENT_NAME","Support"))
    return company.NAME


def outbound_body(db,tid,body,origin):
    result=with_rating(db,tid,body,origin)
    db.execute("INSERT OR IGNORE INTO open_receipts(tid,token) VALUES(?,?)",(tid,secrets.token_urlsafe(32)))
    token=db.execute("SELECT token FROM open_receipts WHERE tid=?",(tid,)).fetchone()[0]
    return result+f'<img src="{origin}/read/{token}.gif" width="1" height="1" alt="">'


def with_rating(db,tid,body,origin):
    cfg=get(db,"ratings")
    if str(cfg.get("enabled","")).lower() not in ("true","1","on","yes"):
        return body
    db.execute("INSERT OR IGNORE INTO rating_links(tid,token) VALUES(?,?)",(tid,secrets.token_urlsafe(32)))
    token=db.execute("SELECT token FROM rating_links WHERE tid=?",(tid,)).fetchone()[0]
    footer=cfg.get("ratingsText","")
    for choice in OPTIONS:
        footer=footer.replace("{%ratings."+choice+"%}",origin+"/rating/"+token+"/"+choice)
    return (footer+body) if cfg.get("placement")=="above" else (body+footer)


if __name__=="__main__":
    import extensions
    extensions.load()
    import argparse
    from contextlib import closing
    p=argparse.ArgumentParser()
    p.add_argument("settings")
    p.add_argument("db")
    args=p.parse_args()
    with closing(store.connect(args.db)) as db:
        initialize(db)
        with store.transaction(db):
            install(db,args.settings)
