import json
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from workbench import core, http, code_index


def pack(pid, company="Woodland", name="Journal"):
    return {
        "id": pid,
        "company": company,
        "name": name,
        "documents": [
            {
                "key": "voice",
                "kind": "voice",
                "title": "Voice",
                "text": "Use the customer's language.",
                "source": "operator guide",
                "revision": "v1",
            },
            {
                "key": "guide-sync",
                "kind": "guide",
                "title": "Sync entries",
                "text": "On mobile choose Sync to refresh the journal.",
                "source": "https://example.org/docs/sync",
                "revision": "v3",
            },
        ],
        "repositories": [
            {
                "key": "journal",
                "url": "https://example.org/journal.git",
                "label": "Journal",
                "revision": "a" * 40,
                "recipes": [{"key": "check", "label": "Check sync"}],
            }
        ],
    }


def install(db, pid, company="Woodland"):
    core.install(db, pack(pid, company))
    for role in ("agent", "runner"):
        db.execute(
            "INSERT INTO desk_tokens VALUES(?,?,?)", (core.sha(pid + role), pid, role)
        )


def test_standalone_company_search_case_run_review_and_product_isolation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DESK_DB", str(tmp_path / "company.db"))
    monkeypatch.delenv("INBOX_DB", raising=False)
    with TestClient(http.standalone) as client:
        with http.session(True) as db:
            install(db, "journal")
            install(db, "engineering")
            with pytest.raises(core.Conflict):
                core.install(db, pack("wrong", "Another company"))
        base = "/agent/products/journal"
        agent = {"Authorization": "Bearer journalagent", "Idempotency-Key": "one"}
        runner = {"Authorization": "Bearer journalrunner"}
        other = {"Authorization": "Bearer engineeringagent"}
        assert client.get(base, headers=agent).json()["company"] == "Woodland"
        assert client.get(base).status_code == 401
        assert client.get(base, headers=other).status_code == 403
        answer = client.get(
            base + "/context", headers=agent, params={"q": "mobile sync"}
        ).json()
        assert answer["evidence"][0]["key"] == "guide-sync"
        assert answer["guidance"][0]["key"] == "voice"
        question = {"question": "How do I sync my journal?"}
        res = client.post(base + "/cases", headers=agent, json=question)
        cid = res.json()["id"]
        assert (
            client.post(base + "/cases", headers=agent, json=question).json()
            == res.json()
        )
        assert (
            client.post(
                base + "/cases", headers=agent, json={"question": "Different"}
            ).status_code
            == 409
        )
        path = base + "/cases/" + cid
        assert (
            client.get(
                "/agent/products/engineering/cases/" + cid, headers=other
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/tests",
                headers=agent,
                json={"repo": "outside", "recipe": "check"},
            ).status_code
            == 400
        )
        job = client.post(
            path + "/tests", headers=agent, json={"repo": "journal", "recipe": "check"}
        ).json()["id"]
        assert client.post(base + "/runner/claim", headers=agent).status_code == 403
        claim = client.post(base + "/runner/claim", headers=runner).json()["job"]
        assert claim["id"] == job and "question" not in claim
        assert client.post(base + "/runner/claim", headers=runner).json() == {
            "job": None
        }
        receipt = {
            "revision": "a" * 40,
            "state": "passed",
            "exit_code": 0,
            "output": "Sync works",
        }
        result = base + "/runner/jobs/" + job + "/result"
        assert (
            client.post(
                result, headers=runner, json={"lease": "wrong", "result": receipt}
            ).status_code
            == 409
        )
        for _ in range(2):
            assert (
                client.post(
                    result,
                    headers=runner,
                    json={"lease": claim["lease"], "result": receipt},
                ).status_code
                == 200
            )
        candidate = "example diff with Unicode →\n"
        patched = client.post(
            path + "/tests",
            headers={**agent, "Idempotency-Key": "two"},
            json={"repo": "journal", "recipe": "check", "patch": candidate},
        ).json()["id"]
        patch_path = path + "/tests/" + patched + "/patch"
        assert client.get(patch_path, headers=agent).json()["patch"] == candidate
        assert client.get(patch_path, headers=other).status_code == 403
        assert (
            client.get(patch_path.replace(cid, "f" * 32), headers=agent).status_code
            == 404
        )
        assert (
            client.get(path + "/tests/" + job + "/patch", headers=agent).status_code
            == 404
        )
        assert (
            client.post(
                path + "/proposal",
                headers=agent,
                json={"answer": "Select Sync.", "citations": ["unknown"]},
            ).status_code
            == 400
        )
        assert (
            client.post(
                path + "/proposal",
                headers=agent,
                json={
                    "answer": "Select Sync.",
                    "citations": ["guide-sync", "test:" + job],
                },
            ).status_code
            == 200
        )
        report = client.get(path, headers=agent).json()
        assert (
            next(j for j in report["jobs"] if j["id"] == job)["result"] == receipt
            and report["state"] == "proposed"
        )
        with http.session() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='mail_outbox'"
                ).fetchone()[0]
                == 0
            )


