"""Install a private product pack, issue a product-scoped credential or revoke an existing one."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets

from . import core
from .http import connect, session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="action", required=True)
    cmds.add_parser("install").add_argument("pack", type=Path)
    token = cmds.add_parser("token")
    token.add_argument("product")
    token.add_argument("role", choices=("agent", "runner"))
    token.add_argument("output", type=Path)
    code = cmds.add_parser("index-code")
    code.add_argument("product")
    code.add_argument("repo")
    code.add_argument("checkout", type=Path)
    code.add_argument("paths", nargs="+")
    revoke = cmds.add_parser("revoke")
    revoke.add_argument("file", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    with closing(connect()) as db:
        core.schema(db)
    with session(True) as db:
        if args.action == "install":
            print(json.dumps(core.install(db, json.loads(args.pack.read_text()))))
        elif args.action == "index-code":
            from .code_index import index

            print(
                json.dumps(
                    index(db, args.product, args.repo, args.checkout, args.paths)
                )
            )
        elif args.action == "token":
            core.product_info(db, args.product)
            secret = secrets.token_urlsafe(48)
            with args.output.open("x") as dest:
                dest.write(secret + "\n")
            db.execute(
                "INSERT INTO desk_tokens VALUES(?,?,?)",
                (core.sha(secret), args.product, args.role),
            )
            print("Created product credential in the private output file")
        else:
            count = db.execute(
                "DELETE FROM desk_tokens WHERE digest=?",
                (core.sha(args.file.read_text().strip()),),
            ).rowcount
            print("Revoked", count, "credential")


if __name__ == "__main__":
    import extensions
    extensions.load()
    main()
