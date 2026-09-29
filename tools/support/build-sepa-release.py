"""Create a scoped release without touching the user's main index/worktree."""

import difflib
import hashlib
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
BASE = "1c51df7ac1e93e8093337be58eacac32c1364140"
BRANCH = "refs/heads/release/sepa-webhook-20260929"
FILES = (
    "server/app/api/routes/sepa.py",
    "server/app/models/hub_sepa_submission.py",
    "server/app/services/hub_sepa.py",
    "server/app/db/base.py",
    "server/app/main.py",
    "server/app/services/customer_communications.py",
    "server/app/services/template_placeholders.py",
    "server/tests/test_hub_sepa.py",
    "server/tests/architecture/hub_http_contracts.json",
    "server/migrations/20260929_add_sepa_submissions.sql",
    "docs/sepa-webhook-20260929.md",
    "tools/deploy-lead-conversion.py",
    "tools/support/build-sepa-release.py",
)


def git(*args, data=None, env=None, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, check=check)


def preserve_unchanged_line_endings(previous, current):
    old, new = previous.splitlines(keepends=True), current.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, [line.rstrip(b"\r\n") for line in old],
                                     [line.rstrip(b"\r\n") for line in new], autojunk=False)
    result = []
    for kind, a, b, c, d in matcher.get_opcodes():
        result.extend(old[a:b] if kind == "equal" else new[c:d])
    result = b"".join(result)
    assert result.replace(b"\r\n", b"\n") == current.replace(b"\r\n", b"\n")
    return result


def main():
    if git("show-ref", "--verify", BRANCH, check=False).returncode == 0:
        raise SystemExit("Release branch already exists; do not amend or overwrite it.")
    index = ROOT / "tmp/sepa-webhook-20260929.index"
    if index.exists():
        raise SystemExit("Staging index already exists; inspect it before retrying.")
    index.parent.mkdir(exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    git("read-tree", BASE, env=env)
    for name in FILES:
        data = (ROOT / name).read_bytes()
        previous = git("show", f"{BASE}:{name}", check=False)
        if previous.returncode == 0:
            data = preserve_unchanged_line_endings(previous.stdout, data)
        blob = git("hash-object", "-w", "--stdin", data=data).stdout.decode().strip()
        git("update-index", "--add", "--cacheinfo", "100644", blob, name, env=env)
    tree = git("write-tree", env=env).stdout.decode().strip()
    commit = git("commit-tree", tree, "-p", BASE, "-m",
                 "Receive Elementor SEPA forms with scoped 14-day customer links").stdout.decode().strip()
    git("update-ref", BRANCH, commit, "0" * 40)
    for suffix, revision in (("before", BASE), ("release", commit)):
        destination = ROOT / f"tmp/sepa-webhook-{suffix}.tar"
        git("-c", "core.autocrlf=false", "archive", "--format=tar", "-o", str(destination), revision, "server/app")
        print(suffix, hashlib.sha256(destination.read_bytes()).hexdigest(), destination)
    print("commit", commit)
    print(git("diff", "--stat", BASE, commit).stdout.decode())


if __name__ == "__main__":
    main()
