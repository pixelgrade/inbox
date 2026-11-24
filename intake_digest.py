"""A plain summary of what the intake check filed and flagged, posted into the mailbox for the operator.

    python intake_digest.py --days 7           # print the figures
    python intake_digest.py --days 7 --post    # also post the summary as an internal conversation

The posted summary is one internal note on a conversation assigned to the operator, so it shows
at the top of the open list. It is posted once per end date, sends no email and is ignored by
agents. It lists every filing with its reasons and puts first the ones a person should look at:
anything filed that is open again or that someone answered.
"""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import html
import json
import os

import company
import intake_triage
import store

LABELS = {
    "promotion": "Promotions",
    "site_mail": "Website-generated mail",
    "bounce": "Delivery failures",
    "auto_reply": "Automatic replies",
    "wrong_address": "Possibly meant for another business",
    "support": "Support",
}


def summary(db, days=7, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.execute(
        "SELECT t.conversation,t.category,t.confidence,t.reasons,t.filed,t.assessed,c.data "
        "FROM intake_triage t LEFT JOIN conversations c ON c.id=t.conversation "
        "WHERE t.assessed>=? AND t.assessed<=? ORDER BY t.assessed",
        (since, now.strftime("%Y-%m-%dT%H:%M:%S") + "Z"),
    ).fetchall()
    counts = dict.fromkeys(LABELS, 0)
    filed, flagged, look = [], [], []
    for cid, category, confidence, reasons, was_filed, assessed, data in rows:
        counts[category] = counts.get(category, 0) + 1
        if not data:
            continue
        conv = json.loads(data)
        tags = [t.get("tag") for t in conv.get("tags") or [] if isinstance(t, dict)]
        answered = db.execute(
            "SELECT 1 FROM threads WHERE conversation_id=? AND json_extract(data,'$.type')='message' "
            "AND coalesce(json_extract(data,'$.state'),'published')='published' LIMIT 1",
            (cid,),
        ).fetchone() is not None
        item = {
            "id": cid,
            "number": conv.get("number"),
            "subject": (conv.get("subject") or "(no subject)")[:90],
            "sender": ((conv.get("primaryCustomer") or {}).get("email") or "").rpartition("@")[2],
            "category": category,
            "reasons": json.loads(reasons)[:3],
            "status": conv.get("status"),
            "tags": tags,
            "answered": answered,
        }
        if was_filed:
            filed.append(item)
            # A filing someone reopened or answered was probably wrong: show it first.
            if conv.get("status") != "closed" or answered or "non-support" not in tags:
                look.append(item)
        elif category == "wrong_address":
            item["outcome"] = (
                "wrong-address reply sent" if intake_triage.ANSWERED_TAG in tags
                else "held for you" if company.HELD_TAG in tags
                else "answered as support" if answered
                else f"closed by {company.AUTOMATION_NAME}" if conv.get("status") == "closed"
                else "open"
            )
            flagged.append(item)
    return {
        "days": days,
        "from": since + "Z",
        "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checked": len(rows),
        "counts": counts,
        "filed": filed,
        "look": look,
        "flagged": flagged,
    }


def render(report):
    base = os.environ.get("INBOX_BASE_URL", "").rstrip("/")
    esc = html.escape

    def line(item, extra=""):
        return (
            f'<li><a href="{base}/conversation/{item["id"]}">#{item["number"]}</a> '
            f'{esc(LABELS.get(item["category"], item["category"]))} from {esc(item["sender"] or "unknown")}: '
            f'{esc(item["subject"])}{esc(extra)}<br>{esc("; ".join(item["reasons"]))}</li>'
        )

    parts = [
        f'<p><strong>Intake summary, {esc(report["from"][:10])} to {esc(report["to"][:10])}.</strong> '
        f'{report["checked"]} new messages were checked and {len(report["filed"])} were filed without a person.</p>',
        "<ul>" + "".join(
            f"<li>{esc(label)}: {report['counts'].get(key, 0)}</li>" for key, label in LABELS.items()
        ) + "</ul>",
    ]
    if report["look"]:
        parts.append("<p><strong>Worth a look.</strong> These were filed and are open again, were answered, or lost their filing tag:</p>")
        parts.append("<ul>" + "".join(line(i, f" ({i['status']})") for i in report["look"]) + "</ul>")
    else:
        parts.append("<p>No filed message was reopened or answered afterwards.</p>")
    if report["flagged"]:
        parts.append("<p><strong>Possibly meant for another business:</strong></p>")
        parts.append("<ul>" + "".join(line(i, f" ({i['outcome']})") for i in report["flagged"][:40]) + "</ul>")
    if report["filed"]:
        shown = report["filed"][:80]
        parts.append(f"<p><strong>Everything filed{' (first 80)' if len(report['filed']) > 80 else ''}:</strong></p>")
        parts.append("<ul>" + "".join(line(i) for i in shown) + "</ul>")
    parts.append(f'<p>All verdicts with their evidence: <a href="{base}/triage">Intake checks</a>.</p>')
    return "".join(parts)


def post(db, report):
    """Post the summary once per end date. Returns the conversation id."""
    store.require_writer(db)
    operation = "intake-summary-" + report["to"][:10]
    db.execute("CREATE TABLE IF NOT EXISTS operator_creations(operation TEXT PRIMARY KEY, cid INTEGER NOT NULL)")
    done = db.execute("SELECT cid FROM operator_creations WHERE operation=?", (operation,)).fetchone()
    if done:
        return done[0]
    cid = store.create_conversation(
        db,
        {
            "mailboxId": company.MAILBOX,
            "subject": f"Intake summary, {report['from'][:10]} to {report['to'][:10]}",
            "customer": {"email": company.ADDRESS},
            "status": "active",
            # Agents leave it alone, and it never counts as support or as a customer.
            "tags": [company.IGNORE_TAG, "non-support", "intake-summary"],
            "threads": [{"type": "note", "text": render(report)}],
        },
        source="system",
    )
    store.update_conversation(
        db, cid, {"assignee": {"id": int(os.environ.get("INBOX_AGENT_ID", "1")), "type": "user"}}
    )
    db.execute("INSERT INTO operator_creations VALUES(?,?)", (operation, cid))
    return cid


if __name__ == "__main__":
    import extensions
    extensions.load()
    cli = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cli.add_argument("--days", type=int, default=7)
    cli.add_argument("--post", action="store_true", help="post the summary into the mailbox")
    args = cli.parse_args()
    with closing(store.connect()) as mailbox:
        if args.post:
            with store.transaction(mailbox):
                result = summary(mailbox, args.days)
                conversation = post(mailbox, result)
            print(f"Posted intake summary as conversation {conversation}")
        else:
            result = summary(mailbox, args.days)
        print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}, indent=1))
