"""Mailbox API for support agents and incoming conversations."""

import asyncio
import base64
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shlex
import time
from urllib.parse import parse_qs

from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import asset_library
import beacon
import operator_ui
import operator_actions
import operator_views
import agent_context
import agent_ledger
import insights
import native_workflows
import standard_folders
import store
import company
import extensions
from workbench import core as desk_core, http as desk_http, inbox_ui as desk_ui


@asynccontextmanager
async def lifespan(application):
    if os.getenv("DESK_DB") and os.path.realpath(os.environ["DESK_DB"]) != os.path.realpath(os.environ["INBOX_DB"]):
        raise RuntimeError("The integrated inbox and workbench must use the same company database")
    with closing(store.connect()) as db:
        store.initialize(db)
        desk_core.schema(db)
        operator_ui.ui_schema(db)
        beacon.initialize(db)
        with store.transaction(db):
            agent_ledger.schema(db)
    yield


app = FastAPI(
    title=company.NAME+" Inbox",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

extensions.load()
operator_ui.mount_account_lookup(app)
desk_http.mount(app)
app.include_router(desk_ui.router)
app.include_router(operator_ui.router)
app.include_router(operator_actions.router)
app.include_router(operator_views.router)
app.include_router(beacon.router)


@app.middleware("http")
async def no_cache(request, call_next):
    result = await call_next(request)
    # Fingerprinted stylesheets and scripts declare their own lifetime.
    result.headers.setdefault("Cache-Control", "private, no-store")
    result.headers["X-Content-Type-Options"] = "nosniff"
    result.headers["Referrer-Policy"] = "same-origin"
    return result


@app.exception_handler(store.WriterPaused)
async def writer_paused(request, error):
    return JSONResponse({"message":str(error)}, status_code=503)


@app.exception_handler(store.Invalid)
async def invalid(request, error):
    return JSONResponse({"message": str(error)}, status_code=400)


@app.exception_handler(asset_library.SourceUnavailable)
async def source_file_unavailable(request,error):
    return JSONResponse({"message":str(error),"code":"source_unavailable"},status_code=410)


@app.exception_handler(store.Missing)
async def missing(request, error):
    return JSONResponse({"message": str(error)}, status_code=404)


@app.exception_handler(store.Conflict)
async def conflict(request, error):
    return JSONResponse({"message": str(error)}, status_code=409)


@contextmanager
def session(write=False, *, require_writer=False):
    with closing(store.connect()) as db, store.transaction(db, write=write):
        if require_writer:
            store.require_writer(db)
        yield db


@app.get("/health")
def health():
    with session() as db:
        db.execute("SELECT 1 FROM conversations LIMIT 1").fetchone()
    return {"status": "ok", "pid": os.getpid(), "extensions": extensions.loaded()}


async def read_data(request, max_bytes, form=False):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > max_bytes:
            raise HTTPException(413, "Request exceeds size limit")
        data.extend(chunk)
    raw = bytes(data)
    try:
        if form and request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            return {
                k: v[0]
                for k, v in parse_qs(raw.decode(), max_num_fields=12).items()
                if len(v) == 1
            }
        value = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise store.Invalid("Invalid request body") from error
    if not isinstance(value, dict):
        raise store.Invalid("Expected a JSON object")
    return value


@app.post("/v2/oauth2/token")
async def token(request: Request):
    data = await read_data(request, 8192, form=True)
    client = data.get("client_id")
    secret = data.get("client_secret")
    if (
        data.get("grant_type") != "client_credentials"
        or not isinstance(client, str)
        or not isinstance(secret, str)
    ):
        raise HTTPException(401, "Invalid client")
    for role, prefix in [
        ("gateway", "INBOX_CLIENT"),
        ("intake", "INBOX_INTAKE_CLIENT"),
    ]:
        name, password = (
            os.environ.get(f"{prefix}_ID"),
            os.environ.get(f"{prefix}_SECRET"),
        )
        if (
            name
            and password
            and hmac.compare_digest(client.encode(), name.encode())
            and hmac.compare_digest(secret.encode(), password.encode())
        ):
            break
    else:
        raise HTTPException(401, "Invalid client")
    value = secrets.token_urlsafe(40)
    digest = hashlib.sha256(value.encode()).hexdigest()
    with session(True) as db:
        current = int(time.time())
        db.execute("DELETE FROM access_tokens WHERE expires<=?", (current,))
        db.execute(
            "INSERT INTO access_tokens VALUES(?,?,?)", (digest, role, current + 7200)
        )
    return {"access_token": value, "token_type": "bearer", "expires_in": 7200}


def identity(request: Request):
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(401, "Bearer token required")
    with session() as db:
        row = db.execute(
            "SELECT client FROM access_tokens WHERE digest=? AND expires>?",
            (hashlib.sha256(value.encode()).hexdigest(), int(time.time())),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Invalid bearer token")
    return row[0]


def gateway(role=Depends(identity)):
    if role != "gateway":
        raise HTTPException(403, "Gateway credential required")
    return role


@app.get("/v2/operations", dependencies=[Depends(gateway)])
def operations():
    from operations import counts
    with session() as db:
        return {"writer": db.execute("SELECT mode FROM writer_authority WHERE id=1").fetchone()[0],
                "issues": counts(db)}


def page_response(key, rows, total, number, size):
    pages = math.ceil(total / size)
    return {
        "_embedded": {key: rows},
        "page": {
            "size": size,
            "totalElements": total,
            "totalPages": pages,
            "number": number if pages or key in ("conversations", "webhooks") else 0,
        },
    }


def positive(value, maximum):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise store.Invalid("Invalid page parameter") from None
    if not 1 <= n <= maximum:
        raise store.Invalid("Page parameter outside bounds")
    return n


ISO_MOMENT = re.compile(
    r"\d{4}-\d\d-\d\d(?:T\d\d:\d\d(?::\d\d(?:\.\d{1,9})?)?(?:Z|[+-]\d\d:\d\d)?)?"
)


def time_bound(clauses, params, column, op, value):
    """Compare instants exactly, and let the timestamp index skip rows a day outside the bound.

    Stored timestamps vary in precision and may carry an offset, so text order alone is inexact.
    A text bound one day wider than the requested instant is always safe to apply first.
    """
    clauses.append(f"julianday({column}){op}julianday(?)")
    params.append(value)
    if not isinstance(value, str) or not ISO_MOMENT.fullmatch(value):
        return
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc)
        bound = (moment + timedelta(days=-1 if op == ">=" else 1)).strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return  # A date at the edge of the calendar keeps the exact comparison alone.
    clauses.append(f"{column}{op}?")
    params.append(bound if op == ">=" else bound + "U")


SORT_AFTER_FILTER = 2000  # Results up to this size are filtered first and then sorted.


def add_query(query, clauses, params, db=None):
    query = query.strip()
    if not query:
        return
    if db is None:
        with closing(store.connect()) as reader:
            return add_query(query, clauses, params, reader)
    messages = store.word_matches(db)
    if re.fullmatch(r"#[1-9][0-9]{0,15}",query):
        query="number:"+query[1:]
    elif re.fullmatch(r"[1-9][0-9]{0,15}",query):
        clauses.append(f"""(c.number=? OR c.id IN (SELECT a.target_id FROM operator_merges m
            JOIN conversation_aliases a ON a.source_id=m.source WHERE m.number=?) OR
            c.id IN (SELECT rowid FROM conversation_search WHERE conversation_search MATCH ? UNION
            {messages}))""")
        params.extend([int(query),int(query),query+"*",query+"*"])
        return
    if len(query) > 2400:
        raise store.Invalid("Query too long")
    # Structured search clauses, optionally enclosed in parentheses. Extract ranges before shlex.
    for m in re.finditer(
        r"\b(modifiedAt|createdAt):\[([^\]]+) TO ([^\]]+)\]", query, re.I
    ):
        column = "c.modified" if m[1].lower() == "modifiedat" else "c.created"
        for bound, op in ((m[2], ">="), (m[3], "<=")):
            if bound != "*":
                if not re.fullmatch(r"\d{4}-\d\d-\d\d(?:T[\d:.]+Z)?", bound):
                    raise store.Invalid("Invalid date range")
                time_bound(clauses, params, column, op, bound)
    query = re.sub(r"\b(?:modifiedAt|createdAt):\[[^\]]+\]", "", query, flags=re.I)
    try:
        terms = shlex.split(query.replace("(", " ").replace(")", " "))
    except ValueError as error:
        raise store.Invalid("Unbalanced search quotes") from error
    for term in terms:
        field, sep, value = term.partition(":")
        if sep and field in ("number", "id", "customerIds", "mailbox", "mailboxId"):
            col = {
                "number": "number",
                "id": "id",
                "customerIds": "customer_id",
                "mailbox": "mailbox_id",
                "mailboxId": "mailbox_id",
            }[field]
            n = store.identifier(value)
            if field == "number":
                clauses.append("""(c.number=? OR c.id IN (SELECT a.target_id FROM operator_merges m
                           JOIN conversation_aliases a ON a.source_id=m.source WHERE m.number=?))""")
                params.extend((n,n))
            else:
                clauses.append(f"c.{col}=?")
                params.append(n)
        elif sep and field in ("tag", "tags"):
            clauses.append(store.tag_filter(db))
            params.append(value)
        elif sep and field == "email":
            # Two indexed lookups: the address saved on the conversation, and the customer's profile.
            clauses.append(
                "c.id IN (SELECT id FROM conversations WHERE "
                "json_extract(data,'$.primaryCustomer.email')=? COLLATE NOCASE "
                "UNION SELECT id FROM conversations WHERE customer_id IN "
                "(SELECT customer_id FROM customer_emails WHERE email=?))"
            )
            params.extend([value, value])
        elif sep and field == "status":
            clauses.append("c.status=?")
            params.append(store.status(value))
        elif sep and field == "subject":
            clauses.append("instr(lower(json_extract(c.data,'$.subject')),lower(?))>0")
            params.append(value)
        else:
            # Quote the entire phrase, including FTS operators supplied by the caller.
            # Prefix matching lets support operators enter partial words as they type.
            phrase = '"' + term.replace('"', '""') + '"*'
            clauses.append(
                "c.id IN (SELECT rowid FROM conversation_search WHERE conversation_search MATCH ? "
                f"UNION {messages})"
            )
            params.extend([phrase, phrase])


@app.get("/v2/conversations", dependencies=[Depends(gateway)])
def conversations(request: Request):
    q = request.query_params
    number = positive(q.get("page", 1), 10_000_000)
    size = 25
    # The compatibility collection excludes whole unsent conversations. The operator Drafts view has its own query.
    clauses, params = [standard_folders.PUBLISHED], []
    statuses = q.getlist("status") or ["active"]
    if "all" not in statuses:
        valid = [store.status(s) for s in statuses]
        clauses.append("c.status IN (" + ",".join("?" for _ in valid) + ")")
        params.extend(valid)
    if q.get("mailbox"):
        clauses.append("c.mailbox_id=?")
        params.append(store.identifier(q["mailbox"]))
    if q.get("modifiedSince"):
        time_bound(clauses, params, "c.modified", ">=", q["modifiedSince"])
    sort = {
        "modifiedAt": "modified",
        "createdAt": "created",
        "number": "number",
        "id": "id",
    }.get(q.get("sortField", "createdAt"))
    direction = q.get("sortOrder", "desc").lower()
    if not sort or direction not in ("asc", "desc"):
        raise store.Invalid("Invalid sort")
    with session() as db:
        if q.get("tag"):
            clauses.append(store.tag_filter(db))
            params.append(q["tag"])
        add_query(q.get("query", ""), clauses, params, db)
        if q.get("folder"):
            expr, values = standard_folders.by_id(db, store.identifier(q["folder"]))
            clauses.append(expr)
            params.extend(values)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = db.execute(
            "SELECT count(*) FROM conversations c" + where, params
        ).fetchone()[0]
        # A short result is filtered through its own index and then sorted. Walking the sort
        # index instead reads the whole mailbox when few rows match, as with a recent time window.
        order = (
            f"+{sort} {direction},+id {direction}"
            if total <= SORT_AFTER_FILTER
            else f"{sort} {direction},id {direction}"
        )
        ids = db.execute(
            "SELECT id FROM conversations c" + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, size, (number - 1) * size],
        )
        result = [
            store.conversation(db, r[0], q.get("embed") == "threads") for r in ids
        ]
    return page_response("conversations", result, total, number, size)


