"""What agents did on a conversation, why, and what state it is in: as data, not as notes and tags.

On a hosted help desk the only places an agent could leave its reasoning were an internal note
(prose with "Mode:", "Confidence:" lines) and a tag. People read walls of notes inside the
correspondence, and agents parsed each other's prose or asked a person to change a tag for them.
This module gives both a proper home:

- an **event** is one thing an agent or a person did or decided (a reply decision with its
  confidence and blockers, a reminder, a scheduled send, a request to the owner, a control);
- the **state** of a conversation (who handles it, who is waited on, the campaign stage,
  whether reminders run) is read and changed through one typed view.

Agents that still write notes keep working: a note says who wrote it and may state its record;
otherwise a registered note reader recognises its wording, and a state change is mirrored to the
tags other programs read. What a campaign is, and how an installation's agents worded their
notes, is registered by an extension (`extensions.py`); with none, the mailbox has no campaigns.
`python agent_ledger.py backfill` absorbs history.

Callers own authentication and the transaction.
"""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import html
import json
import logging
import re
import time

import company
import store

KINDS = (
    "decision", "reminder", "scheduled", "sent", "outreach", "responder", "reasoning", "advice",
    "request", "review", "learning", "state", "control", "instruction", "note",
)
# Kinds that describe work waiting on the owner while their status is "open".
NEEDS_OWNER = ("request", "scheduled")
TEXT_LIMIT = 8000

# The person who runs the mailbox, in every record and every state. Their name is a setting.
OWNER = "owner"
WAITING = ("customer", OWNER, "agent", "blocked")
SETTABLE_WAITING = ("customer", OWNER, "blocked")
BASE_FIELDS = ("handling", "waiting_on", "campaign", "stage", "reminders")
RECENT_DAYS = 14  # An absorbed request or booking older than this is history, not something still open.
# How the intake check and triage file a conversation, by tag.
CLASSES = {"marketing": "marketing", "spammy": "marketing", "promotion": "marketing", "admin": "admin",
           "spam": "spam", "site-mail": "site mail", "delivery-report": "delivery report",
           "auto-reply": "automatic reply", "wrong-address": "wrong address", "non-support": "not support"}

# What an installation adds (see `register_campaign`, `register_note_reader`, `configure`). A mailbox
# with nothing registered has no campaigns: who handles a conversation, requests, booked sends and
# decisions all work, and a state change that names a stage is refused.
_campaigns = {}
_readers = []
_actors = {}
_attention = {}
_flags = {}
_aliases = set()
_service = [None]
# Kept for callers that ask "is this a closing stage" or list the stages without naming a campaign.
STAGES, ENDED = (), ()


def reset():
    """Forget every registration. For tests."""
    global STAGES, ENDED
    _campaigns.clear(), _readers.clear(), _actors.clear(), _attention.clear(), _flags.clear(), _aliases.clear()
    _service[0] = None
    STAGES, ENDED = (), ()


RESERVED = (*BASE_FIELDS, "status", "classification", "attention", "draft_waiting", "last_decision", "scheduled", "requests")
WORD = re.compile(r"[a-z][a-z0-9_]{0,40}")


def _flag(name, spec):
    if not isinstance(name, str) or not WORD.fullmatch(name) or name in RESERVED or not isinstance(spec.get("tag"), str) or not spec["tag"].strip():
        raise ValueError(f"A flag needs a name of its own and a tag: {name!r}")
    return {"tag": spec["tag"].lower(), "on": spec.get("on") or name.replace("_", " "),
            "off": spec.get("off") or "not " + name.replace("_", " "), "chip": spec.get("chip")}


def configure(*, owner_aliases=(), service_actor=None, actors=None, attention=None, flags=None):
    """Installation-wide settings of the ledger.

    `owner_aliases`: other keys the owner is known by, in stored rows and in requests.
    `service_actor`: the automation whose own client writes notes for several of its parts.
    `actors`: display names by actor key. `attention`: tags that ask for a person's look, with labels.
    `flags`: yes/no facts kept as one tag each, as `{name: {"tag", "on", "off", "chip"}}`.
    """
    _aliases.update(str(alias).strip().lower() for alias in owner_aliases)
    if service_actor:
        _service[0] = str(service_actor)
    _actors.update(actors or {})
    _attention.update({str(tag).lower(): label for tag, label in (attention or {}).items()})
    for name, spec in (flags or {}).items():
        if any(name in c["flags"] for c in _campaigns.values()):
            raise ValueError(f"A campaign already keeps a flag named {name!r}")
        _flags[name] = _flag(name, spec)


