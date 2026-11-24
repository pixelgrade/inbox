"""Inspect or atomically change native mailbox writer and callback ownership."""

import argparse
from contextlib import closing
import json
import hashlib
import os
import re
import time
from pathlib import Path

import store

def expected_hooks():
    return {int(value) for value in os.getenv("INBOX_WEBHOOK_IDS", "").split(",") if value.strip()}


def status(db):
    mode, changed = db.execute(
        "SELECT mode,changed FROM writer_authority WHERE id=1"
    ).fetchone()
    return {
        "mode": mode,
        "changed": changed,
        "native_conversations": db.execute(
            "SELECT count(*) FROM conversations WHERE native=1"
        ).fetchone()[0],
        "outgoing": dict(
            db.execute("SELECT state,count(*) FROM mail_outbox GROUP BY state")
        ),
        "deliveries": dict(
            db.execute("SELECT state,count(*) FROM deliveries GROUP BY state")
        ),
        "callbacks": dict(
            db.execute(
                "SELECT json_extract(data,'$.state'),count(*) FROM webhooks GROUP BY 1"
            )
        ),
    }


def revision(db):
    """Identify the exact source revisions installed in this serving database."""
    digest = hashlib.sha256()
    for kind, key, value in db.execute(
        "SELECT kind,id,digest FROM source_hash ORDER BY kind,id"
    ):
        digest.update(f"{kind}/{key}/{bytes(value).hex()}\n".encode("ascii"))
    return digest.hexdigest()


def check_reconciliation(db, report):
    if (
        not report
        or report.get("full_comparison") is not True
        or report.get("reconciled") is not True
        or report.get("differences") != {}
    ):
        raise store.Conflict("Historical reconciliation is incomplete")
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='source_hash'"
    ).fetchone() or report.get("serving_revision") != revision(db):
        raise store.Conflict("Reconciliation came from a different serving revision")
    counts = report.get("counts") or {}
    frozen = report.get("frozen_inputs") or {}
    digests = ("source_sha256", "serving_sha256", "asset_index_sha256")
    if (
        any(not re.fullmatch(r"[0-9a-f]{64}", str(frozen.get(key, ""))) for key in digests)
        or frozen["asset_index_sha256"] != counts.get("asset_index_sha256")
        or not isinstance(frozen.get("asset_bytes"), int)
        or frozen["asset_bytes"] != counts.get("asset_bytes")
    ):
        raise store.Conflict("Use the final reconciliation report from the frozen source, mailbox and binary inventory")
    tables = {
        "hosted_conversations": "conversations",
        "compared_hosted_threads": "threads",
        "hosted_customers": "customers",
        "hosted_mailboxes": "mailboxes",
    }
    for key, table in tables.items():
        actual = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if not actual or counts.get(key) != actual:
            raise store.Conflict("Serving counts changed after reconciliation")


def check_native_installation(db, company):
    """Identify an inbox initialized directly for a company, including earlier installations."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "source_hash" in tables or db.execute("SELECT 1 FROM source_profile LIMIT 1").fetchone():
        raise store.Conflict("Use the migration handover for an imported inbox")
    if db.execute("SELECT 1 FROM conversations WHERE native=0 LIMIT 1").fetchone():
        raise store.Conflict("Use the migration handover for imported conversations")
    if "desk_company" not in tables or not db.execute(
        "SELECT 1 FROM desk_company WHERE id=1 AND name=?", (company,)
    ).fetchone():
        raise store.Conflict("The product installation belongs to a different company")
    mailboxes = list(db.execute("SELECT data FROM mailboxes LIMIT 2"))
    if len(mailboxes) != 1 or json.loads(mailboxes[0][0]).get("name") != company:
        raise store.Conflict("The native mailbox does not match the installed company")


def change(db, target, expected, report=None, *, native_company=None):
    with store.transaction(db):
        before = status(db)
        if expected != before["mode"]:
            raise store.Conflict("Writer ownership changed")
        if target == "paused" and expected != "live":
            raise store.Conflict("Only the active writer can be paused")
        if native_company is not None:
            check_native_installation(db, native_company)
        elif target == "live" and expected == "standby":
            import mailbox_settings

            check_reconciliation(db, report)
            mailbox_settings.capture_source_window(db, int(time.time()))
        if target == "standby" and before["native_conversations"]:
            raise store.Conflict(
                "Preserve and reconcile native changes before transferring ownership"
            )
        if native_company is None:
            hooks = {row[0] for row in db.execute("SELECT id FROM webhooks")}
            if hooks != expected_hooks():
                raise store.Conflict("Callback registrations changed")
            db.execute(
                "UPDATE webhooks SET data=json_set(data,'$.state',?)",
                ("enabled" if target == "live" else "disabled",),
            )
        db.execute(
            "UPDATE writer_authority SET mode=?,changed=? WHERE id=1",
            (target, store.now()),
        )
        return status(db)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "standby", "live", "paused"))
    parser.add_argument(
        "--from", dest="expected", choices=("standby", "live", "paused")
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reconciliation", type=Path)
    mode.add_argument("--native-company", help="Registered company name for an inbox created without historical imports")
    args = parser.parse_args()
    with closing(store.connect()) as db:
        if args.action == "status":
            result = status(db)
        else:
            if not args.expected:
                parser.error("--from is required when changing writer ownership")
            report = (
                json.loads(args.reconciliation.read_text())
                if args.reconciliation
                else None
            )
            result = change(db, args.action, args.expected, report, native_company=args.native_company)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    import extensions
    extensions.load()
    main()
