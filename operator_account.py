"""Configure the shared human mailbox account from the trusted operator host."""

import argparse
from contextlib import closing
from getpass import getpass
import hashlib
import hmac
import secrets
import sys
import time

import store

ATTEMPTS = 10
WINDOW = 15 * 60
# Store the algorithm and parameters alongside each credential for future migrations.
SCHEME = "scrypt-32768-8-1"


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS operator_password(
            id INTEGER PRIMARY KEY CHECK(id=1), email TEXT NOT NULL,
            scheme TEXT NOT NULL, salt BLOB NOT NULL, digest BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operator_password_attempts(
            peer TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS operator_password_attempts_expiry ON operator_password_attempts(expires);
    """)


def derive(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=1,
                          maxmem=64 * 1024 * 1024, dklen=32)


def configure(db, email, password):
    email = store.email(email.strip()).lower()
    if not 12 <= len(password) <= 1024:
        raise ValueError("Choose a password between 12 and 1024 characters")
    salt = secrets.token_bytes(16)
    digest = derive(password, salt)
    with store.transaction(db):
        db.execute("INSERT OR REPLACE INTO operator_password VALUES(1,?,?,?,?)",
                   (email, SCHEME, salt, digest))
        db.execute("DELETE FROM operator_password_attempts")
        db.execute("DELETE FROM operator_auth WHERE kind='session'")


def reserve_attempt(db, peer):
    now = int(time.time())
    db.execute("DELETE FROM operator_password_attempts WHERE expires<=?", (now,))
    row = db.execute("SELECT attempts,expires FROM operator_password_attempts WHERE peer=?", (peer,)).fetchone()
    if row and row[0] >= ATTEMPTS:
        return row[1] - now
    db.execute("""INSERT INTO operator_password_attempts VALUES(?,1,?)
                  ON CONFLICT(peer) DO UPDATE SET attempts=attempts+1""", (peer, now + WINDOW))
    return 0


def verify(row, email, password):
    if not row or row["scheme"] != SCHEME or not 0 < len(password) <= 1024:
        return False
    actual = derive(password, row["salt"])
    return hmac.compare_digest(actual, row["digest"]) and email.strip().lower() == row["email"]


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("email")
    cli.add_argument("--password-stdin", action="store_true", help="Read a single password from protected standard input")
    args = cli.parse_args()
    if args.password_stdin:
        password = sys.stdin.readline(1026).rstrip("\r\n")
    else:
        password = getpass("New inbox password: ")
        if password != getpass("Confirm inbox password: "):
            cli.error("Passwords differ")
    with closing(store.connect()) as db:
        schema(db)
        configure(db, args.email, password)
    print("Shared inbox sign-in configured.")


if __name__ == "__main__":
    import extensions
    extensions.load()
    main()