def register_campaign(name, *, marker, label, stages, waiting_tags, stage_names=None, ended=(), no_contact=(),
                      flags=None, quiet_stages=("draft",), off_pipeline=True):
    """A line of work that marks a conversation's progress with tags other programs read.

    `marker` is the tag every conversation of the campaign carries; a stage `not_now` is the tag
    `<name>-not-now`, one at a time; `waiting_tags` maps who is waited on ("customer", OWNER,
    "agent", "blocked") to its tag; `<name>-no-reminders` switches reminders off. `ended` stages
    wait on nobody, `no_contact` stages also stop reminders for good. Reminders count as running
    only while the customer is waited on, outside the `quiet_stages`. With `off_pipeline`, enrolling
    a conversation also takes it off the general agents (the hand-off tag).
    """
    global STAGES, ENDED
    if not re.fullmatch(r"[a-z][a-z0-9]{1,30}", str(name)):
        raise ValueError("A campaign name is a short lowercase word")
    stages = tuple(stages)
    if not set(ended) <= set(stages) or not set(no_contact) <= set(ended) or not set(waiting_tags) <= set(WAITING):
        raise ValueError(f"Campaign {name!r}: closing stages and waiting values must be known ones")
    if not isinstance(label, str) or not label.strip() or not stages or any(
            not isinstance(s, str) or not WORD.fullmatch(s) or s == "no_reminders" for s in stages):
        raise ValueError(f"Campaign {name!r}: it needs a label and stages named in lowercase words")
    # Its tags all start with its name; no tag the mailbox itself files with may look like one of them.
    if any(tag.startswith(name + "-") for tag in (company.IGNORE_TAG, company.HELD_TAG, "possible-wrong-address", *CLASSES)):
        raise ValueError(f"Campaign {name!r}: the name is the start of a tag the mailbox already uses")
    if any(flag in _flags for flag in (flags or {})):
        raise ValueError(f"Campaign {name!r}: a flag of that name is already kept for every conversation")
    _campaigns[name] = {
        "name": name, "marker": str(marker).lower(), "label": label, "stages": stages,
        "stage_names": dict(stage_names or {}), "ended": tuple(ended), "no_contact": tuple(no_contact),
        "waiting": {who: str(tag).lower() for who, tag in waiting_tags.items()},
        "flags": {flag: _flag(flag, spec) for flag, spec in (flags or {}).items()},
        "quiet": tuple(quiet_stages), "off_pipeline": bool(off_pipeline),
    }
    STAGES = tuple(dict.fromkeys(stage for c in _campaigns.values() for stage in c["stages"]))
    ENDED = tuple(dict.fromkeys(stage for c in _campaigns.values() for stage in c["ended"]))


def register_note_reader(reader):
    """`reader(rows, source)` recognises a note by its wording and returns
    `{"kind", "actor", "title", "status"?, "data"?}`, or None. Readers are asked in order."""
    if reader not in _readers:
        _readers.append(reader)


def who(actor):
    """An actor key as the ledger keeps it: the owner under one key, whatever older rows call them."""
    return OWNER if isinstance(actor, str) and actor.strip().lower() in (OWNER, *_aliases) else actor


def attention():
    # An installation's own tags first, in the order it gave them.
    return {**_attention, **{tag: label for tag, label in (
        (company.HELD_TAG, "wrong-address reply held"), ("possible-wrong-address", "possible wrong address"))
        if tag not in _attention}}


def state_fields():
    return (*BASE_FIELDS, *_flags, *dict.fromkeys(flag for c in _campaigns.values() for flag in c["flags"]))


BREAK = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])\s*>")
TAG = re.compile(r"<[^<>]*>")
FIELD = re.compile(r"^([A-Z][A-Za-z ’'()-]{1,40}):(?:\s+(.*))?$")  # "See https://…" is not a field.


Invalid = store.Invalid


def schema(db):
    # Single statements, so a caller's open transaction stays open.
    db.execute(
        "CREATE TABLE IF NOT EXISTS agent_events("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "conversation INTEGER NOT NULL REFERENCES conversations ON DELETE CASCADE,"
        "at TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, title TEXT NOT NULL,"
        "data TEXT NOT NULL, note INTEGER UNIQUE, about INTEGER,"
        "status TEXT NOT NULL DEFAULT '', operation TEXT UNIQUE)")
    db.execute("CREATE INDEX IF NOT EXISTS agent_events_conversation ON agent_events(conversation,at,id)")
    db.execute("CREATE INDEX IF NOT EXISTS agent_events_recent ON agent_events(at DESC,id DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS agent_events_open ON agent_events(kind,at) WHERE status='open'")