def test_code_index_binds_real_repository_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DB", str(tmp_path / "product.db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "engine.py").write_text("def synchronize_entries():\n    return True\n")
    for command in (
        ["add", "engine.py"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "Baseline",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True)
    rev = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    with TestClient(http.standalone), http.session(True) as db:
        p = pack("journal")
        p["repositories"][0]["revision"] = rev
        core.install(db, p)
        result = code_index.index(db, "journal", "journal", repo, ["engine.py"])
        assert result["indexed"] == 1
        d = core.search(db, "journal", "synchronize")[0]
        assert d["revision"] == rev and d["source"].endswith(rev + "/engine.py")
        assert "return True" in d["excerpt"]


def test_generic_agent_tracks_sources_and_observed_tests(tmp_path, monkeypatch):
    from workbench.investigate import run

    monkeypatch.setenv("DESK_DB", str(tmp_path / "woodland.db"))
    with TestClient(http.standalone) as api:
        with http.session(True) as db:
            install(db, "journal")
            config = pack("journal")
            config["documents"][0]["text"] += " " * 5000 + "Preserve offline entries."
            config["documents"][1]["text"] += " " * 17000 + "Check the sync badge."
            core.install(db, config)
        base = "/agent/products/journal"
        headers = {
            "Authorization": "Bearer journalagent",
            "Idempotency-Key": "model-case",
        }
        cid = api.post(
            base + "/cases",
            headers=headers,
            json={"question": "How do I refresh my entries?"},
        ).json()["id"]

        class LocalClient:
            def call(self, path, body=None, key=None):
                request_headers = dict(headers)
                if key:
                    request_headers["Idempotency-Key"] = key
                if body is None:
                    response = api.get(base + path, headers=request_headers)
                else:
                    response = api.post(base + path, headers=request_headers, json=body)
                response.raise_for_status()
                return response.json()

        agent = LocalClient()
        steps = iter(["search", "read", "next_page", "test"])

        def choose(facts):
            assert facts["guidance"][0]["key"] == "voice"
            assert facts["guidance"][0]["excerpt"].endswith("Preserve offline entries.")
            action = next(steps)
            if action == "search":
                return {"action": action, "query": "mobile sync"}
            if action == "read":
                key = facts["observations"][-1]["matches"][0]["key"]
                return {"action": action, "key": key}
            page = facts["observations"][-1]
            if action == "next_page":
                assert "On mobile choose Sync" in page["text"]
                assert page["next_offset"] == len(page["text"])
                return {"action": "read", "key": page["key"], "offset": page["next_offset"]}
            assert page["text"].endswith("Check the sync badge.")
            assert page["next_offset"] is None
            return {"action": action, "repo": "journal", "recipe": "check"}

        paused = run(agent, cid, choose, wait_seconds=0)
        assert paused["state"] == "waiting_for_test"
        runner = {"Authorization": "Bearer journalrunner"}
        lease = api.post(base + "/runner/claim", headers=runner).json()["job"]
        job = lease["id"]
        assert job == paused["job"]
        result = {
            "state": "passed",
            "revision": "a" * 40,
            "exit_code": 0,
            "output": "entries synchronized",
        }
        api.post(
            base + "/runner/jobs/" + job + "/result",
            headers=runner,
            json={"lease": lease["lease"], "result": result},
        ).raise_for_status()

        patch = "--- a/engine.py\n+++ b/engine.py\n@@ -1 +1 @@\n-return False\n+return True\n"

        def try_change(facts):
            return {"action": "test", "repo": "journal", "recipe": "check", "patch": patch}

        candidate = run(agent, cid, try_change, wait_seconds=0)
        assert candidate["state"] == "waiting_for_test" and candidate["job"] != job
        assert run(agent, cid, try_change, wait_seconds=0) == candidate
        changed = api.post(base + "/runner/claim", headers=runner).json()["job"]
        assert changed["patch"] == patch and changed["id"] == candidate["job"]
        api.post(
            base + "/runner/jobs/" + changed["id"] + "/result",
            headers=runner,
            json={"lease": changed["lease"], "result": result},
        ).raise_for_status()

        def conclude(facts):
            receipts = {test["id"]: test for test in facts["tests"]}
            assert receipts[job]["result"]["output"] == "entries synchronized"
            assert not receipts[job]["has_patch"] and receipts[changed["id"]]["has_patch"]
            return {
                "action": "propose",
                "answer": "On mobile, choose Sync.",
                "citations": ["guide-sync", "test:" + job],
            }

        assert run(agent, cid, conclude)["state"] == "proposed"
        assert (
            run(agent, cid, lambda facts: pytest.fail("submitted twice"))["state"]
            == "proposed"
        )


def test_agent_assignments_are_explicit_product_scoped_and_recoverable(
    tmp_path, monkeypatch
):
    from workbench import assignments

    monkeypatch.setenv("DESK_DB", str(tmp_path / "company.db"))
    with TestClient(http.standalone), http.session(True) as db:
        install(db, "journal")
        install(db, "another")
        cid = core.open_case(db, "journal", "How do I sync?", "case")
        assert assignments.status(db, "journal", cid) is None
        assert assignments.claim(db, "journal", "Woodland agent") is None
        assert assignments.status(db, "journal", cid)["state"] == "available"
        with pytest.raises(core.Conflict):
            assignments.enqueue(db, "journal", cid, "Unconfigured processor")
        assignments.enqueue(db, "journal", cid, "Woodland agent")
        assert assignments.claim(db, "another", "Woodland agent") is None
        job = assignments.claim(db, "journal", "Woodland agent")
        assert (
            job["id"] == cid
            and assignments.claim(db, "journal", "Woodland agent") is None
        )
        with pytest.raises(core.Invalid):
            assignments.finish(db, "journal", cid, job["lease"], "proposed")
        with pytest.raises(core.Conflict):
            assignments.finish(db, "journal", cid, "wrong", "failed")
        db.execute("UPDATE desk_assignments SET available=0")
        new = assignments.claim(db, "journal", "Woodland agent")
        assert new["lease"] != job["lease"]
        with pytest.raises(core.Conflict):
            assignments.finish(db, "journal", cid, job["lease"], "failed")
        core.propose(db, "journal", cid, "Choose Sync.", ["guide-sync"])
        for _ in range(2):
            assignments.finish(db, "journal", cid, new["lease"], "proposed")
        assert assignments.status(db, "journal", cid)["state"] == "completed"
        with pytest.raises(core.Conflict):
            assignments.finish(db, "journal", cid, new["lease"], "failed")
