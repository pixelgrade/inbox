"""Ticket-scoped handoff from a private operator decision to a product agent."""
import secrets
import time

from . import assignments, core


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS desk_draft_requests(
            case_id TEXT PRIMARY KEY REFERENCES desk_cases, ticket INTEGER NOT NULL,
            target_thread INTEGER, target_body TEXT);
        CREATE INDEX IF NOT EXISTS desk_ticket_requests ON desk_draft_requests(ticket);
    """)


def agents(db):
    return db.execute("""
        SELECT p.id,p.name AS product,a.name AS agent FROM desk_agent_presence a
        JOIN desk_products p ON p.id=a.product WHERE a.seen>=? ORDER BY p.name
    """, (int(time.time()) - 120,)).fetchall()


def latest(db, ticket):
    return db.execute("""
        SELECT c.id,c.product,c.state,c.proposal,c.ticket_version,a.state AS assignment
        FROM desk_draft_requests r JOIN desk_cases c ON c.id=r.case_id
        JOIN desk_assignments a ON a.case_id=r.case_id
        WHERE r.ticket=? ORDER BY r.rowid DESC LIMIT 1
    """, (ticket,)).fetchone()


def request(db, ticket, form, version, note):
    import agent_context
    import store

    instruction = core.string(form.get("body"), 8000, "private instruction")
    product = form.get("product")
    agent = next((a for a in agents(db) if a["id"] == product), None)
    if agent is None:
        raise core.Conflict("The product agent is offline. Save your note or try again.")
    brief = agent_context.brief(db, ticket, budget=8000)
    target = None
    if form.get("thread"):
        target = store.thread(db, ticket, store.identifier(form["thread"]))
        if target["state"] != "draft" or target["type"] != "message":
            raise core.Conflict("This draft has changed. Reload the conversation.")
    history = "\n".join(f"{m['from']}: {m['text']}" for m in brief["messages"])
    draft = agent_context.plain(target["body"]) if target else ""
    question = (f"Prepare a customer reply for review. Operator instruction:\n{instruction}\n\n"
                f"Subject: {brief['subject']}\n\nCurrent draft:\n{draft[:9000]}\n\nConversation:\n{history}")[:32000]
    names = ", ".join(file["fileName"] for file in note.get("attachments", []))
    if names:
        question = (f"Attached to the private note (filenames only): {names[:1000]}\n" + question)[:32000]
    store.append_thread(db, ticket, "note", note, source="web")
    case = core.open_case(db, product, question, secrets.token_hex(16), ticket, version(db, ticket))
    db.execute("INSERT INTO desk_draft_requests VALUES(?,?,?,?)",
               (case, ticket, target["id"] if target else None, target["body"] if target else None))
    assignments.enqueue(db, product, case, agent["agent"])
    return case


def apply_revision(db, detail, text):
    import store

    row = db.execute("SELECT target_thread,target_body FROM desk_draft_requests WHERE case_id=?",
                     (detail["id"],)).fetchone()
    if not row or not row["target_thread"]:
        return store.append_thread(db, detail["ticket"], "reply", {"text": text, "draft": True}, source="web")
    thread = store.thread(db, detail["ticket"], row["target_thread"])
    if thread["state"] != "draft" or thread["body"] != row["target_body"]:
        raise core.Conflict("The draft changed. Review the current conversation.")
    thread.update(body=text, modifiedAt=store.now())
    db.execute("UPDATE threads SET data=? WHERE conversation_id=? AND id=?",
               (store.encode(thread), detail["ticket"], thread["id"]))
    store.update_conversation(db, detail["ticket"], {})
    return thread["id"]