@app.get("/v2/conversations/{cid}", dependencies=[Depends(gateway)])
def get_conversation(cid: int, embed: str = ""):
    with session() as db:
        alias = db.execute(
            "SELECT target_id FROM conversation_aliases WHERE source_id=?", (cid,)
        ).fetchone()
        if alias:
            url = (
                os.environ["INBOX_BASE_URL"].rstrip("/")
                + f"/v2/conversations/{alias[0]}"
            )
            return Response(status_code=301, headers={"Location": url})
        return store.conversation(db, cid, embed == "threads")


@app.get("/v2/conversations/{cid}/threads", dependencies=[Depends(gateway)])
def list_threads(cid: int, page: int = 1):
    page = positive(page, 10_000_000)
    with session() as db:
        store.load(db, "SELECT data FROM conversations WHERE id=?", (cid,))
        total = db.execute(
            "SELECT count(*) FROM threads WHERE conversation_id=?", (cid,)
        ).fetchone()[0]
        return page_response(
            "threads", store.thread_list(db, cid, 25, (page - 1) * 25), total, page, 25
        )


@app.get(
    "/v2/conversations/{cid}/attachments/{aid}/data", dependencies=[Depends(gateway)]
)
def attachment(cid: int, aid: int):
    with session() as db:
        data, mime = store.attachment(db, cid, aid)
    return {"data": base64.b64encode(data).decode(), "mimeType": mime}


