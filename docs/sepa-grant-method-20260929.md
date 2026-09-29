# SEPA grant method

Successful new submissions through the existing Elementor webhook now also set
the customer field `Art SEPA-Erteilung` (`sepa_grant_type`) to
`per Online-Formular`, replacing any previous selection.

The value is derived on the server, not trusted from submitted form fields.
It is stored in the encrypted customer profile in the same transaction as bank
details, the Berlin receipt date, the receipt digest and the audit entry.
Existing field metadata and unrelated customer fields are preserved.

No Elementor configuration, database migration or historical backfill is needed.
Identical retries remain idempotent and do not overwrite subsequent manual
changes. Failed submissions do not change customer data or consume the link.

Tests cover missing/empty/previous methods, preserved picklist metadata, ignored
forged methods across all supported Elementor envelopes, retries and rollback.
No real mandate is submitted during verification.
