"""Transactional mailbox storage. Callers own the transaction."""

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import re
import sqlite3

import native_workflows


class WriterPaused(RuntimeError):
    pass


class Invalid(ValueError):
    pass


class Missing(LookupError):
    pass


class Conflict(Exception):
    pass


def now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def connect(path=None):
    db = sqlite3.connect(
        path or os.environ["INBOX_DB"], timeout=15, isolation_level=None
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=FULL")
    return db


@contextmanager
def transaction(db, write=True):
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise


def writer_live(db):
    row = db.execute("SELECT mode FROM writer_authority WHERE id=1").fetchone()
    return bool(row and row[0] == "live")


def require_writer(db):
    if not writer_live(db):
        raise WriterPaused("Mailbox writes are paused")


def initialize(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS writer_authority(id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT NOT NULL
            CHECK(mode IN ('standby','live','paused')),changed TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS serial(name TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS source_profile(kind TEXT,id INTEGER,data TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS mailboxes(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS customer_emails(email TEXT COLLATE NOCASE, customer_id INTEGER
            REFERENCES customers ON DELETE CASCADE, PRIMARY KEY(email,customer_id));
        CREATE TABLE IF NOT EXISTS conversations(
            id INTEGER PRIMARY KEY, data TEXT NOT NULL, native INTEGER NOT NULL DEFAULT 0,
            owner TEXT NOT NULL DEFAULT '', number INTEGER GENERATED ALWAYS AS (json_extract(data,'$.number')) VIRTUAL,
            mailbox_id INTEGER GENERATED ALWAYS AS (json_extract(data,'$.mailboxId')) VIRTUAL,
            customer_id INTEGER GENERATED ALWAYS AS (json_extract(data,'$.primaryCustomer.id')) VIRTUAL,
            status TEXT GENERATED ALWAYS AS (json_extract(data,'$.status')) VIRTUAL,
            modified TEXT GENERATED ALWAYS AS (json_extract(data,'$.modifiedAt')) VIRTUAL,
            created TEXT GENERATED ALWAYS AS (json_extract(data,'$.createdAt')) VIRTUAL);
        CREATE TABLE IF NOT EXISTS snoozes(conversation INTEGER PRIMARY KEY REFERENCES conversations ON DELETE CASCADE,
            due TEXT NOT NULL,regardless INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS snoozes_due ON snoozes(due);
        CREATE TABLE IF NOT EXISTS conversation_aliases(
            source_id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES conversations(id)
            ON DELETE CASCADE, CHECK (source_id != target_id));
        CREATE INDEX IF NOT EXISTS conversation_number ON conversations(number);
        CREATE INDEX IF NOT EXISTS conversation_customer ON conversations(customer_id);
        CREATE INDEX IF NOT EXISTS conversation_modified ON conversations(modified DESC,id DESC);
        CREATE INDEX IF NOT EXISTS conversation_collection_created ON conversations(created DESC,id DESC)
            WHERE coalesce(json_extract(data,'$.state'),'published')='published';
        CREATE INDEX IF NOT EXISTS conversation_queue ON conversations(status,modified DESC,id DESC);
        CREATE TABLE IF NOT EXISTS threads(
            id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations ON DELETE CASCADE,
            data TEXT NOT NULL, created TEXT GENERATED ALWAYS AS (json_extract(data,'$.createdAt')) VIRTUAL);
        CREATE INDEX IF NOT EXISTS thread_conversation ON threads(conversation_id,created DESC,id DESC);
        CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL
            REFERENCES threads ON DELETE CASCADE, mime TEXT NOT NULL, content BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS attachment_thread ON attachments(thread_id);
        CREATE TABLE IF NOT EXISTS webhooks(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, webhook_id INTEGER NOT NULL
            REFERENCES webhooks, event TEXT NOT NULL, conversation_id INTEGER NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued');
        CREATE TABLE IF NOT EXISTS mail_outbox(thread_id INTEGER PRIMARY KEY REFERENCES threads,
            conversation_id INTEGER NOT NULL REFERENCES conversations, state TEXT NOT NULL DEFAULT 'queued');
        CREATE TABLE IF NOT EXISTS reply_context(tid INTEGER PRIMARY KEY REFERENCES threads ON DELETE CASCADE,
            customer_thread INTEGER, prior_status TEXT, prior_assignee INTEGER, assign_replying_user INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS operator_merges(
            source INTEGER PRIMARY KEY, target INTEGER NOT NULL, number INTEGER NOT NULL,
            header TEXT NOT NULL, owner TEXT NOT NULL, merged TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS operator_merged_number ON operator_merges(number);
        CREATE TABLE IF NOT EXISTS operator_thread_origin(
            thread INTEGER PRIMARY KEY, conversation INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS operator_customer_edits(
            customer INTEGER PRIMARY KEY, fields TEXT NOT NULL, emails TEXT NOT NULL, changed TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS access_tokens(digest TEXT PRIMARY KEY, client TEXT NOT NULL, expires INTEGER NOT NULL);
    """)

    db.execute("INSERT OR IGNORE INTO writer_authority VALUES(1,?,?)",
               (os.environ.get("INBOX_INITIAL_AUTHORITY","live"),now()))
    native_workflows.initialize(db)

    # External-content indexes reuse the canonical JSON bodies. Triggers also cover archive
    # re-imports, UI actions, successful sends and cascading deletes within the same transaction.
    with transaction(db):
        index_search(
            db, "thread", "threads", "coalesce(json_extract(data,'$.body'),'')"
        )
        index_search(
            db,
            "conversation",
            "conversations",
            "coalesce(json_extract(data,'$.subject'),'') || ' ' || "
            "coalesce(json_extract(data,'$.primaryCustomer'),'')",
        )
        build_indexes(db)


def index_search(db, label, table, expression):
    name = label + "_search"
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone():
        return
    view = name + "_content"
    db.execute(f"CREATE VIEW {view} AS SELECT id, ({expression}) AS words FROM {table}")
    db.execute(
        f"CREATE VIRTUAL TABLE {name} USING fts5(words,content='{view}',content_rowid='id')"
    )
    old = expression.replace("json_extract(data,", "json_extract(old.data,")
    new = expression.replace("json_extract(data,", "json_extract(new.data,")
    remove = f"INSERT INTO {name}({name},rowid,words) VALUES('delete',old.id,{old});"
    add = f"INSERT INTO {name}(rowid,words) VALUES(new.id,{new});"
    db.execute(f"CREATE TRIGGER {name}_insert AFTER INSERT ON {table} BEGIN {add} END")
    db.execute(
        f"CREATE TRIGGER {name}_delete AFTER DELETE ON {table} BEGIN {remove} END"
    )
    db.execute(
        f"CREATE TRIGGER {name}_update AFTER UPDATE OF data ON {table} "
        f"WHEN ({old}) IS NOT ({new}) BEGIN {remove} {add} END"
    )
    db.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")


LARGE_MAILBOX = 20_000  # Conversations above which index builds wait for an explicit command.


def build_indexes(db, force=False):
    """Create the tag table, the sender index and the message-owner table in this transaction.

    Returns False when a large mailbox must opt in: each of them reads the whole mailbox, and
    inside one write transaction that would hold every other writer at start-up. There the
    operator runs `python store.py indexes`, which works in short steps. Until then filters
    and searches use the slower scans.
    """
    ready = owners_ready(db) and db.execute(
        "SELECT count(*) FROM sqlite_master WHERE name IN ('conversation_tags','conversation_sender')"
    ).fetchone()[0] == 2
    if ready:
        return True
    if not force and db.execute(
        "SELECT 1 FROM conversations LIMIT 1 OFFSET ?", (LARGE_MAILBOX,)
    ).fetchone():
        return False
    index_tags(db)
    index_senders(db)
    index_owners(db)
    cursor = 0
    while cursor is not None:
        cursor = fill_owners(db, cursor)
    return True


def index_senders(db):
    # Exact sender lookups read this index together with the customer address list.
    db.execute(
        "CREATE INDEX IF NOT EXISTS conversation_sender ON conversations("
        "json_extract(data,'$.primaryCustomer.email') COLLATE NOCASE)"
    )


def index_owners(db):
    """Create the message-owner table and the triggers that keep it current. No rows yet.

    Word search maps each matching message to its conversation. Asking the messages table
    costs one read among all message bodies per match; this narrow table answers from a few
    megabytes. It is filled by fill_owners and used only once owners_ready says so.
    """
    db.execute("CREATE TABLE IF NOT EXISTS index_state(name TEXT PRIMARY KEY)")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_owner'"
    ).fetchone():
        return
    db.execute(
        "CREATE TABLE message_owner(thread INTEGER PRIMARY KEY,conversation INTEGER NOT NULL) WITHOUT ROWID"
    )
    keep = "INSERT OR REPLACE INTO message_owner VALUES(new.id,new.conversation_id)"
    db.execute(f"CREATE TRIGGER message_owner_insert AFTER INSERT ON threads BEGIN {keep}; END")
    db.execute(
        f"CREATE TRIGGER message_owner_update AFTER UPDATE OF conversation_id ON threads BEGIN {keep}; END"
    )
    db.execute(
        "CREATE TRIGGER message_owner_delete AFTER DELETE ON threads "
        "BEGIN DELETE FROM message_owner WHERE thread=old.id; END"
    )


def fill_owners(db, after=0, batch=1500):
    """Copy the owners of one batch of conversations; returns the next cursor, or None when done.

    Reads only the existing conversation index of the messages table, so a batch is quick and
    the caller can commit between batches on a live mailbox.
    """
    ids = [row[0] for row in db.execute(
        "SELECT id FROM conversations WHERE id>? ORDER BY id LIMIT ?", (after, batch)
    )]
    if not ids:
        db.execute("INSERT OR IGNORE INTO index_state VALUES('message_owner')")
        return None
    db.execute(
        "INSERT OR REPLACE INTO message_owner SELECT id,conversation_id FROM threads "
        "INDEXED BY thread_conversation WHERE conversation_id>? AND conversation_id<=?",
        (after, ids[-1]),
    )
    return ids[-1]


def owners_ready(db):
    return bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_state'").fetchone()
        and db.execute("SELECT 1 FROM index_state WHERE name='message_owner'").fetchone()
    )


def word_matches(db):
    """Conversations whose messages match a word search (one bound parameter)."""
    if owners_ready(db):
        return (
            "SELECT o.conversation FROM thread_search f JOIN message_owner o ON o.thread=f.rowid "
            "WHERE thread_search MATCH ?"
        )
    return (
        "SELECT t.conversation_id FROM thread_search f JOIN threads t ON t.id=f.rowid "
        "WHERE thread_search MATCH ?"
    )


def index_tags(db):
    """Keep one row per conversation tag so tag filters avoid reading every conversation."""
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_tags'"
    ).fetchone():
        return
    # Source tags are objects with a text name. Other shapes never matched a tag filter, and
    # the CASE keeps JSON functions away from array members that are not objects.
    rows = (
        "SELECT json_extract(CASE WHEN t.type='object' THEN t.value ELSE '{{}}' END,'$.tag'),{id} "
        "FROM json_each({data},'$.tags') t "
        "WHERE json_type(CASE WHEN t.type='object' THEN t.value ELSE '{{}}' END,'$.tag')='text'"
    )
    db.execute(
        "CREATE TABLE conversation_tags(tag TEXT NOT NULL COLLATE NOCASE,"
        "conversation INTEGER NOT NULL,PRIMARY KEY(tag,conversation)) WITHOUT ROWID"
    )
    db.execute("CREATE INDEX conversation_tags_owner ON conversation_tags(conversation)")
    add = "INSERT OR IGNORE INTO conversation_tags " + rows.format(
        id="new.id", data="new.data"
    )
    remove = "DELETE FROM conversation_tags WHERE conversation=old.id"
    db.execute(
        f"CREATE TRIGGER conversation_tags_insert AFTER INSERT ON conversations BEGIN {add}; END"
    )
    db.execute(
        f"CREATE TRIGGER conversation_tags_delete AFTER DELETE ON conversations BEGIN {remove}; END"
    )
    db.execute(
        "CREATE TRIGGER conversation_tags_update AFTER UPDATE OF data ON conversations "
        "WHEN json_extract(old.data,'$.tags') IS NOT json_extract(new.data,'$.tags') "
        f"BEGIN {remove}; {add}; END"
    )
    db.execute(
        "INSERT OR IGNORE INTO conversation_tags "
        + rows.format(id="c.id", data="c.data").replace(
            "FROM json_each", "FROM conversations c,json_each"
        )
    )


TAGGED = "c.id IN (SELECT conversation FROM conversation_tags WHERE tag=?)"
SCANNED_TAG = (
    "EXISTS(SELECT 1 FROM json_each(c.data,'$.tags') t "
    "WHERE json_extract(t.value,'$.tag')=? COLLATE NOCASE)"
)


def tag_filter(db):
    """The tag condition for conversations aliased `c`: indexed once the tag table exists."""
    indexed = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_tags'"
    ).fetchone()
    return TAGGED if indexed else SCANNED_TAG


def allocate(db, kind):
    # Source and native records can coexist during iterative migration. Stay within JS safe integers.
    first = 1_000_000_000_000
    db.execute(
        "INSERT INTO serial(name,last_id) VALUES(?,?) ON CONFLICT(name) "
        "DO UPDATE SET last_id=last_id+1",
        (kind, first),
    )
    return db.execute("SELECT last_id FROM serial WHERE name=?", (kind,)).fetchone()[0]


def identifier(value):
    if (
        isinstance(value, bool)
        or not str(value).isdigit()
        or not 0 < int(value) < 2**63
    ):
        raise Invalid("Expected a positive numeric ID")
    return int(value)


def text(value, field, limit):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise Invalid(f"Invalid {field}")
    return value


def email(value):
    if (
        not isinstance(value, str)
        or len(value) > 254
        or not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", value)
    ):
        raise Invalid("Expected one email address")
    return value


def tag_list(items):
    if not isinstance(items, list) or len(items) > 256:
        raise Invalid("Expected an array of tags")
    values = [text(item, "tag", 160) for item in items]
    return [{"tag": v} for v in dict.fromkeys(values)]


def status(value):
    if value not in ("active", "pending", "closed", "spam"):
        raise Invalid("Invalid conversation status")
    return value


def load(db, sql, params):
    row = db.execute(sql, params).fetchone()
    if row is None:
        raise Missing("Record not found")
    return json.loads(row[0])


def save_mailbox(db, profile):
    key = identifier(profile.get("id"))
    db.execute(
        "INSERT INTO mailboxes VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (key, encode(profile)),
    )


def save_customer(db, profile):
    key = identifier(profile.get("id"))
    edits=db.execute("SELECT fields,emails FROM operator_customer_edits WHERE customer=?",(key,)).fetchone()
    if edits:
        profile={**profile, **json.loads(edits[0])}
        embedded=dict(profile.get("_embedded") or {})
        emails=list(embedded.get("emails") or profile.get("emails") or [])
        known={str(row.get("value") if isinstance(row,dict) else row).casefold() for row in emails}
        for address in json.loads(edits[1]):
            if address.casefold() not in known:
                emails.append({"type":"other","value":address})
        embedded["emails"]=emails
        profile["_embedded"]=embedded
    db.execute(
        "INSERT INTO customers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (key, encode(profile)),
    )
    db.execute("DELETE FROM customer_emails WHERE customer_id=?", (key,))
    emails = (
        (profile.get("_embedded") or {}).get("emails") or profile.get("emails") or []
    )
    addresses = [a.get("value") if isinstance(a, dict) else a for a in emails]
    for addr in [profile.get("email"), *addresses]:
        if isinstance(addr, str) and addr:
            db.execute("INSERT OR IGNORE INTO customer_emails VALUES(?,?)", (addr, key))


def resolve_customer(db, value):
    if not isinstance(value, dict):
        raise Invalid("Missing customer")
    key = value.get("id")
    supplied = value.get("email")
    if key is None:
        supplied = email(supplied)
        found = db.execute(
            "SELECT customer_id FROM customer_emails WHERE email=? ORDER BY customer_id LIMIT 2",
            (supplied,),
        ).fetchall()
        if len(found) > 1:
            raise Conflict("Ambiguous customer email")
        if found:
            key = found[0][0]
        else:
            key = allocate(db, "customer")
            save_customer(
                db,
                {
                    "id": key,
                    "firstName": value.get("firstName") or value.get("first", ""),
                    "lastName": value.get("lastName") or value.get("last", ""),
                    "_embedded": {"emails": [{"type": "work", "value": supplied}]},
                },
            )
    key = identifier(key)
    profile = load(db, "SELECT data FROM customers WHERE id=?", (key,))
    if supplied:
        if not db.execute(
            "SELECT 1 FROM customer_emails WHERE customer_id=? AND email=?",
            (key, supplied),
        ).fetchone():
            raise Conflict("Email does not belong to customer")
    else:
        sources = (
            (profile.get("_embedded") or {}).get("emails")
            or profile.get("emails")
            or []
        )
        supplied = profile.get("email") or next(
            (e["value"] for e in sources if isinstance(e, dict) and e.get("value")), ""
        )
    return {
        "id": key,
        "type": "customer",
        "email": supplied,
        "first": profile.get("firstName") or profile.get("first", ""),
        "last": profile.get("lastName") or profile.get("last", ""),
    }


def conversation(db, cid, embed=False):
    item = load(db, "SELECT data FROM conversations WHERE id=?", (cid,))
    reminder=db.execute("SELECT due,regardless FROM snoozes WHERE conversation=?",(cid,)).fetchone()
    if reminder:
        item["snooze"]={"until":reminder[0],"regardless":bool(reminder[1])}
    customer=item.get("primaryCustomer") or {}
    edited=db.execute("SELECT fields FROM operator_customer_edits WHERE customer=?",(customer.get("id"),)).fetchone()
    if edited:
        fields=json.loads(edited[0])
        for name,short in (("firstName","first"),("lastName","last")):
            if name in fields:
                customer[short]=fields[name]
    base = os.environ.get("INBOX_BASE_URL", "").rstrip("/")
    item.setdefault("_links", {}).update(
        {
            "self": {"href": f"{base}/v2/conversations/{cid}"},
            "web": {"href": f"{base}/conversation/{cid}"},
            "threads": {"href": f"{base}/v2/conversations/{cid}/threads"},
        }
    )
    if embed:
        item.setdefault("_embedded", {})["threads"] = thread_list(db, cid)
    return item


def save_alias(db, source_id, target_id):
    """Record a source merge, flattening known aliases to one canonical target."""
    source_id, target_id = identifier(source_id), identifier(target_id)
    row = db.execute(
        "SELECT target_id FROM conversation_aliases WHERE source_id=?", (target_id,)
    ).fetchone()
    if row is not None:
        target_id = row[0]
    if (
        source_id == target_id
        or db.execute("SELECT 1 FROM conversations WHERE id=?", (source_id,)).fetchone()
    ):
        raise Conflict("An existing conversation cannot be replaced by a merge alias")
    if not db.execute(
        "SELECT 1 FROM conversations WHERE id=?", (target_id,)
    ).fetchone():
        raise Missing("Canonical conversation not found")
    db.execute(
        "INSERT INTO conversation_aliases VALUES(?,?) ON CONFLICT(source_id) "
        "DO UPDATE SET target_id=excluded.target_id",
        (source_id, target_id),
    )


def thread_list(db, cid, limit=None, offset=0):
    sql = (
        "SELECT data FROM threads WHERE conversation_id=? ORDER BY created DESC,id DESC"
    )
    args = [cid]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args.extend([limit, offset])
    return [json.loads(row[0]) for row in db.execute(sql, args)]


def thread(db, cid, tid):
    return load(
        db, "SELECT data FROM threads WHERE id=? AND conversation_id=?", (tid, cid)
    )


def attachment(db, cid, aid):
    row = db.execute(
        "SELECT a.content,a.mime FROM attachments a JOIN threads t ON t.id=a.thread_id "
        "WHERE a.id=? AND t.conversation_id=?",
        (aid, cid),
    ).fetchone()
    if row is None:
        from asset_library import migrated_attachment

        # A migrated blob is indexed by the conversation that owned its thread in the source archive.
        original = db.execute("""SELECT o.conversation FROM operator_thread_origin o JOIN threads t ON t.id=o.thread
                    JOIN json_each(coalesce(json_extract(t.data,'$._embedded.attachments'),json_extract(t.data,'$.attachments'))) a
                    WHERE t.conversation_id=? AND json_extract(a.value,'$.id')=? LIMIT 1""",(cid,aid)).fetchone()
        try:
            content, mime = migrated_attachment(cid, aid)
        except Missing:
            if not original:
                raise
            content, mime = migrated_attachment(original[0], aid)
        if mime == "application/octet-stream":
            meta = db.execute("""
                SELECT json_extract(a.value,'$.mimeType') FROM threads t,
                       json_each(coalesce(json_extract(t.data,'$._embedded.attachments'),
                                          json_extract(t.data,'$.attachments'))) a
                 WHERE t.conversation_id=? AND json_extract(a.value,'$.id')=? LIMIT 1
            """, (cid, aid)).fetchone()
            if meta and meta[0]:
                mime = meta[0]
        return content, mime
    return row[0], row[1]


def update_conversation(db, cid, changes, when=None):
    item = load(db, "SELECT data FROM conversations WHERE id=?", (cid,))
    item.update(changes)
    item["modifiedAt"] = when or now()
    item["userUpdatedAt"] = item["modifiedAt"]
    item["updatedAt"] = item["modifiedAt"]
    if changes.get("status") == "closed":
        item["closedAt"] = item["modifiedAt"]
    elif changes.get("status") in ("active", "pending"):
        item["closedAt"] = None
    db.execute(
        "UPDATE conversations SET data=?,native=1 WHERE id=?", (encode(item), cid)
    )


def emit(db, cid, event):
    native_workflows.observe(db, cid, event)
    eligible = []
    item = conversation(db, cid)
    for row in db.execute("SELECT id,data FROM webhooks"):
        hook = json.loads(row["data"])
        mailboxes = hook.get("mailboxIds", [])
        if (
            hook.get("state") == "enabled"
            and event in hook["events"]
            and (not mailboxes or item.get("mailboxId") in mailboxes)
        ):
            eligible.append((row["id"], hook.get("notification", False)))
    if not eligible:
        return
    payload = encode(conversation(db, cid, True))
    for hook_id, notify in eligible:
        data = encode({"id": cid, "_links": item["_links"]}) if notify else payload
        db.execute(
            "INSERT INTO deliveries(webhook_id,event,conversation_id,payload) VALUES(?,?,?,?)",
            (hook_id, event, cid, data),
        )


def import_conversation(db, conversation, threads, attachments=None):
    """Insert a complete source snapshot. Import never emits webhook or mail jobs.

    A supplied attachment map contains verified content for IDs in this snapshot. When absent,
    existing attachment bytes survive a metadata/delta refresh. Native changes are never overwritten.
    """
    cid = identifier(conversation.get("id"))
    existing = db.execute(
        "SELECT native FROM conversations WHERE id=?", (cid,)
    ).fetchone()
    if existing and existing[0]:
        raise Conflict("Native changes prevent source import")
    item = dict(conversation)
    embedded = dict(item.get("_embedded") or {})
    embedded.pop("threads", None)
    item["_embedded"] = embedded
    item.pop("requested_conversation_id", None)
    db.execute(
        "INSERT INTO conversations(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (cid, encode(item)),
    )
    customer = item.get("primaryCustomer") or {}
    if (
        customer.get("id")
        and not db.execute(
            "SELECT 1 FROM customers WHERE id=?", (customer["id"],)
        ).fetchone()
    ):
        save_customer(db, customer)
    retained = set()
    for t in threads:
        tid = identifier(t.get("id"))
        if tid in retained:
            raise Invalid("Duplicate thread in source snapshot")
        retained.add(tid)
        existing_tid = db.execute(
            "SELECT conversation_id FROM threads WHERE id=?", (tid,)
        ).fetchone()
        if existing_tid and existing_tid[0] != cid:
            raise Conflict("Source thread belongs to another conversation")
        db.execute(
            "INSERT INTO threads(id,conversation_id,data) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (tid, cid, encode(t)),
        )
        present = set()
        for a in (
            (t.get("_embedded") or {}).get("attachments") or t.get("attachments") or []
        ):
            aid = identifier(a.get("id"))
            present.add(aid)
            if attachments is not None and aid in attachments:
                db.execute(
                    "INSERT INTO attachments VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "mime=excluded.mime,content=excluded.content WHERE thread_id=excluded.thread_id",
                    (
                        aid,
                        tid,
                        a.get("mimeType") or "application/octet-stream",
                        attachments[aid],
                    ),
                )
        stale_aids = [
            r[0]
            for r in db.execute("SELECT id FROM attachments WHERE thread_id=?", (tid,))
            if r[0] not in present
        ]
        db.executemany("DELETE FROM attachments WHERE id=?", ((i,) for i in stale_aids))
    stale_tids = [
        r[0]
        for r in db.execute("SELECT id FROM threads WHERE conversation_id=?", (cid,))
        if r[0] not in retained
    ]
    db.executemany("DELETE FROM threads WHERE id=?", ((i,) for i in stale_tids))
    return cid


def create_conversation(db, payload, source="api", owner=""):
    mailbox = identifier(payload.get("mailboxId"))
    load(db, "SELECT data FROM mailboxes WHERE id=?", (mailbox,))
    subject = text(payload.get("subject"), "subject", 2000)
    customer = resolve_customer(db, payload.get("customer"))
    threads = payload.get("threads")
    if not isinstance(threads, list) or not threads or len(threads) > 100:
        raise Invalid("Expected conversation threads")
    cid = allocate(db, "conversation")
    number = db.execute(
        "SELECT max(coalesce((SELECT max(number) FROM conversations),0),coalesce((SELECT max(number) FROM operator_merges),0))+1"
    ).fetchone()[0]
    when = payload.get("createdAt") or now()
    item = {
        "id": cid,
        "number": number,
        "subject": subject,
        "status": status(payload.get("status", "active")),
        "type": "email",
        "state": "draft" if all(isinstance(t, dict) and t.get("draft") is True for t in threads) else "published",
        "createdAt": when,
        "modifiedAt": when,
        "mailboxId": mailbox,
        "primaryCustomer": customer,
        "createdBy": customer,
        "threads": 0,
        "preview": "",
        "tags": tag_list(payload.get("tags", [])),
    }
    db.execute(
        "INSERT INTO conversations(id,data,native,owner) VALUES(?,?,1,?)",
        (cid, encode(item), owner),
    )
    for t in threads:
        if not isinstance(t, dict):
            raise Invalid("Expected a thread object")
        append_thread(
            db,
            cid,
            t.get("type"),
            t,
            source=source,
            initial=True,
            imported=payload.get("imported") is True,
        )
    first = thread_list(db, cid)[-1]
    update_conversation(
        db,
        cid,
        {"createdBy": first["createdBy"]},
        when=thread_list(db, cid)[0]["createdAt"],
    )
    if not payload.get("imported") and first["type"] != "message":
        emit(db, cid, "convo.created")
    return cid


def ledger_note(db, cid, tid, strict=False, edited=False):
    """Record what an internal note says in the agent ledger. A fault there never loses the note,
    unless the caller declared an event itself and got it wrong."""
    import agent_ledger

    if not agent_ledger.ready(db):
        return
    db.execute("SAVEPOINT ledger_note")
    try:
        (agent_ledger.refresh if edited else agent_ledger.absorb)(db, cid, tid)
    except Exception as fault:
        db.execute("ROLLBACK TO ledger_note")
        db.execute("RELEASE ledger_note")
        if strict and isinstance(fault, (Invalid, Conflict)):
            raise
        import logging
        logging.getLogger("inbox.ledger").exception("Note %s was not recorded in the agent ledger", tid)
        return
    db.execute("RELEASE ledger_note")


def append_thread(db, cid, kind, payload, source="api", initial=False, imported=False):
    if kind not in ("customer", "note", "reply", "message"):
        raise Invalid("Unsupported thread type")
    conv = conversation(db, cid)
    body = text(payload.get("text"), "thread text", 10_000_000)
    draft = payload.get("draft", False)
    if type(draft) is not bool or (draft and kind not in ("reply", "message")):
        raise Invalid("Invalid draft state")
    customer = (
        resolve_customer(db, payload["customer"])
        if "customer" in payload
        else conv["primaryCustomer"]
    )
    tid = allocate(db, "thread")
    when = payload.get("createdAt") or now()
    agent = {
        "type": "user",
        "id": identifier(payload.get("user") or os.environ.get("INBOX_AGENT_ID", "1")),
        "first": os.environ.get("INBOX_AGENT_NAME", "Support"),
        "last": "",
    }
    reply = kind in ("message", "reply")
    send = reply and not draft and not imported
    data = {
        "id": tid,
        "type": "message" if reply else kind,
        "status": payload.get("status", conv["status"]),
        "state": "draft" if draft else "pending" if send else "published",
        "body": body,
        "createdAt": when,
        "source": {"type": source, "via": "customer" if kind == "customer" else "user"},
        "createdBy": customer if kind == "customer" else agent,
        "customer": customer,
    }
    for field in ("cc", "bcc", "to"):
        if field in payload:
            if not isinstance(payload[field], list) or len(payload[field]) > 128:
                raise Invalid("Expected recipient array")
            data[field] = [email(addr) for addr in payload[field]]
    if send and not customer.get("email"):
        raise Invalid("Customer has no delivery address")
    if send:
        data.setdefault("to", [email(customer["email"])])
        # An uncertain request retried by a caller must not schedule a second delivery.
        for r in db.execute(
            "SELECT t.data FROM mail_outbox o JOIN threads t ON t.id=o.thread_id "
            "WHERE o.conversation_id=? AND o.state NOT IN ('sent','failed','cancelled')",
            (cid,),
        ):
            pending = json.loads(r[0])
            if all(
                pending.get(k) == data.get(k)
                for k in ("body", "to", "cc", "bcc", "customer")
            ):
                raise Conflict("Matching reply already awaits delivery")
    files = payload.get("attachments", [])
    if not isinstance(files, list) or len(files) > 128:
        raise Invalid("Invalid attachments")
    metas, blobs = [], []
    for f in files:
        if not isinstance(f, dict):
            raise Invalid("Invalid attachment")
        filename = text(f.get("fileName"), "filename", 255)
        mime = text(f.get("mimeType"), "mimeType", 255)
        try:
            blob = base64.b64decode(f["data"], validate=True)
        except (KeyError, binascii.Error, TypeError, ValueError) as error:
            raise Invalid("Invalid attachment data") from error
        aid = allocate(db, "attachment")
        meta = {"id": aid, "filename": filename, "mimeType": mime, "size": len(blob)}
        if f.get("contentId"):
            meta["contentId"] = f["contentId"]
        metas.append(meta)
        blobs.append((aid, tid, mime, blob))
    data["_embedded"] = {"attachments": metas}
    if kind == "note" and isinstance(payload.get("event"), dict):
        # An agent states what its note is, so nobody has to recognise it from its wording.
        data["event"] = payload["event"]
    db.execute(
        "INSERT INTO threads(id,conversation_id,data) VALUES(?,?,?)",
        (tid, cid, encode(data)),
    )
    db.executemany("INSERT INTO attachments VALUES(?,?,?,?)", blobs)
    if kind == "note":
        # A full declaration must be valid; a note that only names its writer is never refused.
        ledger_note(db, cid, tid, strict=isinstance(data.get("event"), dict) and data["event"].get("kind") is not None)
    changes = {"threads": conv.get("threads", 0) + 1}
    if not send and not draft:
        changes["state"] = "published"
        if payload.get("status"):
            changes["status"] = status(payload["status"])
        elif kind == "customer" and not initial:
            changes["status"] = "active"
        if kind == "customer":
            if not initial and conv.get("snooze") and not conv["snooze"].get("regardless"):
                import snooze
                snooze.clear(db,cid,"Customer replied")
            changes["customerWaitingSince"] = conv.get("customerWaitingSince") or {
                "time": when
            }
            changes["preview"] = re.sub("<[^>]+>", " ", body).strip()[:250]
    update_conversation(db, cid, changes, when=when)
    if send:
        queue_reply(db,cid,tid,source=="web",payload.get("status"))
    elif not initial and not draft and not imported:
        emit(
            db,
            cid,
            "convo.customer.reply.created"
            if kind == "customer"
            else "convo.note.created",
        )
    return tid


def newest_customer_id(db,cid):
    return db.execute("SELECT max(id) FROM threads WHERE conversation_id=? AND json_extract(data,'$.type')='customer'",(cid,)).fetchone()[0]


def queue_reply(db,cid,tid,operator=False,target_status=None):
    conv=conversation(db,cid)
    config={}
    if operator:
        import mailbox_settings
        config=mailbox_settings.get(db,"general")
        target_status=target_status or native_workflows.STATUS.get(config.get("convoStatus"))
    if target_status:
        db.execute("UPDATE threads SET data=json_set(data,'$.status',?) WHERE id=?",(status(target_status),tid))
    db.execute("INSERT INTO mail_outbox(thread_id,conversation_id) VALUES(?,?)",(tid,cid))
    db.execute("INSERT INTO reply_context VALUES(?,?,?,?,?)",(tid,newest_customer_id(db,cid),conv["status"],
        (conv.get("assignee") or {}).get("id"),int(config.get("convoAssignee")==3)))


def publish(db, cid, tid):
    data = thread(db, cid, tid)
    row = db.execute(
        "SELECT state FROM mail_outbox WHERE thread_id=? AND conversation_id=?",
        (tid, cid),
    ).fetchone()
    if row is None:
        raise Missing("Missing outgoing delivery")
    if row[0] == "sent" and data["state"] == "published":
        return
    if data["state"] != "pending" or data["type"] not in ("message","forwardchild"):
        raise Conflict("Thread cannot be published")
    data["state"] = "published"
    db.execute("UPDATE threads SET data=? WHERE id=?", (encode(data), tid))
    db.execute("UPDATE mail_outbox SET state='sent' WHERE thread_id=?", (tid,))
    if data["type"] == "forwardchild":
        db.execute("UPDATE threads SET data=json_set(data,'$.state','published') WHERE id=? AND json_extract(data,'$.linkedThreadId')=?",
                   (data["linkedThreadId"], tid))
        return
    context = db.execute("SELECT * FROM reply_context WHERE tid=?", (tid,)).fetchone()
    current = conversation(db,cid)
    if context:
        newer_customer = newest_customer_id(db,cid) != context["customer_thread"]
    else:
        # Compatibility with queued deliveries created before reply context was recorded.
        newer_customer=db.execute("SELECT 1 FROM threads WHERE conversation_id=? AND json_extract(data,'$.type')='customer' AND created>? LIMIT 1",
                                  (cid,data["createdAt"])).fetchone()
    changes={"state":"published"}
    if not newer_customer:
        changes["customerWaitingSince"]=None
        if not context or current["status"]==context["prior_status"]:
            changes["status"]=status(data["status"])
        previous_assignee=(current.get("assignee") or {}).get("id")
        if context and context["assign_replying_user"] and previous_assignee==context["prior_assignee"]:
            changes["assignee"]=data["createdBy"]
    update_conversation(db,cid,changes)
    if "assignee" in changes and changes["assignee"].get("id")!=(current.get("assignee") or {}).get("id"):
        emit(db,cid,"convo.assigned")
    emit(db, cid, "convo.agent.reply.created")


if __name__ == "__main__":
    import argparse
    import time
    from contextlib import closing

    cli = argparse.ArgumentParser(description="Mailbox storage maintenance")
    cli.add_argument(
        "command", choices=["indexes"],
        help="Build the tag table, sender index and message-owner table in short steps",
    )
    cli.parse_args()
    import extensions
    import sys

    sys.modules.setdefault("store", sys.modules[__name__])
    extensions.load()
    with closing(connect()) as mailbox:
        started = time.monotonic()
        # Each step is its own short transaction, so mail keeps arriving while this runs.
        for step in (index_tags, index_senders, index_owners):
            with transaction(mailbox):
                step(mailbox)
        position, batches = 0, 0
        while position is not None and not owners_ready(mailbox):
            with transaction(mailbox):
                position = fill_owners(mailbox, position)
            batches += 1
            time.sleep(0.05)
        tags = mailbox.execute("SELECT count(*) FROM conversation_tags").fetchone()[0]
        owners = mailbox.execute("SELECT count(*) FROM message_owner").fetchone()[0]
        print(
            f"Indexed {tags} conversation tags, the sender index and {owners} message owners "
            f"in {batches} batches, {time.monotonic() - started:.1f} s"
        )
