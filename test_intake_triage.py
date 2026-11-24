"""Non-support mail is recognised from headers, shape and sender history, and filed only on request."""

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import intake_triage
import store
import transport
from test_transport import db_at


def mail(mid, author="guest@example.org", subject="Table for four on Friday", text="Could we book a table for four at eight?",
         html=None, **headers):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = author
    msg["To"] = transport.ADDRESS
    msg["Sender"] = transport.ADDRESS
    msg["List-ID"] = "<hello.woodland.example>"
    # A forwarding list rewrites these on every message, so they say nothing about the sender.
    msg["List-Unsubscribe"] = "<mailto:hello+unsubscribe@woodland.example>"
    msg["Precedence"] = "list"
    msg["Message-ID"] = mid
    msg["Subject"] = subject
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def parsed(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def verdict(raw, **known):
    msg = parsed(raw)
    author, thread = transport.extract_message(msg)
    return intake_triage.assess(msg, str(msg["Subject"]), thread["text"], author,
                                own_domains=("woodland.example",), vocabulary=("maple", "woodland"), **known)


NEWSLETTER = ('<table><tr><td><img src="https://cdn.example/a.png"><img src="https://cdn.example/b.png">'
              '<img src="https://cdn.example/c.png"><a href="https://x.example/1">Shop</a><a href="https://x.example/2">Deals</a>'
              '<a href="https://x.example/3">New</a><a href="https://x.example/4">Blog</a>'
              '<a href="https://x.example/u">Unsubscribe</a></td></tr></table>')


GROUP_FOOTER = ("\n-- \nYou received this message because you are subscribed to the Google Groups \"hello\" group.\n"
                "To unsubscribe from this group and stop receiving emails from it, send an email to hello+unsubscribe@woodland.example.\n"
                "To view this discussion visit https://groups.google.com/a/woodland.example/d/msgid/hello/abc%40mail.example.org.\n")


def test_the_forwarding_list_never_counts_against_the_sender(tmp_path, monkeypatch):
    """Regression for the first live message, 24 November 2026: a first-time sender was filed
    because Google Groups' own one-click header and footer were read as the sender's."""
    stamped = dict(List_Unsubscribe_Post="List-Unsubscribe=One-Click", List_Archive="<https://groups.google.com/a/woodland.example/group/hello/>",
                   List_Help="<https://support.google.com/a/woodland.example/bin/topic.py?topic=25838>", List_Post="<mailto:hello@woodland.example>",
                   X_BeenThere="hello@woodland.example", X_Google_Group_Id="123", X_Spam_Checked_In_Group="hello@woodland.example")
    for text, expected in (("We would like to talk about our Woodland site and what we could do together.", "support"),
                           ("Could we book a table for four at eight on Friday?", "wrong_address"),
                           ("My Maple theme license will not activate after the update.", "support")):
        seen = verdict(mail("<g1@example.org>", author="kim@studio.example", subject="A question", text=text + GROUP_FOOTER, **stamped))
        assert seen["category"] == expected, (text, seen)
        assert not intake_triage.acts(seen)
        assert not any("unsubscribe" in reason for reason in seen["reasons"])
    # End to end in filing mode: stays open, is acknowledged, carries no filing tags.
    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setenv("INBOX_TRIAGE_MODE", "act")
    raw = mail("<g2@example.org>", author="kim@studio.example", subject="Partnership with Woodland",
               text="We would like to talk about our Woodland site." + GROUP_FOOTER, **stamped)
    assert transport.admit(db, "raw/live", raw, verify=lambda *_: True) == "imported"
    row = db.execute("SELECT conversation,filed FROM intake_triage WHERE object_key='raw/live'").fetchone()
    conv = store.conversation(db, row[0])
    assert (row[1], conv["status"], conv["tags"]) == (0, "active", [])
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1
    # A real newsletter relayed by the list is still recognised by the sender's own headers.
    news = verdict(mail("<g3@example.org>", author="news@shop.example", subject="November deals",
                        text="Big savings. Click here to unsubscribe from our newsletter." + GROUP_FOOTER,
                        X_Mailchimp_ID="42", **stamped))
    assert news["category"] == "promotion" and intake_triage.acts(news)
    # Without any list in between, the sender's own one-click header does count.
    direct = parsed(mail("<g4@example.org>", author="news@shop.example", subject="Deals", text="Unsubscribe here."))
    del direct["List-ID"], direct["List-Unsubscribe"], direct["Precedence"]
    direct["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    direct["List-Unsubscribe"] = "<https://shop.example/u/1>"
    author, thread = transport.extract_message(direct)
    assert intake_triage.acts(intake_triage.assess(direct, "Deals", thread["text"], author, own_domains=("woodland.example",)))


def test_support_mail_stays_support():
    plain = verdict(mail("<a@example.org>", subject="Maple header overlaps the logo",
                         text="After updating the theme the header overlaps my logo. How can I fix it?"))
    assert plain["category"] == "support" and plain["reasons"] == []
    # A customer quoting our newsletter footer in a reply remains a customer.
    quoted = verdict(mail("<b@example.org>", subject="Re: Your license", text="Thanks! PS please unsubscribe me from news."),
                     answered_before=True, contacts_before=3, replies_to_us=True)
    assert quoted["category"] == "support"
    # The list's own unsubscribe header never counts against the sender.
    assert "List-Unsubscribe points to the sender" not in str(plain)


def test_bulk_promotion_is_recognised_from_headers_and_layout():
    by_header = verdict(mail("<c@example.org>", author="deals@shop.example", subject="Black Friday starts now",
                             text="Big savings this week. Unsubscribe here.", X_Mailchimp_ID="42", X_Mailer="Mailchimp Mailer"))
    assert (by_header["category"], by_header["confidence"]) == ("promotion", "high")
    assert intake_triage.acts(by_header)
    assert any("x-mailchimp-id" in reason for reason in by_header["reasons"])
    by_layout = verdict(mail("<d@example.org>", author="newsletter@shop.example", subject="Our November news",
                             text="View this email in your browser", html=NEWSLETTER))
    assert by_layout["category"] == "promotion" and intake_triage.acts(by_layout)  # Layout plus a robot sender.
    person = verdict(mail("<d2@example.org>", author="sam@shop.example", subject="Our November news",
                          text="View this email in your browser", html=NEWSLETTER))
    assert person["category"] == "promotion" and not intake_triage.acts(person)  # Content alone only flags.
    pitch = verdict(mail("<e@example.org>", author="sam@agency.example", subject="Collaboration Proposal",
                         text="I can offer a guest post with a backlink to increase your traffic."))
    assert pitch["category"] == "promotion" and not intake_triage.acts(pitch)  # One signal alone only flags.


def test_real_customers_behind_relays_and_odd_wording_are_never_filed():
    # Mail providers and relays stamp personal mail with headers that bulk senders also use.
    proton = verdict(mail("<u1@example.org>", author="kim@proton.example", subject="License",
                          text="My Maple theme license will not activate.", Feedback_ID="abc:user:proton",
                          X_Pm_Message_ID="x1"))
    assert proton["category"] == "support"
    relay = verdict(mail("<u2@example.org>", author="kim@studio.example", subject="Question",
                         text="Can you help me with the header?", Feedback_ID="1.eu-west-1.abc:AmazonSES"))
    assert not intake_triage.acts(relay)
    crm = verdict(mail("<u3@example.org>", author="kim@studio.example", subject="Question about my site",
                       text="The header overlaps my logo.", X_Mailer="Salesforce"))
    assert not intake_triage.acts(crm)
    sendgrid = verdict(mail("<u4@example.org>", author="kim@studio.example", subject="Help needed",
                            text="The menu is broken on mobile.", X_SG_EID="abc", X_SG_ID="def"))
    assert not intake_triage.acts(sendgrid)
    wording = verdict(mail("<u5@example.org>", author="kim@studio.example", subject="Help",
                           text="We outsourced our site build and installed your theme. Please unsubscribe me from "
                                "your marketing emails, I only need help activating the license."))
    assert not intake_triage.acts(wording)
    # Our own sites send through the same services.
    own = verdict(mail("<u6@example.org>", author="noreply@woodland.example", subject="Your November receipt",
                       text="Unsubscribe from receipts.", X_SG_EID="abc", X_Mailer="SendGrid"))
    assert own["category"] == "support"
    # A customer we answered before is only ever flagged, whatever their site or mail tool adds.
    known = verdict(mail("<u7@example.org>", author="wordpress@bistro.example", subject="[Bistro] New reservation",
                         text="This e-mail was sent from a contact form on Bistro.", X_Mailer="PHPMailer 6.9.1"),
                    answered_before=True, contacts_before=2)
    assert known["category"] == "site_mail" and not intake_triage.acts(known)
    reply_bounce = verdict(mail("<u8@example.org>", author="MAILER-DAEMON@mx.example", subject="Undeliverable: Re: Your license",
                                text="The address was not found."), replies_to_us=True)
    assert reply_bounce["category"] == "bounce" and not intake_triage.acts(reply_bounce)  # A failed reply needs eyes.


def test_hostile_bodies_are_read_in_linear_time():
    import time

    started = time.perf_counter()
    assessed = verdict(mail("<v@example.org>", text="x", html="<" * 300_000))
    assert time.perf_counter() - started < 2
    assert assessed["category"] in intake_triage.CATEGORIES


def test_mail_generated_by_a_website_is_recognised():
    form = verdict(mail("<f@example.org>", author="wordpress@bistro.example", subject="[Bistro Verde] New reservation",
                        text="Name: Kim\nGuests: 4\n\nThis e-mail was sent from a contact form on Bistro Verde (https://bistro.example)",
                        X_Mailer="PHPMailer 6.9.1 (https://github.com/PHPMailer/PHPMailer)"))
    assert (form["category"], form["confidence"]) == ("site_mail", "high") and intake_triage.acts(form)
    assert "evidence" in form and "x-mailer" in form["evidence"]["headers"]
    own = verdict(mail("<g@example.org>", author="wordpress@woodland.example", subject="[Woodland] New order",
                       text="Order received.", X_Mailer="PHPMailer 6.9.1"))
    assert own["category"] != "site_mail"  # Our own sites' notifications are not misdirected.


def test_bounces_and_automatic_replies():
    bounce = verdict(mail("<h@example.org>", author="MAILER-DAEMON@mx.example", subject="Undeliverable: Your website",
                          text="The address was not found."))
    assert (bounce["category"], bounce["confidence"]) == ("bounce", "high")
    away = verdict(mail("<i@example.org>", subject="Automatic reply: Your website", text="I am away until Monday.",
                        X_Autoreply="yes"))
    assert away["category"] == "auto_reply" and intake_triage.acts(away)


def test_a_person_writing_to_the_wrong_business_is_flagged_never_filed():
    guest = verdict(mail("<j@example.org>"))
    assert (guest["category"], guest["confidence"]) == ("wrong_address", "low")
    assert not intake_triage.acts(guest)
    assert verdict(mail("<k@example.org>"), contacts_before=1)["category"] == "support"
    assert verdict(mail("<l@example.org>", text="Could we book a table? I found you through your Maple site."))["category"] == "support"
    assert verdict(mail("<m@example.org>", subject="Re: Table for four"))["category"] == "support"


def test_observe_mode_records_verdicts_and_changes_nothing(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    raw = mail("<n@example.org>", author="deals@shop.example", subject="Sale", text="Savings. Unsubscribe.", X_Mailchimp_ID="1", X_Campaign="bf")
    assert transport.admit(db, "raw/promo", raw, verify=lambda *_: True) == "imported"
    row = db.execute("SELECT conversation,category,confidence,filed FROM intake_triage WHERE object_key='raw/promo'").fetchone()
    assert tuple(row[1:]) == ("promotion", "high", 0)
    conv = store.conversation(db, row[0])
    assert (conv["status"], conv["tags"]) == ("active", [])
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1
    assert intake_triage.for_conversation(db, row[0])["category"] == "promotion"
    # Before handover, shadow mail is assessed too and still creates nothing.
    with store.transaction(db):
        db.execute("UPDATE writer_authority SET mode='standby'")
    assert transport.admit(db, "raw/shadow", mail("<o@example.org>"), verify=lambda *_: True) == "shadow"
    assert db.execute("SELECT category,conversation FROM intake_triage WHERE object_key='raw/shadow'").fetchone()[:] == ("wrong_address", None)
    monkeypatch.setenv("INBOX_TRIAGE_MODE", "off")
    assert transport.admit(db, "raw/off", mail("<p@example.org>"), verify=lambda *_: True) == "shadow"
    assert not db.execute("SELECT 1 FROM intake_triage WHERE object_key='raw/off'").fetchone()


def test_act_mode_files_machine_mail_without_an_acknowledgment(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setenv("INBOX_TRIAGE_MODE", "act")
    promo = mail("<q@example.org>", author="deals@shop.example", subject="Sale", text="Savings. Unsubscribe.", X_Mailchimp_ID="1", X_Campaign="bf")
    assert transport.admit(db, "raw/promo", promo, verify=lambda *_: True) == "imported"
    cid = db.execute("SELECT conversation FROM intake_triage WHERE object_key='raw/promo' AND filed=1").fetchone()[0]
    conv = store.conversation(db, cid)
    assert conv["status"] == "closed"
    assert [t["tag"] for t in conv["tags"]] == ["agent-ignore", "non-support", "promotion"]
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 0
    # A possible wrong address stays open for a person, tagged, and is still acknowledged.
    assert transport.admit(db, "raw/guest", mail("<r@example.org>"), verify=lambda *_: True) == "imported"
    guest = store.conversation(db, db.execute("SELECT conversation FROM intake_triage WHERE object_key='raw/guest'").fetchone()[0])
    assert (guest["status"], [t["tag"] for t in guest["tags"]]) == ("active", ["possible-wrong-address"])
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1
    # Holding that acknowledgment is a separate, explicit choice.
    monkeypatch.setenv("INBOX_TRIAGE_SUSPECT_ACK", "hold")
    assert transport.admit(db, "raw/guest2", mail("<r2@example.org>", author="other@example.org"), verify=lambda *_: True) == "imported"
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1
    monkeypatch.delenv("INBOX_TRIAGE_SUSPECT_ACK")
    # Real support is untouched and acknowledged.
    real = mail("<s@example.org>", author="owner@studio.example", subject="Maple license", text="My Maple theme license will not activate.")
    assert transport.admit(db, "raw/real", real, verify=lambda *_: True) == "imported"
    kept = store.conversation(db, db.execute("SELECT conversation FROM intake_triage WHERE object_key='raw/real'").fetchone()[0])
    assert (kept["status"], kept["tags"]) == ("active", [])
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 2
    # A sender we answered before is never filed on content alone.
    with store.transaction(db):
        store.append_thread(db, kept["id"], "reply", {"text": "<p>Fixed.</p>", "imported": True}, imported=True)
    again = mail("<t@example.org>", author="owner@studio.example", subject="Our studio news",
                 text="View this email in your browser", html=NEWSLETTER)
    assert transport.admit(db, "raw/again", again, verify=lambda *_: True) == "imported"
    assert db.execute("SELECT filed FROM intake_triage WHERE object_key='raw/again'").fetchone()[0] == 0


def test_a_fault_in_the_check_never_stops_the_mail(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setenv("INBOX_TRIAGE_MODE", "act")

    def broken(*_):
        raise RuntimeError("rule fault")

    monkeypatch.setattr(intake_triage, "judge", broken)
    assert transport.admit(db, "raw/1", mail("<w@example.org>"), verify=lambda *_: True) == "imported"
    cid = db.execute("SELECT conversation FROM inbound_objects WHERE object_key='raw/1'").fetchone()[0]
    assert store.conversation(db, cid)["status"] == "active"
    assert db.execute("SELECT count(*) FROM acknowledgments").fetchone()[0] == 1
    assert not db.execute("SELECT 1 FROM intake_triage").fetchone()
    monkeypatch.undo()
    db2 = db_at(tmp_path / "second", monkeypatch) if (tmp_path / "second").mkdir() is None else None
    monkeypatch.setattr(intake_triage, "record", broken)
    assert transport.admit(db2, "raw/2", mail("<x@example.org>"), verify=lambda *_: True) == "imported"
    assert db2.execute("SELECT disposition FROM inbound_objects WHERE object_key='raw/2'").fetchone()[0] == "imported"


def test_a_repeat_wrong_address_writer_is_flagged_again(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setenv("INBOX_TRIAGE_MODE", "act")
    assert transport.admit(db, "raw/first", mail("<y1@example.org>"), verify=lambda *_: True) == "imported"
    first = db.execute("SELECT conversation FROM intake_triage WHERE object_key='raw/first'").fetchone()[0]
    with store.transaction(db):
        # The wrong-address reply went out and the conversation was tagged and closed.
        store.append_thread(db, first, "reply", {"text": "<p>Not the venue.</p>"}, imported=True)
        store.update_conversation(db, first, {"status": "closed", "tags": [{"tag": "possible-wrong-address"}, {"tag": "wrong-address"}]})
    again = mail("<y2@example.org>", subject="Booking for Saturday", text="Do you have room for six on Saturday?")
    # Within a day of the reply the address is not flagged again: one reply per address per day.
    assert transport.admit(db, "raw/same-day", again, verify=lambda *_: True) == "imported"
    assert db.execute("SELECT category FROM intake_triage WHERE object_key='raw/same-day'").fetchone()[0] == "support"
    with store.transaction(db):
        for cid in (first, db.execute("SELECT conversation FROM intake_triage WHERE object_key='raw/same-day'").fetchone()[0]):
            item = store.conversation(db, cid)
            db.execute("UPDATE conversations SET data=json_set(data,'$.createdAt','2026-01-05T10:00:00Z') WHERE id=?", (cid,))
        db.execute("UPDATE conversations SET data=json_set(data,'$.tags',json('[{\"tag\":\"wrong-address\"}]')) WHERE id<>?", (first,))
    later = mail("<y3@example.org>", subject="Booking for Saturday", text="Do you have room for six on Saturday?")
    assert transport.admit(db, "raw/second", later, verify=lambda *_: True) == "imported"
    row = db.execute("SELECT category,conversation FROM intake_triage WHERE object_key='raw/second'").fetchone()
    assert row[0] == "wrong_address" and row[1] != first
    assert [t["tag"] for t in store.conversation(db, row[1])["tags"]] == ["possible-wrong-address"]


def test_a_flagged_sender_we_then_answered_as_support_is_a_customer(tmp_path, monkeypatch):
    db = db_at(tmp_path, monkeypatch)
    monkeypatch.setenv("INBOX_TRIAGE_MODE", "act")
    assert transport.admit(db, "raw/first", mail("<z1@example.org>", text="I cannot log in to my account since yesterday."), verify=lambda *_: True) == "imported"
    first = db.execute("SELECT conversation FROM intake_triage WHERE object_key='raw/first'").fetchone()[0]
    assert [t["tag"] for t in store.conversation(db, first)["tags"]] == ["possible-wrong-address"]
    with store.transaction(db):
        store.append_thread(db, first, "reply", {"text": "<p>Reset link sent.</p>"}, imported=True)
    # Their next message is ordinary history: not flagged, and never treated as a stranger's.
    assert transport.admit(db, "raw/next", mail("<z2@example.org>", subject="Still locked out", text="It still does not work."), verify=lambda *_: True) == "imported"
    assert db.execute("SELECT category FROM intake_triage WHERE object_key='raw/next'").fetchone()[0] == "support"
    # Being answered counts: even their newsletter is only flagged, never filed.
    news = mail("<z3@example.org>", subject="Our news", text="Savings. Unsubscribe.", X_Mailchimp_ID="1", X_Campaign="bf")
    assert transport.admit(db, "raw/news", news, verify=lambda *_: True) == "imported"
    assert db.execute("SELECT category,filed FROM intake_triage WHERE object_key='raw/news'").fetchone()[:] == ("promotion", 0)
