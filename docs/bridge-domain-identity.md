# Bridge domain identities (0.3.67)

## Policy

- A changed WordPress home hostname creates a fresh random UUID and secret.
  HTTP/HTTPS, path and a leading `www.` do not create a new identity.
- The source is WordPress's configured `home_url`, never an arbitrary request
  Host header. A copied template must have its WordPress address configured.
- The identity pair and bound hostname are one non-autoloaded option. A short
  exclusive initialization lease prevents overlapping requests from creating
  different identities. Initialization failures block registration and Bridge
  authentication until retry.
- Domain changes clear inherited registration status. Even an inherited daily
  heartbeat then performs a full bootstrap with the fresh key.
- Legacy installations retain their identity on upgrade, but recheck with the
  Hub. After a signed registration receives `bridge_domain_changed`, the Bridge
  rotates once and registers as a new website. Other failures never rotate keys.
- The Hub verifies the request signature before issuing this conflict and never
  silently replaces the domain of an existing UUID. WordPress and MCP endpoints
  must belong to that same normalized domain.

## Customer assignment

One shared matcher is used by registration, Hub customer creation/editing and
the existing imported-customer edit path. Website, work domain and work-domain
login are resolved through the canonical customer field schema. Previous website
addresses, customer names and inherited template ownership are not candidates.
Exactly one customer must match; inactive/ambiguous/unreadable customer profiles
prevent automatic assignment. Existing assignments are never changed.
Assignments and their audit entries are part of the enclosing transaction.

No domain-move history merge is implemented: a new domain means a new website.
Old records and history are retained. Existing duplicate identities must be
upgraded on every affected installation, including the template itself.

## Verification and rollout

- `php tools/test-bridge-identity.php`
- `server/.venv/Scripts/python.exe -m pytest` (from `server`, omit `server/`)
  with `tests/test_bridge_domain_identity.py`, the customer/CRM tests and the
  shared-operation architecture tests.
- Deploy the guarded Hub registration service before publishing Bridge 0.3.67.
- Upgrade the template and known copies, then verify distinct UUIDs, secrets,
  successful signed reads and the intended customer assignment.
- Never reset keys in the Hub alone: keys must be created by each installation.

## Verified deployment (2026-09-23)

- Hub registration guard and both customer-save paths deployed; health check 200.
- Native WordPress ZIP replacement on `p-ywum9r.project.space` and
  `p-t18mx1.project.space`. Both report Bridge 0.3.67, registration `ok` and
  successful authenticated reads with distinct UUIDs and secrets.
- Alpe is site 188, automatically linked to customer 1. Its customer-page admin
  shortcut is available again. Site 183 remains Neukunden Flex; history retained.
- The historic HTTP address `wordpress.p665666.webspaceconfig.de` redirects to
  Neukunden Flex. Its old HTTPS certificate does not cover that hostname;
  certificate validation was not bypassed and no credentials were sent by HTTP.
- Full Python suite: 1878 passed, 1 skipped. PHP identity contracts: 30 passed;
  all 16 plugin PHP files passed syntax checks.
- Public update metadata and ZIP report 0.3.67; downloaded SHA-256 matches the
  locally tested archive. Future templates must include 0.3.67 or newer.
