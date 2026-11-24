"""Product-facing CLI for agents and operators. Configuration contains only origin, product and token_file."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode, quote
from .runner import Client


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--config", type=Path, default=os.environ.get("SUPPORT_PRODUCT_CONFIG")
    )
    cmds = cli.add_subparsers(dest="command", required=True)
    cmds.add_parser("product")
    cmds.add_parser("context").add_argument("query")
    cmds.add_parser("document").add_argument("source")
    new = cmds.add_parser("open")
    new.add_argument("question")
    new.add_argument("--key", required=True)
    cmds.add_parser("case").add_argument("id")
    attach = cmds.add_parser("cite")
    attach.add_argument("id")
    attach.add_argument("sources", nargs="+")
    test = cmds.add_parser("test")
    test.add_argument("id")
    test.add_argument("repo")
    test.add_argument("recipe")
    test.add_argument("--patch", type=Path)
    test.add_argument("--key", required=True)
    patch = cmds.add_parser(
        "patch", help="Save a candidate diff for development review"
    )
    patch.add_argument("id")
    patch.add_argument("job")
    patch.add_argument("output", type=Path)
    reply = cmds.add_parser("propose")
    reply.add_argument("id")
    reply.add_argument("--reply", type=Path, required=True)
    reply.add_argument("--cite", nargs="+", required=True)
    args = cli.parse_args()
    if not args.config:
        cli.error("Set --config or SUPPORT_PRODUCT_CONFIG")
    client = Client(json.loads(args.config.read_text()))
    suffix = "/cases/" + quote(getattr(args, "id", ""), safe="")
    if args.command == "product":
        result = client.call("")
    elif args.command == "context":
        result = client.call("/context?" + urlencode({"q": args.query}))
    elif args.command == "document":
        result = client.call("/document?" + urlencode({"key": args.source}))
    elif args.command == "open":
        result = client.call("/cases", {"question": args.question}, args.key)
    elif args.command == "case":
        result = client.call(suffix)
    elif args.command == "cite":
        result = client.call(suffix + "/sources", {"sources": args.sources})
    elif args.command == "test":
        result = client.call(
            suffix + "/tests",
            {
                "repo": args.repo,
                "recipe": args.recipe,
                "patch": args.patch.read_text() if args.patch else "",
            },
            args.key,
        )
    elif args.command == "patch":
        change = client.call(suffix + "/tests/" + quote(args.job, safe="") + "/patch")
        with args.output.open("xb") as output:
            output.write(change["patch"].encode("utf-8"))
        result = {k: change[k] for k in ("repo", "revision", "sha256")}
    else:
        result = client.call(
            suffix + "/proposal",
            {"answer": args.reply.read_text(), "citations": args.cite},
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
