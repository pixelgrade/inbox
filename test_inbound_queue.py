import pytest

import inbound_queue


def test_only_exact_s3_events_and_decoded_private_prefix():
    config = ("private-inbox", "raw/")
    assert (
        inbound_queue.object_keys(
            {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": config[0]},
            *config,
        )
        == []
    )
    row = {
        "eventSource": "aws:s3",
        "eventName": "ObjectCreated:Put",
        "s3": {"bucket": {"name": config[0]}, "object": {"key": "raw%2Fone%2Btwo"}},
    }
    assert inbound_queue.object_keys({"Records": [row]}, *config) == ["raw/one+two"]
    row["s3"]["bucket"]["name"] = "other"
    with pytest.raises(ValueError):
        inbound_queue.object_keys({"Records": [row]}, *config)


class MailObjects:
    def __init__(self, messages):
        self.messages = messages
        self.read = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):
        for key in sorted(self.messages):
            yield {"Contents": [{"Key": key, "LastModified": self.messages[key][0]}]}

    def get_object(self, *, Bucket, Key):
        from io import BytesIO

        self.read.append(Key)
        received, raw = self.messages[Key]
        return {
            "LastModified": received,
            "ContentLength": len(raw),
            "Body": BytesIO(raw),
        }


class EmptyQueue:
    def receive_message(self, **kwargs):
        return {}


@pytest.mark.parametrize("seconds", [1, 0])
def test_cutover_replays_parent_before_reply_across_listing_pages(
    tmp_path, monkeypatch, seconds
):
    from datetime import datetime, timedelta, timezone
    from test_transport import db_at, mime
    import transport

    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setattr(transport, "group_signature", lambda *_: True)
    at = datetime.now(timezone.utc)
    s3 = MailObjects(
        {
            "raw/a-child": (
                at + timedelta(seconds=seconds),
                mime("<child@example.org>", reply="<parent@example.org>"),
            ),
            "raw/z-parent": (at, mime("<parent@example.org>")),
        }
    )
    inbound_queue.sweep(db, s3, "test", "raw/", False, None)
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM threads").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1
    s3.read.clear()
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert not s3.read


def test_late_source_identity_reconciles_without_a_second_object_notification(
    tmp_path, monkeypatch
):
    from datetime import datetime, timedelta, timezone
    from test_transport import db_at, mime
    import store
    import transport

    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setattr(transport, "group_signature", lambda *_: True)
    at = datetime.now(timezone.utc)
    s3 = MailObjects(
        {"raw/old": (at - timedelta(seconds=1), mime("<held@example.org>"))}
    )
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert (
        db.execute("SELECT disposition FROM inbound_objects").fetchone()[0]
        == "held_cutover"
    )
    s3.read.clear()
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert not s3.read
    store.import_conversation(
        db,
        {
            "id": 8,
            "mailboxId": 1,
            "status": "active",
            "primaryCustomer": {"id": 4, "email": "someone@example.org"},
        },
        [{"id": 9, "type": "customer", "customer": {"email": "someone@example.org"}}],
    )
    db.execute("INSERT INTO mail_ids VALUES(?,?,?)", ("<held@example.org>", 8, 9))
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert tuple(
        db.execute(
            "SELECT disposition,conversation,thread FROM inbound_objects"
        ).fetchone()
    ) == ("represented_in_source", 8, 9)
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0] == 0


def test_out_of_order_notification_and_expired_parent_window(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from test_transport import db_at, mime
    import transport

    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setattr(transport, "group_signature", lambda *_: True)
    at = datetime.now(timezone.utc)
    s3 = MailObjects(
        {
            "raw/child": (
                at,
                mime("<child@example.org>", reply="<parent@example.org>"),
            ),
            "raw/orphan": (
                at,
                mime(
                    "<orphan@example.org>",
                    "other@example.org",
                    reply="<unknown@example.org>",
                ),
            ),
        }
    )
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert db.execute("SELECT count(*) FROM intake_parent_wait").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0
    s3.read.clear()
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert not s3.read
    s3.messages["raw/parent"] = (at, mime("<parent@example.org>"))
    inbound_queue.process_object(db, s3, "test", "raw/parent", True, at)
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM threads").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM intake_parent_wait").fetchone()[0] == 1
    db.execute("UPDATE intake_parent_wait SET expires=unixepoch()-1")
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    assert db.execute("SELECT count(*) FROM intake_parent_wait").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 2


def test_pending_replay_preserves_original_hash_and_writer_fence(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import hashlib
    import operations
    from test_transport import db_at, mime
    import transport

    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setattr(transport, "group_signature", lambda *_: True)
    at = datetime.now(timezone.utc)
    key = "raw/reply"
    raw = mime("<child@example.org>", reply="<parent@example.org>")
    s3 = MailObjects({key: (at, raw)})
    inbound_queue.run(db, EmptyQueue(), "queue", s3, "test", "raw/", True, at)
    db.execute("UPDATE intake_parent_wait SET expires=0")
    db.execute("UPDATE writer_authority SET mode='paused'")
    inbound_queue.retry_receipts(db, s3, "test", at)
    assert (
        db.execute("SELECT disposition FROM inbound_objects").fetchone()[0]
        == "awaiting_parent"
    )
    assert db.execute("SELECT count(*) FROM intake_parent_wait").fetchone()[0] == 1
    db.execute("UPDATE writer_authority SET mode='live'")
    s3.messages[key] = (at, mime("<changed@example.org>"))
    inbound_queue.retry_receipts(db, s3, "test", at)
    assert tuple(
        db.execute("SELECT disposition,sha FROM inbound_objects").fetchone()
    ) == ("held_object_changed", hashlib.sha256(raw).hexdigest())
    assert operations.counts(db) == {"Incoming email": 1}
    assert db.execute("SELECT count(*) FROM intake_parent_wait").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0
