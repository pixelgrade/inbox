"""Operator pages built on the mailbox's own data: draft review, insights and similar answers."""

from contextlib import closing

from fastapi import APIRouter, HTTPException, Request

import agent_context
import insights
import operator_ui as ui
import standard_folders
import agent_ledger
import store

router = APIRouter()
HEAT = 4  # Shades above empty in the arrivals grid.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CHANNELS = {
    "api": "Agent pipeline",
    "web": "Written in the inbox",
    "mobile": "Mobile app",
    "email": "Email",
    "beacon-v2": "Website widget",
    "chat": "Chat",
    "unknown": "Other",
}


def hours(value):
    if value is None:
        return "–"
    if value < 1:
        return f"{round(value * 60)} min"
    if value < 48:
        return f"{value:.1f} h".replace(".0 h", " h")
    return f"{value / 24:.1f} d".replace(".0 d", " d")


def bars(values, width, height, gap=2):
    """Column geometry in SVG units; data ends are drawn from the baseline."""
    peak = max(values, default=0) or 1
    step = width / max(len(values), 1)
    return [
        {
            "x": round(i * step + gap / 2, 2),
            "w": round(max(step - gap, 0.6), 2),
            "h": round(v / peak * height, 2),
            "y": round(height - v / peak * height, 2),
            "value": v,
        }
        for i, v in enumerate(values)
    ]