@app.get("/v2/conversations/{cid}/brief", dependencies=[Depends(gateway)])
def conversation_brief(cid: int, max_chars: int = 24000, quotes: bool = False, precedents: int = 0):
    """One conversation as chronological plain text, sized for a model's context."""
    if not 1000 <= max_chars <= 200000 or not 0 <= precedents <= 10:
        raise store.Invalid("Brief parameter outside bounds")
    with session() as db:
        alias = db.execute(
            "SELECT target_id FROM conversation_aliases WHERE source_id=?", (cid,)
        ).fetchone()
        cid = alias[0] if alias else cid
        result = agent_context.brief(db, cid, max_chars, quotes)
        try:  # Sized with the brief: a small budget gets the state and the few latest records.
            small = max_chars < 8000
            result["agents"] = agent_ledger.brief(db, cid, limit=4 if small else 12, text=200 if small else 500)
        except Exception:
            logging.getLogger("inbox.ledger").exception("No agent records in the brief of %s", cid)
        if precedents:
            result["precedents"] = agent_context.precedents(db, cid=cid, limit=precedents)
    return result


def canonical_conversation(db, cid):
    alias = db.execute("SELECT target_id FROM conversation_aliases WHERE source_id=?", (cid,)).fetchone()
    return alias[0] if alias else cid


