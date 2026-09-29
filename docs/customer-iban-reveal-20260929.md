# Customer IBAN reveal

The immutable role key `admin` is labeled Superadmin in Hub access control.
Only active Superadmins can request a customer's canonical IBAN. Generic module
grants do not authorize disclosure; the actor is reloaded and session version
checked on every request, before decrypting any customer profile.

POST `/customers/{customer_id}/iban/reveal` is authenticated, CSRF-protected,
human-only and absent from agent operations. The response is private/no-store.
Audit records actor, customer ID and action, never bank data; the audit commit
must succeed before plaintext is returned. No schema or encryption changes.

Customer read and edit views have an eye/Anzeigen control only for Superadmins.
The initial document and all edit input values stay masked/empty. The explicit
POST fills a non-form text node only; copy uses the system clipboard on click.
Display clears after 30 seconds, outside click, Escape, pagehide or hidden tab.
Stale/aborted responses cannot redisplay data. Copy does not clear the clipboard.

Tests cover real persistence, audit failures, empty/malformed values, stale
sessions, demotion, inactive users, unauthenticated/CSRF/GET denial and all other
roles even with full module grants. Browser tests use synthetic values and the
actual Jinja macros at desktop/mobile widths, including cancellation, copy,
auto-hide and edit-form isolation. No real bank values are printed or sent.
