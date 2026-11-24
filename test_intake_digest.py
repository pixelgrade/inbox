"""The intake summary lists what was filed and puts first what a person should check."""

from datetime import datetime, timedelta, timezone

import intake_digest
import intake_triage
import store
import transport
from test_intake_triage import mail
from test_transport import db_at


def test_summary_counts_filings_and_surfaces_the_doubtful_ones(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setenv("INBOX_TRIAGE_MODE", "act")
    promo = lambda mid, who: mail(mid, author=who, subject="Sale <script>x</script>", text="Savings. Unsubscribe.", X_Mailchimp_ID="1", X_Campaign="bf")
    for key, raw in (("raw/p1", promo("<p1@example.org>", "deals@shop.example")), ("raw/p2", promo("<p2@example.org>", "offers@store.example")),
                     ("raw/g", mail("<g@example.org>")),
                     ("raw/s", mail("<s@example.org>", author="owner@studio.example", subject="Maple license", text="My Maple theme license will not activate."))):
        assert transport.admit(db, key, raw, verify=lambda *_: True) == "imported"
    conversation = lambda key: db.execute("SELECT conversation FROM intake_triage WHERE object_key=?", (key,)).fetchone()[0]
    with store.transaction(db):
        # Someone reopened the second filing: it is probably a wrong one.
        store.update_conversation(db, conversation("raw/p2"), {"status": "active"})
        # The guest got the wrong-address reply.
        store.update_conversation(db, conversation("raw/g"), {"tags": [{"tag": "possible-wrong-address"}, {"tag": "wrong-address"}], "status": "closed"})
    report = intake_digest.summary(db, 7)
    assert report["checked"] == 4
    assert (report["counts"]["promotion"], report["counts"]["wrong_address"], report["counts"]["support"]) == (2, 1, 1)
    assert [i["id"] for i in report["filed"]] == [conversation("raw/p1"), conversation("raw/p2")]
    assert [i["id"] for i in report["look"]] == [conversation("raw/p2")]
    assert [(i["id"], i["outcome"]) for i in report["flagged"]] == [(conversation("raw/g"), "wrong-address reply sent")]
    assert report["filed"][0]["sender"] == "shop.example"  # The domain, never the full address.
    note = intake_digest.render(report)
    assert "Worth a look" in note and "&lt;script&gt;" in note and "<script>" not in note
    assert "https://inbox.woodland.example/triage" in note
    # Older verdicts fall outside the period.
    later = intake_digest.summary(db, 7, datetime.now(timezone.utc) + timedelta(days=30))
    assert later["checked"] == 0 and "No filed message was reopened" in intake_digest.render(later)


def test_summary_is_posted_once_as_an_internal_conversation(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setenv("INBOX_AGENT_ID", "19")
    with store.transaction(db):
        report = intake_digest.summary(db, 7)
        first = intake_digest.post(db, report)
        again = intake_digest.post(db, intake_digest.summary(db, 7))
    assert first == again
    conv = store.conversation(db, first, True)
    assert conv["status"] == "active" and conv["assignee"]["id"] == 19
    assert [t["tag"] for t in conv["tags"]] == ["agent-ignore", "non-support", "intake-summary"]
    assert [t["type"] for t in conv["_embedded"]["threads"]] == ["note"]
    assert db.execute("SELECT count(*) FROM mail_outbox").fetchone()[0] == 0  # Nothing is emailed.
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 0
    assert not db.execute("SELECT 1 FROM intake_triage WHERE conversation=?", (first,)).fetchone()
    # It never counts as a customer contact for the mailbox's own address.
    with store.transaction(db):
        db.execute("UPDATE writer_authority SET mode='standby'")
    try:
        with store.transaction(db):
            intake_digest.post(db, dict(report, to="2099-01-01T00:00:00Z"))
        raise AssertionError("posting must need the live writer")
    except store.WriterPaused:
        pass
