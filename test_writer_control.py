import json

import pytest

import store
import writer_control


def test_standby_prevents_writes_and_activation_requires_complete_history(tmp_path, monkeypatch):
    monkeypatch.setenv("INBOX_WEBHOOK_IDS", "11,12,13,14")
    db = store.connect(tmp_path / "fixture.db")
    store.initialize(db)
    for key in (11, 12, 13, 14):
        db.execute(
            "INSERT INTO webhooks VALUES(?,?)", (key, json.dumps({"state": "disabled"}))
        )
    writer_control.change(db, "standby", "live")
    with pytest.raises(store.WriterPaused):
        store.require_writer(db)
    with pytest.raises(store.Conflict):
        writer_control.change(db, "live", "standby", {"reconciled": False})
    assert writer_control.status(db)["callbacks"] == {"disabled": 4}
    store.save_mailbox(db, {"id": 1})
    store.import_conversation(
        db,
        {
            "id": 3,
            "mailboxId": 1,
            "status": "active",
            "primaryCustomer": {"id": 9, "email": "fixture@example.org"},
        },
        [{"id": 21, "type": "customer", "state": "published"}],
    )
    db.execute("CREATE TABLE source_hash(kind TEXT,id INTEGER,digest BLOB)")
    db.execute("INSERT INTO source_hash VALUES('conversations',3,x'01')")
    report = {
        "full_comparison": True,
        "reconciled": True,
        "differences": {},
        "serving_revision": writer_control.revision(db),
        "counts": {
            "hosted_mailboxes": 1,
            "hosted_customers": 1,
            "hosted_conversations": 1,
            "compared_hosted_threads": 1,
        },
    }
    db.execute("UPDATE source_hash SET digest=x'02'")
    with pytest.raises(store.Conflict, match="serving revision"):
        writer_control.change(db, "live", "standby", report)
    report["serving_revision"] = writer_control.revision(db)
    with pytest.raises(store.Conflict, match="frozen"):
        writer_control.change(db, "live", "standby", report)
    report["counts"].update(asset_index_sha256="3" * 64, asset_bytes=0)
    report["frozen_inputs"] = {
        "source_sha256": "1" * 64, "serving_sha256": "2" * 64,
        "asset_index_sha256": "3" * 64, "asset_bytes": 0,
    }
    writer_control.change(db, "live", "standby", report)
    store.require_writer(db)
    assert writer_control.status(db)["callbacks"] == {"enabled": 4}


def test_native_company_can_open_pause_and_resume_without_a_source_provider(tmp_path, monkeypatch):
    from workbench import core

    monkeypatch.setenv("INBOX_INITIAL_AUTHORITY", "standby")
    db = store.connect(tmp_path / "company.db")
    store.initialize(db)
    core.schema(db)
    db.execute("INSERT INTO desk_company VALUES(1,'Woodland Press')")
    store.save_mailbox(db, {"id": 5, "name": "Woodland Press", "email": "help@woodland.example"})
    for key, state in [(8, "enabled"), (9, "disabled")]:
        db.execute("INSERT INTO webhooks VALUES(?,?)", (key, json.dumps({"state": state})))
    callbacks = {"enabled": 1, "disabled": 1}
    def change(target, expected, name="Woodland Press"):
        return writer_control.change(db, target, expected, native_company=name)

    with pytest.raises(store.Conflict):
        change("live", "standby", "Another company")
    assert change("live", "standby")["callbacks"] == callbacks
    store.require_writer(db)
    assert change("paused", "live")["mode"] == "paused"
    with pytest.raises(store.WriterPaused):
        store.require_writer(db)
    assert change("live", "paused")["callbacks"] == callbacks
    change("paused", "live")
    db.execute("CREATE TABLE source_hash(kind TEXT,id INTEGER,digest BLOB)")
    with pytest.raises(store.Conflict, match="migration"):
        change("live", "paused")
    assert writer_control.status(db)["mode"] == "paused"
