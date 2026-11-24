"""Support insights computed from the mailbox itself. Read-only; callers own the connection.

Every figure comes from conversations and threads already stored here. Each query reads an
index range for the requested period, so the report stays quick on a large archive.
"""

from datetime import datetime, timezone
import json
import threading
import time

import company
import standard_folders

PERIODS = (3, 6, 12, 24)
BAD = ("bad", "not_good", "not-good")  # Imported ratings say not_good; native ones say bad.
# Mail a workflow, the intake check or an agent marked as something other than a support request.
NOT_SUPPORT_TAGS = frozenset(
    "spammy spam marketing admin non-support auto-reply delivery-report promotion site-mail wrong-address".split() + [company.IGNORE_TAG]
)
WAITING = (("Under a day", 1), ("1–3 days", 3), ("3–7 days", 7), ("Over a week", None))
_cache = {}
_lock = threading.Lock()


def moment(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor(value):
    """A text bound at or below every stored spelling of this instant and later ones."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def month_start(value, back=0):
    index = value.year * 12 + value.month - 1 - back
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=timezone.utc)


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    position = share * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def change(current, previous):
    """Relative change against the preceding period, or None when there is no base."""
    if not previous:
        return None
    return round((current - previous) / previous * 100)


def queue(db, now):
    rows = db.execute(
        "SELECT c.id,c.number,json_extract(c.data,'$.subject') subject,"
        "json_extract(c.data,'$.customerWaitingSince.time') waiting,c.created,"
        f"{standard_folders.ASSIGNEE} assignee,{standard_folders.DRAFT} draft "
        f"FROM conversations c WHERE c.status='active' AND {standard_folders.VISIBLE} "
        f"AND {standard_folders.PUBLISHED}"
    ).fetchall()
    buckets = [0] * len(WAITING)
    oldest = None
    for row in rows:
        since = moment(row["waiting"] or row["created"])
        if not since:
            continue
        days = (now - since).total_seconds() / 86400
        slot = next(
            (i for i, (_, limit) in enumerate(WAITING) if limit is None or days < limit)
        )
        buckets[slot] += 1
        if oldest is None or since < oldest["since"]:
            oldest = {
                "since": since,
                "id": row["id"],
                "number": row["number"],
                "subject": row["subject"] or "(no subject)",
                "days": round(days, 1),
            }
    if oldest:
        oldest["since"] = stamp(oldest["since"])
    pending = db.execute(
        "SELECT count(*) FROM conversations WHERE status='pending'"
    ).fetchone()[0]
    return {
        "active": len(rows),
        "pending": pending,
        "drafts": sum(1 for r in rows if r["draft"]),
        "unassigned": sum(1 for r in rows if r["assignee"] is None),
        "waiting": [
            {"label": label, "count": count}
            for (label, _), count in zip(WAITING, buckets)
        ],
        "oldest": oldest,
    }


def history(db):
    """New conversations per month over the whole archive, read from the creation index."""
    return [
        {"month": row[0], "count": row[1]}
        for row in db.execute(
            "SELECT substr(c.created,1,7),count(*) FROM conversations c "
            f"WHERE {standard_folders.PUBLISHED} AND c.created IS NOT NULL GROUP BY 1 ORDER BY 1"
        )
    ]


def direction(db, cid):
    """Who started a conversation that carries no source of its own, read from its first message."""
    row = db.execute(
        "SELECT json_extract(data,'$.type'),json_extract(data,'$.source.type') FROM threads "
        "WHERE conversation_id=? AND json_extract(data,'$.type') IN ('customer','message') "
        "ORDER BY created,id LIMIT 1",
        (cid,),
    ).fetchone()
    if not row:
        return "customer", None
    return ("user" if row[0] == "message" else "customer"), row[1]


def report(db, months=12, now=None):
    if months not in PERIODS:
        raise ValueError("Unsupported period")
    now = now or datetime.now(timezone.utc)
    start = month_start(now, months - 1)
    # The comparison span is as long as the time elapsed in this period, so a month that has
    # only begun is never measured against a full one.
    earlier = start - (now - start)
    window = (
        f"{standard_folders.PUBLISHED} AND c.created>=? AND c.status<>'spam'",
        [floor(earlier)],
    )
    conversations = db.execute(
        "SELECT c.id,c.number,c.created,c.status,c.customer_id,"
        "json_extract(c.data,'$.source.via') via,json_extract(c.data,'$.source.type') channel,"
        "json_extract(c.data,'$.closedAt') closed,json_extract(c.data,'$.subject') subject,"
        "json_extract(c.data,'$.tags') tags,"
        "json_extract(c.data,'$.primaryCustomer.first') first,"
        "json_extract(c.data,'$.primaryCustomer.last') last,"
        "json_extract(c.data,'$.primaryCustomer.email') email "
        f"FROM conversations c WHERE {window[0]}",
        window[1],
    ).fetchall()
    current = {}
    previous = {"inbound": 0, "outbound": 0, "other": 0}
    tags = {}
    for row in conversations:
        created = moment(row["created"])
        if not created or created < earlier or created > now:
            continue
        via, channel = row["via"], row["channel"]
        if via is None:
            via, channel = direction(db, row["id"])
        try:
            named = [str(t.get("tag")) for t in json.loads(row["tags"] or "[]") if isinstance(t, dict) and t.get("tag")]
        except ValueError:
            named = []
        local = str(row["email"] or "").partition("@")[0].casefold()
        noise = bool({n.casefold() for n in named} & NOT_SUPPORT_TAGS) or local in ("mailer-daemon", "postmaster")
        kind = "outbound" if via == "user" else "other" if noise else "inbound"
        recent = created >= start
        if kind == "inbound":
            for name in named:
                entry = tags.setdefault(name.casefold(), {"tag": name, "count": 0, "before": 0})
                entry["count" if recent else "before"] += 1
        if not recent:
            previous[kind] += 1
            continue
        current[row["id"]] = {
            "row": row,
            "kind": kind,
            "channel": channel,
            "created": created,
            "replies": [],
            "customer_messages": 0,
            "opened": False,
        }

    replies_by_channel = {}
    ratings = {"great": 0, "okay": 0, "bad": 0}
    unhappy = []
    workflow_actions = 0
    threads = db.execute(
        "SELECT t.id,t.conversation_id cid,json_extract(t.data,'$.type') type,"
        "json_extract(t.data,'$.state') state,t.created,"
        "json_extract(t.data,'$.source.type') channel,"
        "json_extract(t.data,'$.rating.rating') rating,"
        "json_extract(t.data,'$.rating.comments') comment,"
        "json_extract(t.data,'$.openedAt') opened,"
        "json_extract(t.data,'$.action.type') action "
        "FROM threads t WHERE t.conversation_id IN "
        f"(SELECT c.id FROM conversations c WHERE {window[0]})",
        [floor(start)],
    )
    for t in threads:
        item = current.get(t["cid"])
        if not item:
            continue
        if t["type"] == "customer":
            item["customer_messages"] += 1
        elif t["type"] == "message" and (t["state"] or "published") == "published":
            at = moment(t["created"])
            if at:
                item["replies"].append(at)
            channel = t["channel"] or "unknown"
            replies_by_channel[channel] = replies_by_channel.get(channel, 0) + 1
            if t["opened"]:
                item["opened"] = True
            if t["rating"]:
                grade = "bad" if t["rating"] in BAD else t["rating"]
                if grade in ratings:
                    ratings[grade] += 1
                if grade == "bad":
                    unhappy.append(
                        {
                            "id": t["cid"],
                            "thread": t["id"],
                            "number": item["row"]["number"],
                            "subject": item["row"]["subject"] or "(no subject)",
                            "comment": (t["comment"] or "")[:400],
                            "at": t["created"],
                        }
                    )
        elif t["type"] == "lineitem" and t["action"] in (
            "automatic-workflow",
            "manual-workflow",
        ):
            workflow_actions += 1

    # One row per calendar month of the period, oldest first.
    series = {}
    for back in range(months - 1, -1, -1):
        key = month_start(now, back).strftime("%Y-%m")
        series[key] = {"month": key, "inbound": 0, "outbound": 0, "other": 0, "first_response": []}
    first_response, resolution = [], []
    inbound = outbound = other = answered = one_reply = unanswered_closed = 0
    opened = replied = 0
    channels = {}
    grid = [[0] * 24 for _ in range(7)]
    people = {}
    for item in current.values():
        row, created = item["row"], item["created"]
        bucket = series.get(created.strftime("%Y-%m"))
        if bucket:
            bucket[item["kind"]] += 1
        if item["kind"] == "outbound":
            outbound += 1
            opened += item["opened"]
            replied += item["customer_messages"] > 0
            continue
        if item["kind"] == "other":
            other += 1
            continue
        inbound += 1
        channel = item["channel"] or "unknown"
        channels[channel] = channels.get(channel, 0) + 1
        grid[created.weekday()][created.hour] += 1
        if row["customer_id"]:
            person = people.setdefault(
                row["customer_id"],
                {
                    "id": row["customer_id"],
                    "name": " ".join(filter(None, (row["first"], row["last"])))
                    or row["email"]
                    or "Unknown customer",
                    "count": 0,
                },
            )
            person["count"] += 1
        after = sorted(r for r in item["replies"] if r >= created)
        if after:
            answered += 1
            hours = (after[0] - created).total_seconds() / 3600
            first_response.append(hours)
            if bucket:
                bucket["first_response"].append(hours)
            closed = moment(row["closed"]) if row["status"] == "closed" else None
            if closed and closed >= created:
                resolution.append((closed - created).total_seconds() / 3600)
                if len(item["replies"]) == 1:
                    one_reply += 1
        elif row["status"] == "closed":
            unanswered_closed += 1
    for bucket in series.values():
        values = bucket.pop("first_response")
        median = percentile(values, 0.5)
        bucket["first_response_hours"] = None if median is None else round(median, 1)

    returning = 0
    for key in people:
        first = db.execute(
            "SELECT min(c.created) FROM conversations c WHERE c.customer_id=?", (key,)
        ).fetchone()[0]
        seen = moment(first)
        if seen is not None and seen < start:
            returning += 1

    topics = sorted(
        (t for t in tags.values() if t["count"]), key=lambda t: (-t["count"], t["tag"].lower())
    )
    for topic in topics:
        topic["change"] = change(topic["count"], topic["before"])
    rising = sorted(
        (t for t in topics if t["count"] - t["before"] >= 3),
        key=lambda t: (t["before"] - t["count"], t["tag"].lower()),
    )[:6]

    rated = sum(ratings.values())
    total_replies = sum(replies_by_channel.values())
    return {
        "generated": stamp(now),
        "period": {"months": months, "from": stamp(start), "to": stamp(now), "compared_from": stamp(earlier)},
        "queue": queue(db, now),
        "history": history(db),
        "volume": {
            "inbound": inbound,
            "outbound": outbound,
            "other": other,
            "other_share": share(other, inbound + other),
            "inbound_change": change(inbound, previous["inbound"]),
            "outbound_change": change(outbound, previous["outbound"]),
            "other_change": change(other, previous["other"]),
            "months": list(series.values()),
            "channels": sorted(
                ({"channel": k, "count": v} for k, v in channels.items()),
                key=lambda c: -c["count"],
            ),
        },
        "response": {
            "answered": answered,
            "median_hours": rounded(percentile(first_response, 0.5)),
            "p90_hours": rounded(percentile(first_response, 0.9)),
            "within_day": share(sum(1 for h in first_response if h <= 24), answered),
            "median_resolution_hours": rounded(percentile(resolution, 0.5)),
            "one_reply": one_reply,
            "one_reply_share": share(one_reply, len(resolution)),
            "closed_without_reply": unanswered_closed,
        },
        "replies": {
            "total": total_replies,
            "channels": sorted(
                (
                    {"channel": k, "count": v, "share": share(v, total_replies)}
                    for k, v in replies_by_channel.items()
                ),
                key=lambda c: -c["count"],
            ),
            "workflow_actions": workflow_actions,
        },
        "outreach": {
            "sent": outbound,
            "opened": opened,
            "replied": replied,
            "open_share": share(opened, outbound),
            "reply_share": share(replied, outbound),
        },
        "ratings": {
            **ratings,
            "total": rated,
            "great_share": share(ratings["great"], rated),
            "unhappy": sorted(unhappy, key=lambda u: u["at"] or "", reverse=True)[:8],
        },
        "customers": {
            "total": len(people),
            "returning": returning,
            "new": len(people) - returning,
            "repeat": sorted(
                (p for p in people.values() if p["count"] >= 3),
                key=lambda p: (-p["count"], p["name"]),
            )[:8],
        },
        "topics": topics[:14],
        "rising": rising,
        "rhythm": {"zone": "UTC", "grid": grid, "peak": max(map(max, grid))},
    }


def rounded(value):
    return None if value is None else round(value, 1)


def share(part, whole):
    return round(part / whole * 100) if whole else None


def cached(db, months=12, refresh=False, lifetime=600):
    """A recent report, shared by every worker through one small table.

    A report up to `lifetime` seconds old is served as it is. An older one is still served
    at once while a fresh one is computed in the background, so only the very first reader
    waits. A refresh is honoured at most twice a minute. The queue is small and read on
    every request, so "right now" is never a stale copy.
    """
    path = db.execute("PRAGMA database_list").fetchone()[2]
    saved = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='insight_reports'").fetchone():
        row = db.execute("SELECT computed,report FROM insight_reports WHERE months=?", (months,)).fetchone()
        if row:
            saved = (row[0], json.loads(row[1]))
    age = time.time() - saved[0] if saved else None
    if saved is None:
        result = report(db, months)
        _store(path, months, result)
    else:
        result = saved[1]
        if age >= (30 if refresh else lifetime):
            _refresh(path, months)
    return {**result, "queue": queue(db, datetime.now(timezone.utc))}


def _store(path, months, result):
    import sqlite3

    writer = sqlite3.connect(path, timeout=15)
    try:
        with writer:
            writer.execute(
                "CREATE TABLE IF NOT EXISTS insight_reports(months INTEGER PRIMARY KEY,computed REAL NOT NULL,report TEXT NOT NULL)"
            )
            writer.execute(
                "INSERT INTO insight_reports VALUES(?,?,?) ON CONFLICT(months) DO UPDATE SET computed=excluded.computed,report=excluded.report",
                (months, time.time(), json.dumps(result, ensure_ascii=False)),
            )
    finally:
        writer.close()


def _refresh(path, months):
    """Compute a new report on a background thread; one at a time per period in this process."""
    key = (path, months)
    with _lock:
        if key in _cache:
            return
        _cache[key] = True

    def work():
        import sqlite3

        try:
            reader = sqlite3.connect(path, timeout=15)
            reader.row_factory = sqlite3.Row
            try:
                _store(path, months, report(reader, months))
            finally:
                reader.close()
        except Exception:  # The stale report keeps being served; the next reader retries.
            import logging

            logging.exception("Insights refresh failed")
        finally:
            with _lock:
                _cache.pop(key, None)

    threading.Thread(target=work, name="insights-refresh", daemon=True).start()
