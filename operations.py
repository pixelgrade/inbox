"""Body-free delivery and intake exceptions for the private operator inbox."""

from contextlib import closing
import json
import argparse
import provider_feedback

import store


def issues(db):
    return [
        dict(row)
        for row in db.execute("""
        SELECT 'Outgoing reply' AS kind, state, conversation_id AS cid, cast(thread_id AS text) AS reference
          FROM mail_outbox WHERE state IN ('failed','uncertain') OR
            (state='submitting' AND thread_id IN (SELECT id FROM threads WHERE julianday(created)<julianday('now','-5 minutes')))
        UNION ALL SELECT 'Acknowledgment',state,cid,cast(cid AS text) FROM acknowledgments
          WHERE state IN ('failed','uncertain') OR (state='submitting' AND queued<unixepoch()-300)
        UNION ALL SELECT 'Agent event',d.state,d.conversation_id,cast(d.id AS text) FROM deliveries d
          LEFT JOIN hook_attempts a ON a.id=d.id WHERE d.state='held' OR (d.state='queued' AND a.count>=3)
            OR (d.state='submitting' AND a.next<=unixepoch())
        UNION ALL SELECT 'Incoming email',disposition,conversation,object_key FROM inbound_objects
          WHERE disposition IN ('held_non_group','held_cutover','held_object_changed','held_duplicate_identity','held_threading','held_filter','held_parse','held_size','held_source_identity','held_sender_verification','held_delivery_report')
        UNION ALL SELECT 'Provider notification','awaiting_receipt',NULL,id FROM unmatched_feedback WHERE received<unixepoch()-300
        UNION ALL SELECT 'Incoming notification',reason,NULL,id FROM intake_event_errors
        UNION ALL SELECT 'Provider notification',reason,NULL,id FROM feedback_quarantine
        UNION ALL SELECT 'Provider notification',disposition,NULL,id FROM provider_events WHERE disposition LIKE 'held%'
        ORDER BY kind,cid,reference
    """)
    ]


def counts(db):
    totals = {}
    for item in issues(db):
        totals[item["kind"]] = totals.get(item["kind"], 0) + 1
    return totals


if __name__ == "__main__":
    import extensions
    extensions.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", action="store_true")
    parser.add_argument("--review-simulator", metavar="RECEIPT")
    parser.add_argument("--sha")
    parser.add_argument("--reason")
    args = parser.parse_args()
    with closing(store.connect()) as db:
        if args.review_simulator:
            if not args.sha or not args.reason:
                parser.error("--sha and --reason are required")
            provider_feedback.review_simulator(
                db, args.review_simulator, args.sha, args.reason
            )
            print("reviewed_simulator")
        else:
            print(json.dumps(counts(db) if args.counts else issues(db), sort_keys=True))
