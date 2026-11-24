"""Compact reading tools for agents: one conversation as plain text, and earlier answers to similar questions.

Both are read-only and work from the mailbox's own search index. Callers own authentication
and the connection.
"""

import html
import json
import math
import os
import re
import unicodedata

import nh3

import standard_folders
import store
from insights import NOT_SUPPORT_TAGS

WORD = re.compile(r"[^\W_]{3,40}", re.UNICODE)
BREAK = re.compile(r"(?i)<br\s*/?>|</(?:div|li|tr)\s*>")
PARAGRAPH = re.compile(r"(?i)</(?:p|h[1-6]|blockquote|pre|ul|ol|table)\s*>")
# Line anchors use [ \t] because \s also crosses line ends and makes long blank runs quadratic.
QUOTE_LINE = re.compile(r"(?m)^[ \t]*>.*$")
# The line a mail client writes above the message it quotes.
QUOTE_HEADER = re.compile(
    r"(?im)^[ \t]*(?:on\b.{0,200}\bwrote|le\b.{0,200}\ba écrit|am\b.{0,200}\bschrieb.{0,80})[ \t]*:?[ \t]*$"
)
READ_LIMIT = 400_000  # Characters of one stored message that are turned into text.
COMMON = frozenset(
    """the and for that this with you your have has had not are was were but can could would should
    will from they them their there here what when where which who how why any all get got use using
    used please thanks thank hello dear regards kind best hope just like want need also about into
    been being does did done its it's i'm i've don't can't doesn't isn't our out one two too very
    more most some such than then these those over under again still only site website page pages
    http https www com html mailto wordpress theme themes""".split()
)


def plain(markup, quotes=False):
    """Readable text of a stored message. Quoted earlier mail is dropped unless requested."""
    text = PARAGRAPH.sub("\n\n", BREAK.sub("\n", str(markup or "")[:READ_LIMIT]))
    text = nh3.clean(
        text,
        tags=set(),
        attributes={},
        clean_content_tags={"script", "style"} | (set() if quotes else {"blockquote"}),
    )
    text = html.unescape(text).replace("\xa0", " ").replace("\r", "")
    if not quotes:
        text = QUOTE_LINE.sub("", text)
        # Only a quote header that closes the message marks the end of the new text.
        tail = text.rstrip()
        last = tail.rfind("\n") + 1
        if last and QUOTE_HEADER.fullmatch(tail[last:]):
            text = tail[:last]
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def person(value):
    value = value or {}
    name = " ".join(filter(None, (value.get("first"), value.get("last")))).strip()
    return name or value.get("email") or ""


def message(thread, quotes=False):
    kind = thread.get("type")
    # Chats, calls and forwards carry a body too; who wrote it decides the side.
    if kind == "lineitem":
        role = "activity"
    elif kind == "note":
        role = "note"
    elif kind == "customer" or (thread.get("createdBy") or {}).get("type") == "customer":
        role = "customer"
    else:
        role = "agent"
    item = {
        "id": thread.get("id"),
        "at": thread.get("createdAt"),
        "from": role,
        "author": person(thread.get("createdBy")),
        "state": thread.get("state") or "published",
    }
    if role == "activity":
        action = thread.get("action") or {}
        item["text"] = str(action.get("text") or action.get("type") or kind or "")
    else:
        item["text"] = plain(thread.get("body"), quotes)
    files = (thread.get("_embedded") or {}).get("attachments") or thread.get("attachments") or []
    if files:
        item["attachments"] = [
            {
                "id": f.get("id"),
                "filename": f.get("filename") or f.get("fileName"),
                "mimeType": f.get("mimeType"),
                "size": f.get("size"),
            }
            for f in files
            if isinstance(f, dict)
        ]
    if (thread.get("rating") or {}).get("rating"):
        item["rating"] = thread["rating"]["rating"]
    if thread.get("openedAt"):
        item["opened_at"] = thread["openedAt"]
    return item


