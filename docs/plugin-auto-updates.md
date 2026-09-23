# Plugin Auto-Update Policy

The Update Workbench action "Automatische Plugin-Updates verwalten" uses
`wordpress.plugins.auto_updates`, the same confirmed operation available to the
Hub agent. It queues a durable WordPress job, never performs remote writes in
the web request, and rechecks administrator and website/customer access before
dispatch. Selection is explicit (up to 500 sites, 100 plugin basenames, 5000
combinations). No plugin or website is selected implicitly by the server.

Bridge 0.3.68 provides native and legacy abilities to read/set a plugin-scoped
policy. Only signed MCP requests or WordPress users with `update_plugins` may
call them. Multisite is rejected rather than changing an entire network from a
single-site selection. Inputs are fully validated before writing.

Blocking stores an explicit deny-list, removes those installed plugins from
WordPress's native auto-update selection, and vetoes `auto_update_plugin` only
for those basenames. Other plugins, core, themes, update discovery, and manual
installations are untouched. The WordPress plugin list explains the Hub lock.
Releasing the Hub lock does NOT enable native automatic updates. No policy is
created just by installing or updating the Bridge.

Independent updates initiated by hosting or management products such as MainWP
are outside this policy. Security updates of blocked plugins must be installed
through supervised maintenance. This is not a rollback or recovery mechanism.

Results are checked for each exact requested plugin and persisted per completed
site, with audit records. Missing plugins are skipped; missing abilities demand
a Bridge upgrade. Transport failures/ambiguous responses are not success and
are never automatically retried. Previously recorded outcomes survive a worker
crash. UI displays individual site and plugin results on the job page.

Validation:

- `php tools/test-bridge-auto-updates.php`
- `php tools/test-bridge-identity.php`
- `python -m pytest -q tests/test_plugin_auto_updates.py tests/test_wordpress_shared_actions.py tests/test_wordpress_workbench_operations.py tests/test_hub_architecture_contracts.py`
- `node tests/js/plugin_auto_updates.cjs` (Playwright available via NODE_PATH)

## Release Verification (2026-09-23)

- Hub release snapshot: `4fb8af3ae4d1324295d7bf51e4cf173f0dec9070`, branch
  `release/plugin-auto-update-policy-20260923`. The isolated release snapshot
  preserves the main working tree and includes the existing deployed baseline.
  Comparison with production found only this feature's changes in shared app
  files. No database migration or customer policy change was required.
- Full app archive deployed; service and health endpoint healthy. Live admin
  workbench renders the plugin inventory. A real browser selected Elementor and
  Elementor Pro, opened confirmation, then cancelled with zero policy requests.
- Bridge `bridge-v0.3.68` released by GitHub Actions to the existing public
  update channel. All 18 package files match the Git release after newline
  normalization. Release workflow including both PHP contract tests succeeded.
- Dedicated test site 2 (`test-gasthofloewen.kosmos-medien.de`) was upgraded.
  Signed reads/writes blocked the installed Content Kit plugin, confirmed the
  persisted state, and restored the original disabled/unblocked policy.
  No customer installation or customer auto-update policy was changed.
- Architecture gate detected the initially unreviewed HTTP route, then passed
  all 329 reviewed route contracts after the explicit ledger review.
- The complete test run exposed seven stale agent fixtures using an invalid
  model name and an obsolete tool-output representation. Fixtures now use the
  configured model profile and decode both plain and cached content blocks;
  production model validation was not relaxed. The affected file and policy/
  architecture tests subsequently passed (57 tests).
- Final complete Python rerun: 2006 passed, 6 skipped. Desktop/mobile browser
  checks, both PHP contract suites and syntax checks of all plugin PHP files
  also passed.

Live checks are intentionally separate from ordinary tests:
`tools/test-bridge-policy-live.cjs --install` upgrades only the dedicated test
site; `tools/test-bridge-policy-live.py` verifies and restores its policy.
`tools/plugin-policy-workbench-live.cjs` aborts policy POSTs and tests cancellation
on the live Hub without changing any customer website.