@app.get("/v2/conversations/{cid}/ledger", dependencies=[Depends(gateway)])
def conversation_ledger(cid: int, limit: int = 50, text: bool = False):
    """The conversation's state and what agents and people did on it, newest first."""
    if not 1 <= limit <= 200:
        raise store.Invalid("Limit outside bounds")
    with session() as db:
        cid = canonical_conversation(db, cid)
        events = agent_ledger.events(db, cid, limit)
        if not text:
            for event in events:
                event["data"].pop("text", None)
        return {"conversation": cid, "state": agent_ledger.state(db, cid), "events": events}


@app.post("/v2/conversations/{cid}/events", dependencies=[Depends(gateway)])
async def record_event(cid: int, request: Request):
    """An agent states what it did or decided, as data. Nothing is added to the correspondence."""
    data = await read_data(request, 400_000)
    with session(True, require_writer=True) as db:
        cid = canonical_conversation(db, cid)
        store.conversation(db, cid)
        operation = data.get("operation")
        if operation is not None and (not isinstance(operation, str) or not 8 <= len(operation) <= 200):
            raise store.Invalid("An operation key is 8 to 200 characters")
        about = data.get("about")
        if about is not None:
            store.thread(db, cid, store.identifier(about))
        known = db.execute("SELECT id,title FROM agent_events WHERE operation=?", (operation,)).fetchone() if operation else None
        key = agent_ledger.record(db, cid, data, about=about, operation=operation)
        if known and known["title"] != str(data.get("title") or "").strip()[:240]:
            # The same key with other content is a different record; say so instead of answering with the old one.
            raise store.Conflict("Operation key already used for another record")
        stored = db.execute("SELECT status FROM agent_events WHERE id=?", (key,)).fetchone()["status"]
    return JSONResponse({"id": key, "status": stored, "replayed": bool(known)}, status_code=201,
                        headers={"Resource-ID": str(key)})


