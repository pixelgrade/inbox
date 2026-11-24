"""HTTP tools for product agents and pull-based runners. Each installation belongs to one company."""

from contextlib import asynccontextmanager, contextmanager, closing
import os
import sqlite3

from fastapi import APIRouter, FastAPI, HTTPException, Request

from . import assignments, core


def connect():
    path = os.environ.get("DESK_DB") or os.environ.get("INBOX_DB")
    if not path:
        raise RuntimeError("Set DESK_DB to the company database")
    db = sqlite3.connect(path, isolation_level=None, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


@contextmanager
def session(write=False):
    with closing(connect()) as db:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


async def payload(req):
    import json

    raw = bytearray()
    async for chunk in req.stream():
        raw.extend(chunk)
        if len(raw) > 400000:
            raise HTTPException(413, "Request exceeds 400 KB")
    try:
        obj = json.loads(raw or b"{}")
    except (ValueError, RecursionError):
        raise HTTPException(400, "Invalid JSON object") from None
    if not isinstance(obj, dict):
        raise HTTPException(400, "Expected a JSON object")
    return obj


def principal(db, req, pid, role):
    scheme, _, token = req.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        raise HTTPException(401, "Use a product credential")
    row = db.execute(
        "SELECT product,role FROM desk_tokens WHERE digest=?", (core.sha(token),)
    ).fetchone()
    if not row:
        raise HTTPException(401, "Invalid product credential")
    if tuple(row) != (pid, role):
        raise HTTPException(403, "Credential is outside this product or role")


router = APIRouter(prefix="/agent/products/{pid}")


@router.get("")
def product(pid: str, request: Request):
    with session() as db:
        principal(db, request, pid, "agent")
        return core.product_info(db, pid)


@router.get("/context")
def context(pid: str, request: Request, q: str = ""):
    if len(q) > 32000:
        raise HTTPException(400, "Question exceeds 32000 characters")
    with session() as db:
        principal(db, request, pid, "agent")
        return core.context(db, pid, q)


@router.get("/document")
def document(pid: str, key: str, request: Request):
    with session() as db:
        principal(db, request, pid, "agent")
        return dict(
            core.one(
                db,
                "SELECT key,kind,title,source,revision,body FROM desk_docs WHERE product=? AND key=?",
                (pid, key),
            )
        )


@router.post("/cases")
async def create(pid: str, request: Request):
    data = await payload(request)
    with session(True) as db:
        principal(db, request, pid, "agent")
        return {
            "id": core.open_case(
                db, pid, data.get("question"), request.headers.get("Idempotency-Key")
            )
        }


@router.get("/cases/{cid}")
def detail(pid: str, cid: str, request: Request):
    with session() as db:
        principal(db, request, pid, "agent")
        return core.case_details(db, pid, cid)


@router.post("/cases/{cid}/sources")
async def cite(pid: str, cid: str, request: Request):
    data = await payload(request)
    with session(True) as db:
        principal(db, request, pid, "agent")
        core.cite(db, pid, cid, data.get("sources"), data.get("revisions"))
        return {"id": cid}


@router.post("/cases/{cid}/tests")
async def test_job(pid: str, cid: str, request: Request):
    data = await payload(request)
    with session(True) as db:
        principal(db, request, pid, "agent")
        return {
            "id": core.request_test(
                db,
                pid,
                cid,
                data.get("repo"),
                data.get("recipe"),
                data.get("patch", ""),
                request.headers.get("Idempotency-Key"),
            )
        }


@router.get("/cases/{cid}/tests/{job}/patch")
def patch(pid: str, cid: str, job: str, request: Request):
    with session() as db:
        principal(db, request, pid, "agent")
        return core.test_patch(db, pid, cid, job)


@router.post("/cases/{cid}/proposal")
async def proposal(pid: str, cid: str, request: Request):
    data = await payload(request)
    with session(True) as db:
        principal(db, request, pid, "agent")
        core.propose(db, pid, cid, data.get("answer"), data.get("citations"))
        return {"id": cid, "state": "proposed"}


@router.post("/investigator/claim")
async def agent_claim(pid: str, request: Request):
    data = await payload(request)
    with session(True) as db:
        principal(db, request, pid, "agent")
        return {"case": assignments.claim(db, pid, data.get("name"))}


@router.post("/cases/{cid}/investigator-result")
async def agent_result(pid: str, cid: str, request: Request):
    data = await payload(request)
    with session(True) as db:
        principal(db, request, pid, "agent")
        assignments.finish(db, pid, cid, data.get("lease"), data.get("state"))
        return {"id": cid}


@router.post("/runner/claim")
def claim(pid: str, request: Request):
    with session(True) as db:
        principal(db, request, pid, "runner")
        return {"job": core.claim(db, pid)}


@router.post("/runner/jobs/{job}/result")
async def result(pid: str, job: str, request: Request):
    data = await payload(request)
    with session(True) as db:
        principal(db, request, pid, "runner")
        core.finish(db, pid, job, data.get("lease"), data.get("result"))
        return {"id": job}


def mount(app):
    from fastapi.responses import JSONResponse

    def error(code):
        async def handle(request, exc):
            return JSONResponse(
                {"detail": str(exc)},
                status_code=code,
                headers={"Cache-Control": "private, no-store"},
            )

        return handle

    for typ, code in ((core.Invalid, 400), (core.Missing, 404), (core.Conflict, 409)):
        app.add_exception_handler(typ, error(code))
    app.include_router(router)


@asynccontextmanager
async def lifespan(app):
    import extensions
    extensions.load()
    with closing(connect()) as db:
        core.schema(db)
    yield


standalone = FastAPI(title="Support Workbench", lifespan=lifespan)
mount(standalone)
