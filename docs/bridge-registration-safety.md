# Bridge registration safety (0.3.69)

The 0.3.67/0.3.68 domain-identity migration could replace credentials on an
unchanged domain when two WordPress requests had different option caches.
Deleting the individual option did not clear WordPress's `notoptions` cache.
Also, `add_option()` uses an upsert and is not an exclusive database lock.
An old, identity-independent success timestamp could then suppress recovery.
This code defect was reproduced offline; the precise historic interleaving on
Baeckerei Glas cannot be established retrospectively from its old logs.

## Invariants

- Read identity-critical options directly from the primary WordPress database.
  A missing row and a failed read are different; failures must not create keys.
- Use `INSERT IGNORE` for first acquisition, byte-exact compare-and-swap for
  replacement and lease-owner-checked deletion. No `add_option()` lock.
- Re-read identity after acquiring the lease. Preserve existing pairs on an
  unchanged domain. Partial/corrupt identities fail closed.
- Bind confirmation to UUID, secret, normalized domain and Hub URL together.
  Legacy success timestamps are not confirmation of the current identity.
- Give registration attempts unique expiring tokens. Only the current attempt
  for the current identity can commit its response. Late replies are discarded.
- Payload, authentication header and HMAC use one immutable identity snapshot.
- Unknown or failed registration sends a full signed bootstrap, even when
  initiated by the heartbeat. Ordinary errors never cause credential rotation.
- Retry after errors despite a historical success; retain the existing throttle
  and cron retry. Only an explicit domain conflict permits one rotation/retry.
- Keep up to ten identity-change events (reason, UUIDs, domains, version, time),
  never secrets. Absence of an event is not proof no pre-0.3.69 change occurred.

Pre-domain-identity legacy keys remain during migration so requests still running
old plugin code cannot regenerate keys simply because they disappeared. After an
actual domain transition they are obsolete and removed. Old unbound registration
options are ignored. Healthy 0.3.67/0.3.68 identities stay unchanged; the first
normal page request/cron registers them once using the new bound state.

## Required regression checks

The tests run offline with the unmodified WordPress 6.9 Options API, including
its real positive/negative caching and upsert behavior. The fake database models
the SQL operations and explicit request interleavings; it is not a real MySQL
concurrency stress test. Live upgrade verification separately checks actual WP.

Download `https://raw.githubusercontent.com/WordPress/WordPress/6.9/wp-includes/option.php`
to `tmp/wordpress-6.9-option.php` (or set `WP_OPTIONS_FIXTURE` to its path).
The required SHA-256 is
`cde15dc93f943de5884c87b26f0786011c6e43b93440cde1792428029ced9e37`.
Upstream source is GPL-2.0-or-later; it is only a test dependency, not packaged.

```sh
php tools/test-bridge-identity.php
php tools/test-bridge-registration.php
php tools/test-bridge-auto-updates.php
```

All three checks are required by `release-kosmos-bridge.yml` before creating a
release or publishing update metadata. The fixture hash is checked both by CI
and the PHP harness. A failed check prevents publication.

## Verification on 2026-09-24

- Local PHP: 70 identity assertions, 67 registration/authentication assertions;
  existing plugin auto-update policy contracts and all plugin syntax checks pass.
- Hub: 50 tests pass (domain identity/customer matching, update policy, MCP proxy,
  admin launch and package installation). No Hub server changes required.
- Scoped live upgrades: test site 2 and Baeckerei Glas site 14 from 0.3.68 to
  0.3.69. Current identity confirmation is `ok`; UUID, secret digest, site ID,
  customer ID and connection IDs/endpoints unchanged; no duplicate site.
  Admin and public pages render after upgrade and repeated requests.
- No fleet-wide installation was started. Other installations need the update.