def fit(messages, budget):
    """Keep the opening message and the newest ones within a character budget."""
    total = sum(len(m["text"]) for m in messages)
    if total <= budget:
        return messages, 0
    if len(messages) < 3:
        each = budget // len(messages)
        for m in messages:
            if len(m["text"]) > each:
                m["text"] = m["text"][: max(each - 2, 0)].rstrip() + " …"
                m["truncated"] = True
        return messages, 0
    share = max(400, budget // 4)
    first = dict(messages[0])
    if len(first["text"]) > share:
        first["text"] = first["text"][:share].rstrip() + " …"
        first["truncated"] = True
    remaining = budget - len(first["text"])
    kept = []
    for m in reversed(messages[1:]):
        if len(m["text"]) > remaining:
            if not kept and remaining > 200:
                m = dict(m, text=m["text"][:remaining].rstrip() + " …", truncated=True)
                kept.append(m)
            break
        remaining -= len(m["text"])
        kept.append(m)
    kept.reverse()
    return [first, *kept], len(messages) - 1 - len(kept)


def history(db, customer, exclude=None, limit=6):
    if not customer:
        return {"conversations": 0, "first_contact": None, "recent": []}
    # Counts the customer's other conversations; the one being read is left out.
    total, first = db.execute(
        "SELECT count(*),min(c.created) FROM conversations c WHERE c.customer_id=? AND c.id<>? AND "
        + standard_folders.PUBLISHED,
        (customer, exclude or 0),
    ).fetchone()
    recent = [
        {
            "id": row["id"],
            "number": row["number"],
            "subject": row["subject"] or "(no subject)",
            "status": row["status"],
            "created": row["created"],
        }
        for row in db.execute(
            "SELECT c.id,c.number,c.status,c.created,json_extract(c.data,'$.subject') subject "
            f"FROM conversations c WHERE c.customer_id=? AND c.id<>? AND {standard_folders.PUBLISHED} "
            "ORDER BY c.created DESC,c.id DESC LIMIT ?",
            (customer, exclude or 0, limit),
        )
    ]
    return {"conversations": total, "first_contact": first, "recent": recent}


def brief(db, cid, budget=24000, quotes=False):
    conv = store.conversation(db, cid)
    threads = list(reversed(store.thread_list(db, cid)))
    messages = [message(t, quotes) for t in threads]
    drafts = [m for m in messages if m["state"] == "draft"]
    messages, omitted = fit(messages, budget)
    customer = conv.get("primaryCustomer") or {}
    emails = [
        row[0]
        for row in db.execute(
            "SELECT email FROM customer_emails WHERE customer_id=? ORDER BY email",
            (customer.get("id"),),
        )
    ]
    waiting = (conv.get("customerWaitingSince") or {}).get("time")
    result = {
        "id": conv["id"],
        "number": conv.get("number"),
        "subject": conv.get("subject") or "",
        "status": conv.get("status"),
        "state": conv.get("state") or "published",
        "tags": [t.get("tag") for t in conv.get("tags") or [] if isinstance(t, dict)],
        "assignee": person(conv.get("assignee")) or None,
        "created": conv.get("createdAt"),
        "modified": conv.get("modifiedAt") or conv.get("userUpdatedAt"),
        "waiting_since": waiting if conv.get("status") == "active" else None,
        "snoozed_until": (conv.get("snooze") or {}).get("until"),
        "customer": {
            "id": customer.get("id"),
            "name": person(customer),
            "email": customer.get("email"),
            "emails": emails,
            **history(db, customer.get("id"), cid),
        },
        "messages": messages,
        "omitted_messages": omitted,
        "draft": {"id": drafts[-1]["id"], "text": drafts[-1]["text"]} if drafts else None,
        "intake": intake(db, cid),
        "links": {
            "web": conv["_links"]["web"]["href"],
            "api": conv["_links"]["self"]["href"],
        },
    }
    return result


def intake(db, cid):
    """The intake verdict for this conversation, when the mailbox assessed its first message."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_triage'").fetchone():
        return None
    import intake_triage

    return intake_triage.for_conversation(db, cid)


def vocabulary(db):
    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.thread_terms USING fts5vocab('main','thread_search','row')"
    )


def key_terms(db, text, skip=(), limit=8):
    """The words that best separate this question from the rest of the archive."""
    counts = {}
    # The search index stores words without accents; look them up the same way.
    folded = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    for word in WORD.findall(folded):
        if word not in COMMON and word not in skip and not word.isdigit():
            counts[word] = counts.get(word, 0) + 1
    if not counts:
        return []
    vocabulary(db)
    size = db.execute("SELECT count(*) FROM thread_search_docsize").fetchone()[0] or 1
    scored = []
    # Frequent words first, so a long message cannot ask for thousands of lookups.
    for word, seen in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:60]:
        row = db.execute("SELECT doc FROM temp.thread_terms WHERE term=?", (word,)).fetchone()
        # A word found in one message only describes this conversation itself.
        # Nor does a word most messages share, once the archive is large enough to tell.
        if not row or row[0] < 2 or (size >= 400 and row[0] > size * 0.25):
            continue
        scored.append((min(seen, 3) * math.log(size / row[0]), word))
    scored.sort(reverse=True)
    return [word for _, word in scored[:limit]]


def precedents(db, text="", cid=None, tag=None, limit=5):
    """Closed conversations with a sent answer, ranked by similarity to the question."""
    limit = max(1, min(int(limit), 10))
    skip, tags = set(), set()
    if cid:
        conv = store.conversation(db, cid)
        customer = conv.get("primaryCustomer") or {}
        skip.update(WORD.findall(person(customer).casefold()))
        skip.update(WORD.findall(str(customer.get("email") or "").casefold()))
        tags = {str(t.get("tag")).casefold() for t in conv.get("tags") or [] if isinstance(t, dict)}
        if not text:
            latest = db.execute(
                "SELECT json_extract(data,'$.body') FROM threads WHERE conversation_id=? AND "
                "json_extract(data,'$.type')='customer' ORDER BY created DESC,id DESC LIMIT 1",
                (cid,),
            ).fetchone()
            text = (conv.get("subject") or "") + "\n" + plain(latest[0] if latest else "")
    if tag:
        tags.add(str(tag).casefold())
    terms = key_terms(db, text[:20000], skip)
    if not terms:
        return {"terms": [], "precedents": []}
    owner = (
        "JOIN message_owner t ON t.thread=f.rowid GROUP BY t.conversation"
        if store.owners_ready(db)
        else "JOIN threads t ON t.id=f.rowid GROUP BY t.conversation_id"
    )
    hits = db.execute(
        "SELECT " + ("t.conversation" if store.owners_ready(db) else "t.conversation_id") + ",min(f.rank) "
        "FROM (SELECT rowid,rank FROM thread_search WHERE thread_search MATCH ? ORDER BY rank LIMIT 400) f "
        + owner,
        (" OR ".join('"' + term + '"' for term in terms),),
    ).fetchall()
    ranked = []
    for conversation, score in hits:
        if conversation == cid:
            continue
        row = db.execute(
            "SELECT c.data FROM conversations c WHERE c.id=? AND c.status='closed' AND "
            f"{standard_folders.PUBLISHED} AND coalesce(json_extract(c.data,'$.source.via'),'customer')<>'user'",
            (conversation,),
        ).fetchone()
        if not row:
            continue
        item = json.loads(row[0])
        labels = {str(t.get("tag")).casefold() for t in item.get("tags") or [] if isinstance(t, dict)}
        # Promotions, site mail and wrong-address exchanges were answered, if at all, with
        # fixed wording; they are no precedent for a support question.
        if labels & NOT_SUPPORT_TAGS:
            continue
        shared = tags & {
            str(t.get("tag")).casefold() for t in item.get("tags") or [] if isinstance(t, dict)
        }
        # Search rank is negative; a shared product tag moves a match ahead of its neighbours.
        ranked.append((score * (1.3 if shared else 1), item, sorted(shared)))
    ranked.sort(key=lambda r: (r[0], -r[1]["id"]))
    found = []
    base = os.environ.get("INBOX_BASE_URL", "").rstrip("/")
    for score, item, shared in ranked[: limit * 4]:
        threads = list(reversed(store.thread_list(db, item["id"])))
        question = next((t for t in threads if t.get("type") == "customer"), None)
        answers = [
            t for t in threads if t.get("type") == "message" and t.get("state", "published") == "published"
        ]
        if not question or not answers:
            continue
        rating = next(
            ((t.get("rating") or {}).get("rating") for t in reversed(answers) if t.get("rating")),
            None,
        )
        found.append(
            {
                "id": item["id"],
                "number": item.get("number"),
                "subject": item.get("subject") or "",
                "created": item.get("createdAt"),
                "tags": [t.get("tag") for t in item.get("tags") or [] if isinstance(t, dict)],
                "shared_tags": shared,
                "score": round(-score, 2),
                "replies": len(answers),
                "rating": rating,
                "question": plain(question.get("body"))[:900],
                "answer": plain(answers[-1].get("body"))[:2400],
                "answered": answers[-1].get("createdAt"),
                "web": f"{base}/conversation/{item['id']}#thread-{answers[-1].get('id')}",
            }
        )
        if len(found) == limit:
            break
    return {"terms": terms, "precedents": found}
