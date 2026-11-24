"""Live mailbox folder membership, using the IDs imported from the source mailbox."""

import os

OPEN = "c.status IN ('active','pending')"
VISIBLE = (
    "NOT EXISTS(SELECT 1 FROM snoozes s WHERE s.conversation=c.id AND s.regardless=0)"
)
PUBLISHED = "coalesce(json_extract(c.data,'$.state'),'published')='published'"
ASSIGNEE = "json_extract(c.data,'$.assignee.id')"
DRAFT = """EXISTS (SELECT 1 FROM threads t WHERE t.conversation_id=c.id
    AND json_extract(t.data,'$.state')='draft')"""
UI_TYPES = {
    "later": "later",
    "mine": "mytickets",
    "drafts": "drafts",
    "assigned": "assigned",
    "unassigned": "open",
}


def predicate(kind, folder=None):
    agent = int(os.environ.get("INBOX_AGENT_ID", "1"))
    if kind == "drafts":
        return f"{OPEN} AND {VISIBLE} AND {DRAFT}", []
    if kind == "mytickets":
        return f"{OPEN} AND {VISIBLE} AND {PUBLISHED} AND {ASSIGNEE}=?", [agent]
    if kind == "assigned":
        return f"{OPEN} AND {VISIBLE} AND {PUBLISHED} AND {ASSIGNEE}<>?", [agent]
    if kind == "open":
        return f"{OPEN} AND {VISIBLE} AND {PUBLISHED} AND {ASSIGNEE} IS NULL", []
    if kind in ("closed", "spam"):
        return f"c.status=? AND {VISIBLE}", [kind]
    if kind == "later":
        if folder is None:
            return "c.id IN (SELECT conversation FROM snoozes)", []
        return (
            "(EXISTS(SELECT 1 FROM snoozes s WHERE s.conversation=c.id) OR json_extract(c.data,'$.folderId')=?)",
            [folder],
        )
    if kind == "needs_attn":
        return f"{OPEN} AND {VISIBLE} AND json_extract(c.data,'$.folderId')=?", [folder]
    return (
        f"{VISIBLE} AND c.id IN (SELECT conversation FROM native_folder_members WHERE folder=?)",
        [folder],
    )


def metadata(db):
    from json import loads

    return [
        loads(row[0])
        for row in db.execute(
            "SELECT data FROM source_profile WHERE kind='folders' AND json_extract(data,'$.type')<>'smart' ORDER BY id"
        )
    ]


def by_id(db, fid):
    item = next((m for m in metadata(db) if m["id"] == fid), {})
    return predicate(item.get("type"), fid)


def listing(db, mid):
    result = []
    for folder in metadata(db):
        expr, params = predicate(folder["type"], folder["id"])
        total, active = db.execute(
            "SELECT count(*),count(CASE WHEN c.status='active' THEN 1 END) "
            f"FROM conversations c WHERE c.mailbox_id=? AND ({expr})",
            [mid, *params],
        ).fetchone()
        result.append(dict(folder, activeCount=active, totalCount=total))
    return result
