"""Binary archive shared by the importer and mailbox. The app opens it read-only."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
from xml.etree import ElementTree


def image_mime(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def inline_mime(data):
    mime = image_mime(data)
    if mime:
        return mime
    # Only inspect the document root. SVG content is returned in a sandbox with downloads forced for navigation.
    prefix = data[:4096]
    if b"<svg" not in prefix or b"<!ENTITY" in prefix:
        return None
    parser = ElementTree.XMLPullParser(events=("start",))
    try:
        parser.feed(prefix)
        root = next((element for _, element in parser.read_events()), None)
    except ElementTree.ParseError:
        return None
    if root is not None and root.tag == "{http://www.w3.org/2000/svg}svg":
        return "image/svg+xml"
    return None


class SourceUnavailable(LookupError):
    pass


def lookup(kind,owner,key):
    import store
    path=os.environ.get("INBOX_ASSET_DB")
    if not path or not Path(path).is_file():
        raise store.Missing("Asset unavailable")
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True,timeout=20)) as db:
        row=db.execute("SELECT data,mime FROM asset WHERE kind=? AND owner=? AND key=?",(kind,owner,str(key))).fetchone()
    if row is None:
        # The importer projects only independently checked source failures.
        with closing(store.connect()) as serving:
            exists = serving.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_asset_status'").fetchone()
            if exists:
                reason = serving.execute("SELECT state FROM archive_asset_status WHERE kind=? AND owner=? AND key=?",
                                         (kind,owner,str(key))).fetchone()
                if reason and reason[0]=="source_unavailable":
                    raise SourceUnavailable("The original provider could not supply this file")
        raise store.Missing("Asset unavailable")
    return row


def migrated_attachment(cid,aid):
    return lookup("attachment",cid,aid)


def writer(path):
    db=sqlite3.connect(path,timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS asset(kind TEXT,owner INTEGER,key TEXT,mime TEXT,
                sha TEXT NOT NULL,data BLOB NOT NULL,PRIMARY KEY(kind,owner,key))""")
    return db