@app.patch("/v2/conversations/{cid}/events/{key}", dependencies=[Depends(gateway)])
async def resolve_event(cid: int, key: int, request: Request):
    data = await read_data(request, 4096)
    with session(True, require_writer=True) as db:
        cid = canonical_conversation(db, cid)
        changed = agent_ledger.resolve(db, cid, key, data.get("status"), data.get("actor") or "agent")
    return {"changed": changed}


@app.patch("/v2/conversations/{cid}/state", dependencies=[Depends(gateway)])
async def change_state(cid: int, request: Request):
    """Change who handles a conversation, its campaign stage, who is waited on, or its reminders."""
    data = await read_data(request, 16384)
    with session(True, require_writer=True) as db:
        cid = canonical_conversation(db, cid)
        return agent_ledger.change_state(db, cid, data.get("changes"), data.get("actor"), data.get("reason", ""))


@app.get("/v2/ledger/needs-owner", dependencies=[Depends(gateway)])
def needs_owner():
    """What waits on the mailbox owner: open requests from agents and sends still inside their veto window."""
    with session() as db:
        items = agent_ledger.needs_owner(db)
        for item in items:
            item["data"].pop("text", None)
        return {"items": items}


@app.get("/v2/precedents", dependencies=[Depends(gateway)])
def precedents(conversation: int = 0, q: str = "", tag: str = "", limit: int = 5):
    """Closed conversations with a sent answer, most similar first."""
    if not conversation and not q.strip():
        raise store.Invalid("Give a conversation or a question")
    if len(q) > 32000 or not 1 <= limit <= 10:
        raise store.Invalid("Precedent parameter outside bounds")
    with session() as db:
        return agent_context.precedents(db, q, conversation or None, tag or None, limit)


@app.post("/v2/precedents", dependencies=[Depends(gateway)])
def precedents_for_question(q: Annotated[str, Body(min_length=1, max_length=32000)],
                            tag: Annotated[str, Body(max_length=80)] = ""):
    """The same read, with private search terms kept out of request URLs."""
    return precedents(q=q, tag=tag)


@app.get("/v2/desk", dependencies=[Depends(gateway)])
def review_desk(p: int = 1):
    if not 1 <= p <= 1_000_000:
        raise store.Invalid("Invalid desk page")
    day = datetime.now(timezone.utc).date()
    with operator_ui.mailbox_reader() as db:
        groups = operator_views.review_groups(db, p, day)
    base = os.environ.get("INBOX_BASE_URL", "").rstrip("/")
    held, handled = [], []
    if groups is not None:
        for item in groups["held"]:
            data = item["data"]
            held.append({
                "conversation": item["conversation"], "number": item["number"], "subject": item["subject"],
                "customer": item["customer"], "kind": data.get("mode") or item["kind"],
                "reason": data.get("why") or data.get("action") or item["lead"] or item["title"],
                "confidence": data.get("confidence"), "blockers": data.get("blockers") or [],
                "waiting_since": item["waiting_since"], "waiting": operator_ui.age(item["waiting_since"]),
                "url": f"{base}/conversation/{item['conversation']}",
            })
        handled = [{key: item[key] for key in ("conversation", "number", "subject", "at", "actor", "action")}
                   for item in groups["handled"]]
    return {"day": day.isoformat(), "timezone": "UTC", "page": p, "held": held, "handled": handled,
            "counts": {"held": len(held), "handled": len(handled)},
            "held_more": bool(groups and groups["held_more"]), "handled_more": bool(groups and groups["handled_more"]),
            "unavailable_reason": "ledger_unavailable" if groups is None else None}


