"""Recover old conversation links from archived merge activities, keeping live IDs authoritative."""

import argparse
from collections import defaultdict
from contextlib import closing
import json
import sqlite3

import store


def reconcile(db, *, apply=False):
    candidates = defaultdict(set)
    # Transferred messages repeat the merge action. The owner-visible merge activity records a destination.
    for row in db.execute("""SELECT json_extract(data,'$.action.associatedEntities.originalConversation'),conversation_id
            FROM threads WHERE json_extract(data,'$.type')='lineitem' AND json_extract(data,'$.action.type')='merged'
            AND json_type(data,'$.action.associatedEntities.originalConversation')='integer' GROUP BY 1,2"""):
        if row[0] > 0:
            candidates[row[0]].add(row[1])
    totals = dict(eligible=0, existing=0, ambiguous=0, live_id=0, invalid=0, inserted=0)
    for old, targets in candidates.items():
        if len(targets) != 1:
            totals["ambiguous"] += 1
            continue
        target = next(iter(targets))
        alias = db.execute(
            "SELECT target_id FROM conversation_aliases WHERE source_id=?", (old,)
        ).fetchone()
        if alias:
            totals["existing" if alias[0] == target else "ambiguous"] += 1
        elif db.execute("SELECT 1 FROM conversations WHERE id=?", (old,)).fetchone():
            totals["live_id"] += 1
        elif (
            old == target
            or not db.execute(
                "SELECT 1 FROM conversations WHERE id=?", (target,)
            ).fetchone()
        ):
            totals["invalid"] += 1
        else:
            totals["eligible"] += 1
            if apply:
                store.save_alias(db, old, target)
                totals["inserted"] += 1
    return totals


if __name__ == "__main__":
    import extensions
    extensions.load()
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with closing(
        sqlite3.connect(
            args.database.resolve().as_uri()
            + ("?mode=rw" if args.apply else "?mode=ro"),
            uri=True,
            timeout=30,
        )
    ) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with store.transaction(db, write=args.apply):
            print(json.dumps(reconcile(db, apply=args.apply), sort_keys=True))
