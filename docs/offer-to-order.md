# Offer to order

The offer detail menu's `In Auftrag umwandeln` POST uses the shared operation
`finance.offers.convert_to_order`. It requires Finance view/create/edit and access
to the source offer. CSRF is checked in the web route.

- A new `AUF` draft opens with `edit=true`. Existing offer data, status, numbers,
  timestamps and PDFs remain unchanged. No PDF or email is created by conversion.
- Every line is a separate order row with its original article reference, position
  index and complete encrypted snapshot. No live article defaults replace prices,
  descriptions, SKU, quantity, unit, discounts or taxes. Currency is retained too.
- Apart from draft status, currency, origin offer and automatic number/audit data,
  the order fields remain blank for manual completion. Required fields are still
  checked on normal save. Unassigned drafts are visible only to their creator and
  admins; after successful save, ordinary customer-based access takes over.
- Repeating conversion by the same user reopens their unfinished draft instead of
  duplicating it. Source-row locking serializes concurrent clicks. Once completed,
  another deliberate conversion may create another independent order.
- Customer offers must link to the same customer on save. A readable lead offer
  may be linked to the customer manually chosen for the new order; the lead and
  offer are never converted or reassigned by this operation.
- An additive nullable owner column is installed at startup. Existing orders keep
  their original access rules; null does not grant employee access to old orphans.

Verification: `test_offer_to_order.py`, existing Finance/duplication/PDF tests,
HTTP contract checks, and `tests/js/offer_to_order.cjs` with rendered fixtures at
desktop and mobile widths. All verification uses isolated test data.
