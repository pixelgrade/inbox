"""Product-scoped case tools. Callers own authentication and the transaction."""

import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlsplit

SLUG = re.compile(r"[a-z0-9][a-z0-9.-]{0,79}")
WORDS = re.compile(r"[^\W_]+", re.UNICODE)


class Invalid(ValueError):
    pass


class Missing(LookupError):
    pass


class Conflict(Exception):
    pass


def schema(db):
    from . import assignments

    db.executescript("""
    CREATE TABLE IF NOT EXISTS desk_company(id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS desk_products(id TEXT PRIMARY KEY,name TEXT NOT NULL,revision TEXT NOT NULL,repositories TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS desk_docs(product TEXT NOT NULL REFERENCES desk_products, key TEXT NOT NULL, kind TEXT NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY(product,key));
    CREATE VIRTUAL TABLE IF NOT EXISTS desk_search USING fts5(product UNINDEXED,key UNINDEXED,title,body);
    CREATE TABLE IF NOT EXISTS desk_tokens(digest TEXT PRIMARY KEY,product TEXT NOT NULL REFERENCES desk_products,role TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS desk_cases(id TEXT PRIMARY KEY,product TEXT NOT NULL REFERENCES desk_products, ticket INTEGER, ticket_version TEXT,
        question TEXT NOT NULL, context TEXT NOT NULL, state TEXT NOT NULL, proposal TEXT, citations TEXT, draft_thread INTEGER,
        created INTEGER NOT NULL, operation TEXT NOT NULL, UNIQUE(product,operation));
    CREATE INDEX IF NOT EXISTS desk_product_cases ON desk_cases(product,created DESC);
    CREATE TABLE IF NOT EXISTS desk_jobs(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES desk_cases,product TEXT NOT NULL,
        repo TEXT NOT NULL,revision TEXT NOT NULL,recipe TEXT NOT NULL,patch TEXT NOT NULL,operation TEXT NOT NULL,
        state TEXT NOT NULL,lease TEXT,expires INTEGER,result TEXT,created INTEGER NOT NULL,UNIQUE(case_id,operation));
    CREATE INDEX IF NOT EXISTS desk_job_queue ON desk_jobs(product,state,created);
    """)

    assignments.schema(db)
    from . import draft_requests
    draft_requests.schema(db)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def word(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise Invalid("Invalid product or recipe identifier")
    return value


def string(value, limit, field):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise Invalid("Invalid " + field)
    return value


def one(db, query, params):
    row = db.execute(query, params).fetchone()
    if not row:
        raise Missing("Record unavailable in this product")
    return row


def install(db, pack):
    product = word(pack["id"])
    company = string(pack["company"], 180, "company")
    name = string(pack["name"], 180, "product name")
    org = db.execute("SELECT name FROM desk_company WHERE id=1").fetchone()
    if org and org[0] != company:
        raise Conflict("Use a separate installation database for another company")
    docs = pack["documents"]
    repos = pack.get("repositories", [])
    if (
        not isinstance(docs, list)
        or not docs
        or len(docs) > 20000
        or not isinstance(repos, list)
    ):
        raise Invalid("Invalid product pack")
    seen = set()
    for r in repos:
        key = word(r["key"])
        if key in seen or not re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}", r["revision"]):
            raise Invalid("Repository needs a unique key and full Git revision")
        seen.add(key)
        url = urlsplit(r["url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise Invalid("Repository must have a credential-free HTTPS URL")
        string(r["label"], 180, "repository label")
        recipes = [word(t["key"]) for t in r["recipes"]]
        if len(set(recipes)) != len(recipes):
            raise Invalid("Duplicate recipe")
        for t in r["recipes"]:
            string(t["label"], 200, "recipe label")
    rows = []
    seen.clear()
    for doc in docs:
        key = string(doc["key"], 260, "source key")
        if key in seen or doc["kind"] not in ("guide", "voice", "practice", "code"):
            raise Invalid("Invalid or duplicate product source")
        seen.add(key)
        rows.append(
            (
                product,
                key,
                doc["kind"],
                string(doc["title"], 400, "title"),
                string(doc["text"], 1_500_000, "document"),
                string(doc["source"], 1024, "source reference"),
                string(doc["revision"], 256, "source revision"),
            )
        )
    revision = sha(encode(pack))
    db.execute("INSERT OR IGNORE INTO desk_company VALUES(1,?)", (company,))
    db.execute(
        "INSERT INTO desk_products VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,revision=excluded.revision,repositories=excluded.repositories",
        (product, name, revision, encode(repos)),
    )
    db.execute("DELETE FROM desk_search WHERE product=?", (product,))
    db.execute("DELETE FROM desk_docs WHERE product=?", (product,))
    db.executemany("INSERT INTO desk_docs VALUES(?,?,?,?,?,?,?)", rows)
    db.executemany(
        "INSERT INTO desk_search VALUES(?,?,?,?)",
        [(p, k, t, b) for p, k, _, t, b, _, _ in rows],
    )
    return {
        "product": product,
        "revision": revision,
        "documents": len(rows),
        "repositories": len(repos),
    }


def product_info(db, pid):
    row = one(db, "SELECT * FROM desk_products WHERE id=?", (pid,))
    return {
        "id": pid,
        "name": row["name"],
        "company": db.execute("SELECT name FROM desk_company").fetchone()[0],
        "revision": row["revision"],
        "repositories": json.loads(row["repositories"]),
    }


def search(db, pid, query):
    words = list(
        dict.fromkeys(
            w.casefold()
            for w in WORDS.findall(query)
            if len(w) > 2
            and w.lower()
            not in {
                "what",
                "how",
                "the",
                "can",
                "with",
                "and",
                "this",
                "when",
                "does",
                "for",
                "from",
                "that",
            }
        )
    )[:24]
    if not words:
        return []
    expression = " OR ".join('"' + w + '"' for w in words)
    rows = db.execute(
        """SELECT d.key,d.kind,d.title,d.source,d.revision,snippet(desk_search,3,'','', ' … ',96) excerpt
        FROM desk_search JOIN desk_docs d ON d.product=desk_search.product AND d.key=desk_search.key
        WHERE desk_search MATCH ? AND d.product=? AND d.kind IN ('guide','code')
        ORDER BY bm25(desk_search,0,0,8,1),d.key LIMIT 12""",
        (expression, pid),
    )
    return [dict(r) for r in rows]


def context(db, pid, query):
    info = product_info(db, pid)
    mandatory = [
        dict(r)
        for r in db.execute(
            "SELECT key,kind,title,source,revision,body AS excerpt FROM desk_docs WHERE product=? AND kind IN ('practice','voice') ORDER BY kind,key",
            (pid,),
        )
    ]
    return {"product": info, "guidance": mandatory, "evidence": search(db, pid, query)}


def case_row(db, pid, cid):
    return one(db, "SELECT * FROM desk_cases WHERE id=? AND product=?", (cid, pid))


def open_case(db, pid, question, op, ticket=None, version=None):
    question = string(question, 32000, "support question")
    op = string(op, 160, "operation key")
    row = db.execute(
        "SELECT id,question,ticket FROM desk_cases WHERE product=? AND operation=?",
        (pid, op),
    ).fetchone()
    if row:
        if (row["question"], row["ticket"]) != (question, ticket):
            raise Conflict("Operation key already used")
        return row["id"]
    ctx = context(db, pid, question)
    cid = secrets.token_hex(16)
    db.execute(
        "INSERT INTO desk_cases(id,product,ticket,ticket_version,question,context,state,created,operation) VALUES(?,?,?,?,?,?,'investigating',?,?)",
        (cid, pid, ticket, version, question, encode(ctx), int(time.time()), op),
    )
    return cid


def cite(db, pid, cid, keys, revisions=None):
    row = case_row(db, pid, cid)
    if row["state"] != "investigating":
        raise Conflict("Investigation has already been submitted")
    ctx = json.loads(row["context"])
    if not isinstance(keys, list) or not 1 <= len(keys) <= 10:
        raise Invalid("Choose up to ten evidence sources")
    if revisions is not None and not isinstance(revisions, dict):
        raise Invalid("Invalid source revisions")
    revisions = revisions or {}
    for key in keys:
        string(key, 260, "source key")
        saved = next(
            (d for d in ctx["evidence"] + ctx["guidance"] if d["key"] == key), None
        )
        if saved:
            if key in revisions and revisions[key] != saved["revision"]:
                raise Conflict("Evidence changed since the case was opened")
            continue
        doc = one(
            db,
            "SELECT key,kind,title,source,revision,substr(body,1,12000) excerpt FROM desk_docs WHERE product=? AND key=?",
            (pid, key),
        )
        if key in revisions and revisions[key] != doc["revision"]:
            raise Conflict("Evidence changed before it could be attached")
        if len(ctx["evidence"]) >= 40:
            raise Invalid("Case evidence limit reached")
        ctx["evidence"].append(dict(doc))
    db.execute("UPDATE desk_cases SET context=? WHERE id=?", (encode(ctx), cid))


def propose(db, pid, cid, answer, citations):
    answer = string(answer, 32000, "proposed reply")
    case = case_row(db, pid, cid)
    ctx = json.loads(case["context"])
    allowed = {d["key"] for d in ctx["evidence"] + ctx["guidance"]}
    allowed.update(
        "test:" + r[0]
        for r in db.execute(
            "SELECT id FROM desk_jobs WHERE case_id=? AND state IN ('passed','failed')",
            (cid,),
        )
    )
    if (
        not isinstance(citations, list)
        or not citations
        or len(citations) > 32
        or not all(isinstance(k, str) and k in allowed for k in citations)
    ):
        raise Invalid(
            "Cite the product evidence or completed test receipts attached to this investigation"
        )
    if case["state"] == "proposed" and (answer, encode(citations)) == (
        case["proposal"],
        case["citations"],
    ):
        return
    if case["state"] != "investigating":
        raise Conflict("A proposal already exists")
    db.execute(
        "UPDATE desk_cases SET state='proposed',proposal=?,citations=? WHERE id=?",
        (answer, encode(citations), cid),
    )


def request_test(db, pid, cid, repo, recipe, patch, op):
    case = case_row(db, pid, cid)
    if not isinstance(patch, str) or len(patch.encode()) > 250000:
        raise Invalid("Patch exceeds 250 KB")
    op = string(op, 160, "operation key")
    supported = json.loads(case["context"])["product"]["repositories"]
    target = next(
        (
            r
            for r in supported
            if r["key"] == repo and any(v["key"] == recipe for v in r["recipes"])
        ),
        None,
    )
    if not target:
        raise Invalid("Unknown repository or test recipe for this case")
    existing = db.execute(
        "SELECT id,repo,recipe,patch FROM desk_jobs WHERE case_id=? AND operation=?",
        (cid, op),
    ).fetchone()
    if existing:
        if tuple(existing)[1:] != (repo, recipe, patch):
            raise Conflict("Test operation key already used")
        return existing["id"]
    if case["state"] != "investigating":
        raise Conflict("This investigation has already been submitted")
    job = secrets.token_hex(16)
    db.execute(
        """INSERT INTO desk_jobs(id,case_id,product,repo,revision,recipe,patch,operation,state,created)
               VALUES(?,?,?,?,?,?,?,?,'queued',?)""",
        (job, cid, pid, repo, target["revision"], recipe, patch, op, int(time.time())),
    )
    return job


def claim(db, pid):
    now = int(time.time())
    row = db.execute(
        "SELECT * FROM desk_jobs WHERE product=? AND (state='queued' OR (state='running' AND expires<?)) ORDER BY created,id LIMIT 1",
        (pid, now),
    ).fetchone()
    if not row:
        return None
    token = secrets.token_hex(32)
    db.execute(
        "UPDATE desk_jobs SET state='running',lease=?,expires=? WHERE id=?",
        (sha(token), now + 600, row["id"]),
    )
    return {
        **{k: row[k] for k in ("id", "repo", "revision", "recipe", "patch")},
        "lease": token,
    }


def finish(db, pid, job, lease, result):
    if (
        not isinstance(result, dict)
        or result.get("state") not in ("passed", "failed", "error")
        or len(encode(result)) > 131072
    ):
        raise Invalid("Invalid test receipt")
    row = one(
        db,
        "SELECT state,lease,expires,result,revision FROM desk_jobs WHERE id=? AND product=?",
        (job, pid),
    )
    if not secrets.compare_digest(row["lease"] or "", sha(string(lease, 128, "lease"))):
        raise Conflict("Test lease changed")
    if result.get("revision") != row["revision"]:
        raise Conflict("Runner checked a different revision")
    if row["state"] != "running":
        if row["result"] == encode(result):
            return
        raise Conflict("Test already completed")
    if row["expires"] < int(time.time()):
        raise Conflict("Test lease expired")
    db.execute(
        "UPDATE desk_jobs SET state=?,result=? WHERE id=?",
        (result["state"], encode(result), job),
    )


def test_patch(db, pid, cid, job):
    row = one(
        db,
        """SELECT patch,repo,revision FROM desk_jobs WHERE product=? AND case_id=? AND id=?""",
        (pid, cid, job),
    )
    if not row["patch"]:
        raise Missing("This test ran against the original source")
    return {
        "patch": row["patch"],
        "repo": row["repo"],
        "revision": row["revision"],
        "sha256": sha(row["patch"]),
    }


def case_details(db, pid, cid):
    row = dict(case_row(db, pid, cid))
    row["context"] = json.loads(row["context"])
    row["citations"] = json.loads(row["citations"] or "[]")
    row["jobs"] = [
        {**dict(r), "result": json.loads(r["result"] or "null")}
        for r in db.execute(
            "SELECT id,repo,revision,recipe,state,result,length(patch)>0 AS has_patch FROM desk_jobs WHERE case_id=? ORDER BY created,id",
            (cid,),
        )
    ]
    return row
