"""Operator reminders that reactivate the same conversation without sending email."""

from datetime import datetime, timezone

import store


def set_reminder(db, cid, until, regardless=False, clock=None):
    from operator_actions import activity

    try:
        target = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise store.Invalid("Enter a future reminder time") from None
    if target.tzinfo is None or target <= (clock or datetime.now(timezone.utc)):
        raise store.Invalid("Enter a future reminder time with a time zone")
    target = target.astimezone(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO snoozes VALUES(?,?,?) ON CONFLICT(conversation) DO UPDATE SET due=excluded.due,regardless=excluded.regardless",
        (cid, target, int(regardless)),
    )
    activity(
        db,
        cid,
        "conversation-snoozed",
        "Snoozed until " + target,
        {"until": target, "regardless": regardless},
    )
    store.update_conversation(db, cid, {})


def clear(db, cid, reason):
    from operator_actions import activity

    if not db.execute("DELETE FROM snoozes WHERE conversation=?", (cid,)).rowcount:
        return
    activity(
        db, cid, "conversation-unsnoozed", reason, {}, system=reason != "Removed snooze"
    )
    store.update_conversation(db, cid, {})


def due(db, clock):
    for (cid,) in db.execute(
        "SELECT conversation FROM snoozes WHERE due<=? ORDER BY due",
        (clock.astimezone(timezone.utc).isoformat(timespec="seconds"),),
    ).fetchall():
        clear(db, cid, "Snooze time reached")
        if store.conversation(db, cid)["status"] != "active":
            store.update_conversation(db, cid, {"status": "active"})
            store.emit(db, cid, "convo.status")
