"""Create a fresh, private company inbox with its first product pack."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from . import core


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pack", type=Path)
    args = cli.parse_args()
    required = (
        "INBOX_DB",
        "INBOX_BASE_URL",
        "INBOX_COMPANY_NAME",
        "INBOX_SUPPORT_ADDRESS",
        "INBOX_INTAKE_MAILBOX",
        "INBOX_AGENT_ID",
        "INBOX_AGENT_NAME",
    )
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        cli.error("Set " + ", ".join(missing))
    pack = json.loads(args.pack.read_text())
    if pack.get("company") != os.environ["INBOX_COMPANY_NAME"]:
        cli.error("Use the same company name in the environment and product pack")
    import company
    import store
    import operator_ui

    os.umask(0o077)
    target = Path(os.environ["INBOX_DB"])
    # Stage the whole installation beside the target. link() publishes only to a fresh filename.
    with TemporaryDirectory(dir=target.parent) as tmp:
        stage = Path(tmp) / "company.db"
        with closing(store.connect(stage)) as db:
            store.initialize(db)
            operator_ui.ui_schema(db)
            core.schema(db)
            with store.transaction(db, write=True):
                store.save_mailbox(
                    db,
                    {
                        "id": company.MAILBOX,
                        "name": company.NAME,
                        "email": store.email(company.ADDRESS),
                    },
                )
                result = core.install(db, pack)
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Company database integrity check failed")
        os.link(stage, target)
    print(json.dumps(result))


if __name__ == "__main__":
    import extensions
    extensions.load()
    main()
