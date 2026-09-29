# SEPA rejection diagnostics

The real Elementor calls reached the public receiver and passed token validation,
but returned HTTP 422 during bank field validation. Previous access logs cannot
identify which check failed. No previously rejected payload is retained.

Log only the HTTP status, fixed rejection code and recognized canonical field
names. Never log body data, raw field names, bank values, token, URL, customer
identifier or exception text. Missing, malformed, wrong-length and wrong-checksum
IBANs are distinguishable, as are invalid BIC and missing/invalid account holder.
Responses, accepted values, token lifetime, permissions and persistence behavior
are unchanged. All validation must still pass before any customer write.

Tests cover each diagnostic and ensure values do not enter logs and rejected
submissions do not update profiles or create receipts. A new user submission is
needed to determine the actual rejection reason; this release is diagnostic,
not a claim that the underlying form problem has been fixed.