def charts(report):
    history = report["history"]
    past = bars([h["count"] for h in history], 1000, 120, gap=1)
    years = []
    for index, item in enumerate(history):
        year = item["month"][:4]
        if not years or years[-1]["label"] != year:
            years.append({"label": year, "x": past[index]["x"]})
    # Keep year labels from colliding on a long archive.
    stride = max(1, len(years) // 14 + (len(years) % 14 > 0)) if len(years) > 14 else 1
    months = report["volume"]["months"]
    stacked = []
    parts = (("inbound", "s1"), ("outbound", "s2"), ("other", "s5"))
    peak = max((sum(m[name] for name, _ in parts) for m in months), default=0) or 1
    step = 1000 / max(len(months), 1)
    column = min(step - 8, 56)
    for index, m in enumerate(months):
        x = index * step + (step - column) / 2
        top = 170.0
        segments = []
        for name, slot in parts:
            height = m[name] / peak * 150
            if not height:
                continue
            # A two-unit surface gap separates the stacked parts.
            top -= height + (2 if segments else 0)
            segments.append({"slot": slot, "y": round(top, 1), "h": round(height, 1)})
        stacked.append(
            {
                "x": round(x, 1),
                "w": round(column, 1),
                "mid": round(x + column / 2, 1),
                "segments": segments,
                "top": round(top - 6, 1),
                "total": sum(m[name] for name, _ in parts),
                "label": m["month"][5:] + "/" + m["month"][2:4],
                "month": m["month"],
                "data": m,
            }
        )
    response = [m["first_response_hours"] or 0 for m in months]
    speed = bars(response, 1000, 90, gap=8)
    for bar, m, column_ in zip(speed, months, stacked):
        bar["y"] += 20  # Headroom for the value printed above the tallest column.
        bar.update(label=hours(m["first_response_hours"]), month=column_["label"], mid=round(bar["x"] + bar["w"] / 2, 1))
    grid = report["rhythm"]["grid"]
    peak_cell = report["rhythm"]["peak"] or 1
    heat = [
        [
            {"count": count, "shade": 0 if not count else 1 + min(HEAT - 1, int(count / peak_cell * HEAT - 1e-9))}
            for count in row
        ]
        for row in grid
    ]
    replies = report["replies"]["channels"]
    offset = 0
    segments = []
    for index, channel in enumerate(replies):
        width = channel["count"] / (report["replies"]["total"] or 1) * 1000
        segments.append(
            {"x": round(offset, 1), "w": round(max(width - 2, 1), 1), "slot": min(index + 1, 4),
             "label": CHANNELS.get(channel["channel"], channel["channel"]), **channel}
        )
        offset += width
    topics = report["topics"]
    top = max((t["count"] for t in topics), default=0) or 1
    return {
        "past": past,
        "years": years[::stride],
        "stacked": stacked,
        "speed": speed,
        "heat": heat,
        "segments": segments,
        "topics": [dict(t, width=round(t["count"] / top * 100, 1)) for t in topics],
    }


@router.get("/insights")
def insights_page(request: Request, months: int = 12, refresh: int = 0):
    if months not in insights.PERIODS:
        raise HTTPException(400, "Choose a period of 3, 6, 12 or 24 months")
    with closing(store.connect()) as db, store.transaction(db, False):
        if not ui.signed_in(request, db):
            return ui.login_page(request, db)
        report = insights.cached(db, months, bool(refresh))
    return ui.page(
        "insights.html",
        r=report,
        c=charts(report),
        months=months,
        periods=insights.PERIODS,
        hours=hours,
        weekdays=WEEKDAYS,
        channel_names=CHANNELS,
    )


# Only a decision about the current exchange can ask for a review. A later reply or
# customer message ends that exchange, while private notes and agent revisions do not.
def held_decisions(db, cid=None, limit=41):
    scope = " AND c.id=?" if cid is not None else ""
    return db.execute(f"""
        SELECT e.*,c.number,json_extract(c.data,'$.subject') subject,
               (SELECT CASE WHEN count(*)=1 AND json_extract(t.data,'$.source.type')='api' THEN t.id END
                FROM threads t WHERE t.conversation_id=c.id AND json_extract(t.data,'$.type')='message'
                AND json_extract(t.data,'$.state')='draft') draft_id
        FROM conversations c JOIN agent_events e ON e.id=(
            SELECT id FROM agent_events WHERE conversation=c.id AND kind='decision'
            ORDER BY at DESC,id DESC LIMIT 1)
        WHERE {standard_folders.OPEN} AND {standard_folders.VISIBLE}{scope}
        AND json_extract(e.data,'$.mode') IN ('draft','manual_review')
        AND (json_extract(e.data,'$.mode')='manual_review' OR EXISTS
            (SELECT 1 FROM threads d WHERE d.conversation_id=c.id AND json_extract(d.data,'$.state')='draft'
             AND json_extract(d.data,'$.type')='message'))
        AND NOT EXISTS (SELECT 1 FROM threads x WHERE x.conversation_id=c.id AND x.created>e.at
            AND json_extract(x.data,'$.type') IN ('customer','message','reply')
            AND coalesce(json_extract(x.data,'$.state'),'published')<>'draft')
        ORDER BY e.at DESC,e.id DESC LIMIT ?
    """, ([cid] if cid is not None else [])+[limit])


def review_groups(db, page=1, day=None):
    """Real exceptions and today's recorded outcomes. Read faults leave the mailbox accessible."""
    import logging
    from datetime import datetime, timezone

    if not agent_ledger.ready(db):
        return None
    try:
        size = 40
        end = page * size
        limit = end + 1
        day = day or datetime.now(timezone.utc).date()
        since = f"{day}T00:00:00Z"
        held = [agent_ledger.present(agent_ledger.shape(row)) for row in held_decisions(db, limit=limit)]
        held.extend(agent_ledger.present(e) for e in agent_ledger.needs_owner(db, limit=limit))
        held.sort(key=lambda e: (e["at"], e["id"]), reverse=True)
        handled = [dict(conversation=e["conversation"], subject=e["subject"], at=e["at"],
                        actor=agent_ledger.present(e)["who"], action=e["title"], icon="message")
                   for e in agent_ledger.activity(db, since, limit=limit, decision_mode="auto_send")]
        handled.extend(dict(row) for row in db.execute("""
            SELECT c.id conversation,json_extract(c.data,'$.subject') subject,i.assessed at,
                   'Intake' actor, replace(i.category,'_',' ') action,'closed' icon
            FROM intake_triage i JOIN conversations c ON c.id=i.conversation
            WHERE i.assessed>=? AND i.filed=1 ORDER BY i.assessed DESC LIMIT ?
        """, (since, limit)))
        # Start at today's changed conversations, then use the per-ticket thread index.
        handled.extend(dict(row) for row in db.execute("""
            SELECT c.id conversation,json_extract(c.data,'$.subject') subject,t.created at,
                   'Workflow' actor, CASE WHEN json_extract(t.data,'$.type')='lineitem' THEN 'closed'
                        ELSE 'replied' END action,
                   CASE WHEN json_extract(t.data,'$.type')='lineitem' THEN 'closed' ELSE 'message' END icon
            FROM conversations c CROSS JOIN threads t ON t.conversation_id=c.id AND t.created>=?
            WHERE c.modified>=? AND json_extract(t.data,'$.source.type') IN ('workflows','automatic-workflow')
            AND ((json_extract(t.data,'$.type') IN ('message','reply') AND json_extract(t.data,'$.state')='published'
                  AND NOT EXISTS (SELECT 1 FROM mail_outbox o WHERE o.thread_id=t.id AND o.state<>'sent'))
                 OR (json_extract(t.data,'$.type')='lineitem' AND json_extract(c.data,'$.closedAt')=t.created
                     AND c.status='closed' AND (json_extract(t.data,'$.action.status')='closed'
                         OR EXISTS (SELECT 1 FROM native_rules r,json_each(r.data,'$.actions') a
                                    WHERE r.id=json_extract(t.data,'$.action.associatedEntities.workflow')
                                    AND r.kind='Automatic' AND json_extract(a.value,'$.type')=50
                                    AND json_extract(a.value,'$.value')=3))))
            ORDER BY t.created DESC,t.id DESC LIMIT ?
        """, (since, since, limit)))
        handled.sort(key=lambda row: row["at"], reverse=True)
        start = end-size
        held_page, handled_page = held[start:end], handled[start:end]
        listed = held_page + handled_page
        ids = sorted({item["conversation"] for item in listed})
        if ids:
            import json
            people = {}
            for row in db.execute("SELECT id,json_extract(data,'$.primaryCustomer'),json_extract(data,'$.tags'),number, "
                                  "CASE WHEN status='active' THEN json_extract(data,'$.customerWaitingSince.time') END "
                                  f"FROM conversations WHERE id IN ({','.join('?' for _ in ids)})", ids):
                customer = json.loads(row[1] or '{}')
                name = " ".join(filter(None, (customer.get("first"),customer.get("last"))))
                people[row[0]] = (name or customer.get("email") or "",json.loads(row[2] or "[]"), row[3], row[4])
            for item in listed:
                item["customer"], item["tags"], item["number"], item["waiting_since"] = people[item["conversation"]]
            for item in handled_page:
                item["label"] = f'{item["actor"]} · {item["action"]}'
        return dict(held=held_page, handled=handled_page, more=max(len(handled),len(held))>end,
                    held_more=len(held)>end, handled_more=len(handled)>end)
    except Exception:
        logging.getLogger("inbox.ledger").exception("Review groups unavailable")
        return None


@router.get("/review")
def review(request: Request):
    """Every open conversation holding an unsent draft, with the message it answers."""
    import os

    with ui.mailbox_reader() as db:
        if not ui.signed_in(request, db):
            return ui.login_page(request, db)
        groups = review_groups(db)
        ledger = groups is not None
        expr, values = standard_folders.predicate("drafts")
        ids = [
            row[0]
            for row in db.execute(
                f"SELECT c.id FROM conversations c WHERE {expr} ORDER BY c.modified DESC,c.id DESC LIMIT 31",
                values,
            )
        ]
        items = []
        for cid in ids[:30]:
            conv = store.conversation(db, cid)
            threads = store.thread_list(db, cid)
            draft = next((t for t in threads if t.get("state") == "draft" and t.get("type") == "message"), None)
            if not draft:
                continue
            question = next((t for t in threads if t.get("type") == "customer"), None)
            # The drafting agent's latest record; older conversations only have a note.
            agent = next((agent_ledger.present(e) for e in agent_ledger.events(db, cid, 20)
                          if e["kind"] in ("decision", "reminder", "responder")), None) if ledger else None
            note = None if agent else next((t for t in threads if t.get("type") == "note"), None)
            items.append(
                {
                    "conv": conv,
                    "draft": draft,
                    "draft_html": ui.body_html(draft, cid),
                    "question": question,
                    "question_html": ui.body_html(question, cid) if question else "",
                    "agent": agent,
                    "note_html": ui.body_html(note, cid) if note else "",
                    "note": note,
                    "waiting": ui.age((conv.get("customerWaitingSince") or {}).get("time"))
                    if conv.get("status") == "active" and conv.get("customerWaitingSince")
                    else "",
                    "to": ", ".join(str(a) for a in draft.get("to") or []) or (conv.get("primaryCustomer") or {}).get("email", ""),
                    "version": ui.version(db, cid),
                }
            )
        writes = store.writer_live(db) and os.getenv("INBOX_OPERATOR_WRITES") == "1"
        return ui.page(
            "review.html",
            groups=groups,
            items=items,
            more=len(ids) > 30,
            writes=writes,
            send=os.getenv("INBOX_SEND_ENABLED") == "1",
        )


@router.get("/conversation-version/{cid}")
def conversation_version(request: Request, cid: int):
    with closing(store.connect()) as db, store.transaction(db, False):
        ui.operator(request, db)
        from workbench import draft_requests
        requested = draft_requests.latest(db, cid)
        return {"version": ui.version(db, cid), "request_state": requested["state"] if requested else ""}


@router.get("/similar/{cid}")
def similar(request: Request, cid: int):
    with closing(store.connect()) as db, store.transaction(db, False):
        ui.operator(request, db)
        found = agent_context.precedents(db, cid=cid, limit=4)
    for item in found["precedents"]:
        item["web"] = f"/conversation/{item['id']}"
        item["answer"] = item["answer"][:900]
        item["question"] = item["question"][:300]
    return found


TRIAGE_LABELS = {
    "support": "Support",
    "promotion": "Promotion",
    "site_mail": "Website-generated mail",
    "bounce": "Delivery failure",
    "auto_reply": "Automatic reply",
    "wrong_address": "Possibly meant for another business",
}


@router.get("/triage")
def triage_page(request: Request, category: str = ""):
    """Recent intake verdicts with their evidence, so the rules can be checked against real mail."""
    import json

    import intake_triage

    if category and category not in intake_triage.CATEGORIES:
        raise HTTPException(400, "Unknown category")
    with closing(store.connect()) as db, store.transaction(db, False):
        if not ui.signed_in(request, db):
            return ui.login_page(request, db)
        counts = dict(
            db.execute(
                "SELECT category,count(*) FROM intake_triage "
                "WHERE assessed>=strftime('%Y-%m-%dT%H:%M:%S','now','-30 days') GROUP BY 1"
            ).fetchall()
        )
        filed = db.execute(
            "SELECT count(*) FROM intake_triage WHERE filed=1 "
            "AND assessed>=strftime('%Y-%m-%dT%H:%M:%S','now','-30 days')"
        ).fetchone()[0]
        rows = []
        for row in db.execute(
            "SELECT t.object_key,t.conversation,t.category,t.confidence,t.reasons,t.evidence,t.filed,t.assessed,"
            "c.number,c.status,json_extract(c.data,'$.subject') subject FROM intake_triage t "
            "LEFT JOIN conversations c ON c.id=t.conversation "
            + ("WHERE t.category=? " if category else "WHERE t.category<>'support' ")
            + "ORDER BY t.assessed DESC LIMIT 200",
            (category,) if category else (),
        ):
            item = dict(row)
            item["reasons"] = json.loads(item["reasons"])
            item["evidence"] = json.loads(item["evidence"])
            rows.append(item)
    return ui.page(
        "triage.html",
        rows=rows,
        counts=counts,
        filed=filed,
        total=sum(counts.values()),
        category=category,
        labels=TRIAGE_LABELS,
        mode=intake_triage.mode(),
    )