@app.get("/v2/insights", dependencies=[Depends(gateway)])
def support_insights(months: int = 12):
    if months not in insights.PERIODS:
        raise store.Invalid("Choose a period of 3, 6, 12 or 24 months")
    with session() as db:
        return insights.cached(db, months)


def created(path, resource):
    base = os.environ["INBOX_BASE_URL"].rstrip("/")
    return Response(
        status_code=201,
        headers={
            "Location": f"{base}/v2/{path}/{resource}",
            "Resource-Id": str(resource),
        },
    )


def permit_send(db, cid, tid=None):
    sql = "SELECT thread_id FROM mail_outbox WHERE conversation_id=? AND state='queued'"
    ids = [row[0] for row in db.execute(sql, (cid,)) if tid is None or row[0] == tid]
    if ids and os.environ.get("INBOX_SEND_ENABLED") != "1":
        raise HTTPException(503, "Mail transport is disabled")
    return ids


async def await_delivery(ids):
    if not ids:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with session() as db:
            states = [
                db.execute(
                    "SELECT state FROM mail_outbox WHERE thread_id=?", (tid,)
                ).fetchone()[0]
                for tid in ids
            ]
        if all(state == "sent" for state in states):
            return
        if any(state in ("failed", "cancelled") for state in states):
            raise HTTPException(502, "Mail transport rejected delivery")
        await asyncio.sleep(0.15)
    # Cancel any unclaimed work in the same SQLite write transaction as the check.
    # A claimed SMTP operation can be uncertain. Keep its durable receipt for reconciliation.
    with session(True) as db:
        if all(
            db.execute(
                "SELECT state FROM mail_outbox WHERE thread_id=?", (tid,)
            ).fetchone()[0]
            == "sent"
            for tid in ids
        ):
            return
        db.executemany(
            "UPDATE mail_outbox SET state='cancelled' WHERE thread_id=? AND state='queued'",
            ((tid,) for tid in ids),
        )
    raise HTTPException(
        504,
        "Mail acceptance unconfirmed; reconcile the transport receipt",
        headers={"X-Inbox-Delivery-State": "uncertain"},
    )


@app.post("/v2/conversations")
async def create(request: Request, role=Depends(identity)):
    data = await read_data(request, 40 * 1024 * 1024)
    if role == "intake":
        threads = data.get("threads")
        if (
            not isinstance(threads, list)
            or len(threads) != 1
            or not isinstance(threads[0], dict)
            or threads[0].get("type") != "customer"
            or data.get("imported")
            or str(data.get("mailboxId"))
            != str(company.MAILBOX)
        ):
            raise HTTPException(403, "Customer intake only")
    with session(True,require_writer=True) as db:
        cid = store.create_conversation(db, data, owner=role)
        ids = permit_send(db, cid)
    await await_delivery(ids)
    return created("conversations", cid)


@app.post("/v2/conversations/{cid}/notes")
async def note(cid: int, request: Request, role=Depends(identity)):
    data = await read_data(request, 10_000_000)
    with session(True,require_writer=True) as db:
        if (
            role == "intake"
            and not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND owner='intake'", (cid,)
            ).fetchone()
        ):
            raise HTTPException(403, "Conversation belongs to another caller")
        if role != "gateway":
            data.pop("event", None)  # Only the agents' own credential states what a note is.
        tid = store.append_thread(db, cid, "note", data)
    return created(f"conversations/{cid}/threads", tid)


