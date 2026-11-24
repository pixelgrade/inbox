"""Inspect or reconcile one private held mail receipt as the mailbox operator."""

import argparse
from contextlib import closing
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os

import boto3

import store
import transport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "key", help="Exact private object key from the Delivery issues page"
    )
    parser.add_argument("--sha", help="Digest displayed by the read-only inspection")
    parser.add_argument("--reason", help="Private audit note explaining the decision")
    parser.add_argument(
        "--expected", help="Expected disposition displayed during inspection"
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--conversation", type=int, help="Verified destination conversation ID"
    )
    target.add_argument(
        "--new", action="store_true", help="Release as an independent inbound request"
    )
    target.add_argument(
        "--archive",
        action="store_true",
        help="Record that a held receipt needs no mailbox action",
    )
    args = parser.parse_args()
    with closing(store.connect()) as db:
        receipt = db.execute(
            "SELECT sha,disposition,conversation FROM inbound_objects WHERE object_key=?",
            (args.key,),
        ).fetchone()
        if not receipt or not args.key.startswith(os.environ["INBOX_PREFIX"]):
            parser.error("Unknown inbox receipt")
        obj = boto3.client(
            "s3", region_name=os.getenv("AWS_REGION", "us-east-1")
        ).get_object(Bucket=os.environ["INBOX_BUCKET"], Key=args.key)
        with closing(obj["Body"]) as stream:
            raw = stream.read(transport.MAX_RAW + 1)
        if len(raw) > transport.MAX_RAW:
            parser.error("Message exceeds the review limit")
        digest = hashlib.sha256(raw).hexdigest()
        if receipt["sha"] != digest:
            parser.error("Original object differs from the receipt")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if not (args.conversation or args.new or args.archive):
            print(
                json.dumps(
                    {
                        "sha": digest,
                        "disposition": receipt["disposition"],
                        "candidate": receipt["conversation"],
                        "from": message.get("From"),
                        "reply_to": message.get("Reply-To"),
                        "subject": message.get("Subject"),
                        "date": message.get("Date"),
                        "message_id": message.get("Message-ID"),
                        "group_verified": bool(transport.group_signature(raw, message)),
                    },
                    ensure_ascii=True,
                )
            )
            return
        if args.sha != digest or not args.expected or not args.reason:
            parser.error("A verified digest, prior disposition and reason are required")
        if args.archive:
            with store.transaction(db):
                if not args.expected.startswith("held_"):
                    raise store.Invalid("Only a held receipt can be archived")
                if not db.execute(
                    "UPDATE inbound_objects SET disposition='reviewed_no_action' WHERE object_key=? AND sha=? AND disposition=?",
                    (args.key, args.sha, args.expected),
                ).rowcount:
                    raise store.Conflict("Review receipt changed")
                db.execute(
                    "INSERT INTO intake_decisions VALUES(?,?,?,?,?,?,?)",
                    (
                        args.key,
                        args.sha,
                        args.expected,
                        "reviewed_no_action",
                        receipt["conversation"],
                        store.text(args.reason, "reason", 500),
                        store.now(),
                    ),
                )
            print("reviewed_no_action")
            return
        result = transport.admit(
            db,
            args.key,
            raw,
            decision={
                "conversation": args.conversation,
                "sha": args.sha,
                "expected": args.expected,
                "reason": store.text(args.reason, "reason", 500),
            },
        )
        print(result)


if __name__ == "__main__":
    import extensions
    extensions.load()
    main()
