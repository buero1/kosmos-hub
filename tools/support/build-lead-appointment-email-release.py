"""Build the scoped Lead appointment email release without touching the main index."""
import hashlib
import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location("release_helpers", Path(__file__).with_name("build-sepa-release.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
ROOT, git = helpers.ROOT, helpers.git
BASE = "1096ea4e64664aff10eef85f0781698c9fb5a9f4"
BRANCH = "refs/heads/release/lead-appointment-email-20260930"
FILES = (
    "docs/lead-appointment-email-20260930.md",
    "server/app/main.py",
    "server/app/models/hub_scheduled_email.py",
    "server/app/services/hub_workflows.py",
    "server/app/services/lead_appointment_email_reminders.py",
    "server/app/services/scheduled_email_worker.py",
    "server/app/services/scheduled_emails.py",
    "server/migrations/20260930_add_scheduled_email_automation_key.sql",
    "server/tests/test_hub_leads.py",
    "server/tests/test_lead_appointment_email_reminders.py",
    "tools/deploy-lead-conversion.py",
    "tools/support/build-lead-appointment-email-release.py",
)


def main():
    if git("show-ref", "--verify", BRANCH, check=False).returncode == 0:
        raise SystemExit("Release already exists; do not overwrite it.")
    index = ROOT / "tmp/lead-appointment-email-20260930.index"
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
        "commit-tree",
        tree,
        "-p",
        BASE,
        "-m",
        "Send automatic Lead appointment reminder emails",
    ).stdout.decode().strip()
    git("update-ref", BRANCH, commit, "0" * 40)
    for suffix, revision in (("before", BASE), ("release", commit)):
        destination = ROOT / f"tmp/lead-appointment-email-{suffix}.tar"
        git("-c", "core.autocrlf=false", "archive", "--format=tar", "-o", str(destination), revision, "server/app")
        print(suffix, hashlib.sha256(destination.read_bytes()).hexdigest(), destination)
    print("commit", commit)
    print(git("diff", "--stat", BASE, commit).stdout.decode())


if __name__ == "__main__":
    main()
