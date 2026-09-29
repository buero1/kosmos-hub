# Elementor SEPA -> Hub (without Bridge)

## Elementor configuration

Page inspected: `https://kunden.kosmos-medien.de/sepa-mandat/`.
Form ID at inspection: `7898c25`, WordPress post ID `9604`.

Under Actions After Submit, replace the old Zoho webhook destination with:

```text
https://kosmos-hub.31-70-92-95.sslip.io/api/v1/integrations/sepa/submissions
```

**Advanced Data / Erweiterte Daten** is recommended. The receiver accepts stable
field IDs in nested URL-encoded or JSON payloads and the exact approved labels
`IBAN`, `BIC`, `Kontoinhaber:in`, `Kontoinhaber`, `Bank` in either format. This also
supports Elementor's simple label-based webhook. Unknown labels are ignored;
duplicate IDs/labels for the same bank field are rejected, not merged. The token
must always use `ks_hub_sepa_token`; customer IDs never authorize an update.

Add one form field:

| Setting | Value |
| --- | --- |
| Type | Hidden / Versteckt |
| ID | `ks_hub_sepa_token` |
| Default Value | Dynamic tag: Request Parameter / Anfrageparameter |
| Request Type | GET |
| Parameter Name | `ks_hub_sepa_token` |

Existing fields are mapped as follows (no renaming necessary):

| Elementor ID | Hub customer field |
| --- | --- |
| `ks_iban` | IBAN, required, whitespace removed, uppercase, checksum checked |
| `ks_bic` | BIC, optional; blank preserves the previous value |
| `field_a13f37a` | Kontoinhaber, required (current form ID) |
| `ks_kontoinhaber` | Alternative descriptive ID for Kontoinhaber; use only one |
| `ks_bank` | Bank, optional if a bank-name field is added |
| No form field needed | Datum SEPA-Erteilung: Hub receipt date in Europe/Berlin |

`ks_account_id` is no longer needed. Do not replace it with an unprotected Hub ID.
`ks_mandatsreferenz` may remain for display or confirmation, but is never used to
authorize or match the write. It is prefilled with the Hub customer number.
Old emailed Zoho-ID-only links cannot authorize submissions; send a new link.

Remove every old Zoho submission action/custom webhook. Keep the existing SEPA
wording and other desired actions. Confirmation emails must not include the
hidden token (e.g. avoid including it via Elementor's `[all-fields]`).

Exclude the SEPA page from full-page caching, analytics and session replay.
Do not retain full query strings in access logs; the link grants write access
to this customer's limited SEPA fields. Check cache behavior for logged-out users.

## Email link

In a Hub email template, use the customer placeholder **SEPA-Formularlink (14 Tage)**
as the complete link target:

```html
<a href="${Customer.SepaMandateUrl}">SEPA-Mandat erteilen</a>
```

The Hub resolves the selected customer/contact and URL-encodes company, address,
contact name/email, phone, customer number and token. No bank data is put in URLs.
`${Customer.SepaToken}` is also available for custom links, but the complete URL
is preferred. `${Customer.Id}` retains its legacy meaning; it is not repurposed.

Tokens expire exactly 14 * 24 hours after link generation (template loading),
not after first opening. Reload the template before sending a draft older than
14 days. Template rendering remains read-only: encrypted, authenticated claims
include purpose, customer, issuing Hub user, random nonce and exact expiry.
The issuer must have current customer edit/record permission both when generating
the link and when the submission arrives. Deactivating their account invalidates
their links. Bank permissions are not granted by arbitrary posted customer IDs.

## Delivery behavior

Only a successful database commit produces HTTP 200 / `success: true`.
Malformed, expired, revoked, reused-with-different-values and denied links produce
non-200 responses. Elementor may display its generic webhook error rather than
the Hub's detailed message; verify the actual form's failure UI after configuration.

The first accepted delivery atomically stores the encrypted customer profile,
receipt fingerprint and audit entry. A byte-independent identical normalized
retry returns the previous receipt without rewriting bank fields/date or audit.
Other previously generated links cannot overwrite a more recent form submission.
Optional blanks preserve existing values; other customer fields remain unchanged.
No Zoho requests, emails, withdrawals, or WordPress writes occur in this receiver.
IBAN checks verify syntax/checksum, not bank-account ownership or mandate validity.

The receipt ledger stores only keyed token/payload digests, internal customer ID,
expiry and receipt/revocation times. Application logs/audit omit bank values and
tokens. Public POST requests are bounded by body/field size and process-local
rate limits. Only this exact endpoint bypasses Hub session authentication.

Support can revoke a still-valid individual link using `HubSepaService.revoke`
within an authorized transaction. The service verifies the acting user's customer
edit permission; no public revocation or customer lookup endpoint is exposed.

## Rollout and verification

The additive receipt table is installed by the normal startup schema check;
the SQL migration is also included. Runtime is deployed via the guarded release
script with a complete previous-runtime comparison and rollback snapshot.

Regression tests cover token scope/tampering, permissions, exact expiry,
Berlin dates/DST, local encrypted persistence and attribution, invalid data,
retry/idempotency, stale links, revocation, rollback, request limits, real HTTP
envelopes without login and authorized customer/contact template rendering.

WordPress/Elementor configuration is left to the user as requested. Until that
configuration is saved and a controlled form test is performed, end-to-end live
Elementor delivery is not yet verified. Do not test using real customer bank data.
