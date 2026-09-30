"""Build the scoped lead URL display release without touching the main index."""
import hashlib
import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location("release_helpers", Path(__file__).with_name("build-sepa-release.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
ROOT, git = helpers.ROOT, helpers.git
BASE = "2c334d083568b66b4a27d7f1b24e06f5ae1be3c8"
BRANCH = "refs/heads/release/lead-url-fields-20260930"
FILES = (
    "server/app/core/web_urls.py",
    "server/app/services/customer_directory.py",
    "server/app/services/hub_leads.py",
    "server/app/templates/lead_detail.html",
    "server/tests/test_lead_url_fields.py",
    "tools/deploy-lead-conversion.py",
    "tools/support/build-lead-url-fields-release.py",
)


def main():
    if git("show-ref", "--verify", BRANCH, check=False).returncode == 0:
        raise SystemExit("Release already exists; do not overwrite it.")
    index = ROOT / "tmp/lead-url-fields-20260930.index"
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
                 "Render lead website fields as safe new-tab links").stdout.decode().strip()
    git("update-ref", BRANCH, commit, "0" * 40)
    for suffix, revision in (("before", BASE), ("release", commit)):
        destination = ROOT / f"tmp/lead-url-fields-{suffix}.tar"
        git("-c", "core.autocrlf=false", "archive", "--format=tar", "-o", str(destination), revision, "server/app")
        print(suffix, hashlib.sha256(destination.read_bytes()).hexdigest(), destination)
    print("commit", commit)
    print(git("diff", "--stat", BASE, commit).stdout.decode())


if __name__ == "__main__":
    main()
