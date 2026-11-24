"""Process S3 object notifications and periodically verify that the queue missed no raw mail."""

from contextlib import closing
from urllib.parse import unquote_plus
import hashlib
import json
import time

import store

SWEEP_SECONDS = 24 * 60 * 60
PARENT_WAIT_SECONDS = 300


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS intake_sweep(
            id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT NOT NULL,completed INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS intake_event_errors(
            id TEXT PRIMARY KEY,payload TEXT NOT NULL,reason TEXT NOT NULL,received TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS intake_parent_wait(
            object_key TEXT PRIMARY KEY,refs TEXT NOT NULL,expires INTEGER NOT NULL);
    """)


def object_keys(event, bucket, prefix):
    if (
        event.get("Event") == "s3:TestEvent"
        and event.get("Bucket") == bucket
        and event.get("Service") == "Amazon S3"
    ):
        return []
    records = event.get("Records")
    if not isinstance(records, list) or not records:
        raise ValueError("Missing S3 object records")
    keys = []
    for record in records:
        item = record["s3"]
        key = unquote_plus(item["object"]["key"])
        if (
            record.get("eventSource") != "aws:s3"
            or not record["eventName"].startswith("ObjectCreated:")
            or item["bucket"]["name"] != bucket
            or not key.startswith(prefix)
            or key == prefix
        ):
            raise ValueError("Unexpected S3 event")
        keys.append(key)
    return keys


def needs_object(db, key, active):
    prev = db.execute(
        "SELECT disposition FROM inbound_objects WHERE object_key=?", (key,)
    ).fetchone()
    return prev is None or (active and prev[0] == "shadow")


def process_object(db, s3, bucket, key, active, cutoff, verify=None, retry=False):
    import transport

    if key.endswith("AMAZON_SES_SETUP_NOTIFICATION"):
        return
    if not retry and not needs_object(db, key, active):
        return
    response = s3.get_object(Bucket=bucket, Key=key)
    with closing(response["Body"]) as stream:
        digest = None
        if response["ContentLength"] > transport.MAX_RAW:
            reason = "held_size"
        else:
            raw = stream.read(transport.MAX_RAW + 1)
            if len(raw) != response["ContentLength"]:
                raise OSError("Incomplete S3 object read")
            try:
                promotion = None
                if active:
                    promotion = (
                        "after_cutover"
                        if response["LastModified"] >= cutoff
                        else "before_cutover"
                    )
                transport.admit(
                    db, key, raw, active, verify or transport.group_signature, promotion
                )
                return
            except (
                store.Invalid,
                store.Conflict,
                ValueError,
                UnicodeError,
                LookupError,
            ):
                reason = "held_parse"
                digest = hashlib.sha256(raw).hexdigest()
    with store.transaction(db):
        recorded = db.execute(
            """INSERT INTO inbound_objects VALUES(?,?,NULL,?,NULL,NULL,?)
               ON CONFLICT(object_key) DO UPDATE SET
                  disposition=CASE WHEN sha IS NOT excluded.sha THEN 'held_object_changed'
                                   ELSE excluded.disposition END
               WHERE disposition IN ('shadow','held_cutover','awaiting_parent')""",
            (key, digest, reason, store.now()),
        )
        if recorded.rowcount:
            db.execute("DELETE FROM intake_parent_wait WHERE object_key=?", (key,))


def sweep(db, s3, bucket, prefix, active, cutoff):
    pending = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        for item in page.get("Contents", []):
            if needs_object(db, item["Key"], active):
                pending.append((item["LastModified"], item["Key"]))
    for _, key in sorted(pending):
        process_object(db, s3, bucket, key, active, cutoff)


def retry_receipts(db, s3, bucket, cutoff):
    # Use the index only to schedule a fresh verification of the original object.
    rows = db.execute(
        """
        SELECT i.object_key FROM inbound_objects i WHERE
        i.disposition='shadow' OR
        (i.disposition='held_cutover' AND EXISTS (
            SELECT 1 FROM mail_ids m JOIN threads t ON t.id=m.thread AND t.conversation_id=m.conversation
            WHERE m.mid=i.mid AND t.id<1000000000000 AND json_extract(t.data,'$.type')='customer'))
        UNION ALL
        SELECT w.object_key FROM intake_parent_wait w WHERE w.expires<=? OR EXISTS (
            SELECT 1 FROM json_each(w.refs) ref JOIN mail_ids m ON m.mid=ref.value)
        ORDER BY object_key LIMIT 50
    """,
        (int(time.time()),),
    ).fetchall()
    for (key,) in rows:
        process_object(db, s3, bucket, key, True, cutoff, retry=True)


def run(db, sqs, queue, s3, bucket, prefix, active, cutoff):
    mode = cutoff.isoformat() if active else "shadow"
    last = db.execute(
        "SELECT completed FROM intake_sweep WHERE id=1 AND mode=?", (mode,)
    ).fetchone()
    if not last or time.time() - last[0] >= SWEEP_SECONDS:
        sweep(db, s3, bucket, prefix, active, cutoff)
        with store.transaction(db):
            db.execute(
                "INSERT OR REPLACE INTO intake_sweep VALUES(1,?,?)",
                (mode, int(time.time())),
            )
    if active:
        retry_receipts(db, s3, bucket, cutoff)
    for item in sqs.receive_message(
        QueueUrl=queue, WaitTimeSeconds=20, MaxNumberOfMessages=10
    ).get("Messages", []):
        try:
            keys = object_keys(json.loads(item["Body"]), bucket, prefix)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            with store.transaction(db):
                db.execute(
                    "INSERT OR IGNORE INTO intake_event_errors VALUES(?,?,?,?)",
                    (
                        item["MessageId"],
                        item["Body"],
                        type(error).__name__,
                        store.now(),
                    ),
                )
        else:
            for key in keys:
                process_object(db, s3, bucket, key, active, cutoff)
        sqs.delete_message(QueueUrl=queue, ReceiptHandle=item["ReceiptHandle"])
