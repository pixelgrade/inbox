"""Private operator inbox with shared account and owner recovery sign-in."""
import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import time
import threading
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
import nh3

import store
import company
import operator_account
import standard_folders
import asset_library
import mailbox_settings
import native_workflows

router = APIRouter()
VIEWS = Path(__file__).parent / "views"
TEMPLATES = Environment(loader=FileSystemLoader(VIEWS),autoescape=select_autoescape(["html"]))
COOKIE = "__Host-pxg-inbox"
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; font-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; connect-src 'self'")
_account_router = None
_account_enabled = None


def register_account_lookup(routes, enabled):
    global _account_router, _account_enabled
    _account_router, _account_enabled = routes, enabled


def reset_account_lookup():
    """Clear process-wide registration between tests."""
    register_account_lookup(None, None)


def mount_account_lookup(app):
    if _account_router is not None:
        app.include_router(_account_router)



def ui_schema(db):
    import transport
    transport.schema(db)
    operator_account.schema(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS operator_creations(operation TEXT PRIMARY KEY, cid INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS operator_auth(digest TEXT PRIMARY KEY,kind TEXT,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS inline_assets(key TEXT PRIMARY KEY,mime TEXT,content BLOB);
        CREATE TABLE IF NOT EXISTS saved_replies(id INTEGER PRIMARY KEY,name TEXT NOT NULL,body TEXT NOT NULL);
    """)


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issue(db,kind,seconds):
    value = secrets.token_urlsafe(32)
    db.execute("DELETE FROM operator_auth WHERE expires<?",(int(time.time()),))
    db.execute("INSERT INTO operator_auth VALUES(?,?,?)",(fingerprint(value),kind,int(time.time())+seconds))
    return value


def signed_in(request,db):
    cookie = request.cookies.get(COOKIE,"")
    return bool(cookie and db.execute("SELECT 1 FROM operator_auth WHERE digest=? AND kind='session' AND expires>?",
                                      (fingerprint(cookie),int(time.time()))).fetchone())


def operator(request,db):
    if not signed_in(request,db):
        raise HTTPException(401,"Open the inbox and sign in")


def same_origin(request):
    if request.headers.get("origin") != os.getenv("INBOX_BASE_URL"):
        raise HTTPException(403,"Reload the inbox before submitting")


ASSETS = {
    "inbox.css": "text/css",
    "inbox.js": "application/javascript",
    "ibm-plex-sans-latin-400-normal.woff2": "font/woff2",
    "ibm-plex-sans-latin-500-normal.woff2": "font/woff2",
    "ibm-plex-sans-semibold.woff": "font/woff",
    "ibm-plex-mono-latin-400-normal.woff2": "font/woff2",
}
_fingerprints = {}


def asset_version(name):
    """A short content digest, so browsers keep a stylesheet or script until it changes."""
    path = VIEWS/name
    modified = path.stat().st_mtime_ns
    known = _fingerprints.get(name)
    if not known or known[0] != modified:
        known = _fingerprints[name] = (modified, hashlib.sha256(path.read_bytes()).hexdigest()[:16])
    return known[1]


def age(value, now=None):
    """Compact elapsed time for a list row: 12m, 5h, 3d."""
    try:
        then = datetime.fromisoformat(str(value).replace("Z","+00:00"))
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    minutes = max(0,int(((now or datetime.now(timezone.utc))-then).total_seconds()//60))
    return f"{minutes}m" if minutes < 60 else f"{minutes//60}h" if minutes < 2880 else f"{minutes//1440}d"


def page(template, **context):
    assets = {name.rsplit(".",1)[1]: f"/assets/{name}?v={asset_version(name)}" for name in ("inbox.css", "inbox.js")}
    return HTMLResponse(TEMPLATES.get_template(template).render(**company.template(),assets=assets,**context),
                        headers={"Content-Security-Policy":CSP,"Cache-Control":"private, no-store"})


def login_target(value):
    if re.fullmatch(r"/investigations(?:/[a-z0-9][a-z0-9.-]{0,79}/[a-f0-9]{32})?|/review|/insights|/triage", value):
        return value
    return value if re.fullmatch(r"/conversation/(?:draft/)?\d+(?:/\d+)?/?(?:#thread-\d+)?",value) else "/"


def login_page(request, db):
    enabled = db.execute("SELECT 1 FROM operator_password WHERE id=1").fetchone() is not None
    return page("login.html", next_path=login_target(request.url.path), password_enabled=enabled)


@router.get("/assets/{name}")
def asset(name:str,v:str=""):
    if name not in ASSETS:
        raise HTTPException(404)
    # Only the address carrying the current digest may be kept by the browser.
    lifetime = "private, max-age=31536000, immutable" if v and v==asset_version(name) else "private, no-store"
    return Response((VIEWS/name).read_bytes(), media_type=ASSETS[name],
                    headers={"Cache-Control":lifetime})


@router.get("/favicon.ico")
def favicon():
    return Response(status_code=204,headers={"Cache-Control":"private, max-age=86400"})


@router.post("/session")
async def login(request:Request):
    same_origin(request)
    form = await request.form()
    code = str(form.get("code",""))
    with closing(store.connect()) as db, store.transaction(db):
        consumed = db.execute("DELETE FROM operator_auth WHERE digest=? AND kind='login' AND expires>?",
                              (fingerprint(code),int(time.time()))).rowcount
        if not consumed:
            raise HTTPException(401,f"This sign-in code is invalid or expired. Run {company.LOGIN_COMMAND} for a new code.")
        session = issue(db,"session",30*86400)
    return session_response(session, str(form.get("next", "/")))


def session_response(session, target):
    result = RedirectResponse(login_target(target), status_code=303)
    result.set_cookie(COOKIE, session, secure=True, httponly=True, samesite="strict", max_age=30*86400, path="/")
    return result


@router.post("/session/password")
async def password_login(request: Request):
    same_origin(request)
    form = await request.form()
    email, password = str(form.get("email", "")), str(form.get("password", ""))
    target = login_target(str(form.get("next", "/")))
    peer = fingerprint(request.client.host if request.client else "local")
    with closing(store.connect()) as db, store.transaction(db):
        account = db.execute("SELECT * FROM operator_password WHERE id=1").fetchone()
        if account is None:
            raise HTTPException(404)
        wait = operator_account.reserve_attempt(db, peer)
    if wait:
        result = page("login.html", next_path=target, password_enabled=True, email=email[:254],
                      error="Too many sign-in attempts. Please try again in a few minutes.")
        result.status_code = 429
        result.headers["Retry-After"] = str(wait)
        return result
    if await run_in_threadpool(operator_account.verify, account, email, password):
        with closing(store.connect()) as db, store.transaction(db):
            # A credential rotation during verification must also invalidate this attempt.
            if db.execute("SELECT 1 FROM operator_password WHERE id=1 AND digest=?", (account["digest"],)).fetchone():
                db.execute("DELETE FROM operator_password_attempts WHERE peer=?", (peer,))
                return session_response(issue(db, "session", 30*86400), target)
    result = page("login.html", next_path=target, password_enabled=True, email=email[:254],
                  error="Email or password is incorrect.")
    result.status_code = 401
    return result


@router.post("/logout")
def logout(request:Request):
    same_origin(request)
    with closing(store.connect()) as db:
        db.execute("DELETE FROM operator_auth WHERE digest=?",(fingerprint(request.cookies.get(COOKIE,"")),))
    result = RedirectResponse("/",status_code=303)
    result.delete_cookie(COOKIE,path="/",secure=True,httponly=True,samesite="strict")
    return result


def attachments(thread):
    return (thread.get("_embedded") or {}).get("attachments") or thread.get("attachments") or []


RASTER_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def previewable(file):
    mime = (file.get("mimeType") or "").lower()
    name = (file.get("filename") or file.get("fileName") or "").lower()
    return mime in RASTER_TYPES or (
        mime in ("", "binary/octet-stream", "application/octet-stream")
        and name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
    )


def mail_cleaner(cid=None, cids=None):
    def attribute(tag,name,value):
        if tag=="img" and name=="src":
            if cids and value.startswith("cid:") and value[4:] in cids:
                return f"/attachment/{cid}/{cids[value[4:]]}?image=1"
            parsed = urlsplit(value)
            if parsed.scheme=="https" and parsed.netloc and parsed.netloc==os.getenv("INBOX_ARCHIVED_INLINE_HOST") and parsed.path.startswith("/inline/"):
                return "/v2/inline/"+fingerprint(value)
            return None
        if tag=="a" and name=="href":
            parsed = urlsplit(value)
            if parsed.scheme not in ("http","https","mailto"):
                return None
            if parsed.netloc and parsed.netloc==os.getenv("INBOX_ARCHIVED_TICKET_HOST") and (link:=re.fullmatch(r"/conversation/(?:draft/)?(\d+)(?:/\d+)?/?",parsed.path)):
                return "/conversation/"+link[1]+("#"+parsed.fragment if parsed.fragment else "")
        return value
    return nh3.Cleaner(tags={
        "p","div","br","strong","b","em","i","u","ol","ul","li","pre","code","blockquote","a",
        "h1","h2","h3","h4","hr","table","thead","tbody","tr","td","th","img","span","s"},
        attributes={"a":{"href"},"img":{"src","alt"}},attribute_filter=attribute,
        url_schemes={"http","https","mailto","cid"},link_rel="noreferrer noopener")


# Most mail has no embedded images. Reuse its sanitizer without caching any customer content.
MAIL_CLEANER = mail_cleaner()


def body_html(thread, cid):
    cids = {str(a["contentId"]).strip("<>"): a["id"] for a in attachments(thread) if a.get("contentId")}
    cleaner = mail_cleaner(cid, cids) if cids else MAIL_CLEANER
    return Markup(cleaner.clean(str(thread.get("body") or "")))


def version(db,cid,conv=None):
    conv = conv or store.conversation(db,cid)
    latest = db.execute("SELECT count(*),max(id) FROM threads WHERE conversation_id=?",(cid,)).fetchone()
    return fingerprint(store.encode([conv.get("modifiedAt") or conv.get("userUpdatedAt"),*latest]))


_readers = threading.local()


@contextmanager
def mailbox_reader():
    """Each worker reuses SQLite's statement cache, with a fresh snapshot per request."""
    path = os.environ["INBOX_DB"]
    info = os.stat(path)
    identity = (path, info.st_dev, info.st_ino)
    if getattr(_readers, "identity", None) != identity:
        if hasattr(_readers, "db"):
            _readers.db.close()
        db = store.connect(path)
        db.execute("PRAGMA query_only=ON")
        _readers.db, _readers.identity = db, identity
    with store.transaction(_readers.db, False):
        yield _readers.db


def rail_counts(db):
    # Only the open slice needs per-ticket state. Count the archive from its status index.
    visible, published = standard_folders.VISIBLE, standard_folders.PUBLISHED
    assignee = standard_folders.ASSIGNEE
    counts = dict(db.execute(f"""
        SELECT count(*) FILTER (WHERE c.status='active') AS active,
               count(*) FILTER (WHERE c.status='pending') AS pending,
               count(*) FILTER (WHERE {published} AND {assignee}=:operator) AS mine,
               count(*) FILTER (WHERE {published} AND {assignee}<>:operator) AS assigned,
               count(*) FILTER (WHERE {published} AND {assignee} IS NULL) AS unassigned,
               count(*) FILTER (WHERE {standard_folders.DRAFT}) AS drafts
        FROM conversations c WHERE {standard_folders.OPEN} AND {visible}
    """, {"operator": int(os.getenv("INBOX_AGENT_ID", "1"))}).fetchone())
    for status in ("closed", "spam"):
        counts[status] = db.execute("SELECT count(*) FROM conversations WHERE status=?", (status,)).fetchone()[0]
    snoozed = list(db.execute("""SELECT c.status,s.regardless FROM snoozes s
                                 JOIN conversations c ON c.id=s.conversation"""))
    counts["later"] = len(snoozed)
    for status, regardless in snoozed:
        if status in ("closed", "spam") and not regardless:
            counts[status] -= 1
    return counts


@router.get("/")
def inbox(request:Request,q:str="",status:str="active",p:int=1,folder:int=0):
    from app import add_query
    if status not in ("all","active","pending","closed","spam","drafts","mine","later","assigned","unassigned") or not 1<=p<=1_000_000:
        raise HTTPException(400,"Invalid inbox filter")
    with mailbox_reader() as db:
        if not signed_in(request,db):
            return login_page(request, db)
        writes=store.writer_live(db) and os.getenv("INBOX_OPERATOR_WRITES")=="1"
        clauses, params=[],[]
        if folder:
            clauses.append(standard_folders.VISIBLE+" AND EXISTS(SELECT 1 FROM native_folder_members m WHERE m.conversation=c.id AND m.folder=?)")
            params.append(folder)
        if status in standard_folders.UI_TYPES:
            expr, values = standard_folders.predicate(standard_folders.UI_TYPES[status])
            clauses.append(expr)
            params.extend(values)
        elif status!="all":
            clauses.append(standard_folders.VISIBLE+" AND c.status=?")
            params.append(status)
        add_query(q,clauses,params,db)
        where = " WHERE "+" AND ".join(clauses) if clauses else ""
        from operator_views import review_groups
        groups = review_groups(db, p) if not (q or folder or "status" in request.query_params) else None
        found=[] if groups is not None else db.execute("SELECT data FROM conversations c"+where+" ORDER BY c.modified DESC,c.id DESC LIMIT 41 OFFSET ?",
                         [*params,(p-1)*40]).fetchall()
        # Drafts are looked up for the open conversations on this page only. Asked inside the
        # list query, the check ran for every match before sorting: 22 seconds on a broad search.
        shown=[json.loads(r[0]) for r in found[:40]]
        open_ids=[c["id"] for c in shown if c.get("status") in ("active","pending")]
        drafted=set()
        if open_ids:
            drafted={r[0] for r in db.execute(
                "SELECT DISTINCT conversation_id FROM threads WHERE conversation_id IN ("+",".join("?"*len(open_ids))+
                ") AND json_extract(data,'$.state')='draft' AND json_extract(data,'$.type')='message'",open_ids)}
        folders=list(db.execute("SELECT id,name FROM native_folders ORDER BY name"))
        counts = rail_counts(db)
        from operations import counts as issue_counts
        delivery_issues=sum(issue_counts(db).values())
    rows=[]
    for row in shown:
        waiting=(row.get("customerWaitingSince") or {}).get("time") if row.get("status")=="active" else None
        # A draft left on a closed conversation is history; only open ones await a decision.
        state = "draft" if row["id"] in drafted else "later" if status == "later" else row["status"]
        row["view"]={"state":state, "state_label":"Draft ready" if state=="draft" else state.title(),
                     "preview":(row.get("preview") or "")[:170], "draft":row["id"] in drafted,"waiting":age(waiting) if waiting else "",
                     "name":" ".join(filter(None,((row.get("primaryCustomer") or {}).get("first"),(row.get("primaryCustomer") or {}).get("last")))),
                     "when":row.get("modifiedAt") or row.get("userUpdatedAt") or row.get("createdAt") or ""}
        rows.append(row)
    return page("inbox.html",groups=groups,send=os.getenv("INBOX_SEND_ENABLED")=="1",delivery_issues=delivery_issues,rows=rows,q=q,status=status,p=p,counts=counts,folders=folders,folder=folder,writes=writes,
                previous="?"+urlencode(dict(q=q,status=status,p=p-1,folder=folder)),more="?"+urlencode(dict(q=q,status=status,p=p+1,folder=folder)) if len(found)>40 else None)


@router.get("/operations")
def operations_view(request:Request):
    import operations
    with closing(store.connect()) as db:
        if not signed_in(request,db):
            return login_page(request, db)
        deferred=db.execute("""SELECT a.cid, strftime('%Y-%m-%d %H:%M UTC',max(w.expires),'unixepoch') AS until FROM acknowledgments a
            JOIN source_ack_window w ON w.address IN (a.address,'*') WHERE a.state='deferred_cutover'
            GROUP BY a.cid ORDER BY a.queued""").fetchall()
        return page("operations.html", issues=operations.issues(db),deferred=deferred)


def pipeline_name(db):
    configured = os.getenv("INBOX_PIPELINE_NAME")
    if configured:
        return configured
    names = db.execute("SELECT DISTINCT name FROM desk_agent_presence LIMIT 2").fetchall()
    return names[0][0] if len(names) == 1 else "Agent pipeline"


def ledger_owner_key():
    """Use the generic owner key when available, including ledgers that accept a historical alias."""
    from agent_ledger import SETTABLE_WAITING

    if "owner" in SETTABLE_WAITING:
        return "owner"
    # Older ledgers expose the owner under the sole operator-specific waiting key.
    owner, = set(SETTABLE_WAITING) - {"customer", "blocked"}
    return owner


def thread_actor(t, conv, pipeline, operator_name):
    from agent_context import person
    author = t.get("createdBy") or {}
    kind = t.get("type")
    source = (t.get("source") or {}).get("type")
    if kind == "lineitem" or source in ("manual-workflow", "workflows", "automatic-workflow"):
        return "rule", "Workflow" if kind != "lineitem" else "Activity"
    if kind == "customer" or author.get("type") == "customer":
        return "customer", person(author) or person(conv.get("primaryCustomer")) or "Customer"
    if source == "api":
        return "agent", pipeline
    return "operator", person(author) or operator_name


RICH_DRAFT = nh3.Cleaner(
    tags={"p", "br", "div", "a", "strong", "b", "em", "i", "u", "ol", "ul", "li", "blockquote", "code"},
    attributes={"a": {"href"}}, url_schemes={"https", "http", "mailto"}, link_rel=None,
)


def editable_markup(body):
    # Open a formatted editor only when it can represent the entire saved message.
    cleaned = RICH_DRAFT.clean(body)
    return Markup(cleaned) if cleaned == body else None


def draft_text(body):
    """Only offer the text editor when paragraphs and line breaks are all it must preserve."""
    if re.search(r"<(?:/?[a-z]|[!?])", re.sub(r"</?(?:p|div)\s*>|<br\s*/?>", "", body, flags=re.I), re.I):
        return None
    text = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    text = re.sub(r"</(?:p|div)\s*>\s*", "\n\n", text, flags=re.I)
    return html.unescape(re.sub(r"<(?:p|div)\s*>", "", text, flags=re.I)).strip()


@router.get("/conversation/draft/{cid}/{number}")
@router.get("/conversation/{cid}")
@router.get("/conversation/{cid}/{number}")
def detail(request:Request,cid:int,number:int|None=None):
    with mailbox_reader() as db:
        if not signed_in(request,db):
            return login_page(request, db)
        alias=db.execute("SELECT target_id FROM conversation_aliases WHERE source_id=?",(cid,)).fetchone()
        if alias:
            return RedirectResponse(f"/conversation/{alias[0]}",status_code=301)
        conv=store.conversation(db,cid)
        rows=db.execute("""SELECT t.data, o.state AS delivery FROM threads t
            LEFT JOIN mail_outbox o ON o.thread_id=CASE WHEN json_extract(t.data,'$.type')='forwardparent'
                THEN coalesce(json_extract(t.data,'$.linkedThreadId'),t.id) ELSE t.id END
            WHERE t.conversation_id=? ORDER BY t.created,t.id""", (cid,))
        threads=[]
        draft = newest_customer = None
        pipeline = pipeline_name(db)
        operator_name = company.template()["agent_name"]
        for row in rows:
            t = json.loads(row["data"])
            t["actor"], t["author"] = thread_actor(t, conv, pipeline, operator_name)
            t["initials"] = "".join(word[0] for word in t["author"].split()[:2]).upper()
            if t["actor"] == "customer":
                newest_customer = t
            if t.get("state") == "draft" and t.get("type") == "message":
                draft = t
                body = t.get("body") or ""
                t["draft_text"] = draft_text(body)
                t["draft_rich"] = editable_markup(body) if t["draft_text"] is None else None
            t["files"] = attachments(t)
            for file in t["files"]:
                file["previewable"] = previewable(file)
            if t.get("type") != "lineitem":
                t["safe_body"] = body_html(t,cid)
            t["recipients"] = {}
            for field in ("to","cc","bcc"):
                values = [v.get("email","") if isinstance(v,dict) else str(v) for v in t.get(field) or []]
                if any(values):
                    t["recipients"][field] = ", ".join(filter(None,values))
            t["display"]=("Draft" if t.get("state")=="draft" else "Awaiting delivery" if t.get("state")=="pending" else
                          {"message":"Sent reply","customer":"Customer","note":"Internal note","lineitem":"Activity","forwardparent":"Forward","forwardchild":"Forwarded message"}.get(t.get("type"),str(t.get("type") or "Activity")))
            if t.get("state")=="bounced":
                t["display"]="Bounced reply"
            elif row["delivery"] and row["delivery"]!="sent":
                t["display"]={"queued":"Queued for delivery","submitting":"Submitting to mail provider","failed":"Delivery rejected","uncertain":"Delivery unconfirmed","cancelled":"Delivery cancelled"}.get(row["delivery"],row["delivery"])
            threads.append(t)
        manual=list(db.execute("SELECT id,json_extract(data,'$.name') name FROM native_rules WHERE enabled=1 AND kind='Manual' AND mailbox=? ORDER BY priority",(conv["mailboxId"],)))
        from workbench import draft_requests
        agents = draft_requests.agents(db)
        requested = draft_requests.latest(db, cid)
        import agent_context
        past = agent_context.history(db,(conv.get("primaryCustomer") or {}).get("id"),cid)
        import intake_triage
        intake = intake_triage.for_conversation(db,cid)
        wrong_rule = next((r["id"] for r in manual if "wrong address" in (r["name"] or "").lower()),None)
        if intake:
            # Intake is an event at assessment time, including when it follows an older exchange.
            at = intake["assessed"]
            index = next((i for i,t in enumerate(threads) if (t.get("createdAt") or "") > at), len(threads))
            threads.insert(index, {"type": "intake", "createdAt": at, "actor": "rule"})
        import agent_ledger
        try:  # The correspondence must open even if a record cannot be shown.
            ledger = agent_ledger.view(db,cid,conv) if agent_ledger.ready(db) else None
        except Exception:
            import logging
            logging.getLogger("inbox.ledger").exception("No agent records on the page of %s",cid)
            ledger = None
        held = None
        if ledger and draft and ledger["state"]["last_decision"]:
            from operator_views import held_decisions
            row = held_decisions(db,cid,1).fetchone()
            if row and row["draft_id"]==draft["id"]:
                held = agent_ledger.present(agent_ledger.shape(row))
        if ledger:
            for thread in threads:
                event = ledger["by_note"].get(thread.get("id"))
                if event and event["kind"] != "instruction":
                    thread["card"] = event
            threads.extend({"type": "record", "card": event, "actor": "agent", "createdAt": event["at"],
                            "initials": "".join(word[0] for word in event["who"].split()[:2]).upper()}
                           for event in ledger["standalone"] if not held or event["id"] != held["id"])
            threads.sort(key=lambda thread: thread.get("createdAt") or "")
        if held:
            threads.remove(draft)
            threads.append(draft)
        landing = draft or newest_customer
        return page("thread.html",held=held,ledger=ledger,owner_key=ledger_owner_key(),draft=draft,landing=landing["id"] if landing else None,past=past,intake=intake,wrong_rule=wrong_rule,has_shop=bool(_account_enabled and _account_enabled()),conv=conv,threads=threads,v=version(db,cid,conv),operator_id=int(os.getenv("INBOX_AGENT_ID","1")),agents=agents,requested=requested,manual=manual,
                    writes=store.writer_live(db) and os.getenv("INBOX_OPERATOR_WRITES")=="1",send=os.getenv("INBOX_SEND_ENABLED")=="1")


@router.get("/attachment/{cid}/{aid}")
def attachment(request:Request,cid:int,aid:int,image:int=0):
    with closing(store.connect()) as db:
        operator(request,db)
        content,mime=store.attachment(db,cid,aid)
        info=db.execute("""SELECT a.value FROM threads t,json_each(coalesce(json_extract(t.data,'$._embedded.attachments'),json_extract(t.data,'$.attachments'))) a
                           WHERE t.conversation_id=? AND json_extract(a.value,'$.id')=? LIMIT 1""",(cid,aid)).fetchone()
    meta=json.loads(info[0]) if info else {}
    name=meta.get("filename") or meta.get("fileName") or f"attachment-{aid}"
    inline = asset_library.image_mime(content) if image else None
    return Response(content,media_type=inline or "application/octet-stream",headers={
        "Content-Disposition":("inline" if inline else "attachment")+"; filename*=UTF-8''"+quote(str(name),safe=""),
        "Content-Security-Policy":"sandbox; default-src 'none'"})


@router.get("/v2/inline/{key}")
def inline(request:Request,key:str,download:bool=False):
    with closing(store.connect()) as db:
        if not signed_in(request,db):
            from app import identity
            if identity(request)!="gateway":
                raise HTTPException(403)
        row=db.execute("SELECT mime,content FROM inline_assets WHERE key=?",(key,)).fetchone()
        if not row:
            from asset_library import lookup
            content,mime=lookup("inline",0,key)
            row=(mime,content)
        headers = {"Content-Security-Policy": "sandbox; default-src 'none'",
                   "X-Content-Type-Options": "nosniff"}
        if download:
            headers["Content-Disposition"] = 'attachment; filename="original-file"'
            return Response(row[1], media_type="application/octet-stream", headers=headers)
        mime = asset_library.inline_mime(row[1])
        if not mime:
            raise HTTPException(415, "The archived file has no supported image signature")
        if mime == "image/svg+xml":
            headers["Content-Disposition"] = 'attachment; filename="image.svg"'
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
        return Response(row[1], media_type=mime, headers=headers)


async def composer_payload(form):
    body=store.text(form.get("body"),"message",1_000_000)
    if form.get("format")=="html":
        body=nh3.clean(body, tags={"p","div","br","a","b","strong","i","em","ul","ol","li","blockquote","pre","code","h2","h3"},
                       attributes={"a":{"href"}},url_schemes={"http","https","mailto"},link_rel=None)
    else:
        body="<p>"+html.escape(body).replace("\n","<br>")+"</p>"
    payload={"text":body}
    for field in ("cc","bcc"):
        values=[s.strip() for s in str(form.get(field,"")).split(",") if s.strip()]
        if values:
            payload[field]=[store.email(s) for s in values]
    files=[]
    size=0
    for upload in form.getlist("files"):
        if not isinstance(upload,UploadFile) or not upload.filename:
            continue
        data=await upload.read(16*1024**2+1)
        size+=len(data)
        if len(data)>16*1024**2 or size>24*1024**2:
            raise HTTPException(413,"Attachments exceed the message size limit")
        files.append({"fileName":upload.filename,"mimeType":upload.content_type or "application/octet-stream","data":base64.b64encode(data).decode()})
    if files:
        payload["attachments"]=files
    return payload


@router.post("/conversation/{cid}/edit")
async def edit(request:Request,cid:int):
    same_origin(request)
    if os.getenv("INBOX_OPERATOR_WRITES")!="1":
        raise HTTPException(409,"The mailbox is in migration preview")
    form=await request.form()
    action=str(form.get("action",""))
    with closing(store.connect()) as db,store.transaction(db):
        operator(request,db)
        store.require_writer(db)
        if form.get("version")!=version(db,cid):
            raise HTTPException(409,"This conversation changed. Reload before applying your edit.")
        if action == "request-draft":
            from workbench import draft_requests
            draft_requests.request(db, cid, form, version, await composer_payload(form))
        elif action in ("reply","draft","note"):
            if action=="reply" and os.getenv("INBOX_SEND_ENABLED")!="1":
                raise HTTPException(409,"Mail delivery is disabled")
            payload=await composer_payload(form)
            payload["draft"]=action=="draft"
            if action=="reply" and form.get("reply_status"):
                payload["status"]=store.status(form.get("reply_status"))
            store.append_thread(db,cid,"note" if action=="note" else "reply",payload,source="web")
        elif action in ("send-draft","edit-draft","discard-draft"):
            tid=store.identifier(form.get("thread"))
            t=store.thread(db,cid,tid)
            if t.get("state")!="draft" or t.get("type")!="message":
                raise store.Conflict("This message is no longer a draft")
            if action=="discard-draft":
                db.execute("DELETE FROM threads WHERE conversation_id=? AND id=?",(cid,tid))
                conv=store.conversation(db,cid)
                if conv.get("state")=="draft" and not any(t.get("type")!="lineitem" for t in store.thread_list(db,cid)):
                    db.execute("DELETE FROM conversations WHERE id=?",(cid,))
                    return RedirectResponse("/review" if form.get("next")=="review" else "/?status=drafts",status_code=303)
                store.update_conversation(db,cid,{"threads":max(0,int(conv["threads"])-1)})
            else:
                if action=="send-draft":
                    if os.getenv("INBOX_SEND_ENABLED")!="1":
                        raise HTTPException(409,"Mail delivery is disabled")
                    customer=t.get("customer") or store.conversation(db,cid)["primaryCustomer"]
                    t.update(customer=customer,to=t.get("to") or [store.email(customer["email"])],
                             state="pending",draft=False,createdAt=store.now(),status=store.conversation(db,cid)["status"])

                else:
                    if "draft_text" in form:
                        value = store.text(form["draft_text"], "draft text", 1_000_000).replace("\r\n", "\n")
                        if draft_text(t.get("body") or "") is None:
                            raise HTTPException(409, "This draft contains formatting. Open its source editor.")
                        paragraphs = html.escape(value).split("\n\n")
                        t["body"] = "".join("<p>" + part.replace("\n", "<br>") + "</p>" for part in paragraphs)
                    elif "draft_rich" in form:
                        if editable_markup(t.get("body") or "") is None:
                            raise HTTPException(409, "This draft needs its source editor.")
                        value = store.text(form["draft_rich"], "draft reply", 1_000_000)
                        t["body"] = RICH_DRAFT.clean(value)
                    else:
                        t["body"]=store.text(form.get("draft_body"),"draft HTML",1_000_000)
                    t["modifiedAt"]=store.now()
                db.execute("UPDATE threads SET data=? WHERE id=?",(store.encode(t),tid))
                if action=="send-draft":
                    store.queue_reply(db,cid,tid,operator=True,target_status=form.get("reply_status"))
                store.update_conversation(db,cid,{})
        elif action in ("snooze","unsnooze"):
            import snooze
            if action=="unsnooze":
                snooze.clear(db,cid,"Removed snooze")
            else:
                snooze.set_reminder(db,cid,str(form.get("until",""))+"+00:00",form.get("regardless")=="yes")
        elif action=="workflow":
            native_workflows.run_manual(db,cid,store.identifier(form.get("rule")),"operator-"+str(form["version"])+"-"+str(form["rule"]))
        elif action=="assign":
            assignee={"type":"user","id":int(os.getenv("INBOX_AGENT_ID","1"))} if form.get("assigned")=="me" else None
            store.update_conversation(db,cid,{"assignee":assignee})
            store.emit(db,cid,"convo.assigned")
        elif action=="status":
            store.update_conversation(db,cid,{"status":store.status(form.get("status"))})
            store.emit(db,cid,"convo.status")
        elif action in ("state","event-done","hold-send"):
            import agent_ledger
            if action=="state":
                current=agent_ledger.state(db,cid)
                changes={}
                for field in ("handling","stage","waiting_on","reminders"):
                    value=str(form.get(field,"")).strip()
                    known=(current["reminders"]["state"] if field=="reminders" else
                           (current["waiting_on"] if current["waiting_on"] in agent_ledger.SETTABLE_WAITING else "nobody") if field=="waiting_on" else current.get(field) or "")
                    if value and value!=known and not (field=="reminders" and known=="idle" and value=="on"):
                        changes[field]=value
                if changes.get("stage") in agent_ledger.ENDED:
                    # Closing a case from the page: its waiting state and reminders go with it.
                    changes.pop("waiting_on",None)
                    changes.pop("reminders",None)
                if changes:
                    agent_ledger.change_state(db,cid,changes,ledger_owner_key(),str(form.get("reason","")),owner=True)
            elif action=="event-done":
                agent_ledger.resolve(db,cid,store.identifier(form.get("event")),"done",ledger_owner_key())  # Requests only.
            else:
                key=store.identifier(form.get("event"))
                row=db.execute("SELECT title FROM agent_events WHERE id=? AND conversation=? AND kind='scheduled' AND status='open'",(key,cid)).fetchone()
                if not row:
                    raise store.Conflict("This send is no longer waiting")
                # A note written by a person is what holds a booked send.
                store.append_thread(db,cid,"note",{"text":f"Hold this send. Stopped by {html.escape(company.agent_name())} from the inbox: "+html.escape(row["title"]),
                    "event":{"kind":"control","actor":ledger_owner_key(),"title":f"{company.agent_name()} stopped the booked send","data":{"resolves":True,"event":key}}},source="web")
        elif action=="tags":
            tags=store.tag_list([s.strip() for s in str(form.get("tags","")).split(",") if s.strip()])
            store.update_conversation(db,cid,{"tags":tags})
            store.emit(db,cid,"convo.tags")
        else:
            raise HTTPException(400,"Unknown action")
    if form.get("composer") == "ticket" and action in ("note", "request-draft", "reply", "draft"):
        audience = "instruction" if action in ("note", "request-draft") else "customer-reply"
        return RedirectResponse(f"/conversation/{cid}?saved={audience}", status_code=303)
    return RedirectResponse("/review" if form.get("next")=="review" else f"/conversation/{cid}",status_code=303)


@router.get("/compose")
def compose(request:Request):
    with closing(store.connect()) as db:
        if not signed_in(request,db):
            return login_page(request, db)
    return page("compose.html",operation=secrets.token_urlsafe(24),send=os.getenv("INBOX_SEND_ENABLED")=="1")


@router.post("/compose")
async def create(request:Request):
    same_origin(request)
    if os.getenv("INBOX_OPERATOR_WRITES")!="1":
        raise HTTPException(409,"Inbox is in migration preview")
    form=await request.form()
    with closing(store.connect()) as db,store.transaction(db):
        operator(request,db)
        store.require_writer(db)
        operation=store.text(form.get("operation"),"operation",100)
        previous=db.execute("SELECT cid FROM operator_creations WHERE operation=?",(operation,)).fetchone()
        if previous:
            return RedirectResponse(f"/conversation/{previous[0]}",status_code=303)
        draft=form.get("action")=="draft"
        if not draft and (form.get("action")!="send" or os.getenv("INBOX_SEND_ENABLED")!="1"):
            raise HTTPException(409,"Mail delivery is disabled")
        customer={"email":store.email(form.get("email"))}
        thread=await composer_payload(form)
        thread.update(type="reply",customer=customer,draft=draft)
        cid=store.create_conversation(db,{"mailboxId":company.MAILBOX,"subject":form.get("subject"),"customer":customer,
                                          "status":"pending","threads":[thread]},source="web")
        db.execute("INSERT INTO operator_creations VALUES(?,?)",(operation,cid))
    return RedirectResponse(f"/conversation/{cid}",status_code=303)


@router.get("/read/{token}.gif")
def first_open(token:str):
    # Keep only the first open time used by the outbound mail report.
    with closing(store.connect()) as db:
        row=db.execute("SELECT t.id,t.conversation_id,t.data FROM open_receipts r JOIN threads t ON t.id=r.tid WHERE r.token=?",(token,)).fetchone()
        if row:
            thread=json.loads(row[2])
            if not thread.get("openedAt"):
                db.execute("UPDATE threads SET data=json_set(data,'$.openedAt',?) WHERE id=? AND json_extract(data,'$.openedAt') IS NULL",
                           (store.now(),row[0]))
    pixel=base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    return Response(pixel,media_type="image/gif",headers={"Cache-Control":"private, no-store"})


@router.get("/rating/{token}/{choice}")
def rating(token:str,choice:str):
    with closing(store.connect()) as db:
        found=db.execute("SELECT 1 FROM rating_links WHERE token=?",(token,)).fetchone()
        if not found or choice not in mailbox_settings.OPTIONS:
            raise HTTPException(404)
    return page("rating.html",next_path="",choice=choice,done=False)


@router.post("/rating/{token}/{choice}")
async def submit_rating(request:Request,token:str,choice:str):
    same_origin(request)
    if choice not in mailbox_settings.OPTIONS:
        raise HTTPException(404)
    form=await request.form()
    with closing(store.connect()) as db,store.transaction(db):
        row=db.execute("SELECT t.id,t.data,c.customer_id FROM rating_links r JOIN threads t ON t.id=r.tid JOIN conversations c ON c.id=t.conversation_id WHERE r.token=?",(token,)).fetchone()
        if not row:
            raise HTTPException(404)
        comment=str(form.get("comment",""))[:4000]
        db.execute("UPDATE rating_links SET choice=?,comment=?,changed=? WHERE token=?",(choice,comment,store.now(),token))
        thread=json.loads(row["data"])
        customer=(thread.get("customer") or {}).get("id") or row["customer_id"]
        thread["rating"]={"rating":choice,"comments":comment,"customerId":customer}
        db.execute("UPDATE threads SET data=? WHERE id=?",(store.encode(thread),row["id"]))
    return page("rating.html",next_path="",choice=choice,done=True)


if __name__=="__main__":
    import extensions
    extensions.load()
    with closing(store.connect()) as db:
        ui_schema(db)
        with store.transaction(db):
            print(issue(db,"login",300))
