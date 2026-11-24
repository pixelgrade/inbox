"""Index selected source directories at an exact Git commit for product-scoped citations."""

from pathlib import PurePosixPath
import subprocess

from . import core


def index(db, pid, repo, checkout, prefixes):
    config = next(
        (r for r in core.product_info(db, pid)["repositories"] if r["key"] == repo),
        None,
    )
    if not config:
        raise core.Invalid("Unknown configured repository")
    rev = config["revision"]
    git = ["git", "-C", str(checkout)]
    if (
        subprocess.check_output(
            [*git, "rev-parse", rev + "^{commit}"], text=True
        ).strip()
        != rev
    ):
        raise core.Invalid("Configured revision is unavailable")
    if not prefixes or any(
        not p or p.startswith(("-", "/", ":")) or ".." in PurePosixPath(p).parts
        for p in prefixes
    ):
        raise core.Invalid("Choose source paths relative to the repository")
    entries = subprocess.check_output(
        [*git, "ls-tree", "-rlz", rev, "--", *prefixes]
    ).split(b"\0")
    rows = []
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid, size = metadata.split()
        path = raw_path.decode("utf-8")
        parts = PurePosixPath(path)
        if (
            mode not in (b"100644", b"100755")
            or kind != b"blob"
            or int(size) > 180000
            or parts.suffix.lower()
            not in {
                ".md",
                ".txt",
                ".js",
                ".ts",
                ".tsx",
                ".jsx",
                ".php",
                ".py",
                ".rb",
                ".go",
                ".rs",
                ".java",
                ".css",
                ".scss",
                ".json",
                ".c",
                ".h",
                ".cpp",
                ".html",
            }
        ):
            continue
        if any(
            p.startswith(".")
            or p.lower() in ("vendor", "node_modules", "credentials", "secrets")
            for p in parts.parts
        ):
            continue
        key = f"code:{repo}/{path}"
        if len(key) > 260:
            continue
        rows.append((oid.decode(), key, path))
    if not rows:
        raise core.Invalid("No supported source files under the selected paths")
    proc = subprocess.Popen(
        [*git, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    count = 0
    try:
        for oid, key, path in rows:
            proc.stdin.write((oid + "\n").encode())
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) != 3 or header[0].decode() != oid:
                raise RuntimeError("Git object read failed")
            data = proc.stdout.read(int(header[2]))
            if proc.stdout.read(1) != b"\n":
                raise RuntimeError("Incomplete Git object")
            try:
                body = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not body.strip() or "\0" in body:
                continue
            source = config["url"].removesuffix(".git") + "/blob/" + rev + "/" + path
            db.execute(
                "INSERT OR REPLACE INTO desk_docs VALUES(?,?,?,?,?,?,?)",
                (pid, key, "code", path, body, source, rev),
            )
            db.execute("DELETE FROM desk_search WHERE product=? AND key=?", (pid, key))
            db.execute(
                "INSERT INTO desk_search VALUES(?,?,?,?)", (pid, key, path, body)
            )
            count += 1
    finally:
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("Git object reader failed")
    return {"product": pid, "repository": repo, "revision": rev, "indexed": count}
