"""Exercise startup in fresh interpreters, including extension-free installations."""

import os
import subprocess
import sys
from pathlib import Path


def run(code, **settings):
    env = {k: v for k, v in os.environ.items() if not k.startswith("INBOX_")}
    return subprocess.run(
        [sys.executable, "-c", code], env={**env, **settings},
        cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
    )


def test_extensions_load_once(tmp_path):
    (tmp_path / "example_extension.py").write_text("print('registered')\n")
    result = run("from extensions import load; load(); load()",
                 INBOX_EXTENSIONS=" example_extension, example_extension ", PYTHONPATH=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["registered"]


def test_named_extension_failure_stops_web_startup(tmp_path):
    result = run("from fastapi.testclient import TestClient\nfrom app import app\n"
                 "with TestClient(app): pass", INBOX_EXTENSIONS="missing_example_extension",
                 INBOX_DB=str(tmp_path / "test.db"))
    assert result.returncode != 0 and "missing_example_extension" in result.stderr


def test_registrations_can_be_reset_between_mailboxes():
    result = run("""
from fastapi import APIRouter, FastAPI
import operator_ui
from workbench import adapters

routes = APIRouter()
@routes.get('/fixture')
def fixture():
    return True

operator_ui.register_account_lookup(routes, lambda: True)
adapters.register_research(fixture)
first = FastAPI()
operator_ui.mount_account_lookup(first)
assert '/fixture' in first.openapi()['paths'] and adapters.research is fixture
operator_ui.reset_account_lookup()
adapters.reset_research()
second = FastAPI()
operator_ui.mount_account_lookup(second)
assert '/fixture' not in second.openapi()['paths'] and adapters.research is None
""")
    assert result.returncode == 0, result.stderr


def test_default_mailbox_and_source_offer():
    result = run("import company, operator_ui as ui\n"
                 "assert company.MAILBOX == 1 and company.DOMAIN == 'woodland.example'\n"
                 "for template, args in [('login.html', {'next_path':'/'}), ('cases.html', {})]:\n"
                 "    assert b'https://source.example/inbox' in ui.page(template, **args).body",
                 INBOX_SOURCE_URL="https://source.example/inbox")
    assert result.returncode == 0, result.stderr
