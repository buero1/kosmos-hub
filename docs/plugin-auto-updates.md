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
