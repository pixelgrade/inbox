"""Load the configured mailbox extensions before serving requests or opening a worker."""

from importlib import import_module
import os


def load():
    # Python caches successful imports, including modules shared by several entry points.
    for name in os.environ.get("INBOX_EXTENSIONS", "").split(","):
        name = name.strip()
        if name:
            try:
                import_module(name)
            except Exception as exc:
                raise RuntimeError(f"Could not load inbox extension {name!r}") from exc
