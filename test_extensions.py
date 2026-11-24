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


def test_health_reports_only_completed_imports(tmp_path):
    (tmp_path / "example_installation.py").write_text("registered = True\n")
    result = run("""
import os
from fastapi.testclient import TestClient
from app import app
os.environ['INBOX_EXTENSIONS'] = 'changed_after_start'
with TestClient(app) as client:
    health = client.get('/health').json()
assert health == {'status': 'ok', 'pid': os.getpid(), 'extensions': ['example_installation']}, health
""", INBOX_EXTENSIONS="example_installation", INBOX_DB=str(tmp_path / "mail.db"), PYTHONPATH=str(tmp_path))
    assert result.returncode == 0, result.stderr


def test_worker_announces_its_loaded_modules(tmp_path):
    import json
    import socket
    import tempfile

    # A short path fits Unix socket addresses on both Linux and macOS.
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        address = str(Path(runtime) / "notify")
        receiver.bind(address)
        receiver.settimeout(5)
        (tmp_path / "fixture_extension.py").write_text("installed = True\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("INBOX_")}
        worker = subprocess.Popen(
            [sys.executable, "transport.py", "workflows"], cwd=Path(__file__).resolve().parent,
            env={**env, "INBOX_EXTENSIONS": "fixture_extension", "PYTHONPATH": str(tmp_path),
                 "NOTIFY_SOCKET": address, "INVOCATION_ID": "fixture-invocation",
                 "INBOX_DB": str(tmp_path / "mail.db")}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            fields = dict(line.split("=", 1) for line in receiver.recv(8192).decode().splitlines())
            assert fields["READY"] == "1"
            assert json.loads(fields["STATUS"]) == {
                "pid": worker.pid, "invocation": "fixture-invocation", "extensions": ["fixture_extension"],
            }
            assert worker.poll() is None
        finally:
            worker.terminate()
            worker.communicate(timeout=5)
