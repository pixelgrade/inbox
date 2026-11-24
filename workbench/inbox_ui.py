"""Optional operator interface and mailbox adapter for a workbench installed with the inbox."""

import html
import os

import nh3
import secrets
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

import operator_ui as ui
import store
from . import adapters
from . import assignments, core, draft_requests
from .http import session

router = APIRouter()


@router.get("/investigations")
def overview(request: Request, product: str = "", cid: int = 0):
    with session() as db:
        if not ui.signed_in(request, db):
            return ui.login_page(request, db)
        products = [
            dict(r)
            for r in db.execute("SELECT id,name FROM desk_products ORDER BY name")
        ]
        pid = product or (products[0]["id"] if products else "")
        question = ""
        ticket_version = None
        if cid:
            conv = store.conversation(db, cid)
            ticket_version = ui.version(db, cid)
            question = conv["subject"]
            latest = db.execute(
                """SELECT json_extract(data,'$.body') FROM threads WHERE conversation_id=?
                   AND json_extract(data,'$.type')='customer' ORDER BY created DESC,id DESC LIMIT 1""",
                (cid,),
            ).fetchone()
            if latest and latest[0]:
                text = html.unescape(
                    nh3.clean(
                        latest[0], tags=set(), clean_content_tags={"script", "style"}
                    )
                )
                question = (question + "\n\n" + text.strip())[:32000]
        cases = db.execute(
            "SELECT id,question,state,ticket,created FROM desk_cases WHERE product=? ORDER BY created DESC,rowid DESC LIMIT 50",
            (pid,),
        ).fetchall()
        return ui.page(
            "cases.html",
            products=products,
            product=pid,
            cases=cases,
            cid=cid,
            question=question,
            ticket_version=ticket_version,
            op=secrets.token_hex(16),
        )


@router.post("/investigations")
async def open_case(request: Request):
    ui.same_origin(request)
    form = await request.form()
    pid = str(form.get("product", ""))
    with session(True) as db:
        ui.operator(request, db)
        cid = store.identifier(form["ticket"]) if form.get("ticket") else None
        version = ui.version(db, cid) if cid else None
        if cid and version != form.get("ticket_version"):
            raise core.Conflict(
                "The ticket changed. Reopen it to review the current customer message."
            )
        case = core.open_case(
            db, pid, form.get("question"), form.get("operation"), cid, version
        )
    return RedirectResponse(f"/investigations/{pid}/{case}", 303)


@router.get("/investigations/{pid}/{case}")
def case_page(pid: str, case: str, request: Request):
    with session() as db:
        if not ui.signed_in(request, db):
            return ui.login_page(request, db)
        detail = core.case_details(db, pid, case)
        return ui.page(
            "investigation.html",
            case=detail,
            pid=pid,
            ctx=detail["context"],
            assigned=assignments.status(db, pid, case),
            op=secrets.token_hex(16),
            research=adapters.research.status(db, case)
            if adapters.research and adapters.research.enabled(db, pid)
            and detail["ticket"]
            and detail["ticket_version"]
            else None,
            live=store.writer_live(db),
            draft_enabled=os.getenv("INBOX_OPERATOR_WRITES") == "1",
        )


@router.get("/investigations/{pid}/{case}/tests/{job}/patch")
def download_patch(pid: str, case: str, job: str, request: Request):
    with session() as db:
        ui.operator(request, db)
        change = core.test_patch(db, pid, case, job)
        return Response(
            change["patch"],
            media_type="text/x-diff",
            headers={
                "Content-Disposition": f'attachment; filename="candidate-{change["sha256"][:16]}.patch"',
                "Cache-Control": "private, no-store",
            },
        )


@router.post("/investigations/{pid}/{case}")
async def edit(pid: str, case: str, request: Request):
    ui.same_origin(request)
    form = await request.form()
    with session(True) as db:
        ui.operator(request, db)
        detail = core.case_details(db, pid, case)
        action = form.get("action")
        if action == "test":
            repo, _, recipe = str(form.get("recipe", "")).partition("/")
            core.request_test(
                db,
                pid,
                case,
                repo,
                recipe,
                str(form.get("patch", "")),
                form.get("operation"),
            )
        elif action == "assign":
            assignments.enqueue(db, pid, case, form.get("agent"))
        elif action == "research":
            if (
                not store.writer_live(db)
                or not adapters.research
                or not adapters.research.enabled(db, pid)
                or not detail["ticket"]
            ):
                raise core.Conflict(
                    "Ticket research is available when enabled on the active mailbox"
                )
            adapters.research.request_run(db, case)
        elif action == "propose":
            core.propose(db, pid, case, form.get("answer"), form.getlist("citation"))
        elif action == "draft":
            store.require_writer(db)
            if os.getenv("INBOX_OPERATOR_WRITES") != "1":
                raise core.Conflict("Operator mailbox writes are paused")
            if detail["state"] == "draft_saved":
                return RedirectResponse(f"/investigations/{pid}/{case}", 303)
            ticket = detail["ticket"]
            if not ticket or detail["state"] != "proposed":
                raise core.Conflict("A linked case with a proposal is required")
            if ui.version(db, ticket) != detail["ticket_version"]:
                raise core.Conflict(
                    "The conversation changed during the investigation. Review the current thread and open a fresh case."
                )
            body = "<p>" + html.escape(detail["proposal"]).replace("\n", "<br>") + "</p>"
            tid = draft_requests.apply_revision(db, detail, body)
            db.execute(
                "UPDATE desk_cases SET state='draft_saved',draft_thread=? WHERE id=?",
                (tid, case),
            )
            if form.get("next") == "ticket":
                return RedirectResponse(f"/conversation/{ticket}", 303)
        else:
            raise HTTPException(400, "Unknown investigation action")
    return RedirectResponse(f"/investigations/{pid}/{case}", 303)
