"""Pull a product-scoped test job and execute the registered recipe in an isolated Git worktree."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import subprocess
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import quote, urlsplit


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


open_request = build_opener(NoRedirect()).open


class Client:
    def __init__(self, config):
        target = urlsplit(config["origin"])
        local = target.scheme == "http" and target.hostname in (
            "localhost",
            "127.0.0.1",
            "::1",
        )
        if (
            (target.scheme != "https" and not local)
            or target.username
            or target.password
            or target.fragment
            or target.query
        ):
            raise ValueError("Use an HTTPS origin or loopback HTTP")
        self.base = (
            config["origin"].rstrip("/")
            + "/agent/products/"
            + quote(config["product"], safe="")
        )
        self.token = Path(config["token_file"]).read_text().strip()

    def call(self, path, body=None, key=None):
        headers = {
            "Authorization": "Bearer " + self.token,
            "User-Agent": "Product-Workbench/1.0",
        }
        if key:
            headers["Idempotency-Key"] = key
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        with open_request(
            Request(self.base + path, headers=headers, data=data), timeout=25
        ) as r:
            return json.load(r)


def git(mirror, *args, index=None, data=None):
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Support Workbench",
        "GIT_AUTHOR_EMAIL": "runner@localhost",
        "GIT_COMMITTER_NAME": "Support Workbench",
        "GIT_COMMITTER_EMAIL": "runner@localhost",
    }
    if index:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "submodule.recurse=false",
            "--git-dir=" + str(mirror),
            *args,
        ],
        input=data,
        env=env,
        cwd=mirror.parent,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError(
            "Could not apply the patch or prepare the selected Git revision"
        )
    return result.stdout.decode().strip()


def bounded_output(command, seconds):
    started = time.monotonic()
    output = bytearray()
    truncated = False
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as proc:
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(proc.stdout, selectors.EVENT_READ)
                while time.monotonic() - started < seconds:
                    if not sel.select(timeout=0.5):
                        continue
                    buf = os.read(proc.stdout.fileno(), 8192)
                    if not buf:
                        return (
                            proc.wait(timeout=5),
                            output.decode("utf-8", "replace"),
                            truncated,
                        )
                    size = min(len(buf), max(0, 32768 - len(output)))
                    output.extend(buf[:size])
                    truncated |= size != len(buf)
            raise TimeoutError("Test recipe exceeded its time limit")
        finally:
            if proc.poll() is None:
                proc.kill()


def execute(config, job):
    repo = config["repositories"].get(job["repo"])
    if (
        not repo
        or repo["revision"] != job["revision"]
        or not re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}", job["revision"])
    ):
        raise ValueError("Repository revision is not installed on this runner")
    recipe = repo["recipes"].get(job["recipe"])
    if not recipe:
        raise ValueError("Recipe is not installed on this runner")
    image = recipe["image"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Pin the locally installed container image by image ID")
    argv = recipe["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(s, str) and s and "\x00" not in s for s in argv)
    ):
        raise ValueError("Invalid local recipe command")
    mirror = Path(repo["bare_repository"]).resolve(strict=True)
    rev = job["revision"]
    if git(mirror, "rev-parse", rev + "^{commit}") != rev:
        raise ValueError("Git revision mismatch")
    patch = job.get("patch", "").encode()
    if len(patch) > 250000:
        raise ValueError("Patch exceeds 250 KB")
    root = Path(config["work_directory"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The containing directory remains 0700. The unprivileged container user needs
    # read access to the bind-mounted checkout, including directories created by Git.
    name = "support-case-" + secrets.token_hex(12)
    with tempfile.TemporaryDirectory(prefix="case-", dir=root) as private:
        tree = Path(private) / "checkout"
        commit = rev
        if patch:
            index = Path(private) / "index"
            git(mirror, "read-tree", rev, index=index)
            git(
                mirror,
                "apply",
                "--cached",
                "--whitespace=nowarn",
                "-",
                index=index,
                data=patch,
            )
            tree_id = git(mirror, "write-tree", index=index)
            commit = git(
                mirror,
                "commit-tree",
                tree_id,
                "-p",
                rev,
                "-m",
                "Isolated support investigation",
            )
        old_mask = os.umask(0o022)
        try:
            git(mirror, "worktree", "add", "--detach", str(tree), commit)
        finally:
            os.umask(old_mask)
        try:
            cmd = [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--user",
                "65534:65534",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--cpus=0.5",
                "--memory=256m",
                "--memory-swap=256m",
                "--pids-limit=64",
                "--ulimit",
                "nofile=256:256",
                "--log-driver=none",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
                "--mount",
                "type=bind,src=" + str(tree) + ",dst=/work,readonly",
                "--workdir=/work",
                "--entrypoint",
                argv[0],
                image,
                *argv[1:],
            ]
            started = time.monotonic()
            try:
                rc, log, truncated = bounded_output(
                    cmd, min(240, max(1, int(recipe.get("timeout", 60))))
                )
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return {
                "state": (
                    "passed"
                    if rc == 0
                    else "error"
                    if rc in (125, 126, 127)
                    else "failed"
                ),
                "revision": rev,
                "tested_commit": commit,
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
                "recipe": job["recipe"],
                "image": image,
                "exit_code": rc,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "output": log,
                "truncated": truncated,
            }
        finally:
            git(mirror, "worktree", "remove", str(tree))


def once(config, client):
    job = client.call("/runner/claim", {})["job"]
    if not job:
        return False
    try:
        result = execute(config, job)
    except (ValueError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
        result = {
            "state": "error",
            "revision": job["revision"],
            "error": str(exc)[:1000],
        }
    client.call(
        "/runner/jobs/" + job["id"] + "/result",
        {"lease": job["lease"], "result": result},
    )
    print("Completed", job["id"], result["state"], flush=True)
    return True


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("config", type=Path)
    cli.add_argument("--once", action="store_true")
    args = cli.parse_args()
    config = json.loads(args.config.read_text())
    client = Client(config)
    while True:
        try:
            worked = once(config, client)
        except (OSError, ValueError):
            if args.once:
                raise
            print("Runner could not reach the configured workbench", flush=True)
            worked = False
        if args.once:
            return
        if not worked:
            time.sleep(10)


if __name__ == "__main__":
    main()
