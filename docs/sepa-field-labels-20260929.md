# Elementor bank field label compatibility

The 18:37:23 diagnostic showed `iban_missing fields=token`: authorization
succeeded, but no bank field matched. The exact rejected raw payload was not
retained. The public form labels are IBAN, BIC and Kontoinhaber:in. A label-keyed
Elementor webhook reproduces this symptom against the previous ID-only parser.

Accept only those explicit labels plus Kontoinhaber and optional Bank, alongside
the existing technical IDs. Both nested Advanced Data and flat simple payloads
remain supported for JSON and URL-encoded input. Never infer a customer from
posted IDs or labels; the existing 14-day token and record permissions remain
mandatory. Ambiguous ID/label pairs reject the whole submission before writes.
IBAN/BIC validation, transactional persistence, Berlin date and idempotency stay
unchanged. No incoming values or unrecognized labels enter diagnostic logs.

Tests reproduce label-keyed submissions in all four envelopes, including ignored
customer/date/company fields, matching retries, alias collisions and invalid or
missing authorization. Real customer forms and bank data are not replayed.
Another user submission is required for end-to-end confirmation on the website.
