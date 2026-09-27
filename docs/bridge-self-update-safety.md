# Bridge self-update safety (0.3.70)

## Incident

Soundin complete-site run 1414 on 2026-09-27 successfully updated eight
components. Bridge 0.3.68 -> 0.3.69 then threw an undefined-method error for
SecretStore::get_identity(). The old activation callback autoloaded the new
Registrar while the old SecretStore class remained in PHP memory. Reactivation
did not complete, so subsequent requests received REST_NO_ROUTE (404).
Four more plugin requests were attempted before the generic five-error limit.

## Changes

- Compare the in-memory Options version with the plugin header on disk before
  registration or activation-time identity work. Mixed-version requests retain
  the normal registration retry instead of bootstrapping new code with old
  classes. The Registrar guard also protects callbacks from 0.3.68/0.3.69,
  which cannot use the improved activation callback until their next request.
- Keep all credential, domain-identity, signature and registration-state checks.
  No fallback credentials, auth bypass, forced activation or blind retry.
- Confirm an active Bridge self-update with a fresh read-only plugin-inventory
  request, including version and active state, before continuing a workflow.
- Preserve original update errors and record failed reconciliation separately.
  Connection loss stops a complete workflow immediately; known remaining
  components appear as skipped with an explicit Bridge-unreachable reason.
- In direct batches, skip only queued updates for that site in that batch.
  Other sites and already running work are not cancelled. Ordinary plugin
  errors with a reachable Bridge retain the existing failure-limit behavior.

## Verification

`tools/test-bridge-self-update.php` preloads the actual released Options,
SecretStore and Plugin classes, then invokes their activation callback with
registration code from the new package. The original 0.3.68 -> 0.3.69 pair
reproduces the exact incident error. Both 0.3.68 -> 0.3.70 and 0.3.69 -> 0.3.70
pass, retain identity and schedule recovery without network requests.
`tools/test-bridge-registration.php` verifies that a fresh-version activation
registers the same identity successfully. These are offline PHP/WordPress
Options API tests, not a live filesystem-upgrader or MySQL integration test.
The new upgrade test is mandatory in the release workflow.

`server/tests/test_bridge_update_recovery.py` covers immediate workflow stop,
remaining-component reporting, per-site/per-batch isolation, malformed replies,
fresh self-update verification and ordinary recoverable plugin errors.

Publishing the new package does not reactivate an already inactive Bridge.
Such a site needs WordPress-side activation/recovery; the Hub must not report
it repaired until a fresh authenticated request confirms it. Existing failed
history is retained and no old update jobs are automatically replayed.