def ready(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_events'").fetchone() is not None


def lines(markup):
    text = TAG.sub(" ", BREAK.sub("\n", str(markup or "")[:400_000]))
    text = html.unescape(text).replace("\xa0", " ")
    return [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]


def fields(rows):
    """`Key: value` lines of an agent note, with wrapped lines joined to the key above them."""
    found, last = {}, None
    for row in rows:
        match = FIELD.match(row)
        if match:
            last = match[1].strip().lower()
            found[last] = (match[2] or "").strip()
        elif last and row:
            found[last] = (found[last] + " " + row).strip()
    return found


def parse(markup, source="api"):
    """Read a stored note as an event without its conversation. None for no text.

    What a person typed is theirs, whatever it starts with. Any other note is offered to the
    registered note readers (an installation's own wordings); unread, it is its writer's note.
    """
    rows = [row for row in lines(markup) if row]
    if not rows:
        return None
    text = "\n".join(rows)
    head = rows[0][:600]

    def made(kind, actor, title, status="", data=None):
        kept = {k: v for k, v in (data or {}).items() if v not in (None, "", [], {})}
        return {"kind": kind, "actor": actor, "title": str(title)[:240],
                "data": {"text": text[:TEXT_LIMIT], **kept}, "status": status}

    if source != "api" and not source.startswith("beacon"):
        return made("instruction", OWNER, head[:200])
    for reader in _readers:
        found = reader(rows, source)
        if found:
            return made(found["kind"], found["actor"], found["title"], found.get("status") or "", found.get("data"))
    if source.startswith("beacon"):
        return made("note", "inbox", "Technical information from the contact form")
    return made("note", "agent", head[:200])


TEXT_FIELDS = ("text", "due", "what", "clock", "zone", "day", "why", "action", "reason", "missing", "next", "evidence", "key")


def cutoff(days, now=None):
    return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def validated(event):
    """An event's fields, checked. Raises Invalid for anything a reader could not show."""
    kind = event.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise Invalid("Unknown event kind")
    actor = str(who(event.get("actor")) or "").strip()[:80]
    title = str(event.get("title") or "").strip()[:240]
    if not actor or not title:
        raise Invalid("An event needs an actor and a title")
    data = {} if event.get("data") is None else event["data"]
    if not isinstance(data, dict):
        raise Invalid("Event data must be an object")
    # Pages and agents read these as text; anything else would fail every reader later.
    for field in TEXT_FIELDS:
        if data.get(field) is not None and not isinstance(data[field], str):
            raise Invalid(f"Event data '{field}' must be text")
    if "blockers" in data and not (isinstance(data["blockers"], list) and all(isinstance(b, str) for b in data["blockers"])):
        raise Invalid("Event data 'blockers' must be a list of text")
    encoded = store.encode(data)
    if len(encoded) > 200_000:
        raise Invalid("Event data exceeds 200 KB")
    status = event.get("status") or ""
    if status not in ("", "open", "done", "cancelled", "sent"):
        raise Invalid("Unknown event status")
    return {"kind": kind, "actor": actor, "title": title, "data": data, "encoded": encoded, "status": status}


def record(db, cid, event, *, at=None, note=None, about=None, operation=None):
    """Store one event. A repeated note or operation key returns the first event's id."""
    checked = validated(event)
    kind, actor, title, data, encoded, status = (checked[k] for k in ("kind", "actor", "title", "data", "encoded", "status"))
    if operation is not None and not isinstance(operation, str):
        raise Invalid("An operation key is text")
    for column, value in (("note", note), ("operation", operation)):
        if value is not None:
            row = db.execute(f"SELECT id,conversation FROM agent_events WHERE {column}=?", (value,)).fetchone()
            if row:
                if row[1] != cid:
                    raise store.Conflict("Operation key already used on another conversation")
                return row[0]
    key = db.execute(
        "INSERT INTO agent_events(conversation,at,kind,actor,title,data,note,about,status,operation) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (cid, at or store.now(), kind, actor, title, encoded, note, about, status, operation),
    ).lastrowid
    if kind in ("sent", "control") and data.get("resolves") is True:
        # A send or a cancellation that says so settles the booking it is about.
        settle(db, cid, "sent" if kind == "sent" else "cancelled", event=data.get("event"), what=data.get("what"), before=at)
    return key


def settle(db, cid, status, *, event=None, what=None, before=None):
    """Close one open booking of this conversation: the one named, else the one about the same thing,
    else the newest. Returns its id or None."""
    open_bookings = db.execute(
        "SELECT id,data FROM agent_events WHERE conversation=? AND kind='scheduled' AND status='open' AND at<=? "
        "ORDER BY at DESC,id DESC", (cid, before or store.now())).fetchall()
    chosen = None
    if type(event) is int:
        chosen = next((row["id"] for row in open_bookings if row["id"] == event), None)
        if chosen is None:
            return None
    elif what:
        chosen = next((row["id"] for row in open_bookings if json.loads(row["data"]).get("what") == what), None)
    if chosen is None and open_bookings:
        chosen = open_bookings[0]["id"]
    if chosen is not None:
        db.execute("UPDATE agent_events SET status=? WHERE id=?", (status, chosen))
    return chosen


GENERIC_ACTORS = ("agent", "case-agent")
SERVICE_KINDS = ("decision", "reminder", "responder", "outreach", "learning")
OWNER_KINDS = ("sent", "control")  # Written by a tool acting for the owner, such as the phone app.


def read_note(thread):
    """The event a stored note stands for, and whether the note declared it itself.

    A writer may declare the whole record (`event` with a `kind`), or only who wrote the note
    (`event` with just an `actor`): then the wording is read as before and the name replaces a
    generic "agent".
    """
    native = thread.get("event")
    if isinstance(native, dict) and native.get("kind") is not None:
        data = native.get("data") if isinstance(native.get("data"), dict) else {}
        text = "\n".join(row for row in lines(thread.get("body")) if row)
        return {"kind": native.get("kind"), "actor": native.get("actor"), "title": native.get("title"),
                "data": {**data, "text": text[:TEXT_LIMIT]}, "status": native.get("status") or "",
                "operation": native.get("operation")}, True
    event = parse(thread.get("body"), str((thread.get("source") or {}).get("type") or "api"))
    author = native.get("actor") if isinstance(native, dict) else None
    author = author.strip()[:80] if isinstance(author, str) else ""
    if not event or not author or event["kind"] == "instruction":
        return event, False
    if author == _service[0]:
        # The service's own client writes for several of its parts; what the wording says stands.
        if event["actor"] in GENERIC_ACTORS:
            event["actor"] = author
    elif event["kind"] in SERVICE_KINDS:
        # Wording alone does not make another agent's note the service's decision or reminder.
        event = {"kind": "note", "actor": author, "title": event["title"], "data": {"text": event["data"].get("text", "")}, "status": ""}
    elif event["kind"] not in OWNER_KINDS:
        event["actor"] = author  # A booking, a request, advice: the writer is who the mailbox was told.
    return event, False


def absorb(db, cid, tid):
    """Turn one stored internal note into an event. Returns the event id, or None when it is no note."""
    row = db.execute("SELECT data FROM threads WHERE id=? AND conversation_id=?", (tid, cid)).fetchone()
    if not row:
        return None
    thread = json.loads(row[0])
    if thread.get("type") != "note":
        return None
    event, declared = read_note(thread)
    if not event:
        return None
    at = thread.get("createdAt") or store.now()
    if not declared and event["status"] == "open" and at < cutoff(RECENT_DAYS):
        event["status"] = ""
    return record(db, cid, event, at=at, note=tid, operation=event.get("operation") if declared else None)


def refresh(db, cid, tid):
    """A note was edited: its record follows the new text. A request someone closed stays closed."""
    known = db.execute("SELECT id,status FROM agent_events WHERE note=? AND conversation=?", (tid, cid)).fetchone()
    if not known:
        return absorb(db, cid, tid)
    row = db.execute("SELECT data FROM threads WHERE id=? AND conversation_id=?", (tid, cid)).fetchone()
    event, declared = read_note(json.loads(row[0])) if row else (None, False)
    if not event:
        db.execute("DELETE FROM agent_events WHERE id=?", (known["id"],))
        return None
    checked = validated(event)
    status = known["status"] if known["status"] in ("done", "cancelled", "sent") else checked["status"]
    db.execute("UPDATE agent_events SET kind=?,actor=?,title=?,data=?,status=? WHERE id=?",
               (checked["kind"], checked["actor"], checked["title"], checked["encoded"], status, known["id"]))
    return known["id"]


def move(db, source, target):
    """Two conversations were merged: the records follow the correspondence."""
    if ready(db):
        db.execute("UPDATE agent_events SET conversation=? WHERE conversation=?", (target, source))


def events(db, cid, limit=200):
    return [shape(row) for row in db.execute(
        "SELECT * FROM agent_events WHERE conversation=? ORDER BY at DESC,id DESC LIMIT ?", (cid, limit))]


def shape(row):
    item = dict(row)
    item["data"] = json.loads(item["data"])
    item["actor"] = who(item["actor"])
    return item


def tags_of(conv):
    return [str(t.get("tag")) for t in conv.get("tags") or [] if isinstance(t, dict) and t.get("tag")]


def stage_tag(campaign, stage):
    return f"{campaign}-{stage.replace('_', '-')}"


def stage_name(campaign, stage):
    return _campaigns[campaign]["stage_names"].get(stage, stage.replace("_", " "))


def campaign_state(named):
    """Campaign, stage, who is waited on and the switches, from the tags agents keep."""
    flags = {flag: False for c in _campaigns.values() for flag in c["flags"]}
    spec = next((c for c in _campaigns.values()
                 if c["marker"] in named or any(t.startswith(c["name"] + "-") for t in named)), None)
    if not spec:
        return {"campaign": None, "stage": None, "waiting": None, "reminders": None, **flags}
    name = spec["name"]
    reached = [s for s in spec["stages"] if stage_tag(name, s) in named]
    waiting = next((w for w in WAITING if spec["waiting"].get(w) in named), None)
    stage = reached[-1] if reached else None
    stopped = f"{name}-no-reminders" in named or stage in spec["ended"]
    flags.update({flag: f["tag"] in named for flag, f in spec["flags"].items()})
    return {"campaign": name, "stage": stage, "waiting": waiting,
            # Reminders run only while the customer is the one waited on.
            "reminders": "off" if stopped else "on" if waiting == "customer" and stage not in spec["quiet"] else "idle",
            **flags}


def taken_over(db, cid):
    """Whether the latest recorded change of who handles this conversation gave it to a person."""
    row = db.execute(
        "SELECT json_extract(data,'$.changes.handling') FROM agent_events WHERE conversation=? AND kind IN ('control','state') "
        "AND json_extract(data,'$.changes.handling') IS NOT NULL ORDER BY at DESC,id DESC LIMIT 1", (cid,)).fetchone()
    return bool(row) and row[0] == "person"


def state(db, cid, conv=None, now=None):
    """The conversation's state as agents and people need it, read from tags, events and threads."""
    conv = conv or store.conversation(db, cid)
    named = {t.lower() for t in tags_of(conv)}
    campaign = campaign_state(named)
    latest = {}
    for row in db.execute(
        "SELECT kind,title,at,data,status,id FROM agent_events WHERE conversation=? AND kind IN "
        "('decision','reminder') ORDER BY at DESC,id DESC LIMIT 12", (cid,),
    ):
        latest.setdefault(row["kind"], row)
    decision, reminder = latest.get("decision"), latest.get("reminder")
    moment = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    scheduled = []
    for row in db.execute(
        "SELECT id,title,data,at FROM agent_events WHERE conversation=? AND kind='scheduled' AND status='open' "
        "ORDER BY at DESC,id DESC LIMIT 20", (cid,),
    ):
        info = json.loads(row["data"])
        # A send whose time has passed is no longer waiting, whoever sent or dropped it.
        if not isinstance(info.get("due"), str) or info["due"] < moment:
            continue
        scheduled.append({"id": row["id"], "what": info.get("what") or row["title"], "due": info.get("due"),
                          "clock": info.get("clock"), "zone": info.get("zone"), "day": info.get("day")})
    requests = [
        {"id": row["id"], "title": row["title"], "actor": who(row["actor"]), "at": row["at"],
         "action": json.loads(row["data"]).get("action")}
        for row in db.execute(
            "SELECT id,title,actor,at,data FROM agent_events WHERE conversation=? AND kind='request' AND status='open' "
            "ORDER BY at DESC,id DESC LIMIT 20", (cid,))
    ]
    draft = db.execute(
        "SELECT 1 FROM threads WHERE conversation_id=? AND json_extract(data,'$.state')='draft' LIMIT 1", (cid,)
    ).fetchone() is not None
    status = conv.get("status")
    info = json.loads(reminder["data"]) if reminder else {}
    chosen = json.loads(decision["data"]) if decision else {}
    flags = {k: v for k, v in campaign.items() if k not in ("campaign", "stage", "waiting", "reminders")}
    return {
        # A campaign conversation may carry the hand-off tag throughout: the general agents stay off it
        # and the campaign's own agents work it. There the tag says nothing about who is in charge.
        # The same tag on mail that was classified as not support (by the intake check, the receiver
        # or triage) only keeps the agents off it: nobody took it over and nobody needs to.
        "handling": ("campaign" if campaign["campaign"] else "agents" if company.IGNORE_TAG not in named
                     else "nobody" if any(t in named for t in CLASSES) and not taken_over(db, cid) else "person"),
        "status": status,
        "waiting_on": campaign["waiting"],
        "campaign": campaign["campaign"],
        "stage": campaign["stage"],
        **flags,
        **{name: flag["tag"] in named for name, flag in _flags.items()},
        "reminders": {"state": campaign["reminders"],
                      "last": {"number": info.get("number"), "of": info.get("of"), "outcome": info.get("outcome"),
                               "at": reminder["at"]} if reminder else None},
        "classification": next((CLASSES[t] for t in CLASSES if t in named), None),
        "attention": [label for tag, label in attention().items() if tag in named],
        "draft_waiting": draft and status in ("active", "pending"),
        "last_decision": {"title": decision["title"], "at": decision["at"],
                          **{k: chosen.get(k) for k in ("mode", "confidence", "blockers")}} if decision else None,
        "scheduled": scheduled,
        "requests": requests,
    }


def change_state(db, cid, changes, actor, reason="", owner=False):
    """Apply a typed state change and mirror it to the tags existing agents read. Returns the new state.

    `owner` is true for the person who runs the mailbox; only they hand a conversation back to the agents.
    """
    if not isinstance(changes, dict) or not changes:
        raise Invalid("Give at least one state change")
    unknown = set(changes) - set(state_fields())
    if unknown:
        raise Invalid("Unknown state field: " + ", ".join(sorted(map(str, unknown))))
    for field in BASE_FIELDS:
        if field in changes and not isinstance(changes[field], str):
            raise Invalid(f"'{field}' must be text")
    for field in set(changes) - set(BASE_FIELDS):
        if type(changes[field]) is not bool:
            raise Invalid(f"'{field}' is true or false")
    actor = str(who(actor) or "").strip()[:80]
    if not actor:
        raise Invalid("Name who makes the change")
    if "waiting_on" in changes:
        changes = {**changes, "waiting_on": who(changes["waiting_on"])}
    conv = store.conversation(db, cid)
    current = tags_of(conv)
    named = {t.lower(): t for t in current}
    handoff = company.IGNORE_TAG
    before = campaign_state(set(named))
    campaign = changes.get("campaign") or before["campaign"]
    if "campaign" in changes and campaign not in _campaigns:
        raise Invalid("Unknown campaign")
    if before["campaign"] and campaign != before["campaign"]:
        raise Invalid("This conversation already belongs to a campaign")
    spec = _campaigns.get(campaign)
    stage = changes.get("stage", before["stage"])
    if "stage" in changes and (not spec or stage not in spec["stages"]):
        raise Invalid("Unknown stage" if spec else "This conversation belongs to no campaign; name one to set its stage, waiting state or reminders")
    asked = changes.get("waiting_on")
    if asked is not None and asked not in (*SETTABLE_WAITING, "nobody"):
        raise Invalid("Waiting on 'customer', 'owner', 'blocked' or 'nobody'")
    if "reminders" in changes and changes["reminders"] not in ("on", "off"):
        raise Invalid("Reminders are 'on' or 'off'")
    of_campaign = {"waiting_on", "stage", "reminders"} | {flag for c in _campaigns.values() for flag in c["flags"]}
    if of_campaign & set(changes) and not spec:
        raise Invalid("This conversation belongs to no campaign; name one to set its stage, waiting state or reminders")
    for flag in set(changes) - set(BASE_FIELDS) - set(_flags):
        if flag not in spec["flags"]:
            raise Invalid(f"'{flag}' is not kept for this campaign")
    if asked is not None and asked != "nobody" and asked not in spec["waiting"]:
        raise Invalid("This campaign has no such waiting state")
    # A closed case waits on nobody and a refusal gets no reminders; a change must not say otherwise.
    if spec and stage in spec["ended"] and asked not in (None, "nobody"):
        raise Invalid("A closed stage waits on nobody")
    if spec and stage in spec["no_contact"] and changes.get("reminders") == "on":
        raise Invalid("No reminders after a decline or an opt-out")

    def put(tag, present):
        if present:
            named.setdefault(tag, tag)
        else:
            named.pop(tag, None)

    done = []
    if "handling" in changes:
        if changes["handling"] not in ("agents", "person"):
            raise Invalid("Handling is 'agents' or 'person'")
        if campaign:
            raise Invalid("A campaign conversation is steered by its stage, waiting state and reminders")
        if changes["handling"] == "agents" and handoff in named and not owner:
            raise store.Conflict("Only the mailbox owner hands a conversation back to the agents")
        put(handoff, changes["handling"] == "person")
        done.append("handled by " + ("a person" if changes["handling"] == "person" else "the agents"))
    for name, flag in _flags.items():
        if name in changes:
            put(flag["tag"], changes[name])
            done.append(flag["on"] if changes[name] else flag["off"])
    if "campaign" in changes:
        put(spec["marker"], True)
        if spec["off_pipeline"]:
            put(handoff, True)  # Campaign conversations stay off the general agents.
        done.append("campaign " + campaign)
    for name, flag in (spec["flags"] if spec else {}).items():
        if name in changes:
            put(flag["tag"], changes[name])
            done.append(flag["on"] if changes[name] else flag["off"])
    if "stage" in changes:
        for other in spec["stages"]:  # Exactly one stage at a time.
            put(stage_tag(campaign, other), other == stage)
        if stage in spec["ended"]:
            for tag in spec["waiting"].values():
                put(tag, False)
        if stage in spec["no_contact"]:
            put(f"{campaign}-no-reminders", True)
        done.append("stage " + stage_name(campaign, stage))
    if asked is not None:
        for other, tag in spec["waiting"].items():
            put(tag, other == asked)
        done.append("waiting on " + asked)
    if "reminders" in changes:
        put(f"{campaign}-no-reminders", changes["reminders"] == "off")
        done.append("reminders " + changes["reminders"])
    tags = list(named.values())
    retagged = sorted(t.lower() for t in tags) != sorted(t.lower() for t in current)
    # Taking over mail that was only filed changes no tag, but it is a decision and is recorded as one.
    claimed = (not retagged and changes.get("handling") == "person" and not campaign
               and any(t in named for t in CLASSES) and not taken_over(db, cid))
    if retagged or claimed:
        if retagged:
            store.update_conversation(db, cid, {"tags": [{"tag": t} for t in tags]})
            store.emit(db, cid, "convo.tags")
        summary = ", ".join(done)
        record(db, cid, {"kind": "control" if owner else "state", "actor": actor,
                         "title": summary[:1].upper() + summary[1:],
                         "data": {"changes": changes, "reason": str(reason or "")[:1000],
                                  "added": sorted(set(tags) - set(current)), "removed": sorted(set(current) - set(tags))}})
    return state(db, cid)


def resolve(db, cid, key, status, actor):
    """Close an open request or booking: done, cancelled or sent."""
    if status not in ("done", "cancelled", "sent"):
        raise Invalid("Unknown event status")
    row = db.execute("SELECT kind,status,title FROM agent_events WHERE id=? AND conversation=?", (key, cid)).fetchone()
    if not row:
        raise store.Missing("Event not found")
    if row["kind"] not in NEEDS_OWNER:
        raise Invalid("Only a request or a booked send can be closed")
    if (row["kind"] == "request") != (status == "done"):
        raise Invalid("A request is marked done; a booked send is cancelled or sent")
    if row["status"] != "open":
        return False
    db.execute("UPDATE agent_events SET status=? WHERE id=?", (status, key))
    record(db, cid, {"kind": "control", "actor": str(who(actor) or "agent")[:80],
                     "title": f"Marked {status}: {row['title']}"[:240], "data": {"event": key}})
    return True


ACTORS = {"agent": "Agent", "case-agent": "Case agent", "inbox": "Inbox"}


def actor_name(actor):
    return company.agent_name() if actor == OWNER else {**ACTORS, **_actors}.get(actor, actor)


FACTS = {
    "decision": (("why", "Why"), ("blockers", "Blocked by"), ("missing", "Missing"), ("next", "Next"),
                 ("mode_note", "Mode"), ("scores", "Evaluator scores"), ("deep_pass", "Deep pass"),
                 ("scheduled", "Scheduled send"), ("evidence", "Evidence"), ("sources", "Sources"),
                 ("risk_flags", "Risk flags"), ("providers", "Models")),
    "reminder": (("why", "Why"), ("held", "Why it was not sent"), ("blockers", "Blocked by"),
                 ("issues", "Corrections needed"), ("scores", "Evaluator scores")),
    "scheduled": (("what", "What"), ("day", "Day"), ("clock", "Time"), ("zone", "Time zone")),
    "sent": (("what", "What"),),
    "responder": (("why", "Why"), ("recommended", "Recommended action"), ("customer", "Customer context"),
                  ("intent", "Draft intent"), ("blockers", "Review concerns"), ("edit", "Suggested edit"),
                  ("category", "Reply type"), ("score", "Evaluator")),
    "reasoning": (("why", "Why this person"), ("evidence", "Their own words"), ("approach", "Approach"),
                  ("cautions", "Cautions"), ("authorization", "Authorization")),
    "advice": (("recommendation", "Recommendation"), ("alternatives", "Alternatives"), ("owned", "Already owned"),
               ("route", "Route"), ("open_for_owner", "Open for you")),
    "outreach": (("strategy", "Strategy"), ("market", "Market"), ("verdict", "Verdict"), ("why", "Why"),
                 ("observation", "Observation"), ("score", "Evaluator"), ("approval", "Approved by"),
                 ("problems", "Verification found differences")),
    "request": (("action", "Asked of you"),),
    "learning": (("change", "Detected change"), ("why", "Summary")),
    "state": (("reason", "Reason"), ("added", "Tags added"), ("removed", "Tags removed")),
    "control": (("reason", "Reason"), ("what", "What"), ("added", "Tags added"), ("removed", "Tags removed")),
}
STATUS_NAMES = {"open": "open", "done": "done", "cancelled": "stopped", "sent": "sent"}


def plain(value):
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def present(item):
    """Add what a page shows for one event: who, one line of reason, and its labelled facts."""
    data = item["data"]
    item["who"] = actor_name(item["actor"])
    item["facts"] = [(label, plain(data[key])) for key, label in FACTS.get(item["kind"], ()) if data.get(key) not in (None, "", [], {})]
    text = str(data.get("text") or "")
    rest = " ".join(text.split("\n")[1:]).strip()
    lead = next((plain(data[k]) for k in ("why", "action", "recommendation", "customer", "reason", "what") if data.get(k)), "")
    if item["kind"] in ("note", "instruction", "review", "request") and not lead:
        lead = rest
    passed = item["kind"] == "scheduled" and item["status"] == "open" and str(data.get("due") or "") < cutoff(0)
    if item["kind"] == "scheduled":  # The title already says what and when.
        lead = "Goes out at that time unless it is stopped." if item["status"] == "open" and not passed else ""
    item["lead"] = lead[:280] + ("…" if len(lead) > 280 else "")
    item["status_name"] = "time passed" if passed else STATUS_NAMES.get(item["status"], "")
    item["waiting"] = item["status"] == "open" and not passed
    item["text"] = text
    return item


def chips(found):
    """The state of a conversation as short labels, most important first: (text, kind)."""
    out = [{"person": ("You handle this · agents stopped", "warn"), "agents": ("Agents handle this", ""),
            "campaign": ("Campaign agents work this", ""), "nobody": ("Not support · agents stay off", "")}[found["handling"]]]
    if found["requests"]:
        out.append(("Asks something of you", "warn"))
    for send in found["scheduled"]:
        out.append((f"Send booked for {send.get('clock') or ''} ({send.get('zone') or 'UTC'})", "warn"))
    if found["campaign"]:
        stage, spec = found["stage"], _campaigns.get(found["campaign"]) or {"label": found["campaign"]}
        out.append((spec["label"] + (" · " + stage_name(found["campaign"], stage) if stage else ""), ""))
        if found["waiting_on"] in WAITING:
            out.append(({"customer": "Waiting on the customer", OWNER: "Waiting on you", "agent": "Waiting on an agent",
                         "blocked": "Blocked"}[found["waiting_on"]], "warn" if found["waiting_on"] in (OWNER, "blocked") else ""))
        last = (found["reminders"] or {}).get("last")
        sent = f" · {last['number']} of {last['of']} {last.get('outcome') or 'done'}" if last and last.get("number") else ""
        out.append(({"on": "Reminders on", "off": "Reminders off", "idle": "Reminders idle"}[found["reminders"]["state"]] + sent, ""))
        for name, flag in _flags.items():
            if flag["chip"] and found.get(name):
                out.append((flag["chip"], ""))
    if found["classification"]:
        out.append(("Classified: " + found["classification"], ""))
    for label in found["attention"]:
        out.append((label.capitalize(), "warn"))
    decision = found["last_decision"]
    if decision:
        out.append(("Last decision: " + decision["title"].lower(), ""))
    return out


def view(db, cid, conv=None):
    """Everything the conversation page shows about agents."""
    found = state(db, cid, conv)
    items = [present(item) for item in events(db, cid, 300)]
    return {"state": found, "chips": chips(found), "events": items,
            "by_note": {item["note"]: item for item in items if item["note"]},
            "standalone": [item for item in items if not item["note"]],
            "stages": [(s, stage_name(found["campaign"], s)) for s in _campaigns[found["campaign"]]["stages"]]
            if found["campaign"] in _campaigns else []}


def brief(db, cid, limit=12, text=500):
    """State and recent events for an agent's context: no note texts, long values cut."""
    def cut(value):
        if isinstance(value, str):
            return value if len(value) <= text else value[:text] + "…"
        if isinstance(value, list):
            return [cut(v) for v in value[:12]]
        if isinstance(value, dict):
            return {str(k)[:60]: cut(v) for k, v in list(value.items())[:12]}
        return value

    recent = []
    for item in events(db, cid, limit):
        data = {k: cut(v) for k, v in item["data"].items() if k != "text"}
        recent.append({"id": item["id"], "at": item["at"], "kind": item["kind"], "actor": item["actor"],
                       "title": item["title"], "status": item["status"], "data": data})
    return {"state": state(db, cid), "events": recent}


def needs_owner(db, now=None, limit=60):
    """What waits on the owner across the mailbox, newest first: requests of the last 30 days nobody
    closed, and booked sends whose time has not come."""
    return [shape(row) for row in db.execute(
        "SELECT e.*,c.number,json_extract(c.data,'$.subject') subject,c.status conversation_status "
        "FROM agent_events e INDEXED BY agent_events_open JOIN conversations c ON c.id=e.conversation "
        "WHERE e.status='open' AND c.status<>'spam' AND ((e.kind='scheduled' AND json_extract(e.data,'$.due')>=?) "
        "OR (e.kind='request' AND e.at>=?)) ORDER BY e.at DESC,e.id DESC LIMIT ?",
        (cutoff(0, now), cutoff(30, now), limit),
    )]


def activity(db, since, limit=200, *, decision_mode=None):
    clause = " AND e.kind='decision' AND json_extract(e.data,'$.mode')=?" if decision_mode else ""
    params = [since, decision_mode, limit] if decision_mode else [since, limit]
    return [shape(row) for row in db.execute(
        "SELECT e.*,c.number,json_extract(c.data,'$.subject') subject FROM agent_events e "
        f"JOIN conversations c ON c.id=e.conversation WHERE e.at>=?{clause} ORDER BY e.at DESC,e.id DESC LIMIT ?",
        params)]


def pending(db, after=0, batch=50):
    """Stored notes that have no record yet, oldest first. A read: call it outside the write transaction."""
    return db.execute(
        "SELECT t.id,t.conversation_id FROM threads t WHERE t.id>? AND json_extract(t.data,'$.type')='note' "
        "AND NOT EXISTS(SELECT 1 FROM agent_events e WHERE e.note=t.id) ORDER BY t.id LIMIT ?",
        (after, batch),
    ).fetchall()


def backfill(db, after=0, batch=50, rows=None):
    """Absorb one batch of stored notes that have no record yet. Returns (next cursor or None, absorbed)."""
    rows = pending(db, after, batch) if rows is None else rows
    done = 0
    for tid, cid in rows:
        db.execute("SAVEPOINT ledger_backfill")
        try:
            if absorb(db, cid, tid):
                done += 1
        except Exception:  # One note that cannot be read must not stop the history behind it.
            db.execute("ROLLBACK TO ledger_backfill")
            logging.getLogger("inbox.ledger").exception("Note %s was not recorded in the agent ledger", tid)
        db.execute("RELEASE ledger_backfill")
    return (rows[-1][0] if len(rows) == batch else None), done


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Agent ledger maintenance")
    cli.add_argument("command", choices=["backfill"], help="Turn stored agent notes into events, in short steps")
    cli.parse_args()
    import sys

    import extensions

    # Run as a script this file is `__main__`; an extension importing `agent_ledger` must get this
    # very module, or it would register its campaigns and note readers on a second copy.
    sys.modules.setdefault("agent_ledger", sys.modules[__name__])
    extensions.load()  # History is read with the installation's own note readers.
    with closing(store.connect()) as mailbox:
        with store.transaction(mailbox):
            schema(mailbox)
        started, cursor, total, batches = time.monotonic(), 0, 0, 0
        while cursor is not None:
            found = pending(mailbox, cursor)  # The search reads; only the short write below holds the mailbox.
            with store.transaction(mailbox):
                cursor, done = backfill(mailbox, cursor, rows=found)
            total += done
            batches += 1
            time.sleep(0.2)  # Leave the mailbox to its other writers between steps.
        kinds = dict(mailbox.execute("SELECT kind,count(*) FROM agent_events GROUP BY 1").fetchall())
        print(json.dumps({"absorbed": total, "batches": batches, "seconds": round(time.monotonic() - started, 1), "events": kinds}))
