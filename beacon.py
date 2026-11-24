"""Public contact form. Every successful submission creates one private inbound thread."""

import base64
from contextlib import closing
from hashlib import sha256
import hmac
from html import escape
import ipaddress
import json
import os
from pathlib import Path
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.datastructures import UploadFile

import store
import company

router = APIRouter()
PUBLIC = Path(__file__).with_name("public")
templates = Environment(
    loader=FileSystemLoader(PUBLIC), autoescape=select_autoescape(["html"])
)
FILE_LIMIT = 8 * 1024 * 1024
BODY_LIMIT = 12 * 1024 * 1024
IP_LIMIT = 8
CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS contact_settings(id INTEGER PRIMARY KEY CHECK(id=1), secret BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS contact_receipts(
            nonce TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, cid INTEGER NOT NULL, number INTEGER NOT NULL,
            ip_hash TEXT NOT NULL, email_hash TEXT NOT NULL, received INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS contact_time ON contact_receipts(received);
        CREATE INDEX IF NOT EXISTS contact_peer ON contact_receipts(ip_hash,received);
        CREATE INDEX IF NOT EXISTS contact_email ON contact_receipts(email_hash,received);
    """)
    db.execute(
        "INSERT OR IGNORE INTO contact_settings VALUES(1,?)", (secrets.token_bytes(32),)
    )


def secret(db):
    return db.execute("SELECT secret FROM contact_settings WHERE id=1").fetchone()[0]


def digest(key, context, value):
    return hmac.new(key, (context + "\0" + value).encode(), "sha256").hexdigest()


def encode_form(key):
    payload = f"{int(time.time())}.{secrets.token_hex(16)}"
    return payload + "." + digest(key, "form", payload)


def token_nonce(key, value):
    if not isinstance(value, str) or len(value) > 130:
        raise HTTPException(403, "Reload the form before sending.")
    parts = value.split(".")
    if len(parts) != 3 or not value.isascii() or not parts[0].isdigit():
        raise HTTPException(403, "Reload the form before sending.")
    if (
        not hmac.compare_digest(parts[2], digest(key, "form", ".".join(parts[:2])))
        or not 0 <= time.time() - int(parts[0]) < 86400
    ):
        raise HTTPException(403, "This form expired. Open a new form before sending.")
    return parts[1]


def page(**values):
    return HTMLResponse(
        templates.get_template("contact.html").render(**company.template(),**values),
        headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store"},
    )


@router.get("/contact")
def contact():
    if os.getenv("INBOX_PUBLIC_CONTACT_ENABLED") != "1":
        return page(offline=True)
    with closing(store.connect()) as db:
        if not store.writer_live(db):
            return page(offline=True)
        return page(token=encode_form(secret(db)))


@router.get("/contact/{asset}")
def static(asset: str):
    types = {
        "contact.css": "text/css",
        "contact.js": "application/javascript",
        "widget.js": "application/javascript",
        "widget.css": "text/css",
    }
    if asset not in types:
        raise HTTPException(404)
    return Response(
        (PUBLIC / asset).read_bytes(),
        media_type=types[asset],
        headers={"X-Content-Type-Options": "nosniff"},
    )


def peer(request):
    value = request.client.host if request.client else "unknown"
    try:
        ip = ipaddress.ip_address(value)
        return (
            str(ipaddress.ip_network((ip, 64), strict=False))
            if ip.version == 6
            else str(ip)
        )
    except ValueError:
        return value


async def bounded_form(request):
    raw = request.headers.get("content-length")
    if raw is not None and (
        not raw.isdecimal() or len(raw) > 12 or int(raw) > BODY_LIMIT
    ):
        raise HTTPException(
            413, "Attachments and message must fit within 12 MB together."
        )
    size = 0

    async def receive():
        nonlocal size
        event = await request.receive()
        if event["type"] == "http.request":
            size += len(event.get("body", b""))
            if size > BODY_LIMIT:
                raise HTTPException(
                    413, "Attachments and message must fit within 12 MB together."
                )
        return event

    return await Request(request.scope, receive).form(
        max_files=4, max_fields=8, max_part_size=256 * 1024
    )


def attachment_type(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


async def files_from(form):
    files = []
    for f in form.getlist("attachment"):
        if not isinstance(f, UploadFile):
            if f == "":
                continue
            raise HTTPException(400, "Choose a file to attach.")
        if not f.filename:
            continue
        filename = f.filename.replace("\\", "/").split("/")[-1]
        filename = "".join(x for x in filename if x.isprintable()).strip()
        if not filename or len(filename) > 255:
            raise HTTPException(400, "Use a shorter filename.")
        raw = await f.read(FILE_LIMIT + 1)
        if len(raw) > FILE_LIMIT:
            raise HTTPException(413, "Each attachment can be up to 8 MB.")
        files.append(
            {
                "fileName": filename,
                "mimeType": attachment_type(raw),
                "data": base64.b64encode(raw).decode(),
            }
        )
    return files


def accept(db, key, nonce, addr, customer, subject, body, files):
    now = int(time.time())
    args = [
        customer,
        subject,
        body,
        [
            (f["fileName"], f["mimeType"], sha256(f["data"].encode()).hexdigest())
            for f in files
        ],
    ]
    fingerprint = digest(key, "payload", json.dumps(args, ensure_ascii=False))
    prev = db.execute(
        "SELECT fingerprint,number FROM contact_receipts WHERE nonce=?", (nonce,)
    ).fetchone()
    if prev:
        if prev["fingerprint"] != fingerprint:
            raise HTTPException(
                409, "This form already sent a different message. Open a new form."
            )
        return prev["number"], False
    hashed_ip = digest(key, "peer", addr)
    hashed_email = digest(key, "email", customer["email"].casefold())
    db.execute("DELETE FROM contact_receipts WHERE received<?", (now - 2 * 86400,))
    recent = db.execute(
        "SELECT count(*) FROM contact_receipts WHERE ip_hash=? AND received>=?",
        (hashed_ip, now - 3600),
    ).fetchone()[0]
    sender = db.execute(
        "SELECT count(*) FROM contact_receipts WHERE email_hash=? AND received>=?",
        (hashed_email, now - 3600),
    ).fetchone()[0]
    if recent >= IP_LIMIT or sender >= 6:
        raise HTTPException(
            429,
            f"Please wait before sending another request, or email {company.ADDRESS}.",
            headers={"Retry-After": "3600"},
        )
    thread = {
        "type": "customer",
        "customer": customer,
        "text": "<p>" + escape(body).replace("\n", "<br>") + "</p>",
        "attachments": files,
    }
    cid = store.create_conversation(
        db,
        {
            "mailboxId": company.MAILBOX,
            "subject": subject,
            "customer": customer,
            "threads": [thread],
        },
        source="beacon-v2",
    )
    number = store.conversation(db, cid)["number"]
    db.execute(
        "INSERT INTO contact_receipts VALUES(?,?,?,?,?,?,?)",
        (nonce, fingerprint, cid, number, hashed_ip, hashed_email, now),
    )
    return number, True


@router.post("/contact")
async def submit(request: Request):
    if os.getenv("INBOX_PUBLIC_CONTACT_ENABLED") != "1":
        raise HTTPException(503, f"Please email {company.ADDRESS}.")
    origin = request.headers.get("origin")
    if origin and origin != os.getenv("INBOX_BASE_URL", "").rstrip("/"):
        raise HTTPException(403, "Open the support form directly before sending.")
    form = await bounded_form(request)
    try:
        with closing(store.connect()) as db:
            key = secret(db)
            nonce = token_nonce(key, form.get("token"))
            try:
                email = store.email(str(form.get("email", "")).strip())
                subject = store.text(
                    str(form.get("subject", "")).strip(), "subject", 200
                )
                body = store.text(
                    str(form.get("message", "")).strip(), "message", 30000
                )
                name = str(form.get("name", "")).strip()
                if len(name) > 120:
                    raise store.Invalid("Name is too long")
            except store.Invalid as exc:
                raise HTTPException(400, str(exc)) from exc
            first, _, last = name.partition(" ")
            customer = {"email": email, "firstName": first, "lastName": last}
            files = await files_from(form)
            with store.transaction(db):
                store.require_writer(db)
                number, created = accept(
                    db, key, nonce, peer(request), customer, subject, body, files
                )
    finally:
        await form.close()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"number": number}, status_code=201 if created else 200)
    return page(number=number)
