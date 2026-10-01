"""Build the scoped call directory filter release."""

import hashlib
import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "release_helpers", Path(__file__).with_name("build-sepa-release.py")
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
ROOT, git = helpers.ROOT, helpers.git
BASE = "afa0c00977d1c9de219677532443e3442f830829"
BRANCH = "refs/heads/release/call-directory-filter-20261001"
FILES = (
    "docs/call-directory-filter-20261001.md",
    "server/app/api/routes/web.py",
    "server/app/services/hub_crm_readers.py",
    "server/app/templates/base.html",
    "server/app/templates/partials/activity_view_filter.html",
    "server/tests/test_activity_directories.py",
    "server/tests/test_activity_responsibility.py",
    "tools/deploy-lead-conversion.py",
    "tools/support/build-call-directory-filter-release.py",
)


def main():
    if git("show-ref", "--verify", BRANCH, check=False).returncode == 0:
        raise SystemExit("Release already exists; do not overwrite it.")
    index = ROOT / "tmp/call-directory-filter-20261001.index"
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
    commit = git(
        "commit-tree", tree, "-p", BASE, "-m",
        "Filter calls by due and completion state",
    ).stdout.decode().strip()
    git("update-ref", BRANCH, commit, "0" * 40)
    for suffix, revision in (("before", BASE), ("release", commit)):
        destination = ROOT / f"tmp/call-directory-filter-{suffix}.tar"
        git(
            "-c", "core.autocrlf=false", "archive", "--format=tar", "-o",
            str(destination), revision, "server/app",
        )
        print(suffix, hashlib.sha256(destination.read_bytes()).hexdigest(), destination)
    print("commit", commit)
    print(git("diff", "--stat", BASE, commit).stdout.decode())


if __name__ == "__main__":
    main()
