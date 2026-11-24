"""Execute the mailbox's imported rules and keep their historical receipts."""

import argparse
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import cached_property
from html import escape
from html.parser import HTMLParser
import standard_folders
import json
import os
import re
import time

import store

STATUS = {1: "active", 2: "pending", 3: "closed", 4: "spam"}
FACETS = {
    (40, 1),
    (111, 8),
    (110, 8),
    (60, 3),
    (150, 18),
    (20, 14),
    (140, 11),
    (80, 1),
    (10, 3),
    (120, 23),
}
ACTIONS = {50, 70, 10, 60, 80, 90, 40}
_running = ContextVar("workflow_in_progress", default=False)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS native_rules(
            id INTEGER PRIMARY KEY, mailbox INTEGER NOT NULL, priority INTEGER NOT NULL,
            kind TEXT NOT NULL, enabled INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS native_rules_scan ON native_rules(mailbox,kind,enabled,priority);
        CREATE TABLE IF NOT EXISTS native_folders(
            id INTEGER PRIMARY KEY, mailbox INTEGER NOT NULL, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS native_folder_members(
            folder INTEGER NOT NULL REFERENCES native_folders, conversation INTEGER NOT NULL,
            PRIMARY KEY(folder,conversation));
        CREATE INDEX IF NOT EXISTS native_member_conversation ON native_folder_members(conversation);
        CREATE TABLE IF NOT EXISTS native_manual_receipts(
            operation TEXT PRIMARY KEY, conversation INTEGER NOT NULL, rule INTEGER NOT NULL, result TEXT NOT NULL);
    """)


def install(db, export, folder_snapshot=None):
    if export.get("version") != 1:
        raise store.Invalid("Unrecognized workflow export")
    mailbox = store.identifier(export["mailboxId"])
    rules = [entry["definition"] for entry in export["rules"]]
    if len({r["id"] for r in rules}) != len(rules) or any(
        r["mailboxId"] != mailbox for r in rules
    ):
        raise store.Invalid("Workflow inventory does not match mailbox")
    for rule in rules:
        if rule["statusLabel"] != "Active":
            continue
        if rule["typeLabel"] not in ("Automatic", "Manual"):
            raise store.Invalid("Unknown workflow type")
        if rule["typeLabel"] == "Automatic" and not rule["conditions"]:
            raise store.Invalid("Automatic workflow has no conditions")
        for group in rule["conditions"]:
            if not group["facets"] or any(
                (int(f["type"]), f["operator"]) not in FACETS for f in group["facets"]
            ):
                raise store.Invalid("Unknown workflow condition")
        for a in rule["actions"]:
            if a["type"] not in ACTIONS:
                raise store.Invalid("Unknown workflow action")
            if a["type"] == 40 and rule["typeLabel"] != "Manual":
                raise store.Invalid("Automatic outbound workflow requires review")
            if a["type"] == 90 and a["value"] != mailbox:
                raise store.Invalid("Destination mailbox must be configured")
    db.execute("DELETE FROM native_rules WHERE mailbox=?", (mailbox,))
    db.executemany(
        "INSERT INTO native_rules VALUES(?,?,?,?,?,?)",
        (
            (
                r["id"],
                mailbox,
                r["order"],
                r["typeLabel"],
                r["statusLabel"] == "Active",
                store.encode(r),
            )
            for r in rules
        ),
    )
    for folder in folder_snapshot or []:
        ids = set(folder["members"])
        if (
            len(ids) != folder["total"]
            or len(ids) != len(folder["members"])
            or folder["mailboxId"] != mailbox
        ):
            raise store.Invalid("Incomplete folder membership export")
        save_folder(db, mailbox, folder)
        # A later catch-up export may overlap native usage. Only replace source-owned memberships.
        db.execute(
            "DELETE FROM native_folder_members WHERE folder=? AND conversation NOT IN "
            "(SELECT id FROM conversations WHERE native=1)",
            (folder["id"],),
        )
        db.executemany(
            "INSERT OR IGNORE INTO native_folder_members VALUES(?,?)",
            ((folder["id"], i) for i in ids),
        )


def save_folder(db, mailbox, data):
    db.execute(
        "INSERT INTO native_folders VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
        (data["id"], mailbox, data["name"]),
    )


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "br", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        if tag in ("p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def contains(words, term):
    # The source's Contains operator matches whole words and word prefixes, including phrases.
    words = " ".join(words.casefold().split())
    return bool(
        re.search(r"(?<!\w)" + re.escape(" ".join(term.casefold().split())), words)
    )


def as_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (ValueError, TypeError):
        return None


class Evidence:
    def __init__(self, db, cid, clock):
        self.db, self.cid, self.clock = db, cid, clock
        self.current = store.conversation(db, cid)
        self.threads = store.thread_list(db, cid)

    @cached_property
    def published(self):
        return [t for t in self.threads if t.get("state") == "published"]

    @cached_property
    def customer_messages(self):
        texts = []
        for t in self.published:
            if t.get("type") == "customer":
                parser = VisibleText()
                parser.feed(t.get("body") or "")
                parser.close()
                texts.append("".join(parser.parts))
        return texts

    @cached_property
    def seen(self):
        result = set()
        for t in self.threads:
            a = t.get("action") or {}
            if a.get("type") == "automatic-workflow":
                rid = (a.get("associatedEntities") or {}).get("workflow")
                if str(rid).isdigit():
                    result.add(int(rid))
        return result

    def matches(self, f):
        kind, value = int(f["type"]), f["value"]
        c = self.current
        customer = c.get("primaryCustomer") or {}
        if kind == 40:
            return c.get("status") == STATUS.get(value)
        if kind == 60:
            return contains(c.get("subject", ""), value)
        if kind == 150:
            return any(contains(body, value) for body in self.customer_messages)
        if kind == 20:
            return (customer.get("email") or "").casefold().startswith(value.casefold())
        if kind == 10:
            full = " ".join(customer.get(key) or "" for key in ("first", "last"))
            return contains(full, value)
        if kind == 80:
            return value.casefold() in {t["tag"].casefold() for t in c.get("tags", [])}
        if kind == 140:
            return any(
                t.get("type") in ("message", "reply")
                and (t.get("createdBy") or {}).get("id") == value
                for t in self.published
            )
        if kind == 120:
            first, last = value.split(":")
            stamp = as_time(c.get("createdAt"))
            return bool(stamp and first <= stamp.date().isoformat() <= last)
        # Last user/customer reply conditions require an actual published reply of the specified kind.
        kinds = {"customer"} if kind == 111 else {"message", "reply"}
        stamps = [
            as_time(t.get("createdAt"))
            for t in self.published
            if t.get("type") in kinds
        ]
        latest = max((s for s in stamps if s), default=None)
        n, unit = value.split(":")
        period = timedelta(**{{"d": "days", "h": "hours"}[unit]: int(n)})
        return latest is not None and latest <= self.clock - period


def record_line(db, evidence, rule, manual):
    rid = rule["id"]
    tid = store.allocate(db, "thread")
    item = {
        "id": tid,
        "type": "lineitem",
        "createdAt": store.now(),
        "state": "published",
        "createdBy": {"id": int(os.getenv("INBOX_AGENT_ID", "1")), "type": "user"},
        "source": {"via": "user", "type": "manual-workflow" if manual else "workflows"},
        "action": {
            "type": "manual-workflow" if manual else "automatic-workflow",
            "text": f"Workflow **{rule['name']}** was triggered",
            "associatedEntities": {"workflow": rid, "mailbox": rule["mailboxId"]},
        },
    }
    db.execute(
        "INSERT INTO threads(id,conversation_id,data) VALUES(?,?,?)",
        (tid, evidence.cid, store.encode(item)),
    )
    return tid


def apply(db, evidence, rule, manual):
    cid = evidence.cid
    current = evidence.current
    changes = {}
    emit = set()
    reply_tid = None
    for action in rule["actions"]:
        kind, v = action["type"], action["value"]
        if kind in (70, 80):
            tags = list(changes.get("tags", current.get("tags", [])))
            if kind == 70:
                known = {tag["tag"].casefold() for tag in tags}
                tags.extend({"tag": tag} for tag in v if tag.casefold() not in known)
            else:
                remove = {tag.casefold() for tag in v}
                tags = [tag for tag in tags if tag["tag"].casefold() not in remove]
            changes["tags"] = tags
            emit.add("convo.tags")
        elif kind == 50:
            changes["status"] = STATUS[v]
            emit.add("convo.status")
        elif kind == 60:
            agent = (
                int(os.getenv("INBOX_AGENT_ID", "1"))
                if v == -3
                else store.identifier(v)
            )
            changes["assignee"] = {"id": agent, "type": "user"}
            emit.add("convo.assigned")
        elif kind == 10:
            save_folder(db, rule["mailboxId"], v)
            db.execute(
                "INSERT OR IGNORE INTO native_folder_members VALUES(?,?)",
                (v["id"], cid),
            )
        elif kind == 90:
            changes["mailboxId"] = v
        elif kind == 40:
            if not manual or os.getenv("INBOX_SEND_ENABLED") != "1":
                raise store.Conflict("Mailbox delivery is disabled")
            body = v["body"]
            customer = current.get("primaryCustomer") or {}
            for variable, fallback in re.findall(
                r"{%customer\.(firstName),fallback=([^%]*)%}", body
            ):
                body = body.replace(
                    "{%customer." + variable + ",fallback=" + fallback + "%}",
                    escape(customer.get("first") or fallback),
                )
            if "{%" in body:
                raise store.Invalid("Unsupported template variable")
            reply_tid = store.append_thread(
                db,
                cid,
                "reply",
                {
                    "text": body,
                    "status": next(
                        (
                            STATUS[a["value"]]
                            for a in reversed(rule["actions"])
                            if a["type"] == 50
                        ),
                        current["status"],
                    ),
                    "user": v["from"],
                    "cc": v.get("cc", []),
                    "bcc": v.get("bcc", []),
                },
                source="manual-workflow",
            )
    changes = {k: v for k, v in changes.items() if current.get(k) != v}
    store.update_conversation(db, cid, changes)
    evidence.current.update(changes)
    line = record_line(db, evidence, rule, manual)
    event_fields = {
        "convo.tags": "tags",
        "convo.status": "status",
        "convo.assigned": "assignee",
    }
    for event in sorted(emit):
        if event_fields[event] in changes:
            store.emit(db, cid, event)
    return {"activityThreadId": line, "replyThreadId": reply_tid}


def observe(db, cid, event, clock=None):
    if _running.get():
        return
    mailbox = db.execute(
        "SELECT mailbox_id FROM conversations WHERE id=? AND coalesce(json_extract(data,'$.state'),'published')<>'draft'", (cid,)
    ).fetchone()
    if not mailbox:
        return
    rules = db.execute(
        "SELECT data FROM native_rules WHERE mailbox=? AND kind='Automatic' AND enabled=1 "
        "ORDER BY priority,id",
        (mailbox[0],),
    ).fetchall()
    if not rules:
        return
    e = Evidence(db, cid, clock or datetime.now(timezone.utc))
    guard = _running.set(True)
    try:
        for row in rules:
            r = json.loads(row[0])
            if r["id"] in e.seen:
                continue
            if event == "clock" and not any(
                int(f["type"]) in (110, 111)
                for g in r["conditions"]
                for f in g["facets"]
            ):
                continue
            if all(any(e.matches(f) for f in g["facets"]) for g in r["conditions"]):
                apply(db, e, r, False)
                e.seen.add(r["id"])
    finally:
        _running.reset(guard)


def tick(db, clock=None):
    import snooze
    snooze.due(db,clock or datetime.now(timezone.utc))
    # The enabled time rules both require Pending. An explicit field inventory protects this scan on import.
    for row in db.execute(
        "SELECT id FROM conversations WHERE status='pending'"
    ).fetchall():
        observe(db, row[0], "clock", clock)


def run_manual(db, cid, rule_id, operation):
    store.text(operation, "Idempotency-Key", 128)
    row = db.execute(
        "SELECT * FROM native_manual_receipts WHERE operation=?", (operation,)
    ).fetchone()
    if row:
        if row["conversation"] != cid or row["rule"] != rule_id:
            raise store.Conflict("Operation key already used")
        return json.loads(row["result"])
    e = Evidence(db, cid, datetime.now(timezone.utc))
    rule = store.load(
        db,
        "SELECT data FROM native_rules WHERE id=? AND mailbox=? AND kind='Manual' AND enabled=1",
        (rule_id, e.current["mailboxId"]),
    )
    guard = _running.set(True)
    try:
        result = apply(db, e, rule, True)
    finally:
        _running.reset(guard)
    db.execute(
        "INSERT INTO native_manual_receipts VALUES(?,?,?,?)",
        (operation, cid, rule_id, store.encode(result)),
    )
    observe(db, cid, "manual")
    return result


def folders(db, cid):
    return [
        row[0]
        for row in db.execute(
            "SELECT folder FROM native_folder_members WHERE conversation=? ORDER BY folder",
            (cid,),
        )
    ]


def routes(gateway, delivered=None):
    """`delivered` waits until the mail transport has accepted the given reply threads."""
    from fastapi import APIRouter, Depends, Header, Query

    router = APIRouter(dependencies=[Depends(gateway)])

    @router.get("/v2/workflows")
    def list_rules():
        with closing(store.connect()) as db:
            rows = db.execute(
                "SELECT data FROM native_rules ORDER BY mailbox,kind,priority,id"
            ).fetchall()
            rules = []
            for row in rows:
                r = json.loads(row[0])
                rules.append(
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "mailboxId": r["mailboxId"],
                        "order": r["order"],
                        "type": r["typeLabel"].lower(),
                        "status": r["statusLabel"].lower(),
                    }
                )
            return {
                "_embedded": {"workflows": rules},
                "page": {"number": 1, "totalPages": 1, "totalElements": len(rules)},
            }

    @router.post("/v2/conversations/{cid}/workflows/{rule_id}")
    async def run(
        cid: int,
        rule_id: int,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=1, max_length=128
        ),
    ):
        with closing(store.connect()) as db, store.transaction(db):
            store.require_writer(db)
            result = run_manual(db, cid, rule_id, idempotency_key)
        # A caller that hears "done" may record the reply as sent, so answer only once the
        # transport has accepted it. A repeated request waits on the same reply again.
        if delivered is not None and result.get("replyThreadId"):
            await delivered([result["replyThreadId"]])
        return result

    @router.get("/v2/mailboxes/{mid}/folders")
    def folder_list(mid: int):
        with closing(store.connect()) as db:
            rows = db.execute(
                """
                SELECT f.id,f.name,COUNT(c.id) totalCount,COUNT(CASE WHEN c.status='active' THEN 1 END) activeCount
                FROM native_folders f LEFT JOIN native_folder_members m ON f.id=m.folder
                LEFT JOIN conversations c ON c.id=m.conversation
                WHERE f.mailbox=? GROUP BY f.id ORDER BY f.name
            """,
                (mid,),
            )
            return {"_embedded": {"folders": standard_folders.listing(db, mid) + [dict(r, type="smart") for r in rows]}}

    @router.get("/v2/conversations/{cid}/folders")
    def conversation_folders(cid: int):
        with closing(store.connect()) as db:
            store.conversation(db, cid)
            return {"folders": folders(db, cid)}

    @router.get("/v2/folders/{fid}/conversations")
    def folder_conversations(
        fid: int, page: int = Query(1, ge=1), pageSize: int = Query(25, ge=1, le=100)
    ):
        with closing(store.connect()) as db, store.transaction(db, write=False):
            if not any(row["id"] == fid for row in standard_folders.metadata(db)) and not db.execute(
                "SELECT 1 FROM native_folders WHERE id=?", (fid,)
            ).fetchone():
                raise store.Missing("Unknown folder")
            expr, params = standard_folders.by_id(db, fid)
            query = " FROM conversations c WHERE " + expr
            rows = db.execute(
                "SELECT c.id" + query + " ORDER BY c.modified DESC,c.id DESC LIMIT ? OFFSET ?",
                [*params, pageSize, (page - 1) * pageSize],
            ).fetchall()
            count = db.execute("SELECT count(*)" + query, params).fetchone()[0]
            return {
                "_embedded": {
                    "conversations": [store.conversation(db, r[0]) for r in rows]
                },
                "page": {
                    "number": page,
                    "size": pageSize,
                    "totalPages": (count + pageSize - 1) // pageSize,
                    "totalElements": count,
                },
            }

    return router


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("install")
    load.add_argument("export")
    load.add_argument("--folders")
    sub.add_parser("tick")
    sub.add_parser("worker")
    args = parser.parse_args()
    with closing(store.connect()) as db:
        store.initialize(db)
        initialize(db)
        if args.command == "install":
            with open(args.export, encoding="utf-8") as f:
                export = json.load(f)
            snapshot = None
            if args.folders:
                with open(args.folders, encoding="utf-8") as f:
                    snapshot = json.load(f)
            with store.transaction(db):
                install(db, export, snapshot)
            print(f"Loaded {len(export['rules'])} workflow definitions")
            return
        while True:
            with store.transaction(db):
                tick(db)
            if args.command != "worker":
                return
            time.sleep(60)


if __name__ == "__main__":
    import extensions
    extensions.load()
    main()