@app.post("/v2/conversations/{cid}/reply", dependencies=[Depends(gateway)])
async def reply(cid: int, request: Request):
    data = await read_data(request, 40 * 1024 * 1024)
    with session(True,require_writer=True) as db:
        tid = store.append_thread(db, cid, "reply", data)
        ids = permit_send(db, cid, tid)
    await await_delivery(ids)
    return created(f"conversations/{cid}/threads", tid)


@app.patch("/v2/conversations/{cid}/threads/{tid}", dependencies=[Depends(gateway)])
async def edit_thread(cid: int, tid: int, request: Request):
    data = await read_data(request, 10_000_000)
    if data.get("op") != "replace" or data.get("path") != "/text":
        raise store.Invalid("Unsupported thread update")
    with session(True,require_writer=True) as db:
        t = store.thread(db, cid, tid)
        if t.get("state") != "draft" and not (
            t.get("type") == "note" and (t.get("source") or {}).get("type") == "api"
        ):
            raise store.Conflict("Only drafts and API notes can be edited")
        t["body"] = store.text(data.get("value"), "thread text", 10_000_000)
        t["modifiedAt"] = store.now()
        declared = (t.get("type") == "note" and isinstance(data.get("event"), dict)
                    and data["event"].get("kind") is not None)
        if declared:
            t["event"] = data["event"]  # The writer says what the edited note now is.
        elif isinstance(t.get("event"), dict) and t["event"].get("kind") is not None:
            # Edited by someone who did not restate it: keep who wrote it, read the new wording.
            t["event"] = {"actor": t["event"].get("actor")}
        db.execute("UPDATE threads SET data=? WHERE id=?", (store.encode(t), tid))
        store.update_conversation(db, cid, {})
        if t.get("type") == "note":
            store.ledger_note(db, cid, tid, strict=declared, edited=True)
    return Response(status_code=204)


@app.patch("/v2/conversations/{cid}", dependencies=[Depends(gateway)])
async def edit_conversation(cid: int, request: Request):
    data = await read_data(request, 8192)
    if data.get("op") != "replace" or data.get("path") not in (
        "/status",
        "/subject",
        "/assignTo",
    ):
        raise store.Invalid("Unsupported conversation update")
    field = data["path"][1:]
    value = data.get("value")
    with session(True,require_writer=True) as db:
        if field == "assignTo":
            changes = {
                "assignee": {"id": store.identifier(value), "type": "user"}
                if value
                else None
            }
        elif field == "status":
            changes = {"status": store.status(value)}
        else:
            changes = {"subject": store.text(value, "subject", 2000)}
        store.update_conversation(db, cid, changes)
        if field in ("assignTo", "status"):
            store.emit(
                db, cid, "convo.assigned" if field == "assignTo" else "convo.status"
            )
    return Response(status_code=204)


@app.put("/v2/conversations/{cid}/tags", dependencies=[Depends(gateway)])
async def replace_tags(cid: int, request: Request):
    data = await read_data(request, 65536)
    with session(True,require_writer=True) as db:
        tags = store.tag_list(data.get("tags"))
        current = agent_ledger.tags_of(store.conversation(db, cid))
        kept = [t for t in current if t.lower() == company.IGNORE_TAG]
        if kept and not any(str(t.get("tag", "")).lower() == company.IGNORE_TAG for t in tags):
            # An agent that read the tags before the owner took the conversation over must not undo
            # that by writing its older list back. Only the owner hands a conversation back.
            tags = tags + store.tag_list(kept)
        store.update_conversation(db, cid, {"tags": tags})
        store.emit(db, cid, "convo.tags")
    return Response(status_code=204)


@app.delete("/v2/conversations/{cid}", dependencies=[Depends(gateway)])
def discard(cid: int):
    with session(True,require_writer=True) as db:
        store.conversation(db, cid)
        threads = [t for t in store.thread_list(db, cid) if t.get("type") != "lineitem"]
        if not threads or any(
            t.get("state") != "draft" or t.get("type") != "message" for t in threads
        ):
            raise store.Conflict("Only an unsent draft conversation can be discarded")
        db.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return Response(status_code=204)


