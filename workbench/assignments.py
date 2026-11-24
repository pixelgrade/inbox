"""An operator assigns one case to their configured product agent."""

import time
import secrets

from . import core


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS desk_agent_presence(
            product TEXT PRIMARY KEY REFERENCES desk_products, name TEXT NOT NULL,
            seen INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS desk_assignments(
            case_id TEXT PRIMARY KEY REFERENCES desk_cases, name TEXT NOT NULL,
            state TEXT NOT NULL, lease TEXT, available INTEGER NOT NULL
        );
    """)


def status(db, pid, cid):
    row = db.execute(
        "SELECT name,state FROM desk_assignments WHERE case_id=?", (cid,)
    ).fetchone()
    if row:
        return dict(row)
    agent = db.execute(
        "SELECT name FROM desk_agent_presence WHERE product=? AND seen>=?",
        (pid, int(time.time()) - 120),
    ).fetchone()
    return {"name": agent[0], "state": "available"} if agent else None


def enqueue(db, pid, cid, name):
    if core.case_row(db, pid, cid)["state"] != "investigating":
        raise core.Conflict("This investigation already has a proposal")
    now = int(time.time())
    available = db.execute(
        "SELECT name FROM desk_agent_presence WHERE product=? AND seen>=?",
        (pid, now - 120),
    ).fetchone()
    if not available or available[0] != name:
        raise core.Conflict("The selected product agent is offline or has changed")
    current = status(db, pid, cid)
    if current["state"] in ("queued", "running") and current["name"] != name:
        raise core.Conflict("Another product agent is assigned")
    db.execute(
        """INSERT INTO desk_assignments VALUES(?,?,'queued',NULL,?)
                  ON CONFLICT(case_id) DO UPDATE SET name=excluded.name,state='queued',lease=NULL,available=excluded.available
                  WHERE desk_assignments.state IN ('failed','needs_review')""",
        (cid, name, now),
    )


def claim(db, pid, name):
    name = core.string(name, 120, "agent display name")
    now = int(time.time())
    db.execute(
        """INSERT INTO desk_agent_presence VALUES(?,?,?)
                  ON CONFLICT(product) DO UPDATE SET name=excluded.name,seen=excluded.seen""",
        (pid, name, now),
    )
    row = db.execute(
        """SELECT a.case_id FROM desk_assignments a JOIN desk_cases c ON c.id=a.case_id
        WHERE c.product=? AND c.state='investigating' AND a.name=? AND a.state IN ('queued','running')
        AND a.available<=? ORDER BY a.available,a.case_id LIMIT 1""",
        (pid, name, now),
    ).fetchone()
    if not row:
        return None
    lease = secrets.token_urlsafe(32)
    db.execute(
        "UPDATE desk_assignments SET state='running',lease=?,available=? WHERE case_id=?",
        (core.sha(lease), now + 2400, row[0]),
    )
    return {"id": row[0], "lease": lease}


def finish(db, pid, cid, lease, state):
    case = core.case_row(db, pid, cid)
    row = core.one(
        db, "SELECT lease,state FROM desk_assignments WHERE case_id=?", (cid,)
    )
    if not secrets.compare_digest(
        row["lease"] or "", core.sha(core.string(lease, 128, "lease"))
    ):
        raise core.Conflict("Product agent assignment changed")
    states = {
        "waiting_for_test": "queued",
        "turn_limit": "needs_review",
        "failed": "failed",
        "proposed": "completed",
        "draft_saved": "completed",
    }
    if (
        not isinstance(state, str)
        or state not in states
        or (
            states[state] == "completed"
            and case["state"] not in ("proposed", "draft_saved")
        )
    ):
        raise core.Invalid("Invalid agent outcome")
    if row["state"] != "running":
        if row["state"] == states[state]:
            return
        raise core.Conflict("The product agent already recorded a different outcome")
    db.execute(
        "UPDATE desk_assignments SET state=?,available=? WHERE case_id=?",
        (states[state], int(time.time()) + 10, cid),
    )
