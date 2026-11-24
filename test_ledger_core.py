"""The agent ledger by itself: a mailbox with nothing registered, then one made-up campaign.

An installation's own campaigns, tags and note wordings register through `agent_ledger`'s hooks;
none of the ledger's rules may depend on a particular one."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_ledger
import app
import company
import operator_ui
import store

READER = {"id": 1, "first": "Test", "last": "Reader", "email": "reader@example.test"}
WORDED = "Automation decision note:<br>Mode: draft<br>Confidence: 7/10<br>Why: Not confirmed."


def trial():
    """A made-up campaign: three stages, the customer or the owner waited on, one yes/no fact."""
    agent_ledger.register_campaign(
        "trial", marker="trial-run", label="Trial", stages=("asked", "answered", "closed", "refused"),
        stage_names={"asked": "question asked"}, ended=("closed", "refused"), no_contact=("refused",),
        waiting_tags={"customer": "trial-waiting-customer", agent_ledger.OWNER: "trial-needs-sam"},
        flags={"vip": {"tag": "trial-vip", "on": "marked VIP", "off": "no longer VIP"}}, quiet_stages=("asked",))


class Core(unittest.TestCase):
    def setUp(self):
        agent_ledger.reset()
        self.addCleanup(agent_ledger.reset)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        env = patch.dict(os.environ, {
            "INBOX_DB": str(Path(self.scratch.name) / "mail.db"), "INBOX_BASE_URL": "https://inbox.test",
            "INBOX_OPERATOR_WRITES": "1", "INBOX_SEND_ENABLED": "1", "INBOX_AGENT_ID": "19", "INBOX_AGENT_NAME": "Sam",
            "INBOX_CLIENT_ID": "agent", "INBOX_CLIENT_SECRET": "private"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app.app, base_url="https://inbox.test").__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.db = store.connect()
        self.addCleanup(self.db.close)
        with store.transaction(self.db):
            store.save_mailbox(self.db, {"id": company.MAILBOX, "email": "help@example.test"})
            store.save_customer(self.db, {"id": 1, "firstName": "Test", "_embedded": {"emails": [{"value": "reader@example.test"}]}})
            self.db.execute("UPDATE writer_authority SET mode='live'")
            for cid, tags in ((7, ("trial-run", "trial-asked", "trial-waiting-customer", "billing")), (9, ("billing",))):
                store.import_conversation(self.db, {
                    "id": cid, "number": cid, "mailboxId": company.MAILBOX, "type": "email", "state": "published",
                    "status": "active", "subject": "A question", "createdAt": "2026-01-01T10:00:00Z",
                    "modifiedAt": "2026-01-07T10:00:00Z", "primaryCustomer": READER, "tags": [{"tag": t} for t in tags]},
                    [{"id": cid * 10, "type": "customer", "state": "published", "createdAt": "2026-01-01T10:01:00Z",
                      "body": "<p>Hi.</p>", "createdBy": READER}])
            session = operator_ui.issue(self.db, "session", 600)
        self.client.cookies.set(operator_ui.COOKIE, session)
        self.origin = {"Origin": "https://inbox.test"}
        made = self.client.post("/v2/oauth2/token", data={"grant_type": "client_credentials", "client_id": "agent", "client_secret": "private"})
        self.auth = {"Authorization": "Bearer " + made.json()["access_token"]}

    def note(self, cid, body, source="api", **extra):
        with store.transaction(self.db):
            return store.append_thread(self.db, cid, "note", {"text": body, **extra}, source=source)

    def tags(self, cid):
        return sorted(agent_ledger.tags_of(store.conversation(self.db, cid)))

    def change(self, cid, changes, actor="agent-7", **more):
        with store.transaction(self.db):
            return agent_ledger.change_state(self.db, cid, changes, actor, **more)

    def test_a_mailbox_with_nothing_registered_has_records_requests_and_a_handler_but_no_campaigns(self):
        # Wording means nothing here: an agent's note is its writer's note, a typed one is the owner's.
        self.assertEqual([(agent_ledger.parse(WORDED, source)["kind"], agent_ledger.parse(WORDED, source)["actor"])
                          for source in ("api", "web", "beacon-v2")], [("note", "agent"), ("instruction", "owner"), ("note", "inbox")])
        self.assertIsNone(agent_ledger.parse("<p> </p>"))
        named = self.note(9, WORDED, event={"actor": "agent-7"})
        stated = self.note(9, "<p>Any wording.</p>", event={"kind": "decision", "actor": "agent-7", "title": "Draft left for review", "data": {"confidence": 7}})
        by_note = {e["note"]: (e["kind"], e["actor"]) for e in agent_ledger.events(self.db, 9)}
        self.assertEqual((by_note[named], by_note[stated]), (("note", "agent-7"), ("decision", "agent-7")))
        state = agent_ledger.state(self.db, 7)  # The tags of a campaign nobody registered are just tags.
        self.assertEqual((state["handling"], state["campaign"], state["stage"], state["waiting_on"], state["reminders"]["state"]),
                         ("agents", None, None, None, None))
        self.assertEqual(set(state), {"handling", "status", "waiting_on", "campaign", "stage", "reminders", "classification",
                                      "attention", "draft_waiting", "last_decision", "scheduled", "requests"})
        self.assertEqual((agent_ledger.state_fields(), agent_ledger.STAGES, agent_ledger.ENDED, agent_ledger.view(self.db, 7)["stages"]),
                         (agent_ledger.BASE_FIELDS, (), (), []))
        for refused in ({"stage": "asked"}, {"waiting_on": "customer"}, {"reminders": "off"}, {"campaign": "trial"}, {"vip": True}):
            with self.assertRaises(store.Invalid):
                self.change(7, refused)
            self.assertEqual(self.client.patch("/v2/conversations/7/state", headers=self.auth, json={"changes": refused, "actor": "a"}).status_code, 400)
        # Who handles a conversation works by itself, under the installation's hand-off tag.
        self.assertEqual(self.change(9, {"handling": "person"}, reason="Refund.")["handling"], "person")
        self.assertEqual(self.tags(9), sorted(["billing", company.IGNORE_TAG]))
        with self.assertRaises(store.Conflict):
            self.change(9, {"handling": "agents"})
        self.assertEqual(self.client.put("/v2/conversations/9/tags", headers=self.auth, json={"tags": ["billing"]}).status_code, 204)
        self.assertIn(company.IGNORE_TAG, self.tags(9))  # A stale tag list does not undo the take-over.
        self.assertEqual(self.change(9, {"handling": "agents"}, agent_ledger.OWNER, owner=True)["handling"], "agents")
        # Requests and the page.
        asked = self.client.post("/v2/conversations/9/events", headers=self.auth, json={
            "kind": "request", "actor": "agent-7", "title": "Refund or credit?", "status": "open", "data": {"action": "Choose one."}})
        self.assertEqual(asked.status_code, 201)
        self.assertEqual([i["id"] for i in agent_ledger.needs_owner(self.db)], [asked.json()["id"]])
        page = self.client.get("/conversation/9")
        self.assertEqual(page.status_code, 200)
        for text in ("Agents handle this", "Asks something of you", 'name="handling"', "Choose one."):
            self.assertIn(text, page.text)
        self.assertNotIn('name="stage"', page.text)
        self.assertEqual(agent_ledger.present({"actor": "owner", "kind": "note", "status": "", "data": {}})["who"], "Sam")
        self.assertEqual(agent_ledger.present({"actor": "agent-7", "kind": "note", "status": "", "data": {}})["who"], "agent-7")

    def test_a_registered_campaign_keeps_its_tags_in_step(self):
        trial()
        agent_ledger.configure(owner_aliases=("Sam",), flags={"paid": {"tag": "invoice-paid", "chip": "Paid"}},
                               attention={"needs-look": "needs a look"}, actors={"agent-7": "Agent Seven"})
        state = agent_ledger.state(self.db, 7)
        self.assertEqual((state["handling"], state["campaign"], state["stage"], state["waiting_on"], state["reminders"]["state"], state["vip"], state["paid"]),
                         ("campaign", "trial", "asked", "customer", "idle", False, False))  # Quiet in its first stage.
        self.assertEqual((agent_ledger.state_fields(), agent_ledger.view(self.db, 7)["stages"][0], agent_ledger.ENDED),
                         ((*agent_ledger.BASE_FIELDS, "paid", "vip"), ("asked", "question asked"), ("closed", "refused")))
        self.assertEqual((agent_ledger.state(self.db, 9)["vip"], agent_ledger.state(self.db, 9)["campaign"]), (False, None))
        moved = self.change(7, {"stage": "answered", "waiting_on": "sam", "vip": True, "paid": True}, "Sam", reason="Asked for a call.")
        self.assertEqual((moved["stage"], moved["waiting_on"], moved["reminders"]["state"], moved["vip"], moved["paid"]), ("answered", "owner", "idle", True, True))
        self.assertEqual(self.tags(7), ["billing", "invoice-paid", "trial-answered", "trial-needs-sam", "trial-run", "trial-vip"])
        event = agent_ledger.events(self.db, 7)[0]
        self.assertEqual((event["actor"], event["title"], event["data"]["changes"]["waiting_on"]),
                         ("owner", "Paid, marked VIP, stage answered, waiting on owner", "owner"))
        self.assertEqual([text for text, _ in agent_ledger.chips(moved)][:4], ["Campaign agents work this", "Trial · answered", "Waiting on you", "Reminders idle"])
        self.assertIn(("Paid", ""), agent_ledger.chips(moved))
        self.assertEqual(self.change(7, {"waiting_on": "customer"})["reminders"]["state"], "on")
        for refused in ({"waiting_on": "blocked"}, {"stage": "won"}, {"stage": "closed", "waiting_on": "customer"}, {"handling": "person"},
                        {"vip": "yes"}, {"campaign": "other"}):
            with self.assertRaises(store.Invalid, msg=refused):
                self.change(7, refused)
        closed = self.change(7, {"stage": "closed"})
        self.assertEqual((closed["stage"], closed["waiting_on"], closed["reminders"]["state"]), ("closed", None, "off"))
        self.assertEqual(self.tags(7), ["billing", "invoice-paid", "trial-closed", "trial-run", "trial-vip"])
        self.change(7, {"stage": "refused"})
        self.assertIn("trial-no-reminders", self.tags(7))
        with self.assertRaises(store.Invalid):
            self.change(7, {"reminders": "on"})
        # Enrolling takes a conversation off the general agents; an attention tag is reported.
        enrolled = self.change(9, {"campaign": "trial", "stage": "asked", "waiting_on": "customer"})
        self.assertEqual((enrolled["handling"], self.tags(9)), ("campaign", sorted(["billing", company.IGNORE_TAG, "trial-asked", "trial-run", "trial-waiting-customer"])))
        with store.transaction(self.db):
            store.update_conversation(self.db, 9, {"tags": [{"tag": "Needs-Look"}]})
        self.assertEqual(agent_ledger.state(self.db, 9)["attention"], ["needs a look"])
        self.assertEqual(agent_ledger.present({"actor": "agent-7", "kind": "note", "status": "", "data": {}})["who"], "Agent Seven")
        page = self.client.get("/conversation/7").text
        self.assertIn('name="stage"', page)
        self.assertNotIn('name="handling"', page)

    def test_note_readers_are_asked_in_order_and_only_for_notes_nobody_typed(self):
        asked = []

        def first(rows, source):
            asked.append(("first", rows[0], source))
            if rows[0].startswith("Automation decision note"):
                return {"kind": "decision", "actor": "robo", "title": "Draft left for review · 7/10", "status": "",
                        "data": {"confidence": 7, "why": agent_ledger.fields(rows[1:]).get("why"), "empty": ""}}

        def second(rows, source):
            asked.append(("second", rows[0], source))
            return {"kind": "request", "actor": "robo", "title": "T" * 400, "status": "open"}

        agent_ledger.register_note_reader(first)
        agent_ledger.register_note_reader(second)
        agent_ledger.register_note_reader(first)  # Registering twice asks once.
        self.assertEqual(agent_ledger._readers, [first, second])
        agent_ledger.configure(service_actor="robo")
        read = agent_ledger.parse(WORDED)
        self.assertEqual((read["kind"], read["actor"], read["data"]["confidence"], read["data"]["why"], "empty" in read["data"], read["data"]["text"][:10]),
                         ("decision", "robo", 7, "Not confirmed.", False, "Automation"))
        other = agent_ledger.parse("<p>Something else.</p>")
        self.assertEqual((other["kind"], other["status"], len(other["title"])), ("request", "open", 240))
        self.assertEqual([who for who, _, _ in asked], ["first", "first", "second"])
        self.assertEqual(agent_ledger.parse(WORDED, "web")["kind"], "instruction")
        self.assertEqual(len(asked), 3)  # What a person typed is never offered to a reader.
        # The service's own notes are read by their wording; another agent's wording does not make it the service's.
        mine = self.note(9, WORDED, event={"actor": "robo"})
        theirs = self.note(9, WORDED, event={"actor": "agent-7"})
        by_note = {e["note"]: (e["kind"], e["actor"]) for e in agent_ledger.events(self.db, 9)}
        self.assertEqual((by_note[mine], by_note[theirs]), (("decision", "robo"), ("note", "agent-7")))
        # A reader that fails costs the record, never the note.
        agent_ledger.reset()
        agent_ledger.register_note_reader(lambda rows, source: 1 / 0)
        with self.assertLogs("inbox.ledger", "ERROR"):
            kept = self.note(9, "<p>Still stored.</p>")
        self.assertEqual(store.thread(self.db, 9, kept)["type"], "note")

    def test_a_registration_that_cannot_work_is_refused_and_reset_forgets_everything(self):
        good = dict(marker="m", label="L", stages=("a", "b"), waiting_tags={"customer": "x-waiting"})
        for name, change in (("Bad Name", {}), ("x", {}), ("ok", {"ended": ("z",)}), ("ok", {"ended": ("b",), "no_contact": ("a",)}),
                             ("ok", {"waiting_tags": {"vlad": "t"}}), ("ok", {"flags": {"stage": {"tag": "t"}}}), ("ok", {"flags": {"vip": {}}}),
                             ("ok", {"flags": {"status": {"tag": "t"}}}), ("ok", {"label": None}), ("ok", {"stages": ("A",)}),
                             ("ok", {"stages": ("no_reminders",)}), ("ok", {"stages": ()}), ("agent", {}), ("site", {})):
            with self.assertRaises(ValueError, msg=(name, change)):
                agent_ledger.register_campaign(name, **{**good, **change})
        with self.assertRaises(ValueError):
            agent_ledger.configure(flags={"handling": {"tag": "t"}})
        agent_ledger.configure(flags={"paid": {"tag": "invoice-paid"}})
        with self.assertRaises(ValueError):  # One name, one tag: not kept both for a campaign and for everything.
            agent_ledger.register_campaign("ok", **{**good, "flags": {"paid": {"tag": "ok-paid"}}})
        agent_ledger.register_campaign("ok", **{**good, "flags": {"vip": {"tag": "ok-vip"}}})
        with self.assertRaises(ValueError):
            agent_ledger.configure(flags={"vip": {"tag": "vip"}})
        agent_ledger.reset()
        self.assertEqual(agent_ledger.who("Sam"), "Sam")
        trial()
        agent_ledger.configure(owner_aliases=("sam",))
        self.assertEqual((agent_ledger.who(" SAM "), agent_ledger.who("agent-7"), agent_ledger.who(None), agent_ledger.who(" Owner ")),
                         ("owner", "agent-7", None, "owner"))
        agent_ledger.reset()
        self.assertEqual((agent_ledger.who("sam"), agent_ledger.STAGES, agent_ledger.state(self.db, 7)["campaign"]), ("sam", (), None))

    def test_the_backfill_command_reads_history_with_the_installation_s_note_readers(self):
        home = Path(__file__).resolve().parent
        (Path(self.scratch.name) / "example_reader.py").write_text(
            "import agent_ledger\n"
            "def read(rows, source):\n"
            "    if rows[0].startswith('Automation decision note'):\n"
            "        return {'kind': 'decision', 'actor': 'robo', 'title': 'Read by the extension'}\n"
            "agent_ledger.register_note_reader(read)\n")
        with store.transaction(self.db):  # A note stored before the ledger existed: no record yet.
            self.db.execute("INSERT INTO threads(id,conversation_id,data) VALUES(95,9,?)", (store.encode(
                {"id": 95, "type": "note", "createdAt": "2026-01-03T10:00:00Z", "body": WORDED, "source": {"type": "api"}}),))
        env = {k: v for k, v in os.environ.items() if not k.startswith("INBOX_")}
        env = {**env, "INBOX_DB": os.environ["INBOX_DB"], "PYTHONPATH": self.scratch.name}
        missing = subprocess.run([sys.executable, "agent_ledger.py", "backfill"], capture_output=True, text=True, cwd=home,
                                 env={**env, "INBOX_EXTENSIONS": "missing_example_extension"})
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("missing_example_extension", missing.stderr)
        self.assertEqual(agent_ledger.events(self.db, 9), [])  # Nothing was recorded without its reader.
        for command in (["agent_ledger.py", "backfill"], ["-m", "agent_ledger", "backfill"]):
            with store.transaction(self.db):
                self.db.execute("DELETE FROM agent_events")
            ran = subprocess.run([sys.executable, *command], capture_output=True, text=True, cwd=home,
                                 env={**env, "INBOX_EXTENSIONS": "example_reader"})
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn('"absorbed": 1', ran.stdout)
            self.assertEqual([(e["kind"], e["actor"], e["title"]) for e in agent_ledger.events(self.db, 9)],
                             [("decision", "robo", "Read by the extension")], command)


if __name__ == "__main__":
    unittest.main()