@app.get("/v2/customers/{key}", dependencies=[Depends(gateway)])
def get_customer(key: int):
    with session() as db:
        return store.load(db, "SELECT data FROM customers WHERE id=?", (key,))


@app.get("/v2/mailboxes/{key}", dependencies=[Depends(gateway)])
def get_mailbox(key: int):
    with session() as db:
        return store.load(db, "SELECT data FROM mailboxes WHERE id=?", (key,))


def profiles(table, page):
    page = positive(page, 10_000_000)
    with session() as db:
        rows = db.execute(
            f"SELECT id,data FROM {table} ORDER BY id LIMIT 50 OFFSET ?",
            ((page - 1) * 50,),
        )
        result = []
        for row in rows:
            item = json.loads(row["data"])
            if table == "webhooks":
                item.pop("secret", None)
                item["id"] = row["id"]
            result.append(item)
        total = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return page_response(table, result, total, page, 50)


@app.get("/v2/mailboxes", dependencies=[Depends(gateway)])
def list_mailboxes(page: int = 1):
    return profiles("mailboxes", page)


@app.get("/v2/customers", dependencies=[Depends(gateway)])
def list_customers(page: int = 1, email: str = ""):
    if not email:
        return profiles("customers", page)
    with session() as db:
        keys = list(
            db.execute(
                "SELECT customer_id FROM customer_emails WHERE email=?", (email,)
            )
        )
        items = [
            store.load(db, "SELECT data FROM customers WHERE id=?", (r[0],))
            for r in keys
        ]
    page = positive(page, 10_000_000)
    return page_response(
        "customers", items[(page - 1) * 50 : page * 50], len(items), page, 50
    )


@app.get("/v2/webhooks", dependencies=[Depends(gateway)])
def hooks(page: int = 1):
    return profiles("webhooks", page)


async def hook_data(request):
    data = await read_data(request, 32768)
    allowed = os.environ.get("INBOX_WEBHOOK_URLS", "").split()
    url = data.get("url")
    if not url or url not in allowed:
        raise store.Invalid("Webhook URL is outside configured destinations")
    events = data.get("events")
    if (
        not isinstance(events, list)
        or not events
        or any(
            not isinstance(e, str) or not re.fullmatch(r"[a-z.]{1,80}", e)
            for e in events
        )
    ):
        raise store.Invalid("Invalid webhook subscriptions")
    secret = store.text(data.get("secret"), "webhook secret", 1024)
    state = data.get("state", "enabled")
    if state not in ("enabled", "disabled"):
        raise store.Invalid("Invalid webhook state")
    version = data.get("payloadVersion", "V2")
    if version not in ("V2", "V3") or type(data.get("notification", False)) is not bool:
        raise store.Invalid("Invalid webhook format")
    mailboxes = data.get("mailboxIds", [])
    if not isinstance(mailboxes, list):
        raise store.Invalid("Invalid mailbox filter")
    return {
        "url": url,
        "events": list(dict.fromkeys(events)),
        "secret": secret,
        "state": state,
        "payloadVersion": version,
        "label": data.get("label"),
        "notification": data.get("notification", False),
        "mailboxIds": [store.identifier(x) for x in mailboxes],
    }


@app.post("/v2/webhooks", dependencies=[Depends(gateway)])
async def create_hook(request: Request):
    data = await hook_data(request)
    with session(True) as db:
        key = db.execute(
            "INSERT INTO webhooks(data) VALUES(?)", (store.encode(data),)
        ).lastrowid
    return created("webhooks", key)


@app.put("/v2/webhooks/{key}", dependencies=[Depends(gateway)])
async def update_hook(key: int, request: Request):
    data = await hook_data(request)
    with session(True) as db:
        if not db.execute(
            "UPDATE webhooks SET data=? WHERE id=?", (store.encode(data), key)
        ).rowcount:
            raise store.Missing("Webhook not found")
    return Response(status_code=204)


app.include_router(native_workflows.routes(gateway, lambda ids: await_delivery(ids)))
