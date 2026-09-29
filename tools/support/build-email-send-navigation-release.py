"""Build the isolated composer navigation fix on the current production release."""
import hashlib
import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location("release_helpers", Path(__file__).with_name("build-sepa-release.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
ROOT, git = helpers.ROOT, helpers.git
BASE = "b3c1707ecc4cb0b754c5760d5eea8a3a307e58b4"
BRANCH = "refs/heads/release/email-send-navigation-20260929"
FILES = (
    "server/app/static/email-delivery.js",
    "server/app/templates/base.html",
    "server/app/templates/emails.html",
    "server/tests/js/email_delivery_failure.cjs",
    "server/tests/js/email_delivery_navigation.cjs",
    "tools/deploy-lead-conversion.py",
    "tools/support/build-email-send-navigation-release.py",
    "docs/email-send-navigation-20260929.md",
)


def main():
    if git("show-ref", "--verify", BRANCH, check=False).returncode == 0:
        raise SystemExit("Release already exists; do not overwrite it.")
    index = ROOT / "tmp/email-send-navigation-20260929.index"
    if index.exists():
        raise SystemExit("Release index already exists; inspect before retrying.")
    index.parent.mkdir(exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    git("read-tree", BASE, env=env)
    for name in FILES:
        data = (ROOT / name).read_bytes()
        previous = git("show", f"{BASE}:{name}", check=False)
        if previous.returncode == 0:
            data = helpers.preserve_unchanged_line_endings(previous.stdout, data)
        blob = git("hash-object", "-w", "--stdin", data=data).stdout.decode().strip()
        git("update-index", "--add", "--cacheinfo", "100644", blob, name, env=env)
    tree = git("write-tree", env=env).stdout.decode().strip()
    commit = git("commit-tree", tree, "-p", BASE, "-m",
                 "Refresh email composers after repeated successful sends on the same page").stdout.decode().strip()
    git("update-ref", BRANCH, commit, "0" * 40)
    for suffix, revision in (("before", BASE), ("release", commit)):
        destination = ROOT / f"tmp/email-send-navigation-{suffix}.tar"
        git("-c", "core.autocrlf=false", "archive", "--format=tar", "-o", str(destination), revision, "server/app")
        print(suffix, hashlib.sha256(destination.read_bytes()).hexdigest(), destination)
    print("commit", commit)
    print(git("diff", "--stat", BASE, commit).stdout.decode())


if __name__ == "__main__":
    main()
